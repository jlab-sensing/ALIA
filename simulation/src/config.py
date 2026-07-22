"""Tunable parameters for the filter algorithm. Edit values here."""

# Fallback cadence (s) when a stream is too short to infer its own timing.
samplePeriodSec = 600  # seconds

# Per-channel resolution (smallest change worth transmitting); floors the event
# threshold max(k*sigma, resolution) so calm streams don't fire on noise. From
# datasheets; unlisted channels use defaultSensorResolution.
sensorResolution = {
    "VWC": 0.001,  # TEROS-10/12 volumetric water content (m³/m³)
    "SC": 0.001,  # TEROS-12 bulk electrical conductivity (dS/m)
    "ST": 0.1,  # TEROS-12 soil temperature (°C)
    "AmbientTemp_C": 0.01,  # air temperature (°C)
    "SurfaceTemp_C": 0.01,  # surface/IR temperature (°C)
    "SolarRadiation_Wm2": 1.0,  # solar irradiance (W/m²)
    "RelativeHumidity_pct": 0.08,  # relative humidity (%)
    "WindSpeed_ms": 0.5,  # wind speed (m/s)
    "WindDirection_deg": 1.0,  # wind direction (°)
}
defaultSensorResolution = 0.02  # fallback for unlisted channels


def resolution_for(channel: str) -> float:
    return sensorResolution.get(channel, defaultSensorResolution)


# Sigma multiplier alpha in the event threshold delta = max(alpha*sigma, resolution).
# Lower alpha => tighter deadband => more fidelity, less compression. Per channel:
# calm soil channels tolerate alpha=3; dynamic meteo channels need tighter alpha
# so the zero-order-hold doesn't hold stale values through the diurnal ramp.
# Unlisted channels use eventDeltaThreshold. Tuned from the per-channel alpha sweep.
eventDeltaThreshold = 3  # global default / fallback multiplier
eventDeltaThresholdByChannel = {
    "AmbientTemp_C": 1.0,
    "SurfaceTemp_C": 1.0,
    "RelativeHumidity_pct": 1.0,
    "SolarRadiation_Wm2": 1.5,
    "WindSpeed_ms": 1.5,
    "Rain_mm": 1.0,
    # WindDirection circular (0..360 wraps) — sigma not meaningful yet; keeps
    # default until circular handling lands.
}


def event_multiplier_for(channel: str) -> float:
    return eventDeltaThresholdByChannel.get(channel, eventDeltaThreshold)


# KL-divergence detector params (alternative to Welch t-test). Compares two
# windows by histogram KL; use kl_sweep.py to pick thresholds per stream.
klHistogramBins = 20  # shared histogram bin count
# Threshold auto-calibrates per stream: (1 - klEventRate) quantile of KL over the
# trailing klRollingWindow (0.10 => 90th pct => ~10% fire); causal, look-back only.
klEventRate = 0.10  # target fraction of windows that fire
klRollingWindow = 1000  # trailing KLs the quantile threshold spans
klMinCalibrationWindows = 100  # KLs needed before events may fire
klEventThreshold = 1.5  # nats; fallback when stream too short to calibrate

# Active detector: "ttest" (Welch t-test), "delta" (level guard), "kl" (histogram KL).
eventDetector = "delta"
klBaselineLen = 100  # samples in reference (older) window
klRecentLen = 100  # samples in recent (trailing) window

stdDevWindowHours = 2  # hours; stddev window
# reporting tier heartbeat intervals
normalHeartbeatHours = 1  # hours
quietHeartbeatHours = 3  # hours
burstHeartbeatHours = 0.5  # hours; after an event
# Quiet duration before backing off to quiet tier; converted per stream to a
# sample count so it's the same wall-clock time at any log rate.
quietEntryHours = 1  # hours
numWarmupTx = 100  # warmup measurements for t-test
significance = 0.01  # p-value threshold; event when ttest_ind p < significance
