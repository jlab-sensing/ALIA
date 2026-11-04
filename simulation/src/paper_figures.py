"""
paper_figures.py — every Results/Evaluation figure, in one folder, one style.

Reads the CSVs written by `benchmark.py` and `sweep.py --mode sensitivity`
and re-renders them for print: column-width, vector PDF (+300-dpi PNG
preview), and a fixed colour/marker/linestyle per method
(`plots.METHOD_STYLE`). Only the overlay figure re-runs the filter, because it
needs the transmitted packets themselves.

Figures
-------
fig1_error_frontier     RMSE vs compression, every method, one panel per dataset
fig2_silence_frontier   worst-case silence (longest wait for news) vs compression
fig3_reconstruction     raw vs ALIA vs send-on-delta at an equal packet budget
fig4_ablation           ALIA vs its two ablations at a matched budget
fig5_sensitivity        one-at-a-time parameter sensitivity, indexed to baseline
table_matched.{tex,csv} matched-budget results table

Example (after the benchmark/sweep runs listed in paperFigures/README.md):
    python -m simulation.src.paper_figures --bench data/out/paper --output paperFigures
"""

import argparse
import csv
import math
import os
from collections import defaultdict

import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

from simulation.src import baselines, config, filter as filter_algo, pipeline, plots
from simulation.src.compare import _ingest

# (bench subdir, channel, dataset label) — one panel each in figs 1 and 2.
FRONTIER_PANELS = [
    ("bench-fishnet", "AmbientTemp_C", "FishNet"),
    ("bench-plaine_morte", "RelativeHumidity_pct", "Plaine Morte"),
    ("bench-haybarn", "VWC", "UCSC Haybarn"),
]

# Datasets that go into the matched-budget table, in order.
TABLE_DATASETS = [
    ("bench-fishnet", "FishNet"),
    ("bench-plaine_morte", "Plaine Morte"),
    ("bench-haybarn", "UCSC Haybarn"),
]

# Below this achieved/target ratio a method did not spend the budget it was
# given (it transmitted more than allowed), so its error is not comparable.
HIT_FLOOR = 0.95
# Above this a method could not *spend* the budget: even its least-compressing
# setting sent far fewer packets (e.g. ALIA's resolution floor on wind speed).
# Its error is not comparable either, in the other direction.
HIT_CEIL = 1.25

# Figs 1-2 compare ALIA against the baselines only; the ablations get fig 4.
FRONTIER_METHODS = ["alia", "sod_commissioned", "uniform"]
ABLATION_METHODS = ["alia", "alia_events", "alia_heartbeat"]

# Channels left out of every figure and table. Wind speed's 0.5 m/s resolution
# floor stops ALIA from spending the budget, so it is not an equal-cost result.
EXCLUDED_CHANNELS = {"WindSpeed_ms"}

SENS_LABELS = {
    "eventDeltaThreshold": r"Event multiplier $\alpha$",
    "baseHeartbeatHours": "Base heartbeat (h)",
    "backoffDoublingHours": "Backoff doubling (h)",
    "maxHeartbeatHours": "Max heartbeat (h)",
}
# Indexed quantity -> (label, colour, marker, linestyle). Neutral hues: these
# are measures, not methods, so they must not borrow a method's colour.
SENS_SERIES = {
    "compression_ratio": ("Compression", "#2a78d6", "o", "-"),
    "rmse": ("RMSE", "#eb6834", "s", "--"),
    "mean_latency_hr": ("Mean latency", "#1baf7a", "D", ":"),
}


def _float(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _read_csv(path):
    if not os.path.exists(path):
        print(f"  missing {path} — skipped")
        return None
    with open(path, newline="") as fp:
        return list(csv.DictReader(fp))


def _matched(bench_dir):
    rows = _read_csv(os.path.join(bench_dir, "matched.csv"))
    if rows is None:
        return None
    return [r for r in rows if r["channel"] not in EXCLUDED_CHANNELS]


def _frontier(bench_dir, channel):
    rows = _read_csv(os.path.join(bench_dir, "frontier.csv"))
    if rows is None:
        return None
    out = defaultdict(list)
    for r in rows:
        if r["channel"] == channel:
            out[r["method"]].append(r)
    for m in out:
        out[m].sort(key=lambda r: _float(r["compression"]) or 0)
    return out


def _method_line(ax, method, xs, ys):
    color, marker, ls, lw = plots.METHOD_STYLE[method]
    ax.plot(
        xs,
        ys,
        color=color,
        marker=marker,
        linestyle=ls,
        linewidth=lw,
        markeredgecolor="white",
        markeredgewidth=0.5,
        markersize=4.2 if method == "alia" else 3.6,
        label=plots.METHOD_LABEL[method],
        zorder=5 if method == "alia" else 3,
        clip_on=False,
    )


def _method_legend(fig, methods, ncol=None):
    handles = []
    for m in methods:
        color, marker, ls, lw = plots.METHOD_STYLE[m]
        handles.append(
            Line2D(
                [], [], color=color, marker=marker, linestyle=ls, linewidth=lw,
                markeredgecolor="white", markeredgewidth=0.5,
                label=plots.METHOD_LABEL[m],
            )
        )
    fig.legend(
        handles=handles,
        loc="outside upper center",
        ncol=ncol or len(handles),
        handlelength=2.6,
        columnspacing=1.2,
    )


def _lower_first(text):
    """Sentence-case a label mid-title, leaving acronyms ("VWC") alone."""
    return text[0].lower() + text[1:] if text[1:2].islower() else text


def _frontier_figure(bench_root, out_dir, y_key, y_label, name, y_log=True,
                     max_comp=None):
    # A thesis figure with an empty panel is worse than no figure: refuse to
    # draw unless every dataset has frontier data.
    fronts = [_frontier(os.path.join(bench_root, sub), ch)
              for sub, ch, _d in FRONTIER_PANELS]
    missing = [d for fr, (_s, _c, d) in zip(fronts, FRONTIER_PANELS) if not fr]
    if missing:
        print(f"  {name} skipped: no frontier data for {', '.join(missing)}")
        return
    fig, axes = plt.subplots(
        1, len(FRONTIER_PANELS), figsize=(plots.DOUBLE_IN, 2.35),
        layout="constrained",
    )
    drawn = False
    for i, (ax, fr, (_sub, channel, dataset)) in enumerate(
            zip(axes, fronts, FRONTIER_PANELS)):
        ax.set_title(
            f"({'abc'[i]}) {dataset}, {_lower_first(plots.channel_label(channel, False))}",
            loc="left",
        )
        for m in FRONTIER_METHODS:
            pts = [
                (_float(r["compression"]), _float(r[y_key]))
                for r in fr.get(m, [])
                if r.get(y_key) not in (None, "")
            ]
            pts = [
                (x, y) for x, y in pts
                if x and y and (max_comp is None or x <= max_comp)
            ]
            if pts:
                _method_line(ax, m, [x for x, _ in pts], [y for _, y in pts])
                drawn = True
        ax.set_xscale("log")
        if y_log:
            ax.set_yscale("log")
        ax.set_xlabel("Compression ratio (×)")
    axes[0].set_ylabel(y_label)
    for ax, (_s, channel, _d) in zip(axes, FRONTIER_PANELS):
        if y_key in ("rmse", "max_err"):
            ax.set_ylabel(f"{y_label} ({plots.CHANNEL_LABEL.get(channel, ('', ''))[1]})")
    if drawn:
        _method_legend(fig, FRONTIER_METHODS)
        plots.save_figure(fig, out_dir, name)
        print(f"  wrote {name}")
    plt.close(fig)


# --- fig 3: reconstruction overlay ------------------------------------------


# Fig 3 shades receiver silences longer than this (ALIA's heartbeat cap).
SILENCE_SHADE_HR = 24
# Each shaded gap is inset by this much at both ends, leaving a white break at
# every packet so back-to-back gaps read as separate silences, not one block.
SILENCE_INSET_HR = 2.5
SILENCE_FILL = "#ecebe6"


def _zoh(tx, t0, t1):
    """Step coordinates of the zero-order-hold reconstruction over [t0, t1]."""
    tx = sorted(tx, key=lambda d: d.timestamp)
    before = [d for d in tx if d.timestamp <= t0]
    inside = [d for d in tx if t0 < d.timestamp <= t1]
    seq = ([before[-1]] if before else []) + inside
    xs = [max(d.timestamp, t0) for d in seq] + [t1]
    ys = [d.value for d in seq] + ([seq[-1].value] if seq else [])
    return xs, ys


def fig_reconstruction(data_dir, stream_name, out_dir, target=10.0, days=(10, 13)):
    import datetime as dt

    sensors_by_logger, _ = _ingest([], data_dir, "auto")
    streams = [
        s for sensors in sensors_by_logger.values()
        for s in pipeline.to_channel_streams(sensors)
    ]
    stream = next((s for s in streams if s.name == stream_name), None)
    if stream is None:
        print(f"  stream {stream_name} not found — fig3 skipped")
        return
    channel = next(d.name for d in stream.data if d.name)
    budget = max(2, int(len(stream.data) / target))

    tx = {}
    for m in ("alia", "sod_commissioned"):
        knob, _ = baselines.solve_knob(m, stream, budget)
        tx[m] = baselines.run(m, [stream], knob)[0].data

    start = stream.data[0].timestamp
    t0 = start + dt.timedelta(days=days[0])
    t1 = start + dt.timedelta(days=days[1])
    raw = [d for d in stream.data if t0 <= d.timestamp <= t1 and d.is_present()]

    # Break the raw trace across outages (> 3 sample periods) instead of
    # drawing a straight line through missing data.
    period = filter_algo._estimate_period_sec(stream.data)
    raw_x, raw_y = [], []
    for prev, d in zip([None] + raw[:-1], raw):
        if prev is not None and (d.timestamp - prev.timestamp).total_seconds() > 3 * period:
            raw_x.append(prev.timestamp + (d.timestamp - prev.timestamp) / 2)
            raw_y.append(float("nan"))
        raw_x.append(d.timestamp)
        raw_y.append(d.value)

    fig, axes = plt.subplots(
        2, 1, sharex=True, sharey=True, figsize=(plots.DOUBLE_IN, 3.0),
        layout="constrained",
    )
    any_hb = False
    any_gap = False
    for i, (ax, m) in enumerate(zip(axes, ("alia", "sod_commissioned"))):
        color, _mk, _ls, _lw = plots.METHOD_STYLE[m]
        ax.plot(raw_x, raw_y, color=plots.RAW_GREY, linewidth=1.6,
                label="raw samples", zorder=2, solid_capstyle="round")
        xs, ys = _zoh(tx[m], t0, t1)
        ax.step(xs, ys, where="post", color=color, linewidth=1.0,
                label="reconstruction (zero-order hold)", zorder=3)
        # Packets as a rug along the bottom: shows *when* the radio fired
        # without burying the trace under markers.
        win = [d for d in tx[m] if t0 <= d.timestamp <= t1]
        trans = ax.get_xaxis_transform()
        ev = [d.timestamp for d in win if d.reason != "heartbeat"]
        hb = [d.timestamp for d in win if d.reason == "heartbeat"]
        ax.plot(ev, [0.04] * len(ev), marker="|", linestyle="none", color=color,
                markersize=6, markeredgewidth=0.7, transform=trans, clip_on=False,
                label="packet sent")
        any_hb = any_hb or bool(hb)
        if hb:
            ax.plot(hb, [0.04] * len(hb), marker="|", linestyle="none",
                    color=plots.INK, markersize=9, markeredgewidth=1.1,
                    transform=trans, clip_on=False, label="heartbeat packet")
        # Shade every stretch the receiver heard nothing for longer than
        # SILENCE_SHADE_HR, and label the longest: that is the cost the
        # heartbeat removes, and it is invisible in the trace itself.
        sent = sorted(d.timestamp for d in tx[m])
        edges = [t0] + [t for t in sent if t0 < t < t1] + [t1]
        prior = [t for t in sent if t <= t0]
        if prior:
            edges[0] = prior[-1]
        gaps = [(a, b) for a, b in zip(edges, edges[1:])
                if (b - a).total_seconds() / 3600 > SILENCE_SHADE_HR]
        inset = dt.timedelta(hours=SILENCE_INSET_HR)
        for a, b in gaps:
            lo = t0 if a <= t0 else a + inset
            hi = t1 if b >= t1 else b - inset
            ax.axvspan(lo, hi, color=SILENCE_FILL, zorder=1, linewidth=0)
        any_gap = any_gap or bool(gaps)
        if gaps:
            a, b = max(gaps, key=lambda g: g[1] - g[0])
            hours = (b - a).total_seconds() / 3600
            ax.text(max(a, t0) + (b - max(a, t0)) / 2, 0.5,
                    f"{hours:.0f} h with\nno packet", transform=trans,
                    ha="center", va="center", fontsize=7, color=plots.INK)
        ax.set_title(
            f"({'ab'[i]}) {plots.METHOD_LABEL[m]}: {len(win)} packets in this window "
            f"({len(stream.data) / len(tx[m]):.0f}× compression over the full record)",
            loc="left",
        )
        ax.set_ylabel(plots.channel_label(channel))
    # Neutral-coloured handles: the line colour already says which method.
    fig.legend(
        handles=[
            Line2D([], [], color=plots.RAW_GREY, linewidth=1.6, label="Raw samples"),
            Line2D([], [], color=plots.INK, linewidth=1.0, drawstyle="steps-post",
                   label="Reconstruction (zero-order hold)"),
            Line2D([], [], color=plots.INK, marker="|", linestyle="none",
                   markersize=6, markeredgewidth=0.7, label="Packet sent"),
        ] + ([Line2D([], [], color=plots.INK, marker="|", linestyle="none",
                     markersize=9, markeredgewidth=1.1, label="Heartbeat packet")]
             if any_hb else [])
        + ([Patch(color=SILENCE_FILL, label=f"No packet for > {SILENCE_SHADE_HR} h")]
           if any_gap else []),
        loc="outside upper center", ncol=5, handlelength=2.0,
    )
    import matplotlib.dates as mdates

    if days[1] - days[0] > 3:
        # Slow signals (soil) need a week-scale window: label days.
        step = max(1, round((days[1] - days[0]) / 8))
        axes[-1].xaxis.set_major_locator(mdates.DayLocator(interval=step))
        axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        axes[-1].xaxis.set_minor_locator(mdates.DayLocator())
    else:
        axes[-1].xaxis.set_major_locator(mdates.HourLocator(byhour=[0, 12]))
        axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%b %d %H:%M"))
        axes[-1].xaxis.set_minor_locator(mdates.HourLocator(byhour=[6, 18]))
    axes[-1].set_xlim(t0, t1)
    ymin = min(d.value for d in raw)
    ymax = max(d.value for d in raw)
    # Room below the trace for the packet rug.
    axes[-1].set_ylim(ymin - 0.15 * (ymax - ymin), ymax + 0.08 * (ymax - ymin))
    plots.save_figure(fig, out_dir, "fig3_reconstruction")
    plt.close(fig)
    print("  wrote fig3_reconstruction")


# --- figs 6-7: ALIA alone, for the Results section ---------------------------


def _alia_run(data_dir, stream_name, target):
    """(stream, channel, alpha, transmitted) for ALIA at a matched budget."""
    sensors_by_logger, _ = _ingest([], data_dir, "auto")
    stream = next(
        (s for sensors in sensors_by_logger.values()
         for s in pipeline.to_channel_streams(sensors) if s.name == stream_name),
        None,
    )
    if stream is None:
        return None
    channel = next(d.name for d in stream.data if d.name)
    budget = max(2, int(len(stream.data) / target))
    alpha, _ = baselines.solve_knob("alia", stream, budget)
    tx = baselines.run("alia", [stream], alpha)[0].data
    return stream, channel, alpha, tx


def _raw_trace(stream, t0, t1):
    """Raw samples in [t0, t1], NaN-broken across outages (> 3 sample periods)."""
    raw = [d for d in stream.data if t0 <= d.timestamp <= t1 and d.is_present()]
    period = filter_algo._estimate_period_sec(stream.data)
    xs, ys = [], []
    for prev, d in zip([None] + raw[:-1], raw):
        if prev is not None and (d.timestamp - prev.timestamp).total_seconds() > 3 * period:
            xs.append(prev.timestamp + (d.timestamp - prev.timestamp) / 2)
            ys.append(float("nan"))
        xs.append(d.timestamp)
        ys.append(d.value)
    return raw, xs, ys


def _date_axis(ax, t0, t1, days):
    import matplotlib.dates as mdates

    if days[1] - days[0] > 3:
        step = max(1, round((days[1] - days[0]) / 8))
        ax.xaxis.set_major_locator(mdates.DayLocator(interval=step))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        ax.xaxis.set_minor_locator(mdates.DayLocator())
    else:
        ax.xaxis.set_major_locator(mdates.HourLocator(byhour=[0, 12]))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d %H:%M"))
        ax.xaxis.set_minor_locator(mdates.HourLocator(byhour=[6, 18]))
    ax.set_xlim(t0, t1)


def _packet_rug(ax, tx, t0, t1, color):
    """Event packets as thin ticks, heartbeats as heavy black ticks."""
    trans = ax.get_xaxis_transform()
    win = [d for d in tx if t0 <= d.timestamp <= t1]
    ev = [d.timestamp for d in win if d.reason != "heartbeat"]
    hb = [d.timestamp for d in win if d.reason == "heartbeat"]
    # Skip empty series: an empty unclipped marker line gets a bounding box at
    # the figure origin and collapses constrained layout.
    if ev:
        ax.plot(ev, [0.04] * len(ev), marker="|", linestyle="none", color=color,
                markersize=6, markeredgewidth=0.7, transform=trans, clip_on=False)
    if hb:
        ax.plot(hb, [0.04] * len(hb), marker="|", linestyle="none", color=plots.INK,
                markersize=9, markeredgewidth=1.1, transform=trans, clip_on=False)
    return win


ZOOM_EDGE = "#8a8984"


def _zoom_panel(ax, stream, tx, z0, z1, color, letter, title):
    """Raw samples (dots) vs. ALIA's held value over a short window."""
    raw, rx, ry = _raw_trace(stream, z0, z1)
    ax.plot(rx, ry, color=plots.RAW_GREY, linewidth=0.9, zorder=2)
    ax.plot([d.timestamp for d in raw], [d.value for d in raw], linestyle="none",
            marker="o", markersize=1.6, color=plots.RAW_GREY, zorder=2)
    xs, ys = _zoh(tx, z0, z1)
    ax.step(xs, ys, where="post", color=color, linewidth=1.1, zorder=3)
    win = _packet_rug(ax, tx, z0, z1, color)
    vals = [d.value for d in raw] + [y for y in ys if y == y]
    lo, hi = min(vals), max(vals)
    pad = max(hi - lo, 1e-9)
    ax.set_ylim(lo - 0.22 * pad, hi + 0.08 * pad)
    ax.set_xlim(z0, z1)
    err = [abs(d.value - _held(tx, d.timestamp)) for d in raw
           if _held(tx, d.timestamp) is not None]
    ax.set_title(f"({letter}) {title}\n{len(win)} of {len(raw)} samples sent, "
                 f"max |error| {max(err, default=0):.3f}", loc="left")
    import matplotlib.dates as mdates

    span_h = (z1 - z0).total_seconds() / 3600
    step = 6 if span_h <= 30 else 12 if span_h <= 60 else 24
    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, step)))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d\n%H:%M"))
    for sp in ax.spines.values():
        sp.set_edgecolor(ZOOM_EDGE)


def _held(tx, t):
    """Value the receiver holds at time t (last packet at or before t)."""
    v = None
    for d in tx:
        if d.timestamp <= t:
            v = d.value
        else:
            break
    return v


# Zoom windows for fig 6, as (start, end) day offsets from the stream start,
# with a short title each. Chosen to show the three regimes in the window.
OVERLAY_ZOOMS = [
    ((85.5, 87.5), "Dry spell: heartbeats"),
    ((96.55, 97.15), "Wetting front: dense events"),
    ((101.0, 103.0), "Drainage: sparse events"),
]


def fig_alia_overlay(run, out_dir, days, name="fig6_alia_overlay", zooms=None):
    """Raw vs ALIA's reconstruction over a month, plus zoomed panels in which
    the gap between the raw samples and the held value is actually visible."""
    import datetime as dt

    from matplotlib.patches import Rectangle

    zooms = OVERLAY_ZOOMS if zooms is None else zooms
    stream, channel, _alpha, tx = run
    tx = sorted(tx, key=lambda d: d.timestamp)
    start = stream.data[0].timestamp
    t0 = start + dt.timedelta(days=days[0])
    t1 = start + dt.timedelta(days=days[1])
    raw, raw_x, raw_y = _raw_trace(stream, t0, t1)
    color = plots.METHOD_STYLE["alia"][0]

    fig = plt.figure(figsize=(plots.DOUBLE_IN, 4.2), layout="constrained")
    gs = fig.add_gridspec(2, max(1, len(zooms)), height_ratios=[1.15, 1])
    ax = fig.add_subplot(gs[0, :])
    ax.plot(raw_x, raw_y, color=plots.RAW_GREY, linewidth=1.6, zorder=2,
            solid_capstyle="round")
    xs, ys = _zoh(tx, t0, t1)
    ax.step(xs, ys, where="post", color=color, linewidth=1.0, zorder=3)
    win = _packet_rug(ax, tx, t0, t1, color)
    n_raw = sum(1 for d in stream.data if t0 <= d.timestamp <= t1)
    ax.set_title(
        f"(a) ALIA: {len(win)} of {n_raw} samples sent in this window "
        f"({len(stream.data) / len(tx):.0f}× compression over the full record)",
        loc="left",
    )
    ax.set_ylabel(plots.channel_label(channel))
    _date_axis(ax, t0, t1, days)
    ymin = min(d.value for d in raw)
    ymax = max(d.value for d in raw)
    ax.set_ylim(ymin - 0.15 * (ymax - ymin), ymax + 0.08 * (ymax - ymin))

    import matplotlib.dates as mdates

    for k, ((a, b), title) in enumerate(zooms):
        letter = "bcdefg"[k]
        z0 = start + dt.timedelta(days=a)
        z1 = start + dt.timedelta(days=b)
        zr = [d.value for d in raw if z0 <= d.timestamp <= z1]
        if zr:
            lo, hi = min(zr), max(zr)
            pad = 0.03 * (ymax - ymin)
            x0 = mdates.date2num(z0)
            ax.add_patch(Rectangle((x0, lo - pad), mdates.date2num(z1) - x0,
                                   hi - lo + 2 * pad, fill=False,
                                   edgecolor=ZOOM_EDGE, linewidth=0.8, zorder=4))
            ax.text(mdates.date2num(z0) + (mdates.date2num(z1) - x0) / 2,
                    hi + pad, f"({letter})", ha="center", va="bottom",
                    fontsize=7, color=plots.INK)
        zax = fig.add_subplot(gs[1, k])
        _zoom_panel(zax, stream, tx, z0, z1, color, letter, title)
        if k == 0:
            zax.set_ylabel(plots.channel_label(channel))

    fig.legend(
        handles=[
            Line2D([], [], color=plots.RAW_GREY, linewidth=1.6, marker="o",
                   markersize=2.5, label="Raw samples"),
            Line2D([], [], color=color, linewidth=1.0, drawstyle="steps-post",
                   label="ALIA reconstruction (zero-order hold)"),
            Line2D([], [], color=color, marker="|", linestyle="none",
                   markersize=6, markeredgewidth=0.7, label="Event packet"),
            Line2D([], [], color=plots.INK, marker="|", linestyle="none",
                   markersize=9, markeredgewidth=1.1, label="Heartbeat packet"),
        ],
        loc="outside upper center", ncol=4, handlelength=2.0,
    )
    plots.save_figure(fig, out_dir, name)
    plt.close(fig)
    print(f"  wrote {name}")


BAND_FILL = "#d6e4f7"


def fig_threshold(run, out_dir, days, name="fig7_threshold", zoom_hours=6.0):
    """How ALIA's event threshold adapts.

    (a) delta(t) over the whole window, with every event packet as a tick.
    (b), (c) two short windows -- where delta is narrowest and widest -- drawn
        in the signal's own units: the raw samples, the value the receiver
        holds, and the +/- delta band around it. An event fires exactly where
        the raw signal leaves the band, and the held value (and band) jump.
    """
    import datetime as dt

    import matplotlib.dates as mdates
    from matplotlib.patches import Patch as _Patch

    stream, channel, alpha, tx = run
    start = stream.data[0].timestamp
    t0 = start + dt.timedelta(days=days[0])
    t1 = start + dt.timedelta(days=days[1])
    resolution = config.resolution_for(channel)

    # Replay the same rolling-sigma update the filter uses, from the start of
    # the record so the window is already warm when the plot begins.
    stats = filter_algo.RollingStats(W=filter_algo._window_size(stream.data))
    sent = sorted(tx, key=lambda d: d.timestamp)
    k = 0
    last = None
    th_x, th_y = [], []
    # Per sample in window: (t, value, held value *before* this sample, delta)
    band = []
    for d in stream.data:
        delta = filter_algo.update_delta(d, stats, resolution, alpha)
        if t0 <= d.timestamp <= t1:
            th_x.append(d.timestamp)
            th_y.append(delta)
            if d.is_present() and last is not None and last.is_present():
                band.append((d.timestamp, d.value, last.value, delta))
        while k < len(sent) and sent[k].timestamp <= d.timestamp:
            last = sent[k]
            k += 1
        if d.timestamp > t1:
            break

    color = plots.METHOD_STYLE["alia"][0]
    unit = plots.CHANNEL_LABEL.get(channel, ("", ""))[1]

    # Zoom windows centred on the narrowest and widest threshold, kept inside
    # the plotted window.
    half = dt.timedelta(hours=zoom_hours / 2)

    def _centre(t):
        return min(max(t, t0 + half), t1 - half)

    i_min = min(range(len(th_y)), key=th_y.__getitem__)
    i_max = max(range(len(th_y)), key=th_y.__getitem__)
    zooms = [
        (_centre(th_x[i_min]), "Narrowest band (calm night, then warm-up)"),
        (_centre(th_x[i_max]), "Widest band (after a noisy afternoon)"),
    ]
    zooms.sort(key=lambda z: z[0])

    fig = plt.figure(figsize=(plots.DOUBLE_IN, 3.9), layout="constrained")
    gs = fig.add_gridspec(2, 2, height_ratios=[0.9, 1.2])
    ax = fig.add_subplot(gs[0, :])
    ax.plot(th_x, th_y, color=color, linewidth=1.4, zorder=3)
    floor_binds = min(th_y) <= resolution * 1.01
    if floor_binds:
        ax.axhline(resolution, color=plots.INK_MUTED, linestyle=":",
                   linewidth=0.8, zorder=1)
    ev = [d.timestamp for d in sent if t0 <= d.timestamp <= t1 and d.reason == "event"]
    trans = ax.get_xaxis_transform()
    ax.plot(ev, [0.05] * len(ev), marker="|", linestyle="none", color=color,
            markersize=6, markeredgewidth=0.6, transform=trans, clip_on=False)
    ax.set_ylabel(rf"$\delta(t)$ ({unit})" if unit else r"$\delta(t)$")
    ax.set_ylim(0, max(th_y) * 1.12)
    ax.set_title(
        rf"(a) Event threshold $\delta(t) = \max(\alpha\sigma_t,\ r)$, "
        rf"$\alpha = {alpha:.2g}$, $\sigma$ over a trailing "
        rf"{config.stdDevWindowHours} h window ({len(ev)} event packets)",
        loc="left",
    )
    _date_axis(ax, t0, t1, days)

    for j, (centre, title) in enumerate(zooms):
        z0, z1 = centre - half, centre + half
        letter = "bc"[j]
        ax.axvspan(z0, z1, color=BAND_FILL, alpha=0.7, zorder=0, linewidth=0)
        ax.text(z0 + half, 0.97, f"({letter})", transform=trans, ha="center",
                va="top", fontsize=7, color=plots.INK)

        zax = fig.add_subplot(gs[1, j])
        pts = [b for b in band if z0 <= b[0] <= z1]
        bx = [b[0] for b in pts]
        held = [b[2] for b in pts]
        zax.fill_between(bx, [h - b[3] for h, b in zip(held, pts)],
                         [h + b[3] for h, b in zip(held, pts)],
                         step="post", color=BAND_FILL, linewidth=0, zorder=1)
        zax.step(bx, held, where="post", color=color, linewidth=1.1, zorder=3)
        _r, rx, ry = _raw_trace(stream, z0, z1)
        zax.plot(rx, ry, color=plots.INK_MUTED, linewidth=0.8, zorder=2)
        zax.plot([b[0] for b in pts], [b[1] for b in pts], linestyle="none",
                 marker="o", markersize=1.8, color=plots.INK_MUTED, zorder=2)
        zev = [d for d in sent if z0 <= d.timestamp <= z1 and d.reason == "event"]
        zax.plot([d.timestamp for d in zev], [d.value for d in zev],
                 linestyle="none", marker="o", markersize=3.2,
                 markerfacecolor="white", markeredgecolor=color,
                 markeredgewidth=0.9, zorder=4)
        lo = min(min(ry), min(h - b[3] for h, b in zip(held, pts)))
        hi = max(max(ry), max(h + b[3] for h, b in zip(held, pts)))
        pad = 0.06 * (hi - lo)
        zax.set_ylim(lo - pad, hi + pad)
        zax.set_xlim(z0, z1)
        mean_d = sum(b[3] for b in pts) / max(1, len(pts))
        zax.set_title(f"({letter}) {title}\nmean $\\delta$ = {mean_d:.2f} {unit}, "
                      f"{len(zev)} events in {zoom_hours:g} h", loc="left")
        zax.xaxis.set_major_locator(mdates.HourLocator(interval=2))
        zax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d %H:%M"))
        if j == 0:
            zax.set_ylabel(plots.channel_label(channel))

    fig.legend(
        handles=[
            Line2D([], [], color=plots.INK_MUTED, linewidth=0.8, marker="o",
                   markersize=2.5, label="Raw samples"),
            Line2D([], [], color=color, linewidth=1.1, drawstyle="steps-post",
                   label="Held value (last packet)"),
            _Patch(color=BAND_FILL, label=r"Held value $\pm\,\delta(t)$"),
            Line2D([], [], color=color, marker="o", linestyle="none",
                   markerfacecolor="white", markeredgewidth=0.9, markersize=3.5,
                   label="Event packet"),
        ] + ([Line2D([], [], color=plots.INK_MUTED, linestyle=":", linewidth=0.8,
                      label=f"Resolution $r$ = {resolution:g} {unit}".strip())]
             if floor_binds else []),
        loc="outside upper center", ncol=5, handlelength=2.0,
    )
    plots.save_figure(fig, out_dir, name)
    plt.close(fig)
    print(f"  wrote {name}")


# --- fig 4: ablation ---------------------------------------------------------


def _bars(ax, channels, by, methods, valfn, width, labels=True, fmt="{:.2f}"):
    n_m = len(methods)
    for j, m in enumerate(methods):
        color, *_ = plots.METHOD_STYLE[m]
        for i, ch in enumerate(channels):
            v = valfn(ch, m)
            if v is None:
                continue
            x = i + (j - (n_m - 1) / 2) * width
            ax.bar(x, v, width=width, color=color, edgecolor="white",
                   linewidth=1.0, zorder=3)
            if labels:
                r = by.get((ch, m), {})
                mark = "‡" if (_float(r.get("hit")) or 1) > HIT_CEIL else ""
                ax.annotate(fmt.format(v) + mark, (x, v), xytext=(0, 1.5),
                            textcoords="offset points", ha="center", va="bottom",
                            fontsize=6, color=plots.INK, clip_on=True,
                            annotation_clip=True)


def fig_ablation(bench_root, out_dir, sub="bench-fishnet", dataset="FishNet",
                 target="10"):
    """Ablation at one matched budget.

    (a) RMSE divided by uniform downsampling's RMSE at the same budget.
    (b) Worst-case silence in hours on a linear axis, broken so the one tall
        bar (no heartbeat on solar) does not flatten the rest.
    """
    rows = _matched(os.path.join(bench_root, sub))
    if not rows:
        return
    rows = [r for r in rows if r["target"] == target]
    channels = sorted({r["channel"] for r in rows})
    by = {(r["channel"], r["method"]): r for r in rows}
    n_m = len(ABLATION_METHODS)
    width = 0.8 / n_m

    def ratio(ch, m):
        v = _float(by.get((ch, m), {}).get("rmse"))
        b = _float(by.get((ch, BASELINE), {}).get("rmse"))
        return v / b if v is not None and b else None

    def silence(ch, m):
        return _float(by.get((ch, m), {}).get("max_latency_hr"))

    fig = plt.figure(figsize=(plots.DOUBLE_IN, 2.5), layout="constrained")
    outer = fig.add_gridspec(1, 2)
    ax_a = fig.add_subplot(outer[0])
    right = outer[1].subgridspec(2, 1, height_ratios=[1, 3], hspace=0.06)
    ax_top = fig.add_subplot(right[0])
    ax_bot = fig.add_subplot(right[1], sharex=ax_top)

    # (a) RMSE / uniform
    _bars(ax_a, channels, by, ABLATION_METHODS, ratio, width)
    ax_a.axhline(1.0, color=plots.INK_MUTED, linewidth=0.8, linestyle="--", zorder=4)
    ax_a.text(-0.5, 1.0, " uniform = 1", ha="left", va="bottom",
              fontsize=6.5, color=plots.INK_MUTED)
    ax_a.set_ylabel("RMSE relative to uniform (×)")
    ax_a.set_ylim(0, max(1.15, max((ratio(c, m) or 0) for c in channels
                                    for m in ABLATION_METHODS) * 1.15))
    ax_a.set_title(f"(a) Reconstruction error, {dataset}, {target}× budget", loc="left")

    # (b) worst-case silence, linear with a break
    vals = sorted(v for c in channels for m in ABLATION_METHODS + [BASELINE]
                  if (v := silence(c, m)) is not None)
    hi = vals[-1]
    rest = [v for v in vals if v < 0.6 * hi]
    lo_top = (max(rest) if rest else hi) * 1.18
    for ax in (ax_top, ax_bot):
        _bars(ax, channels, by, ABLATION_METHODS, silence, width, fmt="{:.1f}")
        # Uniform's silence per channel as a dashed reference tick.
        for i, ch in enumerate(channels):
            u = silence(ch, BASELINE)
            if u is not None:
                ax.hlines(u, i - 0.45, i + 0.45, color=plots.INK_MUTED,
                          linestyle="--", linewidth=0.8, zorder=4)
        ax.grid(False, axis="x")
    broken = hi > 1.5 * lo_top
    if broken:
        ax_bot.set_ylim(0, lo_top)
        ax_top.set_ylim(hi * 0.9, hi * 1.08)
        ax_top.spines["bottom"].set_visible(False)
        ax_top.tick_params(axis="x", which="both", bottom=False, labelbottom=False)
        # Diagonal break marks on the left spine.
        kw = dict(marker=[(-1, -0.5), (1, 0.5)], markersize=6, linestyle="none",
                  color=plots.INK_MUTED, mec=plots.INK_MUTED, mew=0.8, clip_on=False)
        ax_top.plot([0], [0], transform=ax_top.transAxes, **kw)
        ax_bot.plot([0], [1], transform=ax_bot.transAxes, **kw)
        from matplotlib.ticker import MaxNLocator
        ax_top.yaxis.set_major_locator(MaxNLocator(2))
    else:
        ax_top.set_visible(False)
        ax_bot.set_ylim(0, hi * 1.15)
    ax_bot.set_ylabel("Worst-case silence (h)")
    ax_bot.yaxis.set_label_coords(-0.08, 0.66)
    (ax_top if broken else ax_bot).set_title("(b) Longest gap between packets", loc="left")

    for ax in (ax_a, ax_bot):
        ax.set_xticks(range(len(channels)))
        ax.set_xticklabels(
            [plots.channel_label(c, False).replace(" ", "\n", 1) for c in channels]
        )
        ax.grid(False, axis="x")

    fig.legend(
        handles=[
            Patch(color=plots.METHOD_STYLE[m][0], label=plots.METHOD_LABEL[m])
            for m in ABLATION_METHODS
        ] + [Line2D([], [], color=plots.INK_MUTED, linestyle="--", linewidth=0.8,
                    label="Uniform (baseline)")],
        loc="outside upper center", ncol=n_m + 1,
    )
    plots.save_figure(fig, out_dir, "fig4_ablation")
    plt.close(fig)
    print("  wrote fig4_ablation")


# --- fig 5: sensitivity -----------------------------------------------------


def _baseline_value(param):
    if param == "eventDeltaThreshold":
        return config.eventDeltaThreshold
    return getattr(filter_algo, param)


def fig_sensitivity(sens_csv, out_dir, name, title):
    rows = _read_csv(sens_csv)
    if not rows:
        return
    params = [p for p in SENS_LABELS if any(r["param"] == p for r in rows)]
    fig, axes = plt.subplots(
        1, len(params), figsize=(plots.DOUBLE_IN, 2.0), sharey=True,
        layout="constrained",
    )
    for ax, p in zip(axes, params):
        pts = sorted(
            (r for r in rows if r["param"] == p), key=lambda r: _float(r["knob_val"])
        )
        base_v = _baseline_value(p)
        base = next(
            (r for r in pts if math.isclose(_float(r["knob_val"]), base_v)), None
        )
        if base is None:
            ax.text(0.5, 0.5, "baseline not\nin sweep", transform=ax.transAxes,
                    ha="center", va="center")
            continue
        xs = [_float(r["knob_val"]) for r in pts]
        for key, (lab, color, marker, ls) in SENS_SERIES.items():
            b = _float(base[key])
            if not b:
                continue
            ys = [100 * (_float(r[key]) / b - 1) for r in pts]
            ax.plot(xs, ys, color=color, marker=marker, linestyle=ls,
                    linewidth=1.2, markeredgecolor="white", markeredgewidth=0.5,
                    markersize=3.6, label=lab)
        ax.axhline(0, color=plots.INK_MUTED, linewidth=0.6, zorder=1)
        ax.axvline(base_v, color=plots.INK_MUTED, linewidth=0.6, linestyle=":",
                   zorder=1)
        ax.set_xscale("log")
        ax.set_xticks(xs)
        ax.set_xticklabels([f"{x:g}" for x in xs], rotation=0)
        ax.minorticks_off()
        ax.set_xlabel(SENS_LABELS[p])
    axes[0].set_ylabel("Change vs. default (%)")
    handles = [
        Line2D([], [], color=c, marker=mk, linestyle=ls, linewidth=1.2,
               markeredgecolor="white", label=lab)
        for lab, c, mk, ls in SENS_SERIES.values()
    ] + [Line2D([], [], color=plots.INK_MUTED, linestyle=":", linewidth=0.6,
                label="Default setting")]
    defaults = (
        rf"default: $\alpha$ = {_baseline_value('eventDeltaThreshold'):g}, "
        f"base {_baseline_value('baseHeartbeatHours'):g} h, "
        f"doubling {_baseline_value('backoffDoublingHours'):g} h, "
        f"max {_baseline_value('maxHeartbeatHours'):g} h, "
        f"σ window {config.stdDevWindowHours} h"
    )
    fig.legend(handles=handles, loc="outside upper center", ncol=len(handles),
               title=f"{title} ({defaults})", title_fontsize=8)
    plots.save_figure(fig, out_dir, name)
    plt.close(fig)
    print(f"  wrote {name}")


# --- table -------------------------------------------------------------------


def _tex(text: str) -> str:
    """Escape a plot label for LaTeX: `%` would comment out the rest of the
    row, and the Unicode unit glyphs break a default pdflatex build."""
    for a, b in (("%", r"\%"), ("°", r"$^\circ$"), ("²", r"$^2$"),
                 ("³", r"$^3$"), ("µ", r"$\mu$")):
        text = text.replace(a, b)
    return text


def write_table(bench_root, out_dir, targets=("10", "30")):
    """Matched-budget table. Best value per column is bolded among methods that
    met their budget; a method that did not is daggered and never bolded."""
    cols = [
        ("rmse", "RMSE", 3),
        ("max_err", "max err", 2),
        ("mean_latency_hr", "mean lat. (h)", 2),
        ("max_latency_hr", "max silence (h)", 1),
    ]
    flat = []
    tex = [
        r"\begin{tabular}{ll" + "r" * len(cols) + "}",
        r"\toprule",
        "Channel & Method & " + " & ".join(c[1] for c in cols) + r" \\",
    ]
    for sub, dataset in TABLE_DATASETS:
        rows = _matched(os.path.join(bench_root, sub))
        if not rows:
            continue
        for target in targets:
            sel = [r for r in rows if r["target"] == target]
            channels = sorted({r["channel"] for r in sel})
            tex.append(r"\midrule")
            tex.append(
                rf"\multicolumn{{{2 + len(cols)}}}{{l}}{{\emph{{{dataset}, "
                rf"{target}$\times$ packet budget}}}} \\"
            )
            for ch in channels:
                group = [r for r in sel if r["channel"] == ch]
                def _met(r):
                    return HIT_FLOOR <= (_float(r["hit"]) or 0) <= HIT_CEIL

                ok = [r for r in group if _met(r)]
                best = {
                    k: min((_float(r[k]) for r in ok if _float(r[k]) is not None),
                           default=None)
                    for k, _l, _p in cols
                }
                for i, r in enumerate(group):
                    met = _met(r)
                    hit = _float(r["hit"]) or 0
                    mark = "" if met else (r"$^\dagger$" if hit < HIT_FLOOR else r"$^\ddagger$")
                    cells = []
                    for k, _l, prec in cols:
                        v = _float(r[k])
                        s = "--" if v is None else f"{v:.{prec}f}"
                        if met and v is not None and best[k] is not None and \
                                math.isclose(v, best[k], rel_tol=1e-9):
                            s = rf"\textbf{{{s}}}"
                        cells.append(s)
                    mlabel = _tex(plots.METHOD_LABEL[r["method"]]) + mark
                    tex.append(
                        f"{_tex(plots.channel_label(ch)) if i == 0 else ''} & "
                        f"{mlabel} & " + " & ".join(cells) + r" \\"
                    )
                    flat.append(
                        [dataset, target, ch, r["method"], r["compression"],
                         r["hit"], "yes" if met else "NO"]
                        + [r.get(k, "") for k, _l, _p in cols]
                    )
    tex += [
        r"\bottomrule",
        r"\end{tabular}",
        "",
        r"% \textbf{bold}: best among methods that met the budget.",
        rf"% $^\dagger$: sent more packets than the budget (achieved/target < {HIT_FLOOR}).",
        rf"% $^\ddagger$: could not use the budget; sent far fewer packets "
        rf"(achieved/target > {HIT_CEIL}).",
        "% Neither is comparable at equal cost, so neither is bolded.",
    ]
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "table_matched_full.tex"), "w") as fp:
        fp.write("\n".join(tex) + "\n")
    with open(os.path.join(out_dir, "table_matched.csv"), "w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["dataset", "target", "channel", "method", "compression", "hit",
                    "met_budget"] + [k for k, _l, _p in cols])
        w.writerows(flat)
    print("  wrote table_matched_full.tex, table_matched.csv")


# Column heads for the summary table, where full labels would not fit.
SHORT_LABEL = {
    "alia": "ALIA",
    "sod_commissioned": "SoD",
    "uniform": "Unif.",
    "alia_events": "no HB",
    "alia_heartbeat": "no ev.",
}


def _met_budget(r):
    return HIT_FLOOR <= (_float((r or {}).get("hit")) or 0) <= HIT_CEIL


def _budget_mark(r):
    if _met_budget(r):
        return ""
    hit = _float((r or {}).get("hit")) or 0
    return r"$^\dagger$" if hit < HIT_FLOOR else r"$^\ddagger$"


# The reference every ratio is taken against: uniform downsampling at the same
# packet budget, i.e. what a node sending on a fixed clock would achieve.
BASELINE = "uniform"


def write_summary_table(bench_root, out_dir, target="10"):
    """Main-text table: one row per dataset x channel at one budget.

    RMSE is given relative to uniform downsampling at the same packet budget
    (uniform = 1.00, < 1 means the method beats a fixed-rate clock); worst-case
    silence in hours. The best value in each *column* is bolded, among rows
    where that method met the budget.
    """
    ratio_methods = [m for m in baselines.METHODS if m != BASELINE]
    sil_methods = list(baselines.METHODS)
    n_cols = len(ratio_methods) + len(sil_methods)

    # Gather every row first: column-best needs the whole column.
    body = []  # (dataset, channel, n_channels_in_dataset, {method: row})
    for sub, dataset in TABLE_DATASETS:
        rows = _matched(os.path.join(bench_root, sub))
        if not rows:
            continue
        sel = [r for r in rows if r["target"] == target]
        channels = sorted({r["channel"] for r in sel})
        for ch in channels:
            body.append((dataset, ch, len(channels),
                         {r["method"]: r for r in sel if r["channel"] == ch}))

    def ratio(by, m):
        v, b = _float(by.get(m, {}).get("rmse")), _float(by.get(BASELINE, {}).get("rmse"))
        return v / b if v is not None and b else None

    def silence(by, m):
        return _float(by.get(m, {}).get("max_latency_hr"))

    def col_best(fn, m):
        vals = [fn(by, m) for *_x, by in body if _met_budget(by.get(m))]
        vals = [v for v in vals if v is not None]
        return min(vals, default=None)

    best_r = {m: col_best(ratio, m) for m in ratio_methods}
    best_s = {m: col_best(silence, m) for m in sil_methods}

    def cell(v, best, r, prec):
        if v is None:
            return "--"
        txt = f"{v:.{prec}f}"
        if _met_budget(r) and best is not None and math.isclose(round(v, prec), round(best, prec)):
            txt = rf"\textbf{{{txt}}}"
        return txt + _budget_mark(r)

    tex = [
        r"\begin{tabular}{ll" + "r" * n_cols + "}",
        r"\toprule",
        rf" & & \multicolumn{{{len(ratio_methods)}}}{{c}}{{RMSE relative to uniform}} & "
        rf"\multicolumn{{{len(sil_methods)}}}{{c}}{{Worst-case silence (h)}} \\",
        rf"\cmidrule(lr){{3-{2 + len(ratio_methods)}}}"
        rf"\cmidrule(lr){{{3 + len(ratio_methods)}-{2 + n_cols}}}",
        "Dataset & Channel & "
        + " & ".join(SHORT_LABEL[m] for m in ratio_methods) + " & "
        + " & ".join(SHORT_LABEL[m] for m in sil_methods) + r" \\",
        r"\midrule",
    ]
    prev = None
    for dataset, ch, n_ch, by in body:
        if prev is not None and dataset != prev:
            tex.append(r"\midrule")
        name = rf"\multirow{{{n_ch}}}{{*}}{{{dataset}}}" if dataset != prev else ""
        prev = dataset
        cells = [cell(ratio(by, m), best_r[m], by.get(m), 2) for m in ratio_methods]
        cells += [cell(silence(by, m), best_s[m], by.get(m), 1) for m in sil_methods]
        tex.append(f"{name} & {_tex(plots.channel_label(ch, False))} & "
                   + " & ".join(cells) + r" \\")
    tex += [
        r"\bottomrule",
        r"\end{tabular}",
        "",
        rf"% {target}x packet budget: every method's knob is fitted per stream so it",
        "% sends at most 1 packet per {target} raw samples.".replace("{target}", target),
        "% RMSE is divided by uniform downsampling's RMSE at the same budget",
        "% (uniform = 1.00; < 1 beats a fixed-rate clock).",
        "% Bold: best in each column among rows where that method met the budget.",
        r"% $^\dagger$ sent more packets than the budget; $^\ddagger$ could not use it",
        "% (sent far fewer). SoD = send-on-delta, Unif. = uniform, no HB = ALIA with",
        "% no heartbeat, no ev. = ALIA with no event detector.",
        "% Needs \\usepackage{booktabs,multirow}.",
    ]
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "table_summary.tex"), "w") as fp:
        fp.write("\n".join(tex) + "\n")
    print("  wrote table_summary.tex")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", default="data/out/paper",
                    help="root holding bench-*/ and sens-*/ outputs")
    ap.add_argument("-o", "--output", default="paperFigures")
    ap.add_argument("--overlay-dir", default="data/UCSC_Haybarn_Field")
    ap.add_argument("--overlay-stream", default="b4c5-0_9-z6-29284_Port3_TEROS10")
    ap.add_argument("--overlay-days", default="78,108",
                    help="window as day offsets from the stream start")
    ap.add_argument("--overlay-target", default=30.0, type=float,
                    help="matched compression budget for the overlay")
    ap.add_argument("--threshold-dir", default="data/fishnet-meteo")
    ap.add_argument("--threshold-stream", default="fishnet-meteo-101-AmbientTemp_C")
    ap.add_argument("--threshold-days", default="6,10",
                    help="fig 7 window as day offsets from the stream start")
    ap.add_argument("--threshold-target", default=10.0, type=float)
    ap.add_argument("--skip-overlay", action="store_true")
    args = ap.parse_args()

    plots.apply_paper_style()
    out = args.output

    _frontier_figure(args.bench, out, "rmse", "RMSE", "fig1_error_frontier")
    _frontier_figure(args.bench, out, "max_latency_hr", "Worst-case silence (h)",
                     "fig2_silence_frontier")
    if not args.skip_overlay:
        d0, d1 = (float(x) for x in args.overlay_days.split(","))
        fig_reconstruction(args.overlay_dir, args.overlay_stream, out,
                           target=args.overlay_target, days=(d0, d1))
        run = _alia_run(args.overlay_dir, args.overlay_stream, args.overlay_target)
        if run is None:
            print(f"  stream {args.overlay_stream} not found — fig 6 skipped")
        else:
            fig_alia_overlay(run, out, (d0, d1))
        run = _alia_run(args.threshold_dir, args.threshold_stream,
                        args.threshold_target)
        if run is None:
            print(f"  stream {args.threshold_stream} not found — fig 7 skipped")
        else:
            d0, d1 = (float(x) for x in args.threshold_days.split(","))
            fig_threshold(run, out, (d0, d1))
    fig_ablation(args.bench, out)
    for sub, ch, label in [
        ("sens-fishnet", "AmbientTemp_C", "FishNet air temperature"),
        ("sens-haybarn", "VWC", "UCSC Haybarn soil VWC"),
    ]:
        fig_sensitivity(
            os.path.join(args.bench, sub, f"sweep_sensitivity_{ch}.csv"),
            out, f"fig5_sensitivity_{sub.split('-', 1)[1]}", label,
        )
    write_table(args.bench, out)
    write_summary_table(args.bench, out)


if __name__ == "__main__":
    main()
