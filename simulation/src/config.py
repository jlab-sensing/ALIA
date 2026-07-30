"""Tunable parameters for the filter algorithm. Edit values here."""

# Fallback cadence (s) when a stream is too short to infer its own timing.
samplePeriodSec = 600  # seconds
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


eventDeltaThreshold = 3  # global default / fallback multiplier
eventDeltaThresholdByChannel = {
    "AmbientTemp_C": 1.0,
    "SurfaceTemp_C": 1.0,
    "RelativeHumidity_pct": 1.0,
    "SolarRadiation_Wm2": 1.5,
    "WindSpeed_ms": 1.5,
    "Rain_mm": 1.0,
    "VWC": 5.0,  # NEW: soil moisture — free compression, error floor-bound
}


def event_multiplier_for(channel: str) -> float:
    return eventDeltaThresholdByChannel.get(channel, eventDeltaThreshold)


stdDevWindowHours = 2  # hours; stddev window
# Continuous heartbeat backoff. The since-last-transmission interval that forces
# a heartbeat grows the longer a stream stays calm (no events):
#   interval = min(baseHeartbeatours * 2**(calm_hours / backoffDoublingHours),
#                  maxHeartbeatHours)
# calm_hours = wall-clock since the last event; reset to 0 on each event.
baseHeartbeatHours = 6.0  # cadence right after an event (responsive)
backoffDoublingHours = 24.0  # calm hours per doubling of the interval
maxHeartbeatHours = 24.0  # ceiling once fully backed off
numWarmupTx = 100  # warmup measurements before events may fire
significance = 0.01  # p-value threshold for the dormant t-test stub


## UNUSED
# KL-divergence detector params (dormant; see filter.py stubs). Compares two
# windows by histogram KL.
klHistogramBins = 20  # shared histogram bin count
# Threshold auto-calibrates per stream: (1 - klEventRate) quantile of KL over the
# trailing klRollingWindow (0.10 => 90th pct => ~10% fire); causal, look-back only.
klEventRate = 0.10  # target fraction of windows that fire
klRollingWindow = 1000  # trailing KLs the quantile threshold spans
klMinCalibrationWindows = 100  # KLs needed before events may fire
klEventThreshold = 1.5  # nats; fallback when stream too short to calibrate
# NOTE: delta is hardcoded as the sole detector in filter.py. This value is no
# longer read by the pipeline; ttest/kl are kept only as dormant stub code.
eventDetector = "delta"
klBaselineLen = 100  # samples in reference (older) window
klRecentLen = 100  # samples in recent (trailing) window
