"""
@file       detector_overlay.py

Runs the filter under each event detector (kl, ttest, delta) at field
parameters and overlays the reconstructions. No per-stream tuning or matched
compression — each detector runs as deployed, so they land at different
compression ratios; each panel's legend reports its actual compression/RMSE.

Writes one PNG per logger under <output>/plots/overlay/{kl,ttest,delta,all}/
plus <output>/detector_metrics.csv (compression/RMSE/latency per logger, stream,
detector; kl rows carry the auto-calibrated threshold). Stale plot categories
from earlier runs are cleared.

Example:
    python -m simulation.src.detector_overlay \\
        --dir data/UCSC_Haybarn_Field --output data/out/UCSC_Haybarn_Field
"""

import argparse
import csv
import os
import shutil
import sys
from copy import deepcopy

import matplotlib.pyplot as plt

from simulation.src import config, metrics, pipeline, plots
from simulation.src import filter as _filter
from simulation.src.compare import _ingest

DETECTORS = ["kl", "ttest", "delta"]
DETECTOR_COLORS = {"kl": "#2ca02c", "ttest": "#1f77b4", "delta": "#d62728"}

# Plot categories from other tools, removed so plots/ holds only overlay/.
_STALE_CATEGORIES = ["histogram", "summary", "kl_sweep", "kl_timeseries", "kl_overlay"]


def _clean_plots(plots_dir: str) -> None:
    for cat in ["overlay"] + _STALE_CATEGORIES:
        shutil.rmtree(os.path.join(plots_dir, cat), ignore_errors=True)


def _filtered_by_detector(raw_streams):
    """{detector: {stream_name: filtered_sensor}} plus {name: kl_threshold}."""
    result = {}
    kl_thresholds = {}
    for det in DETECTORS:
        config.eventDetector = det
        _filter.last_kl_thresholds = {}
        filtered = _filter.filter(deepcopy(raw_streams))
        result[det] = {fs.name: fs for fs in filtered}
        if det == "kl":
            kl_thresholds = dict(_filter.last_kl_thresholds)
    return result, kl_thresholds


def _plot_overlay(logger_name, raw_streams, filtered_by_det, detectors, out_dir, subdir):
    """One subplot per stream: raw (faded) plus a step reconstruction per
    detector, labelled with its achieved compression ratio and RMSE."""
    raw_by_name = {s.name: s for s in raw_streams}
    names = [s.name for s in raw_streams if s.data]
    if not names:
        return
    fig, axes = plt.subplots(
        len(names), 1, sharex=True, layout="constrained",
        figsize=(11, 2.3 * len(names)),
    )
    if len(names) == 1:
        axes = [axes]
    title = detectors[0] if len(detectors) == 1 else "kl / ttest / delta"
    fig.suptitle(f"{logger_name} — raw vs {title} reconstruction")

    for ax, name in zip(axes, names):
        rs = raw_by_name[name]
        ax.plot(
            [d.timestamp for d in rs.data], [d.value for d in rs.data],
            color="0.55", linewidth=0.7, alpha=0.5, label="raw", zorder=1,
        )
        for i, det in enumerate(detectors):
            fs = filtered_by_det[det].get(name)
            if fs is None or not fs.data:
                continue
            err = metrics.reconstruction_error(rs, fs)
            comp = len(rs.data) / len(fs.data)
            rmse = err["rmse"]
            ax.plot(
                [d.timestamp for d in fs.data], [d.value for d in fs.data],
                color=DETECTOR_COLORS[det], linewidth=1.0, drawstyle="steps-post",
                alpha=0.9, zorder=2 + i,
                label=f"{det}: {comp:.0f}× · RMSE {rmse:.3g}"
                if rmse is not None else f"{det}: {comp:.0f}×",
            )
        ax.set_ylabel(name.split("-")[-1], fontsize="small")
        ax.legend(fontsize=6, loc="upper right", ncol=len(detectors))
    axes[-1].set_xlabel("time")

    fig.savefig(plots.plot_path(out_dir, f"overlay/{subdir}", logger_name), dpi=120)
    plt.close(fig)


def _fmt(v, prec=4):
    return f"{v:.{prec}f}" if v is not None else ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-f", "--files", default="", type=str,
                        help="Comma-separated input file paths.")
    parser.add_argument("-d", "--dir", default="", type=str,
                        help="Directory of input files.")
    parser.add_argument("-o", "--output", default="./out", type=str,
                        help="Output directory for the overlay tree and CSV.")
    parser.add_argument("--format", default="auto",
                        choices=["auto", "xlsx", "sensorscope"],
                        help="Input format ('auto' detects .txt vs .xlsx).")
    args = parser.parse_args()

    saved_detector = config.eventDetector
    paths_files = args.files.split(",") if args.files else []
    sensors_by_logger, _f = _ingest(paths_files, args.dir, args.format)
    if not sensors_by_logger:
        print("No sensor data found for the given inputs.")
        return

    plots_dir = os.path.join(args.output, "plots")
    _clean_plots(plots_dir)
    csv_rows = []

    try:
        for logger_name, sensors in sensors_by_logger.items():
            raw_streams = pipeline.to_channel_streams(sensors)
            filtered_by_det, kl_thr = _filtered_by_detector(raw_streams)

            for det in DETECTORS:
                _plot_overlay(logger_name, raw_streams, filtered_by_det, [det], plots_dir, det)
            _plot_overlay(logger_name, raw_streams, filtered_by_det, DETECTORS, plots_dir, "all")

            print(f"=== {logger_name} ===")
            for rs in (s for s in raw_streams if s.data):
                name = rs.name
                cells = []
                for det in DETECTORS:
                    fs = filtered_by_det[det].get(name)
                    if fs is None or not fs.data:
                        continue
                    err = metrics.reconstruction_error(rs, fs)
                    lat = metrics.transmission_latency(rs, fs)
                    comp = len(rs.data) / len(fs.data)
                    csv_rows.append([
                        logger_name, name.split("-")[-1], det,
                        len(rs.data), len(fs.data), f"{comp:.2f}",
                        _fmt(kl_thr.get(name), 4) if det == "kl" else "",
                        _fmt(err["rmse"]), _fmt(err["mae"]), _fmt(err["max_err"]),
                        _fmt((lat["sum_sec"] / lat["n"] / 3600) if lat["n"] else None, 2),
                    ])
                    cells.append(f"{det} {comp:.0f}× RMSE {err['rmse']:.3g}")
                print(f"  {name.split('-')[-1]:<22} " + "  ".join(cells))
    finally:
        config.eventDetector = saved_detector

    csv_path = os.path.join(args.output, "detector_metrics.csv")
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    with open(csv_path, "w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["logger", "stream", "detector", "n_raw", "n_tx",
                    "compression_ratio", "kl_threshold", "rmse", "mae",
                    "max_err", "mean_latency_hr"])
        w.writerows(csv_rows)

    print(f"\nWrote overlays to {os.path.join(plots_dir, 'overlay')} (kl/ ttest/ delta/ all/)")
    print(f"Wrote {csv_path}")


if __name__ == "__main__":
    sys.path.insert(
        0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    )
    main()
