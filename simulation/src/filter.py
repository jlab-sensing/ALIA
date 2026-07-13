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
    quiet run is the length of a run of non events. seeing an event transmits the
    previous quiet run length and resets it to 0. a run of quietEntryRunLength samples enters
    quiet mode; initialized to 1 so a cold start begins in normal mode rather than burst
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


def filter(sensors: list[Sensor]) -> list[Sensor]:
    global last_drops
    last_drops = []

    out: list[Sensor] = []
    for sensor in sensors:
        last_value: float | None = None
        kept: list[Datum] = []
        prev: Datum | None = None
        stats = RollingStats(W=_window_size(sensor.data))
        heartbeat = HeartbeatState()
        runState = RunState()

        recent = deque(maxlen=numWarmupTx)
        baseline = deque(maxlen=numWarmupTx)

        for datum in sensor.data:
            if datum.is_present():
                if last_value is not None:
                    d = datum.value - last_value
                    if recent:
                        baseline.append(recent[-1])
                    recent.append(d)
                last_value = datum.value

            keep, reason = _should_log(
                recent, baseline, datum, prev, heartbeat, runState
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


def _should_log(
    recent: deque,
    baseline: deque,
    datum: Datum,
    prev: Datum | None,
    heartbeatState: HeartbeatState,
    runState: RunState,
) -> tuple[bool, str]:
    if prev is None:
        return True, "first"
    if len(baseline) < numWarmupTx:
        return True, "warmup"

    if _ttest_event(baseline, recent, datum.name):
        heartbeatState.quiet_run = 0
        if runState.run_count > 0:
            datum.run_length = runState.run_count
            runState.run_count = 0
        return True, "event"

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


def _select_heartbeat_hours(state: HeartbeatState) -> float:
    """Choose the heartbeat interval (hours) for the current reporting tier."""
    # been transmitting in a run for a while
    if state.quiet_run >= quietEntryRunLength:
        return quietHeartbeatHours
    # just transmitted event
    if state.quiet_run == 0:
        return burstHeartbeatHours
    # in a run recently started
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
    return abs(recent[-1] - baseline[-1]) >= config.resolution_for(channel)


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
