"""
@file       sweep.py

Traces the compression-ratio tradeoff curves for the filter algorithm.

Two knobs move the operating point. `eventDeltaThreshold` (the sigma-multiplier
above which a change counts as an event) controls how readily the algorithm
fires on transients; on this data it is a weak compression lever because most
samples are held by the heartbeat/quiet-run logic regardless. The heartbeat
intervals are the dominant lever: longer heartbeats force fewer transmissions
during silent runs, sweeping compression over a much wider range.

Four modes:
  --mode delta      sweep eventDeltaThreshold, one curve         (default)
  --mode heartbeat  sweep normalHeartbeatHours (quiet/burst scaled
                    proportionally), one curve over a wide range
  --mode both       sweep eventDeltaThreshold at several heartbeat
                    settings, one curve per setting (multi-curve)
  --mode overlay    render the raw-vs-filtered overlay graphs (as compare.py
                    does) once per eventDeltaThreshold value, each delta in its
                    own subfolder — to eyeball how the threshold reshapes the
                    filtered series rather than a summary curve

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

from simulation.src import config, filter_algo, metrics, pipeline, plots
from simulation.src.compare import _ingest, _plot_overlay_generic

# Knob settings per mode.
DEFAULT_DELTAS = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0]
DEFAULT_HEARTBEAT_HRS = [0.5, 1.0, 2.0, 3.0, 6.0, 12.0, 24.0, 48.0]
# Heartbeat settings that each get their own delta-curve in `both` mode.
MULTI_HEARTBEAT_HRS = [3.0, 6.0, 12.0]

# Heartbeat globals on filter_algo that any mode may rebind; snapshotted and
# restored. The event multiplier is swept through config (per-channel) instead,
# so it is snapshotted separately in _snapshot/_restore.
SWEPT_PARAMS = [
    "normalHeartbeatHours",
    "quietHeartbeatHours",
    "burstHeartbeatHours",
]


def _snapshot() -> dict:
    """Capture every knob a sweep may rebind so it can be restored afterward."""
    return {
        "hb": {k: getattr(filter_algo, k) for k in SWEPT_PARAMS},
        "delta": config.eventDeltaThreshold,
        "by_channel": dict(config.eventDeltaThresholdByChannel),
    }


def _restore(snap: dict) -> None:
    for k, v in snap["hb"].items():
        setattr(filter_algo, k, v)
    config.eventDeltaThreshold = snap["delta"]
    config.eventDeltaThresholdByChannel = snap["by_channel"]

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
        if key == "eventDeltaThreshold":
            # The event multiplier now lives in config and is per-channel; to
            # sweep it as one global knob, set the default and clear the
            # per-channel overrides so every channel uses the swept value.
            config.eventDeltaThreshold = val
            config.eventDeltaThresholdByChannel = {}
        else:
            setattr(filter_algo, key, val)

    n_raw = 0
    n_tx = 0
    sse = 0.0
    abs_sum = 0.0
    sse_interp = 0.0
    abs_sum_interp = 0.0
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
            sse_interp += err["sse_interp"]
            abs_sum_interp += err["abs_sum_interp"]
            err_n += err["n"]

            lat = metrics.transmission_latency(rs, fs)
            lat_sum += lat["sum_sec"]
            lat_n += lat["n"]

    return {
        "delta": config.eventDeltaThreshold,
        "heartbeat_hr": filter_algo.normalHeartbeatHours,
        "n_raw": n_raw,
        "n_tx": n_tx,
        "compression_ratio": (n_raw / n_tx) if n_tx else float("inf"),
        "rmse": math.sqrt(sse / err_n) if err_n else None,
        "mae": (abs_sum / err_n) if err_n else None,
        "rmse_interp": math.sqrt(sse_interp / err_n) if err_n else None,
        "mae_interp": (abs_sum_interp / err_n) if err_n else None,
        "mean_latency_sec": (lat_sum / lat_n) if lat_n else None,
        "mean_latency_hr": (lat_sum / lat_n / 3600) if lat_n else None,
    }


def _collect(
    sensors_by_logger: dict, mode: str, deltas: list, heartbeats: list
) -> list:
    """Return a list of (series_label, points) curves for the given mode."""
    if mode == "delta":
        pts = []
        for d in deltas:
            p = _operating_point(sensors_by_logger, {"eventDeltaThreshold": d})
            pts.append(p)
            print(
                f"  δ={d:>5g}  ratio={p['compression_ratio']:7.2f}:1  "
                f"rmse={p['rmse']:.4f} (interp {p['rmse_interp']:.4f})  "
                f"latency={p['mean_latency_hr']:.2f} hr"
            )
        return [(None, pts)]

    if mode == "heartbeat":
        pts = []
        for hb in heartbeats:
            p = _operating_point(sensors_by_logger, _heartbeat_overrides(hb))
            pts.append(p)
            print(
                f"  hb={hb:>5g}h  ratio={p['compression_ratio']:7.2f}:1  "
                f"rmse={p['rmse']:.4f} (interp {p['rmse_interp']:.4f})  "
                f"latency={p['mean_latency_hr']:.2f} hr"
            )
        return [(None, pts)]

    if mode == "both":
        series = []
        for hb in MULTI_HEARTBEAT_HRS:
            pts = []
            for d in deltas:
                overrides = {"eventDeltaThreshold": d, **_heartbeat_overrides(hb)}
                p = _operating_point(sensors_by_logger, overrides)
                pts.append(p)
                print(
                    f"  hb={hb:>4g}h δ={d:>4g}  ratio={p['compression_ratio']:7.2f}:1  "
                    f"rmse={p['rmse']:.4f} (interp {p['rmse_interp']:.4f})  "
                    f"latency={p['mean_latency_hr']:.2f} hr"
                )
            series.append((f"hb={hb:g}h", pts))
        return series

    raise ValueError(f"unknown mode: {mode}")


def _render_overlays(sensors_by_logger: dict, deltas: list, plots_dir: str) -> None:
    """Write the compare.py overlay graphs once per eventDeltaThreshold value.

    For each delta we set the global event multiplier (config.eventDeltaThreshold
    with per-channel overrides cleared), run both branches for every logger, and
    drop the raw-vs-filtered overlays into a per-delta subfolder
    (`plots/overlay_sweep/delta_<d>/overlay/<logger>.png`) so runs at different
    thresholds sit side by side instead of clobbering each other.

    Sensors are channel-split first, matching compare.py, so every overlay axis
    is a single channel scored on its own dynamics.
    """
    split_by_logger = {
        name: pipeline.to_channel_streams(sensors)
        for name, sensors in sensors_by_logger.items()
    }
    for d in deltas:
        config.eventDeltaThreshold = d
        config.eventDeltaThresholdByChannel = {}
        # A filesystem-safe stamp for the delta (e.g. 1.5 -> "1p5").
        stamp = f"delta_{d:g}".replace(".", "p")
        delta_dir = os.path.join(plots_dir, "overlay_sweep", stamp)
        print(f"  δ={d:>5g}  ->  {delta_dir}")
        for logger_name, sensors in split_by_logger.items():
            branches = pipeline.run_both(sensors)
            _plot_overlay_generic(
                logger_name, branches["raw"], branches["filtered"], delta_dir
            )


def _plot_curves(series, y_key, y_label, title, path, annotate, legend):
    """One or more curves of a metric against compression ratio.

    `series` is a list of (label, points). `annotate` is a point key whose
    value labels each marker (None to skip — used to keep multi-curve plots
    readable). `legend` shows the series labels.
    """
    fig, ax = plt.subplots(layout="constrained")
    for label, points in series:
        pts = [
            p
            for p in points
            if p[y_key] is not None and p["compression_ratio"] != float("inf")
        ]
        pts.sort(key=lambda p: p["compression_ratio"])
        xs = [p["compression_ratio"] for p in pts]
        ys = [p[y_key] for p in pts]
        ax.plot(xs, ys, marker="o", label=label)
        if annotate:
            for p in pts:
                ax.annotate(
                    f"{annotate[0]}={p[annotate[1]]:g}",
                    (p["compression_ratio"], p[y_key]),
                    textcoords="offset points",
                    xytext=(6, 4),
                    fontsize=7,
                    alpha=0.7,
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


def _plot_error_comparison(series, title, path, annotate, legend):
    """Reconstruction RMSE against compression ratio, showing both the causal
    zero-order-hold error (solid) and the offline linear-interpolation error
    (dashed) so the two reconstructions can be compared directly.

    Mirrors `_plot_curves` but draws a paired ZOH/interp line per series.
    """
    fig, ax = plt.subplots(layout="constrained")
    for label, points in series:
        for y_key, style, tag in (
            ("rmse", "-", "ZOH"),
            ("rmse_interp", "--", "interp"),
        ):
            pts = [
                p
                for p in points
                if p[y_key] is not None and p["compression_ratio"] != float("inf")
            ]
            pts.sort(key=lambda p: p["compression_ratio"])
            xs = [p["compression_ratio"] for p in pts]
            ys = [p[y_key] for p in pts]
            line_label = f"{label} — {tag}" if label else tag
            ax.plot(xs, ys, marker="o", linestyle=style, label=line_label)
        if annotate:
            for p in points:
                if p["rmse"] is None or p["compression_ratio"] == float("inf"):
                    continue
                ax.annotate(
                    f"{annotate[0]}={p[annotate[1]]:g}",
                    (p["compression_ratio"], p["rmse"]),
                    textcoords="offset points",
                    xytext=(6, 4),
                    fontsize=7,
                    alpha=0.7,
                )
    ax.set_xlabel("Compression ratio (raw samples : transmitted packets)")
    ax.set_ylabel("Reconstruction RMSE (VWC, m³/m³)")
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    # Always legend here — the ZOH vs interp distinction needs labelling even in
    # the single-curve modes.
    ax.legend(title="heartbeat tier" if legend else "reconstruction")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    fig.savefig(path, dpi=120)
    plt.close(fig)


def _write_sweep_csv(series, path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cols = [
        "series",
        "delta",
        "heartbeat_hr",
        "n_raw",
        "n_tx",
        "compression_ratio",
        "rmse",
        "mae",
        "rmse_interp",
        "mae_interp",
        "mean_latency_sec",
        "mean_latency_hr",
    ]
    with open(path, "w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(cols)
        for label, points in series:
            for p in points:
                w.writerow(
                    [label if label is not None else ""] + [p[c] for c in cols[1:]]
                )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-f", "--files", default="", type=str, help="Comma-separated XLSX paths."
    )
    parser.add_argument(
        "-d", "--dir", default="", type=str, help="Directory of XLSX files."
    )
    parser.add_argument(
        "-o",
        "--output",
        default="./out",
        type=str,
        help="Output directory for charts and sweep CSV.",
    )
    parser.add_argument(
        "--mode",
        default="delta",
        choices=["delta", "heartbeat", "both", "overlay"],
        help="Which knob drives the compression-ratio axis "
        "('overlay' renders per-delta overlay graphs "
        "instead of tradeoff curves).",
    )
    parser.add_argument(
        "--deltas",
        default="",
        type=str,
        help="Comma-separated eventDeltaThreshold values " "(delta/both modes).",
    )
    parser.add_argument(
        "--heartbeats",
        default="",
        type=str,
        help="Comma-separated normalHeartbeatHours values " "(heartbeat mode).",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Display charts interactively in addition to saving.",
    )
    args = parser.parse_args()

    deltas = (
        [float(x) for x in args.deltas.split(",") if x.strip()]
        if args.deltas
        else DEFAULT_DELTAS
    )
    heartbeats = (
        [float(x) for x in args.heartbeats.split(",") if x.strip()]
        if args.heartbeats
        else DEFAULT_HEARTBEAT_HRS
    )

    paths_files = args.files.split(",") if args.files else []
    sensors_by_logger, _fmt = _ingest(paths_files, args.dir, "auto")

    print(f"sweep mode: {args.mode}")
    plots_dir = os.path.join(args.output, "plots")

    # Overlay mode is not a tradeoff curve: it renders the raw-vs-filtered
    # graphs once per delta and returns, leaving the config globals restored.
    if args.mode == "overlay":
        saved = _snapshot()
        try:
            _render_overlays(sensors_by_logger, deltas, plots_dir)
        finally:
            _restore(saved)
        print(f"\nWrote overlay graphs to {os.path.join(plots_dir, 'overlay_sweep')}")
        if args.show:
            plt.show()
        return

    saved = _snapshot()
    try:
        series = _collect(sensors_by_logger, args.mode, deltas, heartbeats)
    finally:
        _restore(saved)

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

    _plot_error_comparison(
        series,
        "Error vs Compression Ratio (ZOH vs interpolated)",
        plots.plot_path(plots_dir, "sweep", f"error_vs_compression{suffix}"),
        annotate,
        legend,
    )
    _plot_curves(
        series,
        "mean_latency_hr",
        "Mean transmission latency (hours)",
        "Latency vs Compression Ratio",
        plots.plot_path(plots_dir, "sweep", f"latency_vs_compression{suffix}"),
        annotate,
        legend,
    )
    _write_sweep_csv(series, os.path.join(args.output, f"sweep{suffix}.csv"))

    print(f"\nWrote charts to {plots_dir} and sweep{suffix}.csv to {args.output}")

    if args.show:
        plt.show()


if __name__ == "__main__":
    # Allow `python simulation/src/sweep.py` from the repo root.
    sys.path.insert(
        0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    )
    main()
