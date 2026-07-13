"""
@file       filter_algo.py

Decides which sensor data points should be logged.

The public entry point is `filter`, a pure function that takes a list of
Sensors and returns a new list with the same shape but only the data points
that pass the algorithm. Each rejected datum is recorded in `last_drops` so
the comparison harness can attribute drops to specific reasons.

An "event" fires when EITHER of two independent tests trips:

  1. Level guard (bounds reconstruction error): |value - prev.value| exceeds an
     adaptive threshold max(alpha * sigma, resolution), sigma being the rolling
     stddev over a trailing window. This directly caps how far the held value
     can drift from the truth, so worst-case error stays bounded.

  2. Regime change (adds sensitivity): a two-sample Welch t-test on the stream's
     consecutive first differences (deltas) between two sliding windows — a
     `recent` window ending at the newest delta and a `baseline` window lagging
     it by one sample — rejects equal mean deltas at p < significance and by
     more than the sensor resolution. The windows overlap in all but their
     endpoints (baseline keeps the just-evicted oldest delta, recent gains the
     newest), so this tracks the signal's rate as each new sample shifts it.

The level guard is the safety net that keeps fidelity bounded; the t-test layers
on extra sensitivity to shifts in the signal's rate that the guard might ride
through. `prev` is the most recently kept datum, so the guard is measured
against exactly what the receiver would hold.
"""

from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from math import sqrt
from statistics import fmean

from simulation.src import config
from simulation.src.models import Datum, Sensor
from simulation.src.config import (
    burstHeartbeatHours,
    normalHeartbeatHours,
    quietEntryRunLength,
    quietHeartbeatHours,
    samplePeriodSec,
    stdDevWindowHours,
    numWarmupTx,
    significance,
)
from scipy.stats import ttest_ind


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
class RunState:

    run_count: int = 0


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


def _ttest_event(baseline: deque, recent: deque, channel: str) -> bool:
    """True when the recent delta window differs from the baseline delta window.

    Welch's t-test (equal_var=False) rejects equal means at p < significance,
    gated by a physical floor: the shift in mean delta must clear the sensor's
    resolution so a statistically-significant but trivially-small change does
    not fire. Constant (zero-variance) windows yield a nan p-value, which fails
    the `< significance` comparison and is correctly treated as a non-event.
    """
    _, p = ttest_ind(list(baseline), list(recent), equal_var=False)
    if not (p < significance):
        return False
    return abs(fmean(recent) - fmean(baseline)) >= config.resolution_for(channel)


def _should_log(
    recent: deque,
    baseline: deque,
    datum: Datum,
    prev: Datum | None,
    delta: float,
    heartbeatState: HeartbeatState,
    runState: RunState,
) -> tuple[bool, str]:
    """Return (keep, reason). `delta` is the current level-guard threshold from
    update_delta. `prev` is the most recently kept Datum, or None on the first
    sample. `baseline`/`recent` are two sliding delta windows lagged by one
    sample (see `filter`); both advance every present sample, so the t-test
    compares the signal's current rate of change against its recent past.
    `heartbeatState`
    tracks the burst/normal/quiet tier.
    """
    if prev is None:
        return True, "first"

    # Warm-up: force-transmit until BOTH delta windows are full, so the first
    # t-test compares two full windows. baseline lags recent by one sample and
    # so fills one sample later, so a full baseline implies a full recent.
    if len(baseline) < numWarmupTx:
        return True, "warmup"

    # Event = level guard OR regime change. The guard bounds reconstruction
    # error by capping how far the held value (prev) may drift from the truth;
    # it is cheap and short-circuits the costlier t-test when it trips. Only
    # meaningful when both endpoints are real readings.
    level_guard = (
        datum.is_present()
        and prev.is_present()
        and abs(datum.value - prev.value) >= delta
    )
    if level_guard or _ttest_event(baseline, recent, datum.name):
        heartbeatState.quiet_run = 0
        if runState.run_count > 0:
            datum.run_length = runState.run_count
            runState.run_count = 0
        return True, "event"

    # Non-event: select the heartbeat for the current tier before advancing the
    # run, then force a transmission if we have been silent past that interval.
    heartbeatHours = _select_heartbeat_hours(heartbeatState)
    heartbeatState.quiet_run += 1
    hoursSinceLastTx = (datum.timestamp - prev.timestamp).total_seconds() / 3600
    if hoursSinceLastTx >= heartbeatHours:
        if runState.run_count > 0:
            datum.run_length = runState.run_count
            runState.run_count = 0
        return True, "heartbeat"
    else:
        runState.run_count += 1
        return False, "in_run"


def _estimate_period_sec(data: list[Datum]) -> float:
    """Median interval (seconds) between consecutive present samples.

    Different deployments log at different cadences (UCSC Haybarn ZL6 at 600 s,
    the SensorScope meteo stations at 120 s), so the stddev window is sized from
    each stream's own timing rather than a global constant. Falls back to
    `config.samplePeriodSec` when a stream is too short to infer a cadence.
    """
    ts = sorted(d.timestamp for d in data if d.is_present())
    diffs = [
        (b - a).total_seconds()
        for a, b in zip(ts, ts[1:])
        if (b - a).total_seconds() > 0
    ]
    if not diffs:
        return samplePeriodSec
    diffs.sort()
    return diffs[len(diffs) // 2]


def _window_size(data: list[Datum]) -> int:
    """Number of samples spanning `stdDevWindowHours` at this stream's cadence."""
    period = _estimate_period_sec(data)
    return max(2, round(stdDevWindowHours * 3600 / period))


def filter(sensors: list[Sensor]) -> list[Sensor]:
    """Run the algorithm over every datum in every sensor.

    Pure: input is not mutated. The returned sensors are deep copies with
    rejected data removed. Drop reasons are recorded in `last_drops`.
    """
    global last_drops
    last_drops = []

    out: list[Sensor] = []
    for sensor in sensors:
        last_value: float | None = None
        kept: list[Datum] = []
        prev: Datum | None = None
        # Rolling stddev for the level guard, sized from this stream's cadence.
        stats = RollingStats(W=_window_size(sensor.data))
        heartbeat = HeartbeatState()
        runState = RunState()
        # Two sliding delta windows for the t-test, each numWarmupTx long:
        # `recent` is the newest numWarmupTx deltas, `baseline` the same window
        # lagged by one sample. They overlap in all but their endpoints (see the
        # eviction handoff below), so the test compares the signal's current rate
        # against its rate one sample ago. Both are full ~numWarmupTx+1 present
        # samples in (the first sample yields no delta).
        recent = deque(maxlen=numWarmupTx)
        baseline = deque(maxlen=numWarmupTx)
        for datum in sensor.data:
            if datum.is_present():
                if last_value is not None:
                    d = datum.value - last_value
                    if recent:
                        # baseline lags recent by exactly one sample: hand it
                        # recent's current newest delta before we push the new
                        # one on. The two windows then overlap in all but their
                        # endpoints — baseline still holds the oldest delta that
                        # recent is about to evict, recent gains the newest.
                        baseline.append(recent[-1])
                    recent.append(d)
                last_value = datum.value
            delta = update_delta(
                datum,
                stats,
                config.resolution_for(datum.name),
                config.event_multiplier_for(datum.name),
            )
            keep, reason = _should_log(
                recent, baseline, datum, prev, delta, heartbeat, runState
            )
            if keep:
                kept.append(datum)
                prev = datum
            else:
                last_drops.append(Drop(sensor.name, datum, reason))
        if sensor.data and runState.run_count > 0:
            flush = Datum(
                timestamp=datum.timestamp,
                value=prev.value,
                name=prev.name,
                run_length=runState.run_count - 1,
            )
            kept.append(flush)

        new_sensor = deepcopy(sensor)
        new_sensor.data = kept
        out.append(new_sensor)

    return out


def update_delta(
    datum: Datum,
    stats: RollingStats,
    sensor_resolution: float,
    alpha: float,
) -> float:
    """Maintain rolling mean/variance over `stats.window` (length W) and
    return the level-guard threshold = max(alpha * sigma, sensor_resolution),
    where `alpha` is the channel's sigma multiplier (config.event_multiplier_for).

    During warm-up (fewer than W samples seen) sigma is estimated over the
    partial window so the threshold adapts from the start, floored at
    sensor_resolution. Once the window fills, mean/variance are seeded from it;
    thereafter an O(1) incremental update takes over, consistent with the
    warm-up seed.
    """
    if not datum.is_present():
        # Absent sample: no real reading to fold in. Folding None/NaN would
        # poison the rolling mean/variance (and thus every later threshold), so
        # leave the stats untouched and return the current threshold.
        return max(alpha * stats.sdev, sensor_resolution)

    x = datum.value

    if not stats.warmed_up:
        stats.window.append(x)
        n = len(stats.window)
        stats.mean = sum(stats.window) / n
        stats.variance = (
            sum((v - stats.mean) ** 2 for v in stats.window) / (n - 1)
            if n >= 2
            else 0.0
        )
        stats.sdev = sqrt(max(0.0, stats.variance))
        return max(alpha * stats.sdev, sensor_resolution)

    goingaway = stats.window[0]
    oldmean = stats.mean
    stats.window.append(x)  # auto-evicts goingaway (maxlen=W)
    newmean = oldmean + (x - goingaway) / stats.W
    stats.variance += (
        (x - goingaway) * ((x - newmean) + (goingaway - oldmean)) / (stats.W - 1)
    )
    stats.mean = newmean
    stats.sdev = sqrt(max(0.0, stats.variance))

    return max(alpha * stats.sdev, sensor_resolution)
