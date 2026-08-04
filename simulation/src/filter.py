from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from math import sqrt
from statistics import fmean

from simulation.src import config
from simulation.src.models import Datum, Sensor
from simulation.src.config import (
    baseHeartbeatHours,
    backoffDoublingHours,
    maxHeartbeatHours,
    samplePeriodSec,
    stdDevWindowHours,
)


@dataclass
class Drop:
    sensor_name: str
    datum: Datum
    reason: str


@dataclass
class HeartbeatState:
    """Backoff state for one sensor. `last_event_ts` is when the stream last fired
    an event; calm time since then stretches the heartbeat interval. None until
    the first event (heartbeats run at the base cadence until then)."""

    last_event_ts: datetime | None = None


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
    m2: float = 0.0  # Welford accumulator, warm-up only

    def __post_init__(self):
        self.window = deque(maxlen=self.W)

    @property
    def warmed_up(self) -> bool:
        return len(self.window) == self.W


# Populated by the most recent `filter` call. Read by metrics.py.
last_drops: list[Drop] = []


def filter(
    sensors: list[Sensor],
    *,
    fixed_delta: float | None = None,
    alpha: float | None = None,
    use_events: bool = True,
    use_heartbeat: bool = True,
) -> list[Sensor]:
    """Run the ALIA decision loop over each stream.

    The keyword arguments exist so `baselines.py` can build ablations and
    fixed-threshold controls that share this exact run-length/heartbeat/flush
    machinery — a reimplementation could drift and make the comparison unfair.

    - `fixed_delta`: use this constant threshold instead of `max(alpha*sigma,
      resolution)`. This is the fixed-deadband (send-on-delta) control; sigma is
      not consulted at all.
    - `alpha`: override the per-channel sigma multiplier (ignored when
      `fixed_delta` is set).
    - `use_events` / `use_heartbeat`: disable either half for ablations. With
      `use_events=False` nothing ever resets the calm clock, so the heartbeat
      saturates at `maxHeartbeatHours` and the method degenerates to periodic
      transmission.
    """
    global last_drops
    last_drops = []

    out: list[Sensor] = []
    for sensor in sensors:
        kept: list[Datum] = []
        prev: Datum | None = None
        stats = RollingStats(W=_window_size(sensor.data))
        heartbeat = HeartbeatState()
        runState = RunState()

        prev_tier: str | None = None
        state_changes: list = []  # (timestamp, value, from_tier, to_tier)

        for datum in sensor.data:

            # delta threshold, updated every sample so rolling sigma stays
            # consistent through absent ones.
            if fixed_delta is not None:
                delta = fixed_delta
            else:
                delta = update_delta(
                    datum,
                    stats,
                    config.resolution_for(datum.name),
                    (
                        alpha
                        if alpha is not None
                        else config.event_multiplier_for(datum.name)
                    ),
                )
            keep, reason, state = _should_log(
                datum,
                prev,
                heartbeat,
                runState,
                delta,
                use_events=use_events,
                use_heartbeat=use_heartbeat,
            )
            if prev_tier is not None and state != prev_tier:
                state_changes.append((datum.timestamp, datum.value, prev_tier, state))
            prev_tier = state
            if keep:
                datum.reason = reason
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
                reason="flush",
            )
            kept.append(flush)
        new_sensor = deepcopy(sensor)
        new_sensor.data = kept
        new_sensor.state_changes = state_changes  # for overlay diagnostics
        out.append(new_sensor)
    return out


def _should_log(
    datum: Datum,
    prev: Datum | None,
    heartbeatState: HeartbeatState,
    runState: RunState,
    delta: float = 0.0,
    *,
    use_events: bool = True,
    use_heartbeat: bool = True,
) -> tuple[bool, str, str]:
    """Returns (keep, reason, state). `state` is the backoff bucket the sample was
    evaluated in (responsive/backing/max), or "warmup" before events may fire.

    `use_events` / `use_heartbeat` switch off either decision rule for the
    ablation studies in `baselines.py`; both default on, which is ALIA proper."""
    if prev is None:
        return True, "first", "warmup"

    # Start the calm clock at the end of warm-up so a stream with no events yet
    # still backs off (rather than sitting forever at the base cadence).
    if heartbeatState.last_event_ts is None:
        heartbeatState.last_event_ts = prev.timestamp
    interval, state = _backoff(heartbeatState, datum.timestamp)

    # delta level guard: fire when the value moved past the rolling threshold.
    fired = (
        use_events
        and datum.is_present()
        and prev.is_present()
        and abs(datum.value - prev.value) >= delta
    )
    if fired:
        heartbeatState.last_event_ts = datum.timestamp  # reset backoff
        if runState.run_count > 0:
            datum.run_length = runState.run_count
            runState.run_count = 0
        return True, "event", state

    hoursSinceLastTx = (datum.timestamp - prev.timestamp).total_seconds() / 3600
    if use_heartbeat and hoursSinceLastTx >= interval:
        if runState.run_count > 0:
            datum.run_length = runState.run_count
            runState.run_count = 0
        return True, "heartbeat", state
    else:
        runState.run_count += 1
        return False, "in_run", state


def _backoff(state: HeartbeatState, now: datetime) -> tuple[float, str]:
    """Current heartbeat interval (hours) and backoff bucket for reporting.

    Interval grows with calm time since the last event, capped at
    maxHeartbeatHours. Bucket: "responsive" (< one doubling of calm), "max"
    (interval at the ceiling), else "backing"."""
    if state.last_event_ts is None:
        calm_hours = 0.0
    else:
        calm_hours = (now - state.last_event_ts).total_seconds() / 3600
    interval = min(
        baseHeartbeatHours * 2 ** (calm_hours / backoffDoublingHours),
        maxHeartbeatHours,
    )
    if interval >= maxHeartbeatHours:
        bucket = "max"
    elif calm_hours < backoffDoublingHours:
        bucket = "responsive"
    else:
        bucket = "backing"
    return interval, bucket


def update_delta(
    datum: Datum,
    stats: RollingStats,
    sensor_resolution: float,
    alpha: float,
) -> float:
    """Rolling level-guard threshold = max(alpha*sigma, sensor_resolution).

    Rolling mean/variance over `stats.window` (length W): partial-window sigma
    during warm-up, then an O(1) incremental update. Absent samples are skipped
    so None/NaN can't poison the stats. This is the "delta" detector threshold.
    """
    if not datum.is_present():
        return max(alpha * stats.sdev, sensor_resolution)

    x = datum.value
    if not stats.warmed_up:
        # Welford, so warm-up is O(1) per sample rather than O(W). The old
        # recompute-the-whole-window form was O(W^2) over the warm-up phase,
        # which is invisible at W=12 but makes week-long windows (W in the
        # thousands) intractable to sweep.
        stats.window.append(x)
        n = len(stats.window)
        d = x - stats.mean
        stats.mean += d / n
        stats.m2 += d * (x - stats.mean)
        stats.variance = stats.m2 / (n - 1) if n >= 2 else 0.0
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


def _estimate_period_sec(data: list[Datum]) -> float:
    """Median interval (seconds) between consecutive present samples.

    Deployments log at different cadences (Haybarn ZL6 600 s, SensorScope meteo
    120 s), so windows are sized per stream. Falls back to config.samplePeriodSec
    when a stream is too short to infer a cadence.
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
