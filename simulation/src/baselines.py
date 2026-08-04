"""Comparison methods for ALIA, plus the knob solver that matches their packet
budget to ALIA's.

Why this exists: every number in `metrics.csv` compares ALIA against sending
*everything*, which only proves that sending less costs less. The question a
reader actually asks is whether ALIA reconstructs better than a trivial scheme
*at the same number of packets*. These are those trivial schemes.

Methods
-------
alia            full algorithm: adaptive delta = max(alpha*sigma, resolution)
                plus the heartbeat backoff. Knob: alpha.
deadband        classic send-on-delta: transmit when |x - last_sent| >= c, for
                a constant c. No sigma, no heartbeat. Knob: c (channel units).
deadband_hb     the same fixed c, but with ALIA's heartbeat backoff bolted on.
                This is the control that isolates *adaptivity*: it differs from
                `alia` only in that the threshold does not track sigma.
uniform         transmit every Nth sample. Knob: N. The dumbest baseline.
alia_events     ALIA with the heartbeat disabled (detector only). Knob: alpha.
alia_heartbeat  ALIA with the detector disabled (heartbeat only). Knob: the
                backoff ceiling. Note this necessarily degenerates to periodic
                transmission -- with no events the calm clock never resets, so
                the interval pins at the ceiling. It is kept because showing
                that degeneracy is itself informative.

All of the ALIA-derived methods call `filter.filter` with keyword switches
rather than reimplementing the loop, so run-length encoding, the flush packet
and the backoff curve are bit-identical across methods and any difference in
the results is attributable to the decision rule alone.
"""

from copy import deepcopy

from simulation.src import filter as filter_algo
from simulation.src.models import Datum, Sensor

# Larger knob => wider deadband / sparser sampling => fewer packets. Every
# method's knob is oriented this way so one monotone solver serves all of them.
METHODS = [
    "alia",
    "deadband",
    "deadband_hb",
    "deadband_gsigma",
    "uniform",
    "alia_events",
    "alia_heartbeat",
]

# Human-readable labels for plots and tables.
LABELS = {
    "alia": "ALIA (adaptive + heartbeat)",
    "deadband": "fixed deadband (send-on-delta)",
    "deadband_hb": "fixed deadband + heartbeat",
    "deadband_gsigma": "deadband, beta*sigma from commissioning",
    "uniform": "uniform downsampling",
    "alia_events": "ablation: events only",
    "alia_heartbeat": "ablation: heartbeat only",
}

# How much of a stream a device would observe before committing to a threshold.
commissioningHours = 24.0


def commissioning_sigma(stream: Sensor, hours: float = commissioningHours) -> float:
    """Standard deviation over the stream's first `hours`.

    This is what a real node could measure during a commissioning period and
    then hold fixed for the rest of the deployment -- no look-ahead, unlike
    solving a threshold against the whole record. It backs `deadband_gsigma`,
    whose knob (beta) is a dimensionless sigma multiplier exactly like ALIA's
    alpha, but multiplies a *static* scale instead of a rolling one. That
    separates the two things ALIA conflates: dimensionless portability (which
    comes from dividing by a sigma) and a threshold that moves with the signal
    (which is what costs accuracy).
    """
    present = [d for d in stream.data if d.is_present()]
    if not present:
        return 0.0
    t0 = present[0].timestamp
    window = [
        d.value
        for d in present
        if (d.timestamp - t0).total_seconds() <= hours * 3600
    ]
    if len(window) < 2:
        window = [d.value for d in present]
    if len(window) < 2:
        return 0.0
    mean = sum(window) / len(window)
    var = sum((v - mean) ** 2 for v in window) / (len(window) - 1)
    return var**0.5


def _uniform(sensors: list[Sensor], stride: float) -> list[Sensor]:
    """Keep every Nth sample. Reconstruction is zero-order hold, matching how
    every other method is scored."""
    n = max(1, int(round(stride)))
    out = []
    for sensor in sensors:
        new = deepcopy(sensor)
        new.data = [
            Datum(
                timestamp=d.timestamp,
                value=d.value,
                name=d.name,
                reason="uniform",
            )
            for d in sensor.data[::n]
        ]
        new.state_changes = []
        out.append(new)
    return out


def run(method: str, sensors: list[Sensor], knob: float) -> list[Sensor]:
    """Apply `method` at setting `knob` and return the transmitted streams."""
    if method == "alia":
        return filter_algo.filter(deepcopy(sensors), alpha=knob)
    if method == "deadband":
        return filter_algo.filter(
            deepcopy(sensors), fixed_delta=knob, use_heartbeat=False
        )
    if method == "deadband_hb":
        return filter_algo.filter(deepcopy(sensors), fixed_delta=knob)
    if method == "deadband_gsigma":
        # knob is beta, a dimensionless multiplier on the commissioning sigma.
        out = []
        for s in sensors:
            c = knob * commissioning_sigma(s)
            out.extend(
                filter_algo.filter([deepcopy(s)], fixed_delta=c, use_heartbeat=False)
            )
        return out
    if method == "uniform":
        return _uniform(sensors, knob)
    if method == "alia_events":
        return filter_algo.filter(
            deepcopy(sensors), alpha=knob, use_heartbeat=False
        )
    if method == "alia_heartbeat":
        # Detector off; knob is the heartbeat ceiling in hours. base is pinned
        # below the ceiling so the backoff curve still reaches it.
        saved = (filter_algo.maxHeartbeatHours, filter_algo.baseHeartbeatHours)
        filter_algo.maxHeartbeatHours = knob
        filter_algo.baseHeartbeatHours = min(
            filter_algo.baseHeartbeatHours, knob
        )
        try:
            return filter_algo.filter(deepcopy(sensors), use_events=False)
        finally:
            filter_algo.maxHeartbeatHours, filter_algo.baseHeartbeatHours = saved
    raise ValueError(f"unknown method {method!r}")


def knob_bounds(method: str, stream: Sensor) -> tuple[float, float]:
    """(lo, hi) bracketing the knob, scaled to this stream where the knob is in
    physical units. lo must transmit at least as much as the target, hi at most."""
    n = len(stream.data)
    if method in ("deadband", "deadband_hb"):
        vals = [d.value for d in stream.data if d.is_present()]
        span = (max(vals) - min(vals)) if vals else 1.0
        return 0.0, max(span, 1e-9) * 1.001
    if method == "deadband_gsigma":
        return 0.0, 500.0
    if method == "uniform":
        return 1.0, float(max(2, n))
    if method == "alia_heartbeat":
        # Hours; upper bound spans the whole record so compression can go as
        # high as "one packet".
        if n >= 2:
            span_hr = (
                stream.data[-1].timestamp - stream.data[0].timestamp
            ).total_seconds() / 3600
        else:
            span_hr = 1.0
        return 1e-3, max(span_hr, 1e-3)
    # alpha-based methods
    return 0.0, 500.0


def n_transmitted(method: str, stream: Sensor, knob: float) -> int:
    return len(run(method, [stream], knob)[0].data)


def solve_knob(
    method: str, stream: Sensor, target_n: int, iters: int = 24
) -> tuple[float, int]:
    """Smallest knob whose packet count is <= `target_n`, by bisection.

    Packet count is non-increasing in the knob for every method here, so plain
    bisection converges. Biasing to `<= target_n` means a baseline is never
    handed a larger budget than ALIA -- if it still wins, the win is real.
    Returns (knob, achieved_n).
    """
    lo, hi = knob_bounds(method, stream)
    n_hi = n_transmitted(method, stream, hi)
    if n_hi > target_n:
        # Even the widest setting transmits too much; that is the best it can do.
        return hi, n_hi
    n_lo = n_transmitted(method, stream, lo)
    if n_lo <= target_n:
        return lo, n_lo
    for _ in range(iters):
        mid = (lo + hi) / 2
        if n_transmitted(method, stream, mid) > target_n:
            lo = mid
        else:
            hi = mid
    return hi, n_transmitted(method, stream, hi)
