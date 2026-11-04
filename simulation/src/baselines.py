"""Comparison methods for ALIA, plus the knob solver used by both study tiers.

Why this exists: every number in `metrics.csv` compares ALIA against sending
*everything*, which only proves that sending less costs less. The real question
is whether ALIA does better than a simpler scheme at the same packet cost.

Every knob here is **a priori**: a dimensionless multiplier, a sample stride, or
a duration. None is expressed in the channel's physical units, and none is
derived from the recording being scored. That is deliberate. A threshold in raw
units cannot be picked without having already seen the target site, so including
one would mean either fitting it to the answer or guessing -- neither of which
measures anything about a deployable system.

Methods
-------
alia              full algorithm: delta = max(alpha*sigma, resolution) over a
                  rolling sigma, plus the heartbeat backoff. Knob: alpha.
sod_commissioned  send-on-delta with a fixed band, where the band is
                  beta * sigma measured over the stream's first
                  `commissioningHours` and then frozen. Knob: beta,
                  dimensionless. This is the control that isolates whether the
                  sigma needs to keep *updating*: it differs from `alia` only in
                  that its scale is measured once instead of continuously.
uniform           transmit every Nth sample. Knob: N. The floor.
alia_events       ALIA with the heartbeat disabled (detector only). Knob: alpha.
alia_heartbeat    ALIA with the detector disabled (heartbeat only). Knob: the
                  backoff ceiling. Necessarily degenerates to periodic
                  transmission -- with no events the calm clock never resets, so
                  the interval pins at the ceiling. Kept because showing that
                  degeneracy is itself informative.

All ALIA-derived methods call `filter.filter` with keyword switches rather than
reimplementing the loop, so run-length encoding, the flush packet and the
backoff curve are identical across methods, and any difference in the results is
attributable to the decision rule alone.
"""

from copy import deepcopy

from simulation.src import filter as filter_algo
from simulation.src.models import Datum, Sensor

# Larger knob => wider deadband / sparser sampling => fewer packets. Every
# method's knob is oriented this way so one monotone solver serves all of them.
METHODS = [
    "alia",
    "sod_commissioned",
    "uniform",
    "alia_events",
    "alia_heartbeat",
]

# Human-readable labels for plots and tables.
LABELS = {
    "alia": "ALIA (rolling sigma + heartbeat)",
    "sod_commissioned": "send-on-delta (self-scaled at commissioning)",
    "uniform": "uniform downsampling",
    "alia_events": "ablation: events only",
    "alia_heartbeat": "ablation: heartbeat only",
    "sod_fixed": "send-on-delta (fixed threshold)",
}

# A threshold in raw physical units (c in degrees C, in m3/m3) was dropped as a
# comparison method. It cannot be chosen without already having seen the target
# site's data, and every protocol here forbids that -- so any value for it is
# either an oracle fit (which flatters it) or an arbitrary guess (which does not
# test anything). `sod_commissioned` replaces it: same fixed-band decision rule,
# but the scale is measured on-device during commissioning and the transferable
# knob is a dimensionless multiplier, exactly like ALIA's alpha. That keeps the
# standard send-on-delta comparison in the study on terms it can actually meet.

# How much of a stream a device would observe before committing to a threshold.
commissioningHours = 24.0


def commissioning_sigma(stream: Sensor, hours: float = commissioningHours) -> float:
    """Standard deviation over the stream's first `hours`.

    This is what a real node could measure during a commissioning period and
    then hold fixed for the rest of the deployment -- no look-ahead, unlike
    solving a threshold against the whole record. It backs `sod_commissioned`,
    whose knob (beta) is a dimensionless sigma multiplier exactly like ALIA's
    alpha, but multiplies a *static* scale instead of a rolling one. That
    separates the two things ALIA conflates: dimensionless portability (which
    comes from dividing by a sigma) and a threshold that moves with the signal
    (which is what costs accuracy).
    """
    present = [d for d in stream.data if d.is_present()]
    if len(present) < 2:
        return 0.0
    t0 = present[0].timestamp

    def sigma_over(h):
        w = [
            d.value
            for d in present
            if (d.timestamp - t0).total_seconds() <= h * 3600
        ]
        if len(w) < 2:
            return 0.0, len(w)
        mean = sum(w) / len(w)
        return (sum((v - mean) ** 2 for v in w) / (len(w) - 1)) ** 0.5, len(w)

    # A commissioning window can land on a dead stretch -- a solar channel whose
    # first day is all zeros, a wind channel through a calm -- and report sigma
    # = 0. Left alone that makes the threshold 0, so the node transmits every
    # sample and the method silently degenerates into "send everything" instead
    # of acting as a control. Extend the window until it sees some variation.
    h = hours
    while h <= 24 * 365:
        sigma, n = sigma_over(h)
        if sigma > 0:
            return sigma
        if n >= len(present):  # already spans the whole record
            break
        h *= 2

    mean = sum(d.value for d in present) / len(present)
    var = sum((d.value - mean) ** 2 for d in present) / (len(present) - 1)
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
    if method == "sod_commissioned":
        # knob is beta, a dimensionless multiplier on the commissioning sigma.
        out = []
        for s in sensors:
            c = knob * commissioning_sigma(s)
            out.extend(
                filter_algo.filter([deepcopy(s)], fixed_delta=c, use_heartbeat=False)
            )
        return out
    if method == "sod_fixed":
        # knob is the band in the channel's own units. Only `pilot.py` uses
        # it: there the band is chosen from the pilot, never the scored data.
        return filter_algo.filter(
            deepcopy(sensors), fixed_delta=knob, use_heartbeat=False
        )
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
    if method == "sod_commissioned":
        return 0.0, 500.0
    if method == "uniform":
        return 1.0, float(max(2, n))
    if method == "sod_fixed":
        vals = [d.value for d in stream.data if d.is_present()]
        return 0.0, (max(vals) - min(vals)) if vals else 1.0
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
