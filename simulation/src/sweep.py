"""
sweep.py — delta-detector compression/error tradeoff curves.

Sweeps the delta sensitivity knob alpha (eventDeltaThreshold) and/or the
heartbeat interval, pooled across loggers, optionally one curve set per channel
(--channels). Modes: alpha, heartbeat, both (alpha curves per heartbeat),
overlay (per-alpha raw-vs-filtered graphs instead of curves). Streams are
channel-split first so multi-channel probes are never interleaved. Emits two
charts plus a CSV.

Example:
    python -m simulation.src.sweep \\
        --dir data/UCSC_Haybarn_Field \\
        --output data/out/sweep-haybarn \\
        --mode alpha --channels VWC,ST,SC
"""

import argparse
import csv
import math
import os
import sys

import matplotlib.pyplot as plt

from simulation.src import config, filter as filter_algo, metrics, pipeline, plots
from simulation.src.compare import _ingest, _plot_overlay_generic

# alpha = sigma multiplier in delta = max(alpha*sigma, resolution). Larger =>
# wider deadband => more compression, more error.
DEFAULT_ALPHAS = [5.0, 4.0, 3.0, 2.0, 1.5, 1.0, 0.5]
# `heartbeat` mode sweeps the backoff ceiling maxHeartbeatHours.
DEFAULT_HEARTBEAT_HRS = [0.5, 1.0, 2.0, 3.0, 6.0, 12.0, 24.0, 48.0]
# Ceilings that each get their own alpha-curve in `both` mode.
MULTI_HEARTBEAT_HRS = [3.0, 6.0, 12.0]

_SWEPT_FILTER_PARAMS = [
    "baseHeartbeatHours",
    "backoffDoublingHours",
    "maxHeartbeatHours",
]

# One-at-a-time ranges for `sensitivity` mode: each parameter swept alone around
# the config baseline while the others stay fixed.
SENSITIVITY_SWEEPS = [
    ("eventDeltaThreshold", [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0]),
    ("baseHeartbeatHours", [1.0, 2.0, 4.0, 6.0, 12.0, 24.0]),
    ("backoffDoublingHours", [3.0, 6.0, 12.0, 24.0, 48.0]),
    ("maxHeartbeatHours", [6.0, 12.0, 24.0, 48.0, 60.0, 96.0]),
]
# alpha is read off `config` at runtime; heartbeat params are bound into
# filter_algo at import, so each knob must be set on the module the algorithm reads.
_CONFIG_KNOBS = {"eventDeltaThreshold"}


def _set_knob(name: str, val) -> None:
    setattr(config if name in _CONFIG_KNOBS else filter_algo, name, val)


def _g(x) -> str:
    return f"{x:.4f}" if x is not None else "n/a"


def _snapshot() -> dict:
    """Capture every knob a sweep rebinds, keyed by (module, name)."""
    snap = {("f", k): getattr(filter_algo, k) for k in _SWEPT_FILTER_PARAMS}
    snap[("c", "eventDeltaThreshold")] = config.eventDeltaThreshold
    snap[("c", "eventDeltaThresholdByChannel")] = dict(
        config.eventDeltaThresholdByChannel
    )
    return snap


def _restore(snap: dict) -> None:
    for (mod, name), val in snap.items():
        target = config if mod == "c" else filter_algo
        setattr(target, name, dict(val) if isinstance(val, dict) else val)


def _heartbeat_overrides(max_hr: float) -> dict:
    """Sweep the backoff ceiling (maxHeartbeatHours)."""
    return {"maxHeartbeatHours": max_hr}


def _operating_point(sensors_by_logger: dict, overrides: dict) -> dict:
    """Apply `overrides` (routed to config or filter_algo), run every logger,
    pool the metrics."""
    for key, val in overrides.items():
        _set_knob(key, val)

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
        "alpha": config.eventDeltaThreshold,
        "heartbeat_hr": filter_algo.maxHeartbeatHours,
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


def _collect(sensors_by_logger: dict, mode: str, alphas: list, heartbeats: list) -> list:
    """Return a list of (series_label, points) curves for the given mode."""
    if mode == "alpha":
        pts = []
        for a in alphas:
            p = _operating_point(sensors_by_logger, {"eventDeltaThreshold": a})
            pts.append(p)
            print(
                f"  a={a:>5g}  ratio={p['compression_ratio']:7.2f}:1  "
                f"rmse={_g(p['rmse'])} (interp {_g(p['rmse_interp'])})  "
                f"latency={_g(p['mean_latency_hr'])} hr"
            )
        return [(None, pts)]

    if mode == "heartbeat":
        pts = []
        for hb in heartbeats:
            p = _operating_point(sensors_by_logger, _heartbeat_overrides(hb))
            pts.append(p)
            print(
                f"  hb={hb:>5g}h  ratio={p['compression_ratio']:7.2f}:1  "
                f"rmse={_g(p['rmse'])} (interp {_g(p['rmse_interp'])})  "
                f"latency={_g(p['mean_latency_hr'])} hr"
            )
        return [(None, pts)]

    if mode == "both":
        series = []
        for hb in MULTI_HEARTBEAT_HRS:
            pts = []
            for a in alphas:
                overrides = {"eventDeltaThreshold": a, **_heartbeat_overrides(hb)}
                p = _operating_point(sensors_by_logger, overrides)
                pts.append(p)
                print(
                    f"  hb={hb:>4g}h a={a:>5g}  ratio={p['compression_ratio']:7.2f}:1  "
                    f"rmse={_g(p['rmse'])} (interp {_g(p['rmse_interp'])})  "
                    f"latency={_g(p['mean_latency_hr'])} hr"
                )
            series.append((f"hb={hb:g}h", pts))
        return series

    raise ValueError(f"unknown mode: {mode}")


def _collect_sensitivity(sensors_by_logger: dict, baseline: dict) -> list:
    """One-at-a-time sensitivity: sweep each parameter alone around the config
    baseline (all others held fixed). Returns a list of (param_label, points).
    Uses a uniform global alpha baseline (per-channel overrides dropped)."""
    series = []
    for knob, values in SENSITIVITY_SWEEPS:
        _restore(baseline)
        config.eventDeltaThresholdByChannel = {}
        pts = []
        for v in values:
            p = _operating_point(sensors_by_logger, {knob: v})
            p["param"], p["knob_val"] = knob, v
            pts.append(p)
            print(
                f"  {knob}={v:>6g}  ratio={p['compression_ratio']:7.2f}:1  "
                f"rmse={_g(p['rmse'])}  latency={_g(p['mean_latency_hr'])} hr"
            )
        series.append((knob, pts))
    return series


def _write_sensitivity_csv(series, path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cols = [
        "param", "knob_val", "alpha", "heartbeat_hr", "n_raw", "n_tx",
        "compression_ratio", "rmse", "mae", "rmse_interp", "mae_interp",
        "mean_latency_hr",
    ]
    with open(path, "w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(cols)
        for _label, points in series:
            for p in points:
                w.writerow([p[c] for c in cols])


def _render_overlays(sensors_by_logger: dict, alphas: list, plots_dir: str) -> None:
    """Write compare.py-style overlay graphs into a per-alpha subfolder
    (plots/overlay_sweep/a_<alpha>/) so thresholds don't clobber each other.
    `sensors_by_logger` is already channel-split, so each axis is one channel."""
    for a in alphas:
        config.eventDeltaThreshold = a
        stamp = f"a_{a:g}".replace(".", "p")  # filesystem-safe alpha stamp
        a_dir = os.path.join(plots_dir, "overlay_sweep", stamp)
        print(f"  a={a:>5g}  ->  {a_dir}")
        for logger_name, sensors in sensors_by_logger.items():
            branches = pipeline.run_both(sensors)
            _plot_overlay_generic(
                logger_name, branches["raw"], branches["filtered"], a_dir
            )


def _plot_curves(series, y_key, y_label, title, path, annotate, legend,
                 legend_title="heartbeat tier"):
    """One or more curves of a metric against compression ratio.

    `annotate` is a (tag, point-key) pair labelling each marker, or None. An
    empty tag labels each marker with the bare value (e.g. "0.5").
    """
    # Fan labels in different directions per curve so overlapping markers
    # (all curves share the baseline point) don't stack their labels.
    label_offsets = [(6, 5), (6, -10), (-20, 5), (-20, -10), (10, 16), (10, -18)]
    fig, ax = plt.subplots(figsize=(10, 7), layout="constrained")
    for i, (label, points) in enumerate(series):
        pts = [
            p
            for p in points
            if p[y_key] is not None and p["compression_ratio"] != float("inf")
        ]
        pts.sort(key=lambda p: p["compression_ratio"])
        xs = [p["compression_ratio"] for p in pts]
        ys = [p[y_key] for p in pts]
        (line,) = ax.plot(xs, ys, marker="o", label=label)
        if annotate:
            tag, key = annotate
            dx, dy = label_offsets[i % len(label_offsets)]
            for p in pts:
                text = f"{tag}={p[key]:g}" if tag else f"{p[key]:g}"
                ax.annotate(
                    text,
                    (p["compression_ratio"], p[y_key]),
                    textcoords="offset points",
                    xytext=(dx, dy),
                    fontsize=7,
                    color=line.get_color(),
                    fontweight="bold",
                    alpha=0.9,
                )
    ax.set_xlabel("Compression ratio (raw samples : transmitted packets)")
    ax.set_ylabel(y_label)
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    if legend:
        ax.legend(title=legend_title)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    fig.savefig(path, dpi=120)
    plt.close(fig)


def _plot_error_comparison(series, title, path, annotate, legend):
    """RMSE vs compression ratio, paired ZOH (solid) and interp (dashed) line
    per series so the two reconstructions can be compared directly."""
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
    ax.set_ylabel("Reconstruction RMSE (pooled, channel units)")
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    # Always legend: ZOH vs interp needs labelling even in single-curve modes.
    ax.legend(title="heartbeat tier" if legend else "reconstruction")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    fig.savefig(path, dpi=120)
    plt.close(fig)


def _write_sweep_csv(series, path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cols = [
        "series",
        "alpha",
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


def _channel_subset(split_by_logger: dict, channel: str) -> dict:
    """Keep only the single-channel streams for `channel`, dropping empty loggers."""
    subset = {
        name: [s for s in streams if s.data and s.data[0].name == channel]
        for name, streams in split_by_logger.items()
    }
    return {name: streams for name, streams in subset.items() if streams}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-f", "--files", default="", type=str, help="Comma-separated input paths."
    )
    parser.add_argument(
        "-d", "--dir", default="", type=str, help="Directory of input files."
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
        default="alpha",
        choices=["alpha", "heartbeat", "both", "sensitivity", "overlay"],
        help="Which knob drives the compression-ratio axis. 'both' draws an "
        "alpha curve per heartbeat; 'sensitivity' sweeps each of the 5 "
        "parameters alone around the config baseline (one curve each); "
        "'overlay' renders per-alpha overlay graphs instead of tradeoff curves.",
    )
    parser.add_argument(
        "--alphas",
        default="",
        type=str,
        help="Comma-separated eventDeltaThreshold (alpha) values "
        "(alpha/both/overlay modes).",
    )
    parser.add_argument(
        "--heartbeats",
        default="",
        type=str,
        help="Comma-separated maxHeartbeatHours (backoff ceiling) values (heartbeat mode).",
    )
    parser.add_argument(
        "--channels",
        default="",
        type=str,
        help="Comma-separated channels (e.g. VWC,ST,SC) for a separate "
        "per-channel curve set; default pools all channels together.",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Display charts interactively in addition to saving.",
    )
    args = parser.parse_args()

    alphas = (
        [float(x) for x in args.alphas.split(",") if x.strip()]
        if args.alphas
        else DEFAULT_ALPHAS
    )
    heartbeats = (
        [float(x) for x in args.heartbeats.split(",") if x.strip()]
        if args.heartbeats
        else DEFAULT_HEARTBEAT_HRS
    )

    paths_files = args.files.split(",") if args.files else []
    sensors_by_logger, _fmt = _ingest(paths_files, args.dir, "auto")

    # Channel-split up front so multi-channel probes are filtered per channel.
    split_by_logger = {
        name: pipeline.to_channel_streams(sensors)
        for name, sensors in sensors_by_logger.items()
    }
    channels = [c.strip() for c in args.channels.split(",") if c.strip()]

    print(f"sweep mode: {args.mode}")
    plots_dir = os.path.join(args.output, "plots")
    # alpha sweeps need one global alpha; drop per-channel overrides so every
    # channel shares the swept value (a clean sensitivity curve).
    clear_overrides = args.mode in ("alpha", "both", "overlay")

    # Mode-specific filenames so charts from different modes don't clobber.
    suffix = {"alpha": "", "heartbeat": "_heartbeat", "both": "_both"}
    annotate = {
        "alpha": ("a", "alpha"),
        "heartbeat": ("hb", "heartbeat_hr"),
        "both": None,
    }

    saved = _snapshot()
    try:
        for ch in (channels or [None]):
            if ch is None:
                subset, ch_suffix, title_ch = split_by_logger, "", "all channels"
            else:
                subset = _channel_subset(split_by_logger, ch)
                ch_suffix, title_ch = f"_{ch}", ch
            if not any(subset.values()):
                print(f"no streams for channel {ch!r}; skipping")
                continue
            print(f"\n=== channel: {title_ch} ===")
            if clear_overrides:
                config.eventDeltaThresholdByChannel = {}

            if args.mode == "overlay":
                _render_overlays(subset, alphas, os.path.join(plots_dir, title_ch)
                                 if ch else plots_dir)
                continue

            if args.mode == "sensitivity":
                series = _collect_sensitivity(subset, saved)
                _plot_curves(
                    series,
                    "rmse",
                    "Reconstruction RMSE (channel units)",
                    f"Parameter sensitivity ({title_ch}): RMSE vs Compression",
                    plots.plot_path(
                        plots_dir, "sweep", f"error_vs_compression_sensitivity{ch_suffix}"
                    ),
                    ("", "knob_val"),
                    legend=True,
                    legend_title="parameter",
                )
                _plot_curves(
                    series,
                    "mean_latency_hr",
                    "Mean transmission latency (hours)",
                    f"Parameter sensitivity ({title_ch}): Latency vs Compression",
                    plots.plot_path(
                        plots_dir, "sweep", f"latency_vs_compression_sensitivity{ch_suffix}"
                    ),
                    ("", "knob_val"),
                    legend=True,
                    legend_title="parameter",
                )
                _write_sensitivity_csv(
                    series,
                    os.path.join(args.output, f"sweep_sensitivity{ch_suffix}.csv"),
                )
                continue

            series = _collect(subset, args.mode, alphas, heartbeats)
            legend = args.mode == "both"
            _plot_error_comparison(
                series,
                f"Error vs Compression ({title_ch}, ZOH vs interp)",
                plots.plot_path(
                    plots_dir, "sweep", f"error_vs_compression{suffix[args.mode]}{ch_suffix}"
                ),
                annotate[args.mode],
                legend,
            )
            _plot_curves(
                series,
                "mean_latency_hr",
                "Mean transmission latency (hours)",
                f"Latency vs Compression ({title_ch})",
                plots.plot_path(
                    plots_dir, "sweep", f"latency_vs_compression{suffix[args.mode]}{ch_suffix}"
                ),
                annotate[args.mode],
                legend,
            )
            _write_sweep_csv(
                series,
                os.path.join(args.output, f"sweep{suffix[args.mode]}{ch_suffix}.csv"),
            )
    finally:
        _restore(saved)

    if args.mode == "overlay":
        print(f"\nWrote overlay graphs under {plots_dir}")
    else:
        print(f"\nWrote charts to {plots_dir} and sweep CSVs to {args.output}")

    if args.show:
        plt.show()


if __name__ == "__main__":
    # Allow `python simulation/src/sweep.py` from the repo root.
    sys.path.insert(
        0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    )
    main()
