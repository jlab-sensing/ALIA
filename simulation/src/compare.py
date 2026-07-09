"""
@file       compare.py

CLI entry point for the A/B comparison harness. Ingests sensor data via the
existing data_transformer, runs both the unfiltered and filtered branches,
overlays the two on the same plot, and writes metrics to CSV.

Example:
    python -m simulation.src.compare \\
        --dir data/UCSC_Haybarn_Field/data \\
        --output data/UCSC_Haybarn_Field/out
"""

import argparse
import csv
import json
import os
import sys
import numpy as np
from scipy import stats
import matplotlib.pyplot as plt

from simulation.src import metrics, pipeline, plots


def _ingest_xlsx(paths_files: list[str], path_dir: str):
    """Bridge to the site-specific XLSX ingestion in data_transformer.py."""
    from simulation import data_transformer as dt

    paths_files = list(paths_files) + dt._get_xlsx_files_from_dir(path_dir)
    sensors_by_logger = {}
    for path in paths_files:
        logger_name = os.path.splitext(os.path.basename(path))[0]
        sensors_by_logger[logger_name] = dt._convert_xlsx_to_sensors(path)
    return sensors_by_logger


def _ingest_sensorscope(paths_files: list[str], path_dir: str):
    """Bridge to SensorScope meteo .txt ingestion (fishnet/genepi/luce/
    plaine_morte). One logger == one station file, one Sensor per channel."""
    from simulation import sensorscope_transformer as ss

    paths_files = list(paths_files) + ss.get_txt_files_from_dir(path_dir)
    sensors_by_logger = {}
    for path in paths_files:
        logger_name = os.path.splitext(os.path.basename(path))[0]
        sensors = ss.convert_txt_to_sensors(path)
        if sensors:  # skip -def.txt sidecars and empty files
            sensors_by_logger[logger_name] = sensors
    return sensors_by_logger


def _detect_format(paths_files: list[str], path_dir: str) -> str:
    """'sensorscope' if the inputs are .txt logs, else 'xlsx'."""
    candidates = list(paths_files)
    if path_dir and os.path.isdir(path_dir):
        for root, _dirs, files in os.walk(path_dir):
            candidates.extend(files)
    if any(str(c).endswith(".txt") for c in candidates) and not any(
        str(c).endswith(".xlsx") for c in candidates
    ):
        return "sensorscope"
    return "xlsx"


def _ingest(paths_files: list[str], path_dir: str, fmt: str):
    if fmt == "auto":
        fmt = _detect_format(paths_files, path_dir)
    if fmt == "sensorscope":
        return _ingest_sensorscope(paths_files, path_dir), fmt
    return _ingest_xlsx(paths_files, path_dir), fmt


def _plot_original_histogram(logger_name: str, raw, out_dir: str):
    """One subplot per Sensor (channel): density histogram of raw values
    with a fitted normal PDF overlaid.

    Every Sensor is single-channel (its reading in `.value`) after the
    per-channel split, and different channels carry different units, so each
    gets its own axis rather than sharing one.
    """
    channels = [fs for fs in raw if fs.data]
    if not channels:
        return
    fig, axes = plt.subplots(
        len(channels),
        1,
        layout="constrained",
        figsize=(10, 2.2 * len(channels)),
    )
    if len(channels) == 1:
        axes = [axes]
    fig.suptitle(f"{logger_name} — histogram of raw")

    for ax, fs in zip(axes, channels):
        # Strip the "<station>-" prefix for a compact axis label.
        label = fs.name.split("-")[-1]
        data = np.asarray([d.value for d in fs.data], dtype=float)
        mu, sigma = data.mean(), data.std()
        ax.hist(
            data,
            bins="auto",
            density=True,
            alpha=0.6,
            edgecolor="black",
        )
        if sigma > 0:
            x = np.linspace(data.min(), data.max(), 500)
            ax.plot(x, stats.norm.pdf(x, mu, sigma), "r-", linewidth=2)
        ax.set_title(f"{fs.name} — Histogram")
        ax.set_xlabel(label, fontsize="small")
        ax.set_ylabel("Density", fontsize="small")

    fig.savefig(plots.plot_path(out_dir, "histogram", logger_name), dpi=120)
    plt.close(fig)


def _plot_overlay_generic(logger_name: str, raw, filtered, out_dir: str):
    """One subplot per Sensor (channel), raw faded vs filtered solid.

    Every Sensor is single-channel (its reading in `.value`) after the
    per-channel split, and different channels carry different units, so each
    gets its own axis rather than sharing one.
    """
    raw_by_name = {s.name: s for s in raw}
    channels = [fs for fs in filtered if fs.name in raw_by_name]
    if not channels:
        return
    fig, axes = plt.subplots(
        len(channels),
        1,
        sharex=True,
        layout="constrained",
        figsize=(10, 2.2 * len(channels)),
    )
    if len(channels) == 1:
        axes = [axes]
    fig.suptitle(f"{logger_name} — raw (faded) vs filtered (solid)")

    for ax, fs in zip(axes, channels):
        rs = raw_by_name[fs.name]
        # Strip the "<station>-" prefix for a compact axis label.
        label = fs.name.split("-")[-1]
        ax.plot(
            [d.timestamp for d in rs.data],
            [d.value for d in rs.data],
            alpha=0.25,
            linewidth=0.8,
        )
        ax.plot(
            [d.timestamp for d in fs.data],
            [d.value for d in fs.data],
            linewidth=1.2,
        )
        ax.set_ylabel(label, fontsize="small")

        # Guide-rail annotation: put the fidelity/compression numbers on the
        # panel so the picture and the metric sit together. RMSE and max_err are
        # in the channel's own units (same axis as the y-scale); "×N" is the
        # compression ratio; "Nx res" is RMSE as a multiple of sensor resolution.
        err = metrics.reconstruction_error(rs, fs)
        comp = len(rs.data) / len(fs.data) if fs.data else float("inf")

        def _g(v, prec=3):
            return f"{v:.{prec}g}" if v is not None else "n/a"

        over_res = (
            f"{err['rmse_over_res']:.0f}× res"
            if err["rmse_over_res"] is not None
            else "n/a"
        )
        note = (
            f"RMSE {_g(err['rmse'])}  ·  max {_g(err['max_err'])}"
            f"  ·  {comp:.0f}×  ·  {over_res}"
        )
        ax.text(
            0.006,
            0.94,
            note,
            transform=ax.transAxes,
            fontsize=7,
            va="top",
            ha="left",
            bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="0.7", alpha=0.75),
        )
    axes[-1].set_xlabel("Time")

    fig.savefig(plots.plot_path(out_dir, "overlay", logger_name), dpi=120)
    plt.close(fig)


_METRICS_HEADER = [
    "logger",
    "sensor",
    "n_raw",
    "mean",
    "range",
    "compression_ratio",
    "rmse",
    "mae",
    "max_err",
    "rmse_over_res",
    "mean_latency_hr",
]


def _metric_rows(all_diffs: dict) -> list[list[str]]:
    """One lean row per (logger, sensor): compression, fidelity, timeliness.

    Fidelity is reported in the channel's own physical units (rmse/mae/max_err)
    plus rmse_over_res, the error as a multiple of the sensor's resolution — a
    dimensionless, cross-channel number that doesn't blow up on zero-mean or
    dead-calm channels the way a mean- or std-normalized nrmse does.

    The full per-sensor detail (counts, rates, mean/std deltas, interp errors)
    is still written to metrics.json; these rows are the at-a-glance summary.
    """
    rows = []
    for logger_name, d in all_diffs.items():
        for sensor_name, row in d["per_sensor"].items():
            ratio = (
                row["n_raw"] / row["n_filtered"] if row["n_filtered"] else float("inf")
            )
            lat_hr = row["mean_latency_hr"]

            def _fmt(v, prec=4):
                return f"{v:.{prec}f}" if v is not None else ""

            rows.append(
                [
                    logger_name,
                    sensor_name,
                    str(row["n_raw"]),
                    _fmt(row.get("value_mean")),
                    _fmt(row.get("value_range")),
                    f"{ratio:.2f}",
                    _fmt(row["rmse"]),
                    _fmt(row["mae"]),
                    _fmt(row["max_err"]),
                    _fmt(row["rmse_over_res"], 2),
                    _fmt(lat_hr, 2),
                ]
            )
    return rows


def _write_metrics_csv(all_diffs: dict, csv_path: str):
    """Machine-readable comma-separated summary (one row per logger/sensor)."""
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    with open(csv_path, "w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(_METRICS_HEADER)
        w.writerows(_metric_rows(all_diffs))


def _write_metrics_table(all_diffs: dict, table_path: str):
    """Human-readable, column-aligned version of the same summary."""
    os.makedirs(os.path.dirname(table_path), exist_ok=True)
    rows = [_METRICS_HEADER] + _metric_rows(all_diffs)
    widths = [max(len(r[i]) for r in rows) for i in range(len(_METRICS_HEADER))]

    def fmt(r):
        # Left-align the text columns, right-align the numeric ones.
        cells = [r[i].ljust(widths[i]) if i < 2 else r[i].rjust(widths[i])
                 for i in range(len(r))]
        return "  ".join(cells)

    lines = [fmt(rows[0]), "  ".join("-" * w for w in widths)]
    lines += [fmt(r) for r in rows[1:]]
    with open(table_path, "w") as fp:
        fp.write("\n".join(lines) + "\n")


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
        help="Output directory for plots and metrics.",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Display plots interactively in addition to saving.",
    )
    parser.add_argument(
        "--format",
        default="auto",
        choices=["auto", "xlsx", "sensorscope"],
        help="Input format. 'auto' detects .txt (SensorScope "
        "meteo) vs .xlsx (Haybarn loggers).",
    )
    args = parser.parse_args()

    paths_files = args.files.split(",") if args.files else []
    sensors_by_logger, fmt = _ingest(paths_files, args.dir, args.format)
    if not sensors_by_logger:
        print("No sensor data found for the given inputs.")
        return

    # Split every Sensor into one single-channel stream per data type present,
    # so each channel is filtered and scored on its own dynamics rather than
    # riding on another channel's keep/drop decision. After this, every Sensor
    # is single-channel, so the per-channel plot is used for both formats.
    sensors_by_logger = {
        name: pipeline.to_channel_streams(sensors)
        for name, sensors in sensors_by_logger.items()
    }

    plots_dir = os.path.join(args.output, "plots")
    all_diffs = {}

    for logger_name, sensors in sensors_by_logger.items():
        branches = pipeline.run_both(sensors)
        d = metrics.diff(branches["raw"], branches["filtered"])
        all_diffs[logger_name] = d

        print(f"\n=== {logger_name} ===")
        print(f"  totals: {d['totals']}")
        print(f"  drops_by_reason: {d['drops_by_reason']}")

        _plot_overlay_generic(
            logger_name, branches["raw"], branches["filtered"], plots_dir
        )
        _plot_original_histogram(logger_name, branches["raw"], plots_dir)

    _write_metrics_csv(all_diffs, os.path.join(args.output, "metrics.csv"))
    _write_metrics_table(all_diffs, os.path.join(args.output, "metrics.txt"))
    with open(os.path.join(args.output, "metrics.json"), "w") as fp:
        json.dump(all_diffs, fp, indent=2, default=str)

    if args.show:
        plt.show()


if __name__ == "__main__":
    # Allow `python simulation/src/compare.py` from the repo root.
    sys.path.insert(
        0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    )
    main()
