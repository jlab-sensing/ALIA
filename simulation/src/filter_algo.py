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
from dataclasses import dataclass, field
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


@dataclass
class HeartbeatState:
    """Tracks the reporting tier for a single sensor.

    `quiet_run` is the number of consecutive non-event samples seen. It selects
    the heartbeat interval: an event resets it to 0 (burst mode, report
    aggressively to capture the transient); a stable run of at least
    `quietEntryRunLength` samples enters quiet mode; anything in between is
    normal. Initialized to 1 so a cold start begins in normal mode rather than
    burst.
    """

    quiet_run: int = 1


@dataclass
class RollingStats:
    W: int
    window: deque[float] = field(init=False)
    mean: float = 0.0
    variance: float = 0.0
    sdev: float = 0.0

    def __post_init__(self):
        self.window = deque(maxlen=self.W)

    @property
    def warmed_up(self) -> bool:
        return len(self.window) == self.W


# Populated by the most recent `filter` call. Read by metrics.py.
last_drops: list[Drop] = []


def _select_heartbeat_hours(state: HeartbeatState) -> float:
    """Choose the heartbeat interval (hours) for the current reporting tier."""
    if state.quiet_run >= quietEntryRunLength:
        return quietHeartbeatHours
    if state.quiet_run == 0:
        return burstHeartbeatHours
    return normalHeartbeatHours


def _should_log(
    datum: Datum, prev: Datum | None, delta: float, state: HeartbeatState
) -> tuple[bool, str]:
    """Return (keep, reason). `delta` is the current event threshold from
    update_delta. `prev` is the most recently kept Datum, or None on first
    sample. `state` is mutated to track the burst/normal/quiet reporting tier.
    """
    if prev is None:
        return True, "first"

    # An event always transmits and resets the run, dropping us into burst mode.
    if abs(datum.VWC - prev.VWC) >= delta:
        state.quiet_run = 0
        return True, "event"

    # Non-event: select the heartbeat for the current tier before advancing the
    # run, then force a transmission if we have been silent past that interval.
    heartbeatHours = _select_heartbeat_hours(state)
    state.quiet_run += 1
    hoursSinceLastTx = (datum.timestamp - prev.timestamp).total_seconds() / 3600
    if hoursSinceLastTx >= heartbeatHours:
        return True, "heartbeat"
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
        stats = RollingStats(W=W)
        heartbeat = HeartbeatState()
        for datum in sensor.data:
            delta = update_delta(datum, stats, sensorResolution)
            keep, reason = _should_log(datum, prev, delta, heartbeat)
            if keep:
                kept.append(datum)
                prev = datum
            else:
                last_drops.append(Drop(sensor.name, datum, reason))

        new_sensor = deepcopy(sensor)
        new_sensor.data = kept
        out.append(new_sensor)

    return out


def update_delta(
    datum: Datum,
    stats: RollingStats,
    sensor_resolution: float,
) -> float:
    """Maintain rolling mean/variance over `stats.window` (length W) and
    return the event threshold = max(eventDeltaThreshold * sigma, sensor_resolution).

    During warm-up (fewer than W samples seen) returns 0.0 so every sample
    passes _should_log. Once the window fills, seeds mean/variance directly
    from it; thereafter uses an O(1) incremental update.
    """
    x = datum.VWC

    if not stats.warmed_up:
        stats.window.append(x)
        if stats.warmed_up:
            stats.mean = sum(stats.window) / stats.W
            stats.variance = sum((v - stats.mean) ** 2 for v in stats.window) / (
                stats.W - 1
            )
            stats.sdev = sqrt(stats.variance)
        return 0.0

    goingaway = stats.window[0]
    oldmean = stats.mean
    stats.window.append(x)  # auto-evicts goingaway (maxlen=W)
    newmean = oldmean + (x - goingaway) / stats.W
    stats.variance += (
        (x - goingaway) * ((x - newmean) + (goingaway - oldmean)) / (stats.W - 1)
    )
    stats.mean = newmean
    stats.sdev = sqrt(max(0.0, stats.variance))

    return max(eventDeltaThreshold * stats.sdev, sensor_resolution)
