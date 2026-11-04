"""
pilot.py — the AS-DEPLOYED comparison: pick settings from a short pilot.

`benchmark.py` fits every method's knob to the whole record it is scored on, so
every method lands on exactly the same packet budget. No real node can do that.
What an engineer actually has is a short stretch of data from the start of a
deployment. This module mimics that:

    1. Split each stream into a pilot (its first `--pilot-frac` of samples) and
       the rest.
    2. On the pilot alone, fit ONE knob per channel and method so that pooled
       compression across the site's streams hits `--target`. That is the
       engineer saying "I want about 10x savings" and tuning on what they have.
    3. Freeze the knob and run every method on the rest of each stream. Score
       only the rest.

Nothing is forced to match afterwards: each method lands wherever its frozen
setting takes it. So `compression` must be read next to `rmse` -- a method that
sends more packets will reconstruct better, and the table shows both. `drift`
(compression / target) is how far the pilot's estimate missed on the deployment.

Example:
    python -m simulation.src.pilot \\
        --dir data/fishnet-meteo --output data/out/paper/pilot-fishnet --jobs 4
"""

import argparse
import os
from collections import defaultdict
from copy import deepcopy

from simulation.src import baselines, benchmark, pipeline, transfer
from simulation.src.compare import _ingest

PILOT_FRAC = 0.10

# A pilot shorter than this gives the bisection too few packets to resolve a
# knob at 10x-30x; such streams are left out of the fit (still scored).
MIN_PILOT_SAMPLES = 50

# `sod_fixed` is the textbook send-on-delta: a band in the channel's units.
# The oracle benchmark cannot use it fairly, but here the pilot gives an
# engineer real data to pick the band from, which is how it is deployed.
METHODS = ["alia", "sod_fixed", "sod_commissioned", "uniform", "alia_events", "alia_heartbeat"]

HEADER = [
    "channel",
    "method",
    "target",
    "knob",
    "pilot_compression",
    "n_raw",
    "n_tx",
    "compression",
    "drift",
    "rmse",
    "rmse_vs_alia",
    "max_err",
    "mean_latency_hr",
    "max_latency_hr",
]


def split(stream, frac: float) -> tuple:
    """(pilot, rest): the first `frac` of the stream's samples, and the others."""
    cut = int(len(stream.data) * frac)
    pilot, rest = deepcopy(stream), deepcopy(stream)
    pilot.data = stream.data[:cut]
    rest.data = stream.data[cut:]
    pilot.state_changes = []
    rest.state_changes = []
    return pilot, rest


def _run_one(channel: str, method: str, pilots: list, rests: list, target: float):
    """Fit on the pilots, freeze, score on the rests."""
    knob = transfer.solve_fleet_knob(method, pilots, target)
    pilot_c = transfer.pooled(method, pilots, knob)["compression"]
    acc = benchmark._new_acc()
    for s in rests:
        tx = baselines.run(method, [s], knob)[0]
        benchmark._add(acc, len(s.data), benchmark._score(s, tx))
    return channel, method, knob, pilot_c, benchmark._pool(acc)


def run(by_channel: dict, target: float, frac: float, jobs: int = 1) -> list:
    tasks = []
    for ch in sorted(by_channel):
        pairs = [split(s, frac) for s in by_channel[ch]]
        pilots = [p for p, _ in pairs if len(p.data) >= MIN_PILOT_SAMPLES]
        rests = [r for _, r in pairs if len(r.data) >= 2]
        if not pilots or not rests:
            print(f"  [skip] {ch}: no pilot long enough")
            continue
        for method in METHODS:
            tasks.append((ch, method, pilots, rests, target))

    if jobs > 1:
        from concurrent.futures import ProcessPoolExecutor

        with ProcessPoolExecutor(max_workers=jobs) as pool:
            results = list(pool.map(_run_one, *zip(*tasks)))
    else:
        results = [_run_one(*t) for t in tasks]

    by = defaultdict(dict)
    for ch, method, knob, pilot_c, r in results:
        by[ch][method] = (knob, pilot_c, r)

    def _f(v, p=4):
        return f"{v:.{p}f}" if v is not None else ""

    rows = []
    for ch in sorted(by):
        alia_rmse = by[ch].get("alia", (None, None, {}))[2].get("rmse")
        for method in METHODS:
            if method not in by[ch]:
                continue
            knob, pilot_c, r = by[ch][method]
            ratio = r["rmse"] / alia_rmse if (r["rmse"] is not None and alia_rmse) else None
            rows.append(
                [
                    ch,
                    method,
                    f"{target:.0f}",
                    f"{knob:.4f}",
                    f"{pilot_c:.2f}",
                    str(r["n_raw"]),
                    str(r["n_tx"]),
                    f"{r['compression']:.2f}",
                    f"{r['compression'] / target:.2f}",
                    _f(r["rmse"]),
                    _f(ratio, 3),
                    _f(r["max_err"]),
                    _f(r["mean_latency_hr"], 3),
                    _f(r["max_latency_hr"], 2),
                ]
            )
    return rows


def main():
    p = argparse.ArgumentParser()
    p.add_argument("-d", "--dir", required=True)
    p.add_argument("-o", "--output", default="./out-pilot")
    p.add_argument("--format", default="auto", choices=["auto", "xlsx", "sensorscope", "dirtviz"])
    p.add_argument("--target", default=10.0, type=float,
                   help="Compression the knobs are tuned to on the pilot.")
    p.add_argument("--pilot-frac", default=PILOT_FRAC, type=float,
                   help=f"Fraction of each stream used as the pilot (default {PILOT_FRAC}).")
    p.add_argument("--channels", default="", type=str,
                   help="Comma-separated channel subset (default: every channel).")
    p.add_argument("--jobs", default=1, type=int)
    args = p.parse_args()
    channels = {c for c in args.channels.split(",") if c.strip()}

    by_logger, _fmt = _ingest([], args.dir, args.format)
    by_channel = defaultdict(list)
    for sensors in by_logger.values():
        for s in pipeline.to_channel_streams(sensors):
            ch = benchmark._channel_of(s)
            if channels and ch not in channels:
                continue
            if len(s.data) >= 100:
                by_channel[ch].append(s)

    n = sum(len(v) for v in by_channel.values())
    print(f"{n} streams; pilot = first {args.pilot_frac:.0%} of each; "
          f"knobs tuned to {args.target:.0f}x on the pilot, then frozen")
    rows = run(by_channel, args.target, args.pilot_frac, args.jobs)
    benchmark._write_csv(HEADER, rows, os.path.join(args.output, "pilot.csv"))
    benchmark._write_table(HEADER, rows, os.path.join(args.output, "pilot.txt"))
    print(open(os.path.join(args.output, "pilot.txt")).read())


if __name__ == "__main__":
    main()
