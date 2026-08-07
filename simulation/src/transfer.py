"""
transfer.py — Tier 2: the DEPLOYMENT comparison.

Tier 1 (`benchmark.py`) fits every method's knob to the stream it is then scored
on. That is symmetric, but it is an oracle: it needs the whole record in
advance, which no deployed node has. It answers "how good is this decision rule
at best", not "what happens when you ship it".

This module answers the shipping question, and it is the one that should carry
the paper. The protocol is identical for every method:

    1. Fit ONE knob per channel on the donor site(s), so pooled compression
       across the donor fleet hits the target R.
    2. Freeze it. Apply it unchanged to a held-out site the knob has never seen.
    3. Report what happened there.

No method gets to look at the held-out site, and every knob involved is a priori
(a dimensionless multiplier, a stride, a duration) rather than a threshold in
the channel's units -- a raw-units threshold cannot be chosen without having
already seen the target site, which is exactly what this protocol forbids.

This is the comparison ALIA was designed to win: alpha multiplies a sigma the
node measures for itself, so the operating point should carry across sites.
`sod_commissioned` is the sharpest control, since its knob is dimensionless too
and the only thing separating them is whether the sigma keeps updating.

Columns:
    knob        the frozen value fitted on the donor
    compr       compression actually achieved on the held-out site
    drift       compr / target. 1.00 = the knob transferred perfectly. This is
                the headline number: it is how wrong your battery model is.
    spread      coefficient of variation of per-stream compression on the
                held-out site -- 0 means every node behaved alike.
    rmse        pooled error on the held-out site
    oracle      error the held-out site's OWN best knob achieves at the SAME
                achieved compression
    penalty     rmse / oracle -- what transferring cost in accuracy

Read `drift` and `spread` across methods; they are directly comparable. Do NOT
read `rmse` across methods -- transferred knobs land at different compressions,
so a method that undershoots its budget looks spuriously accurate. `penalty` is
the accuracy number that IS comparable, because each method is measured against
its own oracle at its own operating point.

Example:
    python -m simulation.src.transfer \\
        --donor data/fishnet-meteo \\
        --holdout data/plaine_morte-meteo \\
        --target 10 --output data/out/transfer-fishnet-to-pm
"""

import argparse
import csv
import math
import os
from collections import defaultdict

from simulation.src import baselines, metrics, pipeline
from simulation.src.compare import _ingest


MIN_SAMPLES = 100

# A stream shorter than a few sigma windows is mostly warm-up, and at target R
# it yields only len/R packets -- too few for the bisection to resolve a knob.
# Below this many samples, warn rather than report a number that looks like a
# transfer result but is really a warm-up artifact.
WARN_SAMPLES = 500


def load(path_dir: str, limit: int, min_samples: int = MIN_SAMPLES) -> dict:
    """{channel: [streams]} for one site."""
    by_logger, _ = _ingest([], path_dir, "auto")
    by_channel = defaultdict(list)
    short = 0
    for sensors in by_logger.values():
        for s in pipeline.to_channel_streams(sensors):
            if limit:
                s.data = s.data[:limit]
            if len(s.data) < min_samples:
                continue
            if len(s.data) < WARN_SAMPLES:
                short += 1
            ch = next((d.name for d in s.data if d.name), "")
            by_channel[ch].append(s)
    if short:
        print(
            f"  [warning] {path_dir}: {short} stream(s) under {WARN_SAMPLES} "
            f"samples -- knobs fitted or scored on these are dominated by "
            f"sigma warm-up."
        )
    return by_channel


def pooled(method: str, streams: list, knob: float) -> dict:
    """Fleet-wide compression and error at a single shared knob."""
    n_raw = n_tx = 0
    sse = n = 0.0
    per = []
    for s in streams:
        tx = baselines.run(method, [s], knob)[0]
        e = metrics.reconstruction_error(s, tx)
        n_raw += len(s.data)
        n_tx += len(tx.data)
        sse += e["sse"]
        n += e["n"]
        per.append(len(s.data) / max(1, len(tx.data)))
    return {
        "compression": n_raw / max(1, n_tx),
        "rmse": math.sqrt(sse / n) if n else float("nan"),
        "per_stream": per,
        "n_tx": n_tx,
        "n_raw": n_raw,
    }


def solve_fleet_knob(
    method: str, streams: list, target_r: float, iters: int = 18
) -> float:
    """One knob for a whole fleet, hitting pooled compression == target_r.

    Bisection, exploiting the fact that packet count is non-increasing in the
    knob for every method (see `baselines.knob_bounds`).
    """
    lo, hi = baselines.knob_bounds(method, streams[0])
    for _ in range(iters):
        mid = (lo + hi) / 2
        if pooled(method, streams, mid)["compression"] < target_r:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def cv(xs: list) -> float:
    if not xs:
        return float("nan")
    m = sum(xs) / len(xs)
    if m == 0:
        return float("nan")
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / max(1, len(xs) - 1))
    return sd / m


HEADER = [
    "channel",
    "method",
    "target",
    "knob",
    "compr",
    "drift",
    "spread",
    "rmse",
    "oracle",
    "penalty",
]


def run(donor: dict, holdout: dict, target_r: float) -> list[list[str]]:
    rows = []
    for ch in sorted(set(donor) & set(holdout)):
        print(f"  {ch}", flush=True)
        for method in baselines.METHODS:
            knob = solve_fleet_knob(method, donor[ch], target_r)
            got = pooled(method, holdout[ch], knob)

            # The held-out site's own best knob, at the compression the
            # transferred knob actually achieved -- so the penalty isolates
            # transfer loss from operating-point differences.
            own = solve_fleet_knob(method, holdout[ch], got["compression"])
            oracle = pooled(method, holdout[ch], own)

            penalty = (
                got["rmse"] / oracle["rmse"] if oracle["rmse"] else float("nan")
            )
            rows.append(
                [
                    ch,
                    method,
                    f"{target_r:.0f}",
                    f"{knob:.4f}",
                    f"{got['compression']:.2f}",
                    f"{got['compression'] / target_r:.2f}",
                    f"{cv(got['per_stream']):.2f}",
                    f"{got['rmse']:.4f}",
                    f"{oracle['rmse']:.4f}",
                    f"{penalty:.2f}",
                ]
            )
    return rows


def _write(rows: list[list[str]], out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "transfer.csv"), "w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(HEADER)
        w.writerows(rows)
    all_rows = [HEADER] + rows
    widths = [max(len(r[i]) for r in all_rows) for i in range(len(HEADER))]

    def fmt(r):
        return "  ".join(
            r[i].ljust(widths[i]) if i < 2 else r[i].rjust(widths[i])
            for i in range(len(r))
        )

    lines = [fmt(all_rows[0]), "  ".join("-" * w for w in widths)]
    lines += [fmt(r) for r in all_rows[1:]]
    with open(os.path.join(out_dir, "transfer.txt"), "w") as fp:
        fp.write("\n".join(lines) + "\n")
    print("\n".join(lines))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--donor", required=True, help="Site(s) the knob is fitted on.")
    p.add_argument("--holdout", required=True, help="Unseen site it is applied to.")
    p.add_argument("--target", default=10.0, type=float)
    p.add_argument("-o", "--output", default="./out-transfer")
    p.add_argument(
        "--limit-samples",
        default=20000,
        type=int,
        help="Truncate each stream (0 = no limit); the solver is iterative.",
    )
    p.add_argument(
        "--min-samples",
        default=MIN_SAMPLES,
        type=int,
        help=f"Drop streams shorter than this (default {MIN_SAMPLES}). Streams "
        f"under {WARN_SAMPLES} are kept but warned about.",
    )
    args = p.parse_args()

    donor = load(args.donor, args.limit_samples, args.min_samples)
    holdout = load(args.holdout, args.limit_samples, args.min_samples)
    shared = sorted(set(donor) & set(holdout))
    if not shared:
        print("No channels shared between donor and holdout.")
        return
    print(
        f"fit on {args.donor} -> apply blind to {args.holdout}\n"
        f"target {args.target:.0f}x, channels: {', '.join(shared)}\n"
    )
    _write(run(donor, holdout, args.target), args.output)


if __name__ == "__main__":
    main()
