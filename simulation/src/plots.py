"""Central path resolver for every generated figure, so plots never collide
and the output tree stays self-describing. Layout: <output>/plots/<category>/
<name>.png (categories: overlay, histogram, sweep)."""

import os


def plot_path(plots_dir: str, category: str, name: str) -> str:
    """Return `<plots_dir>/<category>/<name>.png`, creating the category dir.
    The ".png" suffix is added if absent."""
    category_dir = os.path.join(plots_dir, category)
    os.makedirs(category_dir, exist_ok=True)
    if not name.endswith(".png"):
        name += ".png"
    return os.path.join(category_dir, name)


# --- Paper styling ---------------------------------------------------------
#
# One look for every figure that goes into the write-up: column-width sizes,
# print-legible fonts, vector output, and a fixed colour/marker/linestyle per
# method so a method reads the same in every figure. Hues are categorical
# slots 1-5 in fixed order (validated for CVD separation); three sit below 3:1
# contrast on white, so marker and linestyle always carry the identity too.

COLUMN_IN = 3.4  # single-column width (IEEE/ACM two-column layouts)
DOUBLE_IN = 7.0  # full text width

INK = "#1f1f1e"
INK_MUTED = "#6b6b66"
RAW_GREY = "#b4b3ad"

# method -> (colour, marker, linestyle, linewidth)
METHOD_STYLE = {
    "alia": ("#2a78d6", "o", "-", 1.8),
    "sod_commissioned": ("#eb6834", "s", "--", 1.3),
    "uniform": ("#1baf7a", "D", ":", 1.3),
    "alia_events": ("#eda100", "v", "-.", 1.2),
    "alia_heartbeat": ("#e87ba4", "P", (0, (5, 1.5, 1, 1.5, 1, 1.5)), 1.2),
}

METHOD_LABEL = {
    "alia": "ALIA",
    "sod_commissioned": "Send-on-delta",
    "uniform": "Uniform",
    "alia_events": "ALIA, no heartbeat",
    "alia_heartbeat": "ALIA, no events",
}

CHANNEL_LABEL = {
    "AmbientTemp_C": ("Air temperature", "°C"),
    "SurfaceTemp_C": ("Surface temperature", "°C"),
    "RelativeHumidity_pct": ("Relative humidity", "%"),
    "SolarRadiation_Wm2": ("Solar radiation", "W/m²"),
    "WindSpeed_ms": ("Wind speed", "m/s"),
    "VWC": ("Soil VWC", "m³/m³"),
    "ST": ("Soil temperature", "°C"),
    "SC": ("Soil EC", "dS/m"),
}


def channel_label(channel: str, with_unit: bool = True) -> str:
    name, unit = CHANNEL_LABEL.get(channel, (channel, ""))
    return f"{name} ({unit})" if (with_unit and unit) else name


def apply_paper_style() -> None:
    import matplotlib as mpl

    mpl.rcParams.update(
        {
            "font.family": "serif",
            "font.serif": ["Times New Roman", "Times", "DejaVu Serif"],
            "mathtext.fontset": "stix",
            "font.size": 8,
            "axes.titlesize": 8,
            "axes.labelsize": 8,
            "xtick.labelsize": 7,
            "ytick.labelsize": 7,
            "legend.fontsize": 7,
            "axes.edgecolor": INK_MUTED,
            "axes.labelcolor": INK,
            "text.color": INK,
            "xtick.color": INK_MUTED,
            "ytick.color": INK_MUTED,
            "axes.linewidth": 0.6,
            "xtick.major.width": 0.6,
            "ytick.major.width": 0.6,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.color": "#e4e3de",
            "grid.linewidth": 0.5,
            "axes.axisbelow": True,
            "legend.frameon": False,
            "lines.markersize": 4,
            "figure.facecolor": "white",
            "savefig.facecolor": "white",
            "pdf.fonttype": 42,  # embed TrueType: editable text, no Type 3
            "ps.fonttype": 42,
        }
    )


def save_figure(fig, out_dir: str, name: str) -> None:
    """Write `<name>.pdf` (for LaTeX) and a 300-dpi `<name>.png` (for preview)."""
    os.makedirs(out_dir, exist_ok=True)
    for ext, kw in (("pdf", {}), ("png", {"dpi": 300})):
        fig.savefig(os.path.join(out_dir, f"{name}.{ext}"), bbox_inches="tight", **kw)
