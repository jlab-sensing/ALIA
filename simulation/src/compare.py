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

import matplotlib.pyplot as plt

from simulation.src import metrics, pipeline


def _ingest(paths_files: list[str], path_dir: str):
    """Bridge to the site-specific ingestion in data_transformer.py."""
    # Late import — data_transformer lives outside the simulation package.
    from data.UCSC_Haybarn_Field import data_transformer as dt

    paths_files = list(paths_files) + dt._get_xlsx_files_from_dir(path_dir)
    sensors_by_logger = {}
    for path in paths_files:
        logger_name = os.path.splitext(os.path.basename(path))[0]
        sensors_by_logger[logger_name] = dt._convert_xlsx_to_sensors(path)
    return sensors_by_logger


def _plot_overlay(logger_name: str, raw, filtered, out_dir: str):
    fig, axes = plt.subplots(3, 1, sharex=True, layout="constrained")
    fig.suptitle(f"{logger_name} — raw (faded) vs filtered (solid)")

    raw_by_name = {s.name: s for s in raw}
    for ax, field, title in [
        (axes[0], "VWC", "Volumetric Water Content"),
        (axes[1], "SC", "Soil Conductivity"),
        (axes[2], "ST", "Soil Temperature"),
    ]:
        for fs in filtered:
            rs = raw_by_name.get(fs.name)
            if rs is None:
                continue
            ax.plot(
                [d.timestamp for d in rs.data],
                [getattr(d, field) for d in rs.data],
                alpha=0.25,
                linewidth=0.8,
            )
            ax.plot(
                [d.timestamp for d in fs.data],
                [getattr(d, field) for d in fs.data],
                label=fs.name,
                linewidth=1.2,
            )
        ax.set_title(title)
        ax.set_ylabel(field)
    axes[2].set_xlabel("Time")

    os.makedirs(out_dir, exist_ok=True)
    fig.savefig(os.path.join(out_dir, f"{logger_name}.png"), dpi=120)
    plt.close(fig)


def _write_metrics_csv(all_diffs: dict, csv_path: str):
    """One row per (logger, sensor) with the headline metrics."""
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    with open(csv_path, "w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(
            [
                "logger",
                "sensor",
                "n_raw",
                "n_filtered",
                "n_compressed",
                "n_dropped",
                "n_reconstructable",
                "compression_rate",
                "drop_rate",
                "vwc_mean_delta",
                "vwc_std_delta",
            ]
        )
        for logger_name, d in all_diffs.items():
            for sensor_name, row in d["per_sensor"].items():
                w.writerow(
                    [
                        logger_name,
                        sensor_name,
                        row["n_raw"],
                        row["n_filtered"],
                        row["n_compressed"],
                        row["n_dropped"],
                        row["n_reconstructable"],
                        f"{row['compression_rate']:.4f}",
                        f"{row['drop_rate']:.4f}",
                        f"{row['vwc_mean_delta']:.6f}",
                        f"{row['vwc_std_delta']:.6f}",
                    ]
                )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-f", "--files", default="", type=str,
                        help="Comma-separated XLSX paths.")
    parser.add_argument("-d", "--dir", default="", type=str,
                        help="Directory of XLSX files.")
    parser.add_argument("-o", "--output", default="./out", type=str,
                        help="Output directory for plots and metrics.")
    parser.add_argument("--show", action="store_true",
                        help="Display plots interactively in addition to saving.")
    args = parser.parse_args()

    paths_files = args.files.split(",") if args.files else []
    sensors_by_logger = _ingest(paths_files, args.dir)

    plots_dir = os.path.join(args.output, "plots")
    all_diffs = {}

    for logger_name, sensors in sensors_by_logger.items():
        branches = pipeline.run_both(sensors)
        d = metrics.diff(branches["raw"], branches["filtered"])
        all_diffs[logger_name] = d

        print(f"\n=== {logger_name} ===")
        print(f"  totals: {d['totals']}")
        print(f"  drops_by_reason: {d['drops_by_reason']}")

        _plot_overlay(logger_name, branches["raw"], branches["filtered"], plots_dir)

    _write_metrics_csv(all_diffs, os.path.join(args.output, "metrics.csv"))
    with open(os.path.join(args.output, "metrics.json"), "w") as fp:
        json.dump(all_diffs, fp, indent=2, default=str)

    if args.show:
        plt.show()


if __name__ == "__main__":
    # Allow `python simulation/src/compare.py` from the repo root.
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
    main()
