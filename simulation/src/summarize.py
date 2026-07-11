"""
@file       summarize.py

Turns the flat per-stream metrics tables (metrics.csv / metrics.txt) into
figures you can actually draw conclusions from. The table lists every stream's
compression, reconstruction error and latency side by side, but the story is in
the *tradeoffs between* those columns, which a table hides.

Two figures per dataset, written under <output>/plots/summary/:

    tradeoff.png    the map: one dot per stream, x = compression ratio (log),
                    y = reconstruction error as % of the signal's own range,
                    dot size = mean transmit latency, colour = sensor family.
                    Bottom-right = the win (heavy compression, faithful);
                    top-left = streams the algorithm can't compress cleanly.

    by_channel.png  two ranked bar panels: median compression and median error
                    per sensor family, so you can see at a glance which signals
                    are easy (slow, smooth) and which fight the threshold.

Error is normalised by each stream's range so temperature (span ~25 C) and
solar radiation (span ~1100 W/m2) land on the same axis and are comparable.

Usage
-----
    # one dataset (reads data/out/fishnet/metrics.csv)
    python -m simulation.src.summarize --dir data/out/fishnet

    # every dataset under data/out, plus a combined cross-dataset figure
    python -m simulation.src.summarize --root data/out

Both entry points route figures through plots.plot_path, so they land beside
the existing overlay/histogram/sweep plots rather than in an ad-hoc location.
"""

import argparse
import csv
import glob
import os

import numpy as np
import matplotlib.pyplot as plt

from simulation.src import plots


# --- sensor families -> super-family (colour) ------------------------------
# Individual channel suffixes are grouped so the legend stays readable and the
# colours stay colourblind-distinct (validated categorical slots).

_SUPER = {
    "AmbientTemp_C": "Temperature",
    "SurfaceTemp_C": "Temperature",
    "ST": "Temperature",
    "RelativeHumidity_pct": "Moisture / Humidity",
    "VWC": "Moisture / Humidity",
    "SoilMoisture_pct": "Moisture / Humidity",
    "Watermark_kPa": "Moisture / Humidity",
    "SolarRadiation_Wm2": "Solar Radiation",
    "SC": "Conductivity",
    "WindSpeed_ms": "Wind",
    "WindDirection_deg": "Wind",
    "Rain_mm": "Rain",
}

# Colourblind-safe categorical palette (validated); one hue per super-family.
_COLOR = {
    "Temperature": "#2a78d6",          # blue
    "Moisture / Humidity": "#1baf7a",  # aqua
    "Solar Radiation": "#eda100",      # yellow
    "Conductivity": "#008300",         # green
    "Wind": "#4a3aa7",                 # violet
    "Rain": "#e34948",                 # red
}
_ORDER = ["Temperature", "Moisture / Humidity", "Solar Radiation",
          "Conductivity", "Wind", "Rain"]

# Pretty per-channel names for the ranking axis.
_PRETTY = {
    "AmbientTemp_C": "Ambient Temp", "SurfaceTemp_C": "Surface Temp",
    "ST": "Soil Temp", "RelativeHumidity_pct": "Rel. Humidity",
    "VWC": "Soil VWC", "SoilMoisture_pct": "Soil Moisture",
    "Watermark_kPa": "Watermark", "SolarRadiation_Wm2": "Solar Radiation",
    "SC": "Soil EC", "WindSpeed_ms": "Wind Speed",
    "WindDirection_deg": "Wind Direction", "Rain_mm": "Rain",
}


def _channel(sensor: str) -> str:
    """The channel suffix of a stream name, e.g. '...-AmbientTemp_C'."""
    suffix = sensor.rsplit("-", 1)[-1]
    if "Port" in suffix and "TEROS" in suffix:
        return "TEROS10"
    return suffix


def _super(channel: str) -> str:
    if channel == "TEROS10":
        return "Moisture / Humidity"
    return _SUPER.get(channel, "Moisture / Humidity")


def _pretty(channel: str) -> str:
    if channel == "TEROS10":
        return "Soil Moisture (TEROS)"
    return _PRETTY.get(channel, channel)


def _load(csv_path: str) -> list[dict]:
    """Read one metrics.csv, enriching each row with channel/family/error%."""
    rows = []
    with open(csv_path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                comp = float(r["compression_ratio"])
                rmse = float(r["rmse"])
                rng = float(r["range"])
                n_raw = float(r["n_raw"])
            except (ValueError, KeyError):
                continue
            lat = r.get("mean_latency_hr", "")
            try:
                lat = float(lat)
            except ValueError:
                lat = None
            ch = _channel(r["sensor"])
            rows.append({
                "sensor": r["sensor"],
                "channel": ch,
                "family": _super(ch),
                "pretty": _pretty(ch),
                "compression": comp,
                "err_pct": (rmse / rng * 100.0) if rng > 0 else None,
                "rmse": rmse,
                "latency": lat,
                "n_raw": n_raw,
            })
    return rows


def _median(vals) -> float:
    vals = [v for v in vals if v is not None]
    return float(np.median(vals)) if vals else float("nan")


# --- figures ---------------------------------------------------------------

def _plot_tradeoff(rows: list[dict], title: str, path: str) -> None:
    """The map: compression (log-x) vs error% (y), sized by latency."""
    pts = [r for r in rows if r["err_pct"] is not None]
    fig, ax = plt.subplots(figsize=(9, 5.5), layout="constrained")

    lats = [r["latency"] for r in pts if r["latency"] is not None]
    lat_max = max(lats) if lats else 1.0
    for fam in _ORDER:
        fam_pts = [r for r in pts if r["family"] == fam]
        if not fam_pts:
            continue
        sizes = [30 + 220 * ((r["latency"] or 0) / lat_max) for r in fam_pts]
        ax.scatter(
            [r["compression"] for r in fam_pts],
            [r["err_pct"] for r in fam_pts],
            s=sizes, c=_COLOR[fam], label=fam,
            alpha=0.75, edgecolors="white", linewidths=0.6, zorder=3,
        )

    ax.set_xscale("log")
    ax.set_xlabel("Compression ratio  (log scale)  →  more samples skipped")
    ax.set_ylabel("Reconstruction error  (% of signal range)")
    ax.set_title(title, fontsize=13, fontweight="bold", loc="left")
    ax.grid(True, which="both", color="0.88", linewidth=0.7, zorder=0)
    ax.set_axisbelow(True)
    from matplotlib.ticker import ScalarFormatter, FuncFormatter
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}×"))
    ax.legend(title="Sensor family", frameon=False, fontsize=9,
              title_fontsize=9, loc="upper right")
    ax.text(0.01, -0.13,
            "Dot size ∝ mean transmit latency.  Bottom-right is ideal: "
            "heavy compression, faithful reconstruction.",
            transform=ax.transAxes, fontsize=8.5, color="0.4")
    fig.savefig(path, dpi=120)
    plt.close(fig)


def _plot_by_channel(rows: list[dict], title: str, path: str) -> None:
    """Two ranked bar panels: median compression and median error per channel."""
    channels = sorted({r["channel"] for r in rows})
    stats = []
    for ch in channels:
        cr = [r for r in rows if r["channel"] == ch]
        stats.append({
            "pretty": _pretty(ch),
            "color": _COLOR[_super(ch)],
            "comp": _median([r["compression"] for r in cr]),
            "err": _median([r["err_pct"] for r in cr]),
            "n": len(cr),
        })

    fig, (axc, axe) = plt.subplots(
        1, 2, figsize=(11, max(3.2, 0.42 * len(stats) + 1.2)),
        layout="constrained")

    comp_sorted = sorted(stats, key=lambda s: s["comp"])
    y = np.arange(len(comp_sorted))
    axc.barh(y, [s["comp"] for s in comp_sorted],
             color=[s["color"] for s in comp_sorted], zorder=3)
    axc.set_yticks(y, [s["pretty"] for s in comp_sorted], fontsize=9)
    axc.set_xlabel("Median compression ratio  (higher = more skipped)")
    axc.set_title("Which signals compress?", fontsize=11, fontweight="bold",
                  loc="left")
    for i, s in enumerate(comp_sorted):
        axc.text(s["comp"], i, f" {s['comp']:.0f}×", va="center",
                 fontsize=8.5, color="0.25")

    err_sorted = sorted(stats, key=lambda s: (np.isnan(s["err"]), -s["err"]))
    y = np.arange(len(err_sorted))
    axe.barh(y, [0 if np.isnan(s["err"]) else s["err"] for s in err_sorted],
             color=[s["color"] for s in err_sorted], zorder=3)
    axe.set_yticks(y, [s["pretty"] for s in err_sorted], fontsize=9)
    axe.set_xlabel("Median error  (% of range, lower = more faithful)")
    axe.set_title("...and at what cost?", fontsize=11, fontweight="bold",
                  loc="left")
    for i, s in enumerate(err_sorted):
        if not np.isnan(s["err"]):
            axe.text(s["err"], i, f" {s['err']:.1f}%", va="center",
                     fontsize=8.5, color="0.25")

    for ax in (axc, axe):
        ax.grid(True, axis="x", color="0.88", linewidth=0.7, zorder=0)
        ax.set_axisbelow(True)
        ax.margins(x=0.14)
    fig.suptitle(title, fontsize=13, fontweight="bold", x=0.01, ha="left")
    fig.savefig(path, dpi=120)
    plt.close(fig)


def _plot_facets(named_rows: list[tuple], title: str, path: str) -> None:
    """Small multiples: the tradeoff map per dataset, on shared axes.

    `named_rows` is a list of (label, rows). Every panel uses the same x/y
    limits so sites are compared like-for-like — a panel whose cloud sits
    lower/right compresses better at a given fidelity than one shifted up/left.
    """
    named = [(lbl, [r for r in rows if r["err_pct"] is not None])
             for lbl, rows in named_rows]
    named = [(lbl, pts) for lbl, pts in named if pts]
    if not named:
        return
    every = [r for _, pts in named for r in pts]
    xs = [r["compression"] for r in every]
    xlim = (min(xs) * 0.85, max(xs) * 1.18)
    ylim = (-1.0, max(r["err_pct"] for r in every) * 1.08)

    n = len(named)
    ncols = 3 if n > 4 else 2
    nrows = -(-n // ncols)
    fig, axes = plt.subplots(
        nrows, ncols, figsize=(4.3 * ncols, 3.3 * nrows),
        layout="constrained", sharex=True, sharey=True, squeeze=False)
    flat = axes.ravel()

    seen = {}
    for ax, (label, pts) in zip(flat, named):
        for fam in _ORDER:
            fam_pts = [r for r in pts if r["family"] == fam]
            if not fam_pts:
                continue
            h = ax.scatter(
                [r["compression"] for r in fam_pts],
                [r["err_pct"] for r in fam_pts],
                s=20, c=_COLOR[fam], alpha=0.75,
                edgecolors="white", linewidths=0.4, zorder=3)
            seen.setdefault(fam, h)
        ax.set_xscale("log")
        ax.set_xlim(*xlim)
        ax.set_ylim(*ylim)
        ax.set_title(f"{label}  ({len(pts)})", fontsize=10,
                     fontweight="bold", loc="left")
        ax.grid(True, which="both", color="0.9", linewidth=0.6, zorder=0)
        ax.set_axisbelow(True)
        from matplotlib.ticker import FuncFormatter
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}×"))
    for ax in flat[n:]:
        ax.set_visible(False)

    fig.supxlabel("Compression ratio  (log)  →  more skipped", fontsize=10)
    fig.supylabel("Reconstruction error  (% of range)", fontsize=10)
    fig.suptitle(title, fontsize=13, fontweight="bold", x=0.01, ha="left")
    fig.legend([seen[f] for f in _ORDER if f in seen],
               [f for f in _ORDER if f in seen],
               title="Sensor family", frameon=False, fontsize=9,
               title_fontsize=9, loc="lower right",
               bbox_to_anchor=(0.995, 0.02))
    fig.savefig(path, dpi=120)
    plt.close(fig)


def summarize(rows: list[dict], label: str, out_dir: str) -> None:
    """Write both summary figures for one collection of streams."""
    if not rows:
        print(f"  {label}: no usable rows, skipping")
        return
    _plot_tradeoff(
        rows, f"{label} — compression vs. fidelity  ({len(rows)} streams)",
        plots.plot_path(out_dir, "summary", "tradeoff"))
    _plot_by_channel(
        rows, f"{label} — compression & error by channel",
        plots.plot_path(out_dir, "summary", "by_channel"))
    print(f"  {label}: wrote plots/summary/tradeoff.png, "
          f"plots/summary/by_channel.png ({len(rows)} streams)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[3])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("-d", "--dir", help="a single dataset output dir "
                   "(containing metrics.csv), e.g. data/out/fishnet")
    g.add_argument("-r", "--root", help="root holding several dataset dirs "
                   "(e.g. data/out); summarises each plus a combined figure")
    args = ap.parse_args()

    if args.dir:
        csv_path = os.path.join(args.dir, "metrics.csv")
        if not os.path.exists(csv_path):
            ap.error(f"no metrics.csv in {args.dir}")
        label = os.path.basename(os.path.normpath(args.dir))
        print("Summarising:")
        summarize(_load(csv_path), label, os.path.join(args.dir, "plots"))
        return

    csvs = sorted(glob.glob(os.path.join(args.root, "*", "metrics.csv")))
    if not csvs:
        ap.error(f"no */metrics.csv found under {args.root}")
    print("Summarising:")
    combined = []
    named = []
    for csv_path in csvs:
        ds_dir = os.path.dirname(csv_path)
        label = os.path.basename(ds_dir)
        rows = _load(csv_path)
        combined.extend(rows)
        named.append((label, rows))
        summarize(rows, label, os.path.join(ds_dir, "plots"))
    combined_dir = os.path.join(args.root, "_summary", "plots")
    summarize(combined, "All datasets", combined_dir)
    _plot_facets(named, "Compression vs. fidelity by dataset",
                 plots.plot_path(combined_dir, "summary", "by_dataset"))
    print(f"  All datasets: wrote plots/summary/by_dataset.png "
          f"({len(named)} sites)")


if __name__ == "__main__":
    main()
