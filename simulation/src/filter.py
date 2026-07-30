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
    numWarmupTx,
    significance,
    klHistogramBins,
)
from scipy.stats import ttest_ind
from scipy.special import kl_div
import numpy as np
import pandas as pd


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

        # recent/baseline track increments; kept only to gate warm-up (and to
        # feed the dormant t-test stub if it is ever re-enabled).
        recent = deque(maxlen=numWarmupTx)
        baseline = deque(maxlen=numWarmupTx)

        prev_tier: str | None = None
        state_changes: list = []  # (timestamp, value, from_tier, to_tier)

        for datum in sensor.data:
            if datum.is_present():
                if last_value is not None:
                    if recent:
                        baseline.append(recent[-1])
                    recent.append(datum.value - last_value)
                last_value = datum.value

            # delta threshold, updated every sample so rolling sigma stays
            # consistent through absent ones.
            delta = update_delta(
                datum,
                stats,
                config.resolution_for(datum.name),
                config.event_multiplier_for(datum.name),
            )
            keep, reason, state = _should_log(
                baseline, datum, prev, heartbeat, runState, delta
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
    baseline: deque,
    datum: Datum,
    prev: Datum | None,
    heartbeatState: HeartbeatState,
    runState: RunState,
    delta: float = 0.0,
) -> tuple[bool, str, str]:
    """Returns (keep, reason, state). `state` is the backoff bucket the sample was
    evaluated in (responsive/backing/max), or "warmup" before events may fire."""
    if prev is None:
        return True, "first", "warmup"
    if len(baseline) < numWarmupTx:
        return True, "warmup", "warmup"

    # Start the calm clock at the end of warm-up so a stream with no events yet
    # still backs off (rather than sitting forever at the base cadence).
    if heartbeatState.last_event_ts is None:
        heartbeatState.last_event_ts = prev.timestamp
    interval, state = _backoff(heartbeatState, datum.timestamp)

    # delta level guard: fire when the value moved past the rolling threshold.
    fired = (
        datum.is_present()
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
    if hoursSinceLastTx >= interval:
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


# --- Dormant alternative detectors -----------------------------------------
# Kept for reference/experiments; NOT wired into the pipeline (delta is the sole
# active detector). To re-enable one, branch on it back in `_should_log`.


def _ttest_event(baseline: deque, recent: deque, channel: str) -> bool:
    """True when the recent delta window differs from the baseline window.

    Welch's t-test (equal_var=False) at p < significance, floored by the sensor
    resolution so a significant-but-trivial shift doesn't fire. Constant windows
    give a nan p-value, which fails `< significance` and reads as a non-event.
    """
    _, p = ttest_ind(list(baseline), list(recent), equal_var=False)
    if not (p < significance):
        return False
    return abs(recent[-1] - baseline[-1]) >= config.resolution_for(channel)


def _kl_divergence(baseline, recent, bins: int = klHistogramBins) -> float:
    """Histogram KL divergence D(recent || baseline), in nats.

    Bin both windows over a shared range (raw signed samples don't normalize
    cleanly), Laplace add-one smooth so no bin is empty, normalize to pmfs, and
    take KL. ~0 when the windows look alike, growing as they diverge.
    """
    b = np.asarray(baseline, dtype=float)
    r = np.asarray(recent, dtype=float)
    lo = min(b.min(), r.min())
    hi = max(b.max(), r.max())
    if hi <= lo:  # both windows constant => identical distributions
        return 0.0
    edges = np.linspace(lo, hi, bins + 1)
    q = np.histogram(b, bins=edges)[0].astype(float) + 1.0  # baseline pmf (Laplace)
    p = np.histogram(r, bins=edges)[0].astype(float) + 1.0  # recent pmf
    q /= q.sum()
    p /= p.sum()
    return float(np.sum(kl_div(p, q)))


# Per-stream median auto-calibrated KL threshold from the last `_kl_events` call,
# keyed by sensor name; exposed for reporting.
last_kl_thresholds: dict[str, float] = {}


def _rolling_quantile(
    values: np.ndarray, window: int, q: float, min_periods: int
) -> np.ndarray:
    """`q`-quantile of a trailing `window` at each position; causal (look-back
    only). Positions with < min_periods prior values are nan."""
    return (
        pd.Series(values)
        .rolling(window, min_periods=min_periods)
        .quantile(q)
        .to_numpy()
    )


def _kl_events(sensor: Sensor) -> list[bool]:
    """Per-present-sample event flags for the value-window KL detector.

    Slide a klBaselineLen+klRecentLen window over the present values; each stop's
    KL (recent vs preceding baseline block) is attributed to its newest sample.
    Fire where KL clears a self-calibrating threshold AND the value moved past
    the resolution. Threshold rolls with the signal (the 1-klEventRate quantile
    of the trailing klRollingWindow KLs) to hold the event rate steady; causal,
    and inactive until klMinCalibrationWindows accrue (else fixed klEventThreshold).
    """
    values = np.array([d.value for d in sensor.data if d.is_present()], dtype=float)
    channel = next((d.name for d in sensor.data if d.is_present()), "")
    events = [False] * len(values)

    baseline_len, recent_len = config.klBaselineLen, config.klRecentLen
    window_len = baseline_len + recent_len
    if len(values) < window_len:
        return events  # too short to form even one window

    # KL at every window position; newest[w] = index of window w's newest sample.
    newest = np.arange(window_len - 1, len(values))
    kl = np.array(
        [
            _kl_divergence(
                values[end - window_len : end - recent_len],  # baseline block
                values[end - recent_len : end],
            )  # recent block
            for end in range(window_len, len(values) + 1)
        ]
    )

    # Rolling self-calibrating threshold, fixed fallback if too short.
    if len(kl) >= config.klMinCalibrationWindows:
        threshold = _rolling_quantile(
            kl,
            config.klRollingWindow,
            1.0 - config.klEventRate,
            config.klMinCalibrationWindows,
        )
    else:
        threshold = np.full(len(kl), config.klEventThreshold)
    defined = threshold[np.isfinite(threshold)]
    last_kl_thresholds[sensor.name] = (
        float(np.median(defined)) if defined.size else config.klEventThreshold
    )

    # Fire on KL over threshold and value past resolution. `kl > nan` is False,
    # so pre-calibration windows never fire.
    moved = np.abs(values[newest] - values[newest - recent_len])
    with np.errstate(invalid="ignore"):
        fires = (kl > threshold) & (moved >= config.resolution_for(channel))
    for i in newest[fires]:
        events[i] = True
    return events
