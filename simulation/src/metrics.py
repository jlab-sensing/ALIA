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
    """Reconstruction error of the channel value, raw vs received signal, under
    two reconstruction strategies.

    The receiver only ever sees the transmitted packets in `filtered`. We score
    the reconstruction two ways:

    - Zero-order hold (`sse`/`rmse`/`mae`): the live, causal reconstruction —
      hold the most recently transmitted value until the next packet arrives.
      The held samples were, by construction, within `delta` of it, so ZOH is
      the error the algorithm actually bounds.
    - Linear interpolation (`sse_interp`/`rmse_interp`/`mae_interp`): the offline
      reconstruction — a straight line between the bracketing transmitted
      packets. This is non-causal (it needs the *next* packet, so it's only
      available in post-hoc analysis of the archived series), but it is a
      strictly better reconstruction on the trends between packets. Samples
      before the first / after the last packet have no bracket on one side and
      fall back to the held endpoint value.

    Both are aligned onto the raw timestamps and compared against the true
    value. Returns `sse`/`abs_sum`/`n` (and the interp counterparts) so callers
    can pool across sensors, plus the derived per-sensor `rmse`/`mae`.
    """
    tx = sorted(
        (d for d in filtered.data if d.value is not None),
        key=lambda d: d.timestamp,
    )
    raw_data = sorted(raw.data, key=lambda d: d.timestamp)
    raw_vals = _series(raw, "value")
    sigma = float(np.std(raw_vals)) if raw_vals else 0.0
    empty = {
        "sigma": sigma,
        "sse": 0.0,
        "abs_sum": 0.0,
        "n": 0,
        "rmse": None,
        "nrmse": None,
        "mae": None,
        "sse_interp": 0.0,
        "abs_sum_interp": 0.0,
        "rmse_interp": None,
        "nrmse_interp": None,
        "mae_interp": None,
    }
    if not tx or not raw_data:
        return empty

    sse = 0.0
    abs_sum = 0.0
    sse_interp = 0.0
    abs_sum_interp = 0.0
    n = 0
    # `k` indexes the latest packet transmitted at or before the current sample,
    # so tx[k] is the held value and tx[k + 1] (if any) is the next packet used
    # for the interpolation bracket.
    k = -1
    for d in raw_data:
        while k + 1 < len(tx) and tx[k + 1].timestamp <= d.timestamp:
            k += 1
        if d.value is None or (isinstance(d.value, float) and math.isnan(d.value)):
            continue

        held = tx[k].value if k >= 0 else tx[0].value

        # Linear interpolation between the bracketing packets. Falls back to the
        # held value at the leading/trailing tails where only one side exists.
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

        err = d.value - held
        sse += err * err
        abs_sum += abs(err)

        err_i = d.value - interp
        sse_interp += err_i * err_i
        abs_sum_interp += abs(err_i)

        n += 1

    rmse = math.sqrt(sse / n) if n else None
    rmse_interp = math.sqrt(sse_interp / n) if n else None

    return {
        "sigma": sigma,
        "sse": sse,
        "abs_sum": abs_sum,
        "n": n,
        "rmse": rmse,
        "nrmse": (rmse / sigma) if (rmse is not None and sigma > 0) else None,
        "mae": abs_sum / n if n else None,
        "sse_interp": sse_interp,
        "abs_sum_interp": abs_sum_interp,
        "rmse_interp": rmse_interp,
        "nrmse_interp": (
            (rmse_interp / sigma) if (rmse_interp is not None and sigma > 0) else None
        ),
        "mae_interp": abs_sum_interp / n if n else None,
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
        err = reconstruction_error(rs, fs)
        lat = transmission_latency(rs, fs)
        per_sensor[fs.name] = {
            "n_raw": n_raw,
            "n_filtered": n_filt,
            "n_compressed": n_compressed,
            "n_dropped": n_dropped,
            "rmse": err["rmse"],
            "nrmse": err["nrmse"],
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
