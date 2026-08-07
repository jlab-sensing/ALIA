"""
benchmark.py — ALIA against trivial baselines at a matched packet budget.

`compare.py` scores ALIA against transmitting everything, which can only show
that sending fewer packets costs fewer packets. This module answers the question
a reviewer actually asks: *given the same number of packets, does ALIA
reconstruct the signal better than a naive scheme?*

Two products:

1. **Matched-budget table** (`matched.csv` / `matched.txt`). Run ALIA at the
   `config.py` operating point, note how many packets it spent on each stream,
   then solve every baseline's knob so it spends no more than that, and compare
   reconstruction error. `rmse_vs_alia` > 1 means the baseline is worse than
   ALIA at equal cost; < 1 means ALIA is losing.

2. **Frontier curves** (`plots/frontier/<channel>.png`, `frontier.csv`). Sweep
   each method across its own knob grid and plot error against compression, so
   the comparison is visible at every operating point rather than just one.
   Lower and further right is better.

Errors are pooled across the streams of a channel by summing squared error and
sample counts before taking the root, so long streams are not double-counted.
All methods are scored on the same zero-order-hold reconstruction.

Example:
    python -m simulation.src.benchmark \\
        --dir data/UCSC_Haybarn_Field \\
        --output data/out/bench-haybarn
"""

import argparse
import csv
import math
import os
from collections import defaultdict

import matplotlib.pyplot as plt

from simulation.src import baselines, config, metrics, pipeline, plots
from simulation.src.compare import _ingest

# Knob grids for the frontier sweep. Each is ordered least-to-most compressing,
# and log-spaced because what matters is proportional change: 0.25 -> 0.5 moves
# the operating point about as far as 4 -> 8 does.
#
# Every grid is a priori -- dimensionless multipliers, a sample stride, a
# duration. Nothing is scaled by the recording's own observed range, so the
# frontier uses no information a node would lack on its first day.
ALPHA_GRID = [0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 8.0]
BETA_GRID = [0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 8.0]
UNIFORM_GRID = [2, 3, 5, 8, 16, 32, 64, 128]
HEARTBEAT_GRID = [0.25, 0.5, 1.0, 2.0, 6.0, 12.0, 24.0, 48.0]  # hours

GRIDS = {
    "alia": ALPHA_GRID,
    "sod_commissioned": BETA_GRID,
    "uniform": UNIFORM_GRID,
    "alia_events": ALPHA_GRID,
    "alia_heartbeat": HEARTBEAT_GRID,
}

# Okabe-Ito subset, ordered so adjacent entries stay separable under deuteranopia
# and protanopia. Marker and linestyle carry the same distinction as secondary
# encoding, so the figures survive greyscale printing.
STYLE = {
    "alia": ("#0072B2", "o", "-", 2.4),
    "sod_commissioned": ("#D55E00", "s", "--", 1.6),
    "uniform": ("#009E73", "D", ":", 1.6),
    "alia_events": ("#56B4E9", "v", "-.", 1.4),
    "alia_heartbeat": ("#CC79A7", "P", "-.", 1.4),
}


def _channel_of(stream) -> str:
    return next((d.name for d in stream.data if d.name), "")


def _score(raw_stream, tx_stream) -> dict:
    """Packet count and pooled-able error terms for one method on one stream."""
    err = metrics.reconstruction_error(raw_stream, tx_stream)
    return {
        "n_tx": len(tx_stream.data),
        "sse": err["sse"],
        "n": err["n"],
        "max_abs": err["max_abs"],
        "resolution": err["resolution"],
    }


def _knob_for(method: str, stream, grid_value: float) -> float:
    """Resolve a grid entry to a knob value.

    Now the identity: every remaining method's knob is already a priori, so
    nothing needs rescaling by the stream's observed range. Kept as a seam in
    case a method with dimensioned knobs is reintroduced.
    """
    return grid_value


def _pool(acc: dict) -> dict:
    """Collapse accumulated per-stream terms into pooled compression + error."""
    n_raw, n_tx, sse, n = acc["n_raw"], acc["n_tx"], acc["sse"], acc["n"]
    rmse = math.sqrt(sse / n) if n else None
    res = acc["resolution"]
    return {
        "n_raw": n_raw,
        "n_tx": n_tx,
        "compression": (n_raw / n_tx) if n_tx else float("inf"),
        "rmse": rmse,
        "rmse_over_res": (rmse / res) if (rmse is not None and res) else None,
        "max_err": acc["max_abs"],
    }


def _new_acc() -> dict:
    return {
        "n_raw": 0,
        "n_tx": 0,
        "sse": 0.0,
        "n": 0,
        "max_abs": 0.0,
        "resolution": 0.0,
    }


def _add(acc: dict, n_raw: int, s: dict) -> None:
    acc["n_raw"] += n_raw
    acc["n_tx"] += s["n_tx"]
    acc["sse"] += s["sse"]
    acc["n"] += s["n"]
    acc["max_abs"] = max(acc["max_abs"], s["max_abs"])
    acc["resolution"] = s["resolution"]


def run_benchmark(
    streams: list, targets: list[float], do_frontier: bool = True
) -> tuple[dict, dict]:
    """Tier 1: the ORACLE comparison. Returns (matched, frontier).

    Every method -- ALIA included -- has its knob bisection-fitted per stream to
    hit the same external compression target. That symmetry is the whole point:
    an earlier version pitted ALIA's transferred global alpha against baselines
    fitted to the very stream they were scored on, which silently handed the
    baselines the answer. Here nobody is favoured, because *everybody* is given
    the answer.

    These numbers are therefore an upper bound on each decision rule, not a
    deployment result -- fitting a knob per stream needs the whole record in
    advance. For what happens when a knob is chosen without seeing the target
    site, see the Tier 2 transfer harness in `transfer.py`.

    matched:  {target: {channel: {method: pooled}}}
    frontier: {channel: {method: [pooled per grid index]}}
    """
    matched = {
        r: defaultdict(lambda: defaultdict(_new_acc)) for r in targets
    }
    frontier = defaultdict(lambda: defaultdict(lambda: defaultdict(_new_acc)))

    for i, stream in enumerate(streams, 1):
        channel = _channel_of(stream)
        n_raw = len(stream.data)
        print(f"  [{i}/{len(streams)}] {stream.name} ({n_raw} samples)", flush=True)

        for r in targets:
            budget = max(2, int(n_raw / r))
            for method in baselines.METHODS:
                knob, _ = baselines.solve_knob(method, stream, budget)
                tx = baselines.run(method, [stream], knob)[0]
                _add(matched[r][channel][method], n_raw, _score(stream, tx))

        if not do_frontier:
            continue
        for method in baselines.METHODS:
            for gi, g in enumerate(GRIDS[method]):
                knob = _knob_for(method, stream, g)
                tx = baselines.run(method, [stream], knob)[0]
                _add(frontier[channel][method][gi], n_raw, _score(stream, tx))

    matched_out = {
        r: {ch: {m: _pool(a) for m, a in per.items()} for ch, per in by_ch.items()}
        for r, by_ch in matched.items()
    }
    frontier_out = {
        ch: {
            m: [_pool(by_gi[gi]) for gi in sorted(by_gi)]
            for m, by_gi in per.items()
        }
        for ch, per in frontier.items()
    }
    return matched_out, frontier_out


MATCHED_HEADER = [
    "channel",
    "method",
    "target",
    "n_raw",
    "n_tx",
    "compression",
    "hit",
    "rmse",
    "rmse_over_res",
    "max_err",
    "rmse_vs_alia",
]


def _matched_rows(matched: dict) -> list[list[str]]:
    """`hit` is achieved compression / target -- a method far from 1.00 could not
    reach the budget (ALIA's heartbeat, for instance, floors its packet count)."""
    rows = []
    channels = sorted({ch for by_ch in matched.values() for ch in by_ch})
    for channel in channels:
        for target in sorted(matched):
            per = matched[target].get(channel)
            if not per:
                continue
            alia_rmse = per.get("alia", {}).get("rmse")
            for method in baselines.METHODS:
                r = per.get(method)
                if not r:
                    continue
                ratio = (
                    (r["rmse"] / alia_rmse)
                    if (r["rmse"] is not None and alia_rmse)
                    else None
                )

                def _f(v, p=4):
                    return f"{v:.{p}f}" if v is not None else ""

                rows.append(
                    [
                        channel,
                        method,
                        f"{target:.0f}",
                        str(r["n_raw"]),
                        str(r["n_tx"]),
                        f"{r['compression']:.2f}",
                        f"{r['compression'] / target:.2f}",
                        _f(r["rmse"]),
                        _f(r["rmse_over_res"], 2),
                        _f(r["max_err"]),
                        _f(ratio, 3),
                    ]
                )
    return rows


def _write_table(header: list[str], rows: list[list[str]], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    all_rows = [header] + rows
    widths = [max(len(r[i]) for r in all_rows) for i in range(len(header))]

    def fmt(r):
        return "  ".join(
            r[i].ljust(widths[i]) if i < 2 else r[i].rjust(widths[i])
            for i in range(len(r))
        )

    lines = [fmt(all_rows[0]), "  ".join("-" * w for w in widths)]
    lines += [fmt(r) for r in all_rows[1:]]
    with open(path, "w") as fp:
        fp.write("\n".join(lines) + "\n")


def _write_csv(header: list[str], rows: list[list[str]], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(header)
        w.writerows(rows)


def _plot_frontier(channel: str, per_method: dict, plots_dir: str) -> None:
    """Error vs compression, one line per method. Log-log: both axes span
    decades and the interesting comparison is proportional, not absolute."""
    fig, ax = plt.subplots(figsize=(7.2, 4.6), layout="constrained")

    for method in baselines.METHODS:
        pts = per_method.get(method)
        if not pts:
            continue
        xy = [
            (p["compression"], p["rmse"])
            for p in pts
            if p["rmse"] and p["compression"] < float("inf")
        ]
        xy.sort()
        if not xy:
            continue
        color, marker, ls, lw = STYLE[method]
        ax.plot(
            [x for x, _ in xy],
            [y for _, y in xy],
            color=color,
            marker=marker,
            linestyle=ls,
            linewidth=lw,
            markersize=5,
            markeredgecolor="white",
            markeredgewidth=0.6,
            label=baselines.LABELS[method],
            zorder=5 if method == "alia" else 3,
        )

    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("compression ratio  (packets saved — higher is better) →")
    ax.set_ylabel(f"RMSE  ({channel} units)")
    ax.set_title(f"{channel} — reconstruction error vs packet budget", fontsize=11)
    ax.grid(True, which="major", linewidth=0.5, alpha=0.35)
    ax.grid(True, which="minor", linewidth=0.3, alpha=0.18)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.legend(fontsize=7.5, frameon=False, loc="upper left")
    ax.text(
        0.99,
        0.02,
        "lower is better",
        transform=ax.transAxes,
        fontsize=7,
        color="0.35",
        ha="right",
    )

    fig.savefig(plots.plot_path(plots_dir, "frontier", channel), dpi=140)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-f", "--files", default="", type=str)
    parser.add_argument("-d", "--dir", default="", type=str)
    parser.add_argument("-o", "--output", default="./out-bench", type=str)
    parser.add_argument(
        "--format", default="auto", choices=["auto", "xlsx", "sensorscope", "dirtviz"]
    )
    parser.add_argument(
        "--limit-samples",
        default=0,
        type=int,
        help="Truncate each stream to the first N samples (0 = no limit). The "
        "solver re-runs the filter dozens of times per stream, so the "
        "half-million-sample luce records need truncating to stay tractable.",
    )
    parser.add_argument(
        "--no-frontier",
        action="store_true",
        help="Only produce the matched-budget table (much faster).",
    )
    parser.add_argument(
        "--targets",
        default="10,30",
        type=str,
        help="Comma-separated compression ratios every method is fitted to.",
    )
    args = parser.parse_args()
    targets = [float(t) for t in args.targets.split(",") if t.strip()]

    paths_files = args.files.split(",") if args.files else []
    sensors_by_logger, _fmt = _ingest(paths_files, args.dir, args.format)
    if not sensors_by_logger:
        print("No sensor data found for the given inputs.")
        return

    streams = []
    for sensors in sensors_by_logger.values():
        for s in pipeline.to_channel_streams(sensors):
            if args.limit_samples:
                s.data = s.data[: args.limit_samples]
            if len(s.data) >= 100:
                streams.append(s)

    print(
        f"{len(streams)} streams; {len(baselines.METHODS)} methods; "
        f"targets {targets} (Tier 1, oracle-fitted -- every method incl. ALIA)"
    )
    matched, frontier = run_benchmark(
        streams, targets, do_frontier=not args.no_frontier
    )

    rows = _matched_rows(matched)
    _write_csv(MATCHED_HEADER, rows, os.path.join(args.output, "matched.csv"))
    _write_table(MATCHED_HEADER, rows, os.path.join(args.output, "matched.txt"))

    if frontier:
        fr_header = ["channel", "method", "grid_index", "compression", "rmse"]
        fr_rows = [
            [
                ch,
                m,
                str(i),
                f"{p['compression']:.3f}",
                f"{p['rmse']:.6f}" if p["rmse"] is not None else "",
            ]
            for ch, per in sorted(frontier.items())
            for m, pts in per.items()
            for i, p in enumerate(pts)
        ]
        _write_csv(
            fr_header, fr_rows, os.path.join(args.output, "frontier.csv")
        )
        for channel, per_method in frontier.items():
            _plot_frontier(channel, per_method, os.path.join(args.output, "plots"))

    print("\n" + "\n".join(open(os.path.join(args.output, "matched.txt")).read().splitlines()))


if __name__ == "__main__":
    main()
