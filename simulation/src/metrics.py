"""
@file       metrics.py

Computes per-sensor statistics and the diff between raw and filtered runs.
Used by compare.py to decide whether the filter algorithm is worth keeping.
"""

from collections import Counter
import math

import numpy as np

from simulation.src import filter_algo
from simulation.src.models import Sensor


def _series(sensor: Sensor, field: str) -> list[float]:
    vals = [getattr(d, field) for d in sensor.data]
    return [v for v in vals if v is not None and not (isinstance(v, float) and math.isnan(v))]


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
    """Variance of consecutive sample differences. A spike-removal filter
    should reduce this without flattening the underlying signal."""
    if len(values) < 2:
        return None
    diffs = np.diff(np.array(values, dtype=float))
    return float(diffs.var())


def compute(sensors: list[Sensor]) -> dict:
    """Per-stream descriptive stats plus first-diff variance.

    Each Sensor is single-channel after `pipeline.to_channel_streams` (its
    reading in `.value`, the data type named by the stream's suffix), so we
    report one `value` block per stream rather than separate VWC/SC/ST
    blocks."""
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
    """Zero-order-hold reconstruction error of the channel value, raw vs
    received signal.

    The receiver only ever sees the transmitted packets in `filtered`; every
    other sample is reconstructed by holding the most recently transmitted
    value (the held samples were, by construction, within `delta` of it). We
    align that step function onto the raw timestamps and compare against the
    true value.

    Returns `sse` (sum of squared errors) and `n` so callers can pool across
    sensors, plus the derived `rmse` and `mae` for a single sensor.
    """
    tx = sorted(
        (d for d in filtered.data if d.value is not None),
        key=lambda d: d.timestamp,
    )
    raw_data = sorted(raw.data, key=lambda d: d.timestamp)
    if not tx or not raw_data:
        return {"sse": 0.0, "abs_sum": 0.0, "n": 0, "rmse": None, "mae": None}

    sse = 0.0
    abs_sum = 0.0
    n = 0
    j = 0
    held = tx[0].value
    for d in raw_data:
        # Advance the held value to the latest packet transmitted at or before d.
        while j < len(tx) and tx[j].timestamp <= d.timestamp:
            held = tx[j].value
            j += 1
        if d.value is None or (isinstance(d.value, float) and math.isnan(d.value)):
            continue
        err = d.value - held
        sse += err * err
        abs_sum += abs(err)
        n += 1

    return {
        "sse": sse,
        "abs_sum": abs_sum,
        "n": n,
        "rmse": math.sqrt(sse / n) if n else None,
        "mae": abs_sum / n if n else None,
    }


def transmission_latency(raw: Sensor, filtered: Sensor) -> dict:
    """Mean delay (seconds) between a sample being taken and its information
    reaching the receiver.

    A transmitted sample has zero latency. A held (`in_run`) sample's
    information is conveyed by the next transmitted packet (which carries the
    run_length), so its latency is the gap to that packet. The trailing flush
    packet shares the last raw timestamp, so every raw sample has a covering
    transmission.

    Returns `sum_sec` and `n` for pooling, plus `mean_sec` and `max_sec`.
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
            # No transmission at or after this sample; its info never arrives.
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
    """Side-by-side comparison of raw and filtered runs.

    `in_run` samples are encoded into a downstream packet's run_length and so
    are reconstructable on receive — they are reported as `n_compressed`, not
    `n_dropped`. `n_dropped` covers only unrecoverable loss.
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
        # Transmitted packets plus samples the receiver can backfill from run_length.
        n_reconstructable = n_filt + sum(d.run_length for d in fs.data)
        per_sensor[fs.name] = {
            "n_raw": n_raw,
            "n_filtered": n_filt,
            "n_compressed": n_compressed,
            "n_dropped": n_dropped,
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
    total_compressed = sum(
        1 for d in filter_algo.last_drops if d.reason == "in_run"
    )
    total_dropped = sum(
        1 for d in filter_algo.last_drops if d.reason != "in_run"
    )
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
            "overall_compression_rate": (total_raw - total_filt) / total_raw if total_raw else 0.0,
            "overall_drop_rate": total_dropped / total_raw if total_raw else 0.0,
        },
        "drops_by_reason": dict(drops_by_reason),
        "drops_by_sensor": dict(drops_by_sensor),
        "compressed_by_sensor": dict(compressed_by_sensor),
        "per_sensor": per_sensor,
        "raw_stats": raw_stats,
        "filtered_stats": filt_stats,
    }
