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
    quietEntryHours,
    quietHeartbeatHours,
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
    """Reporting tier for one sensor. quiet_run = length of the current non-event
    run; an event transmits then resets it. Spanning quietEntryHours enters quiet
    mode. Init 1 so a cold start begins in normal mode, not burst."""

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
        quiet_entry = _quiet_entry_run_length(sensor.data)

        recent = deque(maxlen=numWarmupTx)
        baseline = deque(maxlen=numWarmupTx)

        # KL detector: precompute per-present-sample event flags upfront.
        kl_events = _kl_events(sensor) if config.eventDetector == "kl" else None
        present_i = -1

        for datum in sensor.data:
            if datum.is_present():
                present_i += 1
                if last_value is not None:
                    d = datum.value - last_value
                    if recent:
                        baseline.append(recent[-1])
                    recent.append(d)
                last_value = datum.value

            kl_event = bool(
                kl_events[present_i]
                if (kl_events is not None and datum.is_present())
                else False
            )
            # "delta" detector threshold; updated every sample so rolling sigma
            # stays consistent through absent ones.
            delta = (
                update_delta(
                    datum,
                    stats,
                    config.resolution_for(datum.name),
                    config.event_multiplier_for(datum.name),
                )
                if config.eventDetector == "delta"
                else 0.0
            )
            keep, reason = _should_log(
                recent, baseline, datum, prev, heartbeat, runState,
                quiet_entry, kl_event, delta,
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
    quiet_entry: int,
    kl_event: bool = False,
    delta: float = 0.0,
) -> tuple[bool, str]:
    if prev is None:
        return True, "first"
    if len(baseline) < numWarmupTx:
        return True, "warmup"

    # Event decision per active detector: "kl" (precomputed), "delta" (level
    # guard vs `delta` threshold), "ttest" (Welch t-test, default).
    detector = config.eventDetector
    if detector == "kl":
        fired = kl_event
    elif detector == "delta":
        fired = (
            datum.is_present()
            and prev.is_present()
            and abs(datum.value - prev.value) >= delta
        )
    else:
        fired = _ttest_event(baseline, recent, datum.name)
    if fired:
        heartbeatState.quiet_run = 0
        if runState.run_count > 0:
            datum.run_length = runState.run_count
            runState.run_count = 0
        return True, "event"

    heartbeatHours = _select_heartbeat_hours(heartbeatState, quiet_entry)
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


def _select_heartbeat_hours(state: HeartbeatState, quiet_entry: int) -> float:
    """Heartbeat interval (hours) for the current reporting tier."""
    if state.quiet_run >= quiet_entry:  # quiet a while => quiet tier
        return quietHeartbeatHours
    if state.quiet_run == 0:  # just fired => burst tier
        return burstHeartbeatHours
    return normalHeartbeatHours


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


def _rolling_quantile(values: np.ndarray, window: int, q: float, min_periods: int) -> np.ndarray:
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
    kl = np.array([
        _kl_divergence(values[end - window_len : end - recent_len],  # baseline block
                       values[end - recent_len : end])               # recent block
        for end in range(window_len, len(values) + 1)
    ])

    # Rolling self-calibrating threshold, fixed fallback if too short.
    if len(kl) >= config.klMinCalibrationWindows:
        threshold = _rolling_quantile(
            kl, config.klRollingWindow, 1.0 - config.klEventRate,
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


def _quiet_entry_run_length(data: list[Datum]) -> int:
    """Quiet-run length (samples spanning quietEntryHours at this stream's
    cadence) after which the stream backs off to the quiet tier. Sized per stream
    so the delay is a fixed wall-clock duration regardless of log rate."""
    period = _estimate_period_sec(data)
    return max(1, round(quietEntryHours * 3600 / period))
