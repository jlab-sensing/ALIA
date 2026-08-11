"""Per-sensor statistics and the raw-vs-filtered diff used by compare.py."""

from collections import Counter
import math

import numpy as np

from simulation.src import config, filter as filter_algo
from simulation.src.models import Sensor


def _series(sensor: Sensor, field: str) -> list[float]:
    vals = [getattr(d, field) for d in sensor.data]
    return [
        v
        for v in vals
        if v is not None and not (isinstance(v, float) and math.isnan(v))
    ]


def _stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean": None, "std": None, "min": None, "max": None}
    arr = np.array(values, dtype=float)
    return {
        "n": int(arr.size),
        "mean": float(arr.mean()),
        "std": float(arr.std()),
        "min": float(arr.min()),
        "max": float(arr.max()),
    }


def _first_diff_var(values: list[float]) -> float | None:
    """Variance of consecutive-sample diffs; spike removal should lower it without flattening the signal."""
    if len(values) < 2:
        return None
    diffs = np.diff(np.array(values, dtype=float))
    return float(diffs.var())


def compute(sensors: list[Sensor]) -> dict:
    """Per-stream descriptive stats plus first-diff variance.

    Sensors are single-channel after `pipeline.to_channel_streams`, so one
    `value` block per stream rather than separate VWC/SC/ST blocks.
    """
    out = {}
    for sensor in sensors:
        values = _series(sensor, "value")
        out[sensor.name] = {
            "value": _stats(values),
            "value_first_diff_var": _first_diff_var(values),
            "n_samples": len(sensor.data),
        }
    return out


def reconstruction_error(raw: Sensor, filtered: Sensor) -> dict:
    """Raw-vs-received reconstruction error, in the channel's physical units.

    Normalized by sensor resolution (`config.resolution_for`), a fixed positive
    datasheet constant, not mean/std — so `rmse_over_res` never blows up on a
    zero-mean or dead-calm channel and reads against the sensor's measurement floor.

    Two reconstructions of the transmitted packets in `filtered`:
    - Zero-order hold (`rmse`/`mae`/`max_err`): causal, holds the last packet;
      held samples were within `delta`, so ZOH is the error the algorithm bounds.
    - Linear interpolation (`*_interp`): non-causal (needs the next packet), a
      better fit between packets; tails outside the first/last packet fall back to held.

    Returns `sse`/`abs_sum`/`n`/`max_abs` (+ interp) for cross-sensor pooling,
    plus derived `rmse`/`mae`/`max_err` and resolution-normalized ratios, and
    `n_over_thresh`/`frac_over_thresh` — samples whose held error exceeds
    `config.error_threshold_for(channel)`.
    """
    tx = sorted(
        (d for d in filtered.data if d.value is not None),
        key=lambda d: d.timestamp,
    )
    raw_data = sorted(raw.data, key=lambda d: d.timestamp)
    raw_vals = _series(raw, "value")
    sigma = float(np.std(raw_vals)) if raw_vals else 0.0
    mean = float(np.mean(raw_vals)) if raw_vals else 0.0
    channel = next((d.name for d in raw.data if d.name), "")
    resolution = config.resolution_for(channel)
    threshold = config.error_threshold_for(channel)
    empty = {
        "sigma": sigma,
        "mean": mean,
        "resolution": resolution,
        "sse": 0.0,
        "abs_sum": 0.0,
        "max_abs": 0.0,
        "n": 0,
        "rmse": None,
        "mae": None,
        "max_err": None,
        "rmse_over_res": None,
        "error_threshold": threshold,
        "n_over_thresh": None,
        "frac_over_thresh": None,
        "sse_interp": 0.0,
        "abs_sum_interp": 0.0,
        "max_abs_interp": 0.0,
        "rmse_interp": None,
        "mae_interp": None,
        "max_err_interp": None,
        "rmse_interp_over_res": None,
    }
    if not tx or not raw_data:
        return empty

    sse = 0.0
    abs_sum = 0.0
    max_abs = 0.0
    sse_interp = 0.0
    abs_sum_interp = 0.0
    max_abs_interp = 0.0
    n = 0
    n_over_thresh = 0  # samples whose held error exceeds config's threshold
    # k = latest packet at/before current sample: tx[k] held, tx[k+1] interp bracket.
    k = -1
    for d in raw_data:
        while k + 1 < len(tx) and tx[k + 1].timestamp <= d.timestamp:
            k += 1
        if d.value is None or (isinstance(d.value, float) and math.isnan(d.value)):
            continue

        held = tx[k].value if k >= 0 else tx[0].value

        # Interp between bracketing packets; held at the tails with only one side.
        if 0 <= k < len(tx) - 1:
            lo, hi = tx[k], tx[k + 1]
            span = (hi.timestamp - lo.timestamp).total_seconds()
            if span > 0:
                frac = (d.timestamp - lo.timestamp).total_seconds() / span
                interp = lo.value + frac * (hi.value - lo.value)
            else:
                interp = held
        else:
            interp = held

        err = abs(d.value - held)
        sse += err * err
        abs_sum += err
        max_abs = max(max_abs, err)
        if threshold is not None and err > threshold:
            n_over_thresh += 1

        err_i = abs(d.value - interp)
        sse_interp += err_i * err_i
        abs_sum_interp += err_i
        max_abs_interp = max(max_abs_interp, err_i)

        n += 1

    rmse = math.sqrt(sse / n) if n else None
    rmse_interp = math.sqrt(sse_interp / n) if n else None
    over_res = resolution if resolution > 0 else None

    return {
        "sigma": sigma,
        "mean": mean,
        "resolution": resolution,
        "sse": sse,
        "abs_sum": abs_sum,
        "max_abs": max_abs,
        "n": n,
        "rmse": rmse,
        "mae": abs_sum / n if n else None,
        "max_err": max_abs,
        "rmse_over_res": (rmse / over_res) if (rmse is not None and over_res) else None,
        # None rather than 0 when the channel has no configured threshold, so a
        # channel that is not scored can't be mistaken for one scoring perfectly.
        "error_threshold": threshold,
        "n_over_thresh": n_over_thresh if threshold is not None else None,
        "frac_over_thresh": (
            (n_over_thresh / n) if (threshold is not None and n) else None
        ),
        "sse_interp": sse_interp,
        "abs_sum_interp": abs_sum_interp,
        "max_abs_interp": max_abs_interp,
        "rmse_interp": rmse_interp,
        "mae_interp": abs_sum_interp / n if n else None,
        "max_err_interp": max_abs_interp,
        "rmse_interp_over_res": (
            (rmse_interp / over_res) if (rmse_interp is not None and over_res) else None
        ),
    }


def transmission_latency(raw: Sensor, filtered: Sensor) -> dict:
    """Mean delay (s) between a sample and its info reaching the receiver.

    Transmitted samples have zero latency; a held (`in_run`) sample's info
    arrives with the next packet (carrying run_length), so latency is the gap
    to it. The trailing flush packet shares the last raw timestamp, so every
    raw sample is covered. Returns `sum_sec`/`n` for pooling, plus `mean_sec`/`max_sec`.
    """
    tx_times = sorted(d.timestamp for d in filtered.data)
    raw_times = sorted(d.timestamp for d in raw.data)
    if not tx_times or not raw_times:
        return {"sum_sec": 0.0, "n": 0, "mean_sec": None, "max_sec": None}

    sum_sec = 0.0
    max_sec = 0.0
    n = 0
    j = 0
    for t in raw_times:
        while j < len(tx_times) and tx_times[j] < t:
            j += 1
        if j >= len(tx_times):
            # No transmission at/after this sample; its info never arrives.
            continue
        delay = (tx_times[j] - t).total_seconds()
        sum_sec += delay
        max_sec = max(max_sec, delay)
        n += 1

    return {
        "sum_sec": sum_sec,
        "n": n,
        "mean_sec": sum_sec / n if n else None,
        "max_sec": max_sec if n else None,
    }


def diff(raw: list[Sensor], filtered: list[Sensor]) -> dict:
    """Side-by-side raw-vs-filtered comparison.

    `in_run` samples are encoded in a downstream packet's run_length and thus
    reconstructable, so they count as `n_compressed`; `n_dropped` is only
    unrecoverable loss.
    """
    raw_stats = compute(raw)
    filt_stats = compute(filtered)

    drops_by_reason = Counter(d.reason for d in filter_algo.last_drops)
    drops_by_sensor = Counter(d.sensor_name for d in filter_algo.last_drops)

    compressed_by_sensor = Counter(
        d.sensor_name for d in filter_algo.last_drops if d.reason == "in_run"
    )
    dropped_by_sensor = Counter(
        d.sensor_name for d in filter_algo.last_drops if d.reason != "in_run"
    )

    per_sensor = {}
    raw_by_name = {s.name: s for s in raw}
    for fs in filtered:
        rs = raw_by_name.get(fs.name)
        if rs is None:
            continue
        n_raw = len(rs.data)
        n_filt = len(fs.data)
        n_compressed = compressed_by_sensor.get(fs.name, 0)
        n_dropped = dropped_by_sensor.get(fs.name, 0)
        # Transmitted packets plus samples backfillable from run_length.
        n_reconstructable = n_filt + sum(d.run_length for d in fs.data)
        err = reconstruction_error(rs, fs)
        lat = transmission_latency(rs, fs)
        rval = raw_stats.get(fs.name, {}).get("value", {})
        _vmin, _vmax = rval.get("min"), rval.get("max")
        per_sensor[fs.name] = {
            "n_raw": n_raw,
            "n_filtered": n_filt,
            "n_compressed": n_compressed,
            "n_dropped": n_dropped,
            # Raw-signal scale, so RMSE/max_err reads against the signal measured.
            "value_mean": rval.get("mean"),
            "value_range": (
                (_vmax - _vmin) if (_vmin is not None and _vmax is not None) else None
            ),
            "resolution": err["resolution"],
            "rmse": err["rmse"],
            "mae": err["mae"],
            "max_err": err["max_err"],
            "rmse_over_res": err["rmse_over_res"],
            "error_threshold": err["error_threshold"],
            "n_over_thresh": err["n_over_thresh"],
            "frac_over_thresh": err["frac_over_thresh"],
            "mean_latency_hr": (lat["mean_sec"] / 3600) if lat["mean_sec"] else None,
            "n_reconstructable": n_reconstructable,
            "compression_rate": (n_raw - n_filt) / n_raw if n_raw else 0.0,
            "drop_rate": n_dropped / n_raw if n_raw else 0.0,
            "value_mean_delta": (
                (filt_stats[fs.name]["value"]["mean"] or 0)
                - (raw_stats[fs.name]["value"]["mean"] or 0)
            ),
            "value_std_delta": (
                (filt_stats[fs.name]["value"]["std"] or 0)
                - (raw_stats[fs.name]["value"]["std"] or 0)
            ),
        }

    total_raw = sum(len(s.data) for s in raw)
    total_filt = sum(len(s.data) for s in filtered)
    total_compressed = sum(1 for d in filter_algo.last_drops if d.reason == "in_run")
    total_dropped = sum(1 for d in filter_algo.last_drops if d.reason != "in_run")
    total_reconstructable = total_filt + sum(
        d.run_length for s in filtered for d in s.data
    )

    return {
        "totals": {
            "n_raw": total_raw,
            "n_filtered": total_filt,
            "n_compressed": total_compressed,
            "n_dropped": total_dropped,
            "n_reconstructable": total_reconstructable,
            "overall_compression_rate": (
                (total_raw - total_filt) / total_raw if total_raw else 0.0
            ),
            "overall_drop_rate": total_dropped / total_raw if total_raw else 0.0,
        },
        "drops_by_reason": dict(drops_by_reason),
        "drops_by_sensor": dict(drops_by_sensor),
        "compressed_by_sensor": dict(compressed_by_sensor),
        "per_sensor": per_sensor,
        "raw_stats": raw_stats,
        "filtered_stats": filt_stats,
    }
