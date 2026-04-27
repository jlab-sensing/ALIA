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
    """Per-sensor descriptive stats for VWC, SC, ST plus first-diff variance."""
    out = {}
    for sensor in sensors:
        out[sensor.name] = {
            "VWC": _stats(_series(sensor, "VWC")),
            "SC": _stats(_series(sensor, "SC")),
            "ST": _stats(_series(sensor, "ST")),
            "VWC_first_diff_var": _first_diff_var(_series(sensor, "VWC")),
            "n_samples": len(sensor.data),
        }
    return out


def diff(raw: list[Sensor], filtered: list[Sensor]) -> dict:
    """Side-by-side comparison of raw and filtered runs.

    Includes drop counts (overall, per sensor, per reason) read from
    filter_algo.last_drops, plus distribution-shift deltas per sensor.
    """
    raw_stats = compute(raw)
    filt_stats = compute(filtered)

    drops_by_reason = Counter(d.reason for d in filter_algo.last_drops)
    drops_by_sensor = Counter(d.sensor_name for d in filter_algo.last_drops)

    per_sensor = {}
    raw_by_name = {s.name: s for s in raw}
    for fs in filtered:
        rs = raw_by_name.get(fs.name)
        if rs is None:
            continue
        n_raw = len(rs.data)
        n_filt = len(fs.data)
        per_sensor[fs.name] = {
            "n_raw": n_raw,
            "n_filtered": n_filt,
            "n_dropped": n_raw - n_filt,
            "drop_rate": (n_raw - n_filt) / n_raw if n_raw else 0.0,
            "vwc_mean_delta": (
                (filt_stats[fs.name]["VWC"]["mean"] or 0)
                - (raw_stats[fs.name]["VWC"]["mean"] or 0)
            ),
            "vwc_std_delta": (
                (filt_stats[fs.name]["VWC"]["std"] or 0)
                - (raw_stats[fs.name]["VWC"]["std"] or 0)
            ),
        }

    total_raw = sum(len(s.data) for s in raw)
    total_filt = sum(len(s.data) for s in filtered)

    return {
        "totals": {
            "n_raw": total_raw,
            "n_filtered": total_filt,
            "n_dropped": total_raw - total_filt,
            "overall_drop_rate": (total_raw - total_filt) / total_raw if total_raw else 0.0,
        },
        "drops_by_reason": dict(drops_by_reason),
        "drops_by_sensor": dict(drops_by_sensor),
        "per_sensor": per_sensor,
        "raw_stats": raw_stats,
        "filtered_stats": filt_stats,
    }
