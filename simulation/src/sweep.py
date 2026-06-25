"""
@file       sweep.py

Traces the compression-ratio tradeoff curves for the filter algorithm.

Two knobs move the operating point. `eventDeltaThreshold` (the sigma-multiplier
above which a change counts as an event) controls how readily the algorithm
fires on transients; on this data it is a weak compression lever because most
samples are held by the heartbeat/quiet-run logic regardless. The heartbeat
intervals are the dominant lever: longer heartbeats force fewer transmissions
during silent runs, sweeping compression over a much wider range.

Three modes:
  --mode delta      sweep eventDeltaThreshold, one curve         (default)
  --mode heartbeat  sweep normalHeartbeatHours (quiet/burst scaled
                    proportionally), one curve over a wide range
  --mode both       sweep eventDeltaThreshold at several heartbeat
                    settings, one curve per setting (multi-curve)

At each operating point we pool across all sensors/loggers:
  - compression ratio  = raw samples / transmitted packets        (x-axis)
  - reconstruction RMSE = zero-order-hold error on VWC            (error curve)
  - mean latency        = avg delay from sample to transmission   (latency curve)

and emit two charts plus a CSV of the raw points.

Example:
    python -m simulation.src.sweep \\
        --dir data/UCSC_Haybarn_Field/data \\
        --output data/UCSC_Haybarn_Field/out \\
        --mode heartbeat
"""

import argparse
import csv
import math
import os
import sys

import matplotlib.pyplot as plt

from simulation.src import config, filter_algo, metrics, pipeline
from simulation.src.compare import _ingest

# Knob settings per mode.
DEFAULT_DELTAS = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0]
DEFAULT_HEARTBEAT_HRS = [0.5, 1.0, 2.0, 3.0, 6.0, 12.0, 24.0, 48.0]
# Heartbeat settings that each get their own delta-curve in `both` mode.
MULTI_HEARTBEAT_HRS = [3.0, 6.0, 12.0]

# Globals on filter_algo that any mode may rebind; snapshotted and restored.
SWEPT_PARAMS = [
    "eventDeltaThreshold",
    "normalHeartbeatHours",
    "quietHeartbeatHours",
    "burstHeartbeatHours",
]

# Tier proportions from config defaults, preserved as the heartbeat is scaled.
_QUIET_OVER_NORMAL = config.quietHeartbeatHours / config.normalHeartbeatHours
_BURST_OVER_NORMAL = config.burstHeartbeatHours / config.normalHeartbeatHours


def _heartbeat_overrides(normal_hr: float) -> dict:
    """All three heartbeat tiers scaled so their ratios match the config."""
    return {
        "normalHeartbeatHours": normal_hr,
        "quietHeartbeatHours": normal_hr * _QUIET_OVER_NORMAL,
        "burstHeartbeatHours": normal_hr * _BURST_OVER_NORMAL,
    }


def _operating_point(sensors_by_logger: dict, overrides: dict) -> dict:
    """Apply `overrides` to filter_algo, run every logger, pool the metrics."""
    for key, val in overrides.items():
        setattr(filter_algo, key, val)

    n_raw = 0
    n_tx = 0
    sse = 0.0
    abs_sum = 0.0
    err_n = 0
    lat_sum = 0.0
    lat_n = 0

    for sensors in sensors_by_logger.values():
        branches = pipeline.run_both(sensors)
        raw, filtered = branches["raw"], branches["filtered"]
        raw_by_name = {s.name: s for s in raw}
        for fs in filtered:
            rs = raw_by_name.get(fs.name)
            if rs is None:
                continue
            n_raw += len(rs.data)
            n_tx += len(fs.data)

            err = metrics.reconstruction_error(rs, fs)
            sse += err["sse"]
            abs_sum += err["abs_sum"]
            err_n += err["n"]

            lat = metrics.transmission_latency(rs, fs)
            lat_sum += lat["sum_sec"]
            lat_n += lat["n"]

    return {
        "delta": filter_algo.eventDeltaThreshold,
        "heartbeat_hr": filter_algo.normalHeartbeatHours,
        "n_raw": n_raw,
        "n_tx": n_tx,
        "compression_ratio": (n_raw / n_tx) if n_tx else float("inf"),
        "rmse": math.sqrt(sse / err_n) if err_n else None,
        "mae": (abs_sum / err_n) if err_n else None,
        "mean_latency_sec": (lat_sum / lat_n) if lat_n else None,
        "mean_latency_hr": (lat_sum / lat_n / 3600) if lat_n else None,
    }


def _collect(sensors_by_logger: dict, mode: str, deltas: list, heartbeats: list) -> list:
    """Return a list of (series_label, points) curves for the given mode."""
    if mode == "delta":
        pts = []
        for d in deltas:
            p = _operating_point(sensors_by_logger, {"eventDeltaThreshold": d})
            pts.append(p)
            print(f"  δ={d:>5g}  ratio={p['compression_ratio']:7.2f}:1  "
                  f"rmse={p['rmse']:.4f}  latency={p['mean_latency_hr']:.2f} hr")
        return [(None, pts)]

    if mode == "heartbeat":
        pts = []
        for hb in heartbeats:
            p = _operating_point(sensors_by_logger, _heartbeat_overrides(hb))
            pts.append(p)
            print(f"  hb={hb:>5g}h  ratio={p['compression_ratio']:7.2f}:1  "
                  f"rmse={p['rmse']:.4f}  latency={p['mean_latency_hr']:.2f} hr")
        return [(None, pts)]

    if mode == "both":
        series = []
        for hb in MULTI_HEARTBEAT_HRS:
            pts = []
            for d in deltas:
                overrides = {"eventDeltaThreshold": d, **_heartbeat_overrides(hb)}
                p = _operating_point(sensors_by_logger, overrides)
                pts.append(p)
                print(f"  hb={hb:>4g}h δ={d:>4g}  ratio={p['compression_ratio']:7.2f}:1  "
                      f"rmse={p['rmse']:.4f}  latency={p['mean_latency_hr']:.2f} hr")
            series.append((f"hb={hb:g}h", pts))
        return series

    raise ValueError(f"unknown mode: {mode}")


def _plot_curves(series, y_key, y_label, title, path, annotate, legend):
    """One or more curves of a metric against compression ratio.

    `series` is a list of (label, points). `annotate` is a point key whose
    value labels each marker (None to skip — used to keep multi-curve plots
    readable). `legend` shows the series labels.
    """
    fig, ax = plt.subplots(layout="constrained")
    for label, points in series:
        pts = [p for p in points
               if p[y_key] is not None and p["compression_ratio"] != float("inf")]
        pts.sort(key=lambda p: p["compression_ratio"])
        xs = [p["compression_ratio"] for p in pts]
        ys = [p[y_key] for p in pts]
        ax.plot(xs, ys, marker="o", label=label)
        if annotate:
            for p in pts:
                ax.annotate(
                    f"{annotate[0]}={p[annotate[1]]:g}",
                    (p["compression_ratio"], p[y_key]),
                    textcoords="offset points", xytext=(6, 4),
                    fontsize=7, alpha=0.7,
                )
    ax.set_xlabel("Compression ratio (raw samples : transmitted packets)")
    ax.set_ylabel(y_label)
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    if legend:
        ax.legend(title="heartbeat tier")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    fig.savefig(path, dpi=120)
    plt.close(fig)


def _write_sweep_csv(series, path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cols = [
        "series", "delta", "heartbeat_hr", "n_raw", "n_tx",
        "compression_ratio", "rmse", "mae", "mean_latency_sec", "mean_latency_hr",
    ]
    with open(path, "w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(cols)
        for label, points in series:
            for p in points:
                w.writerow([label if label is not None else ""]
                           + [p[c] for c in cols[1:]])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-f", "--files", default="", type=str,
                        help="Comma-separated XLSX paths.")
    parser.add_argument("-d", "--dir", default="", type=str,
                        help="Directory of XLSX files.")
    parser.add_argument("-o", "--output", default="./out", type=str,
                        help="Output directory for charts and sweep CSV.")
    parser.add_argument("--mode", default="delta",
                        choices=["delta", "heartbeat", "both"],
                        help="Which knob drives the compression-ratio axis.")
    parser.add_argument("--deltas", default="", type=str,
                        help="Comma-separated eventDeltaThreshold values "
                             "(delta/both modes).")
    parser.add_argument("--heartbeats", default="", type=str,
                        help="Comma-separated normalHeartbeatHours values "
                             "(heartbeat mode).")
    parser.add_argument("--show", action="store_true",
                        help="Display charts interactively in addition to saving.")
    args = parser.parse_args()

    deltas = ([float(x) for x in args.deltas.split(",") if x.strip()]
              if args.deltas else DEFAULT_DELTAS)
    heartbeats = ([float(x) for x in args.heartbeats.split(",") if x.strip()]
                  if args.heartbeats else DEFAULT_HEARTBEAT_HRS)

    paths_files = args.files.split(",") if args.files else []
    sensors_by_logger = _ingest(paths_files, args.dir)

    print(f"sweep mode: {args.mode}")
    saved = {k: getattr(filter_algo, k) for k in SWEPT_PARAMS}
    try:
        series = _collect(sensors_by_logger, args.mode, deltas, heartbeats)
    finally:
        for k, v in saved.items():
            setattr(filter_algo, k, v)

    # Mode-specific filenames so charts from different modes don't clobber.
    suffix = {"delta": "", "heartbeat": "_heartbeat", "both": "_multi"}[args.mode]
    # Single-curve modes annotate each marker with the swept value; the
    # multi-curve mode uses a legend instead to stay readable.
    annotate = {
        "delta": ("δ", "delta"),
        "heartbeat": ("hb", "heartbeat_hr"),
        "both": None,
    }[args.mode]
    legend = args.mode == "both"

    plots_dir = os.path.join(args.output, "plots")
    _plot_curves(
        series, "rmse", "Reconstruction RMSE (VWC, m³/m³)",
        "Error vs Compression Ratio",
        os.path.join(plots_dir, f"error_vs_compression{suffix}.png"),
        annotate, legend,
    )
    _plot_curves(
        series, "mean_latency_hr", "Mean transmission latency (hours)",
        "Latency vs Compression Ratio",
        os.path.join(plots_dir, f"latency_vs_compression{suffix}.png"),
        annotate, legend,
    )
    _write_sweep_csv(series, os.path.join(args.output, f"sweep{suffix}.csv"))

    print(f"\nWrote charts to {plots_dir} and sweep{suffix}.csv to {args.output}")

    if args.show:
        plt.show()


if __name__ == "__main__":
    # Allow `python simulation/src/sweep.py` from the repo root.
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
    main()
