"""
@file       filter_algo.py

Decides which sensor data points should be logged.

The public entry point is `filter`, a pure function that takes a list of
Sensors and returns a new list with the same shape but only the data points
that pass the algorithm. Each rejected datum is recorded in `last_drops` so
the comparison harness can attribute drops to specific reasons.
"""

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from math import sqrt

from simulation.src import config
from simulation.src.models import Datum, Sensor
from simulation.src.config import (
    burstHeartbeatHours,
    eventDeltaThreshold,
    normalHeartbeatHours,
    quietEntryRunLength,
    quietHeartbeatHours,
    samplePeriodSec,
    sensorResolution,
    stdDevWindowHours,
    numSamplesPerDay,
)


@dataclass
class Drop:
    sensor_name: str
    datum: Datum
    reason: str


# Populated by the most recent `filter` call. Read by metrics.py.
last_drops: list[Drop] = []


def _should_log(datum: Datum, prev: Datum | None, delta: float) -> tuple[bool, str]:
    """Return (keep, reason). `delta` is the current event threshold from
    UPDATE_DELTA. `prev` is the most recently kept Datum, or None on first
    sample.
    """
    if prev is None:
        return True, "first"
    if abs(datum.VWC - prev.VWC) >= delta:
        return True, "event"
    return False, "below_delta"


def filter(sensors: list[Sensor]) -> list[Sensor]:
    """Run the algorithm over every datum in every sensor.

    Pure: input is not mutated. The returned sensors are deep copies with
    rejected data removed. Drop reasons are recorded in `last_drops`.
    """
    global last_drops
    last_drops = []

    W = numSamplesPerDay

    out: list[Sensor] = []
    for sensor in sensors:
        kept: list[Datum] = []
        prev: Datum | None = None
        window: deque[float] = deque()
        sums = [0.0, 0.0]  # [sum, sum_sq]
        for datum in sensor.data:
            delta = UPDATE_DELTA(datum, window, sums, W, sensorResolution)
            keep, reason = _should_log(datum, prev, delta)
            if keep:
                kept.append(datum)
                prev = datum
            else:
                last_drops.append(Drop(sensor.name, datum, reason))

        new_sensor = deepcopy(sensor)
        new_sensor.data = kept
        out.append(new_sensor)

    return out


# just using sensorResolution to ensure that data that is not random is detected
def UPDATE_DELTA(
    datum: Datum,
    window: deque,
    sums: list[float],
    W: int,
    sensorResolution: float,
) -> float:
    """Add `datum` to the rolling VWC window (length W), maintaining running
    sum and sum-of-squares so stddev is O(1) per call. Returns the event
    threshold = max(eventDeltaThreshold * sigma, sensorResolution).

    Mutates `window` and `sums` in place.
    """
    v = datum.VWC
    window.append(v)
    sums[0] += v
    sums[1] += v * v
    if len(window) > W:
        old = window.popleft()
        sums[0] -= old
        sums[1] -= old * old

    n = len(window)
    if n < 2:
        return sensorResolution
    mean = sums[0] / n
    var = max(0.0, sums[1] / n - mean * mean)
    return max(2 * sqrt(var), sensorResolution)
