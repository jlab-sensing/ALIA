"""
@file       config.py

Tunable parameters for the filter algorithm. Edit values here.
"""

# Fallback sampling cadence, used only when a stream is too short to infer its
# own. The stddev window is normally sized from each stream's actual timestamps
# (UCSC Haybarn ZL6 ~600 s, SensorScope meteo ~120 s); see filter_algo.
samplePeriodSec = 600  # seconds

# Per-channel measurement resolution: the smallest change worth transmitting.
# Used as the floor on the event threshold, max(k*sigma, resolution), so a
# dead-calm stream (sigma ~ 0) never fires on sub-resolution noise. Keyed by the
# channel name the input transformer stamps on each Datum. Set these from your
# sensors' datasheets; channels not listed fall back to defaultSensorResolution.
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
defaultSensorResolution = 0.02  # fallback for any channel not listed above


def resolution_for(channel: str) -> float:
    """Measurement resolution for a channel name, or the default if unlisted."""
    return sensorResolution.get(channel, defaultSensorResolution)


# Sigma multiplier (alpha) in the event threshold: delta = max(alpha*sigma, resolution).
# Lower alpha => tighter deadband => better reconstruction fidelity, but less
# compression. The multiplier is PER CHANNEL because a single global value can't
# serve both signal types: calm soil channels can afford a wide (alpha=3)
# deadband with near-perfect reconstruction, but dynamic meteo channels
# (temperature, solar, humidity) have a large 24h sigma, so alpha*sigma becomes
# several units wide and the zero-order-hold holds a stale value straight
# through the diurnal ramp. Tightening alpha on those channels trades some
# compression for a large fidelity gain. Channels not listed here fall back to
# eventDeltaThreshold. Values tuned from the per-channel alpha sweep.
eventDeltaThreshold = 3  # global default / fallback multiplier
eventDeltaThresholdByChannel = {
    "AmbientTemp_C": 1.0,
    "SurfaceTemp_C": 1.0,
    "RelativeHumidity_pct": 1.0,
    "SolarRadiation_Wm2": 1.5,
    "WindSpeed_ms": 1.5,
    "Rain_mm": 1.0,
    # WindDirection is circular (0..360 wraps) — sigma/threshold are not
    # meaningful for it yet, so it keeps the default until circular handling
    # lands; see the wind-direction note in the metrics discussion.
}


def event_multiplier_for(channel: str) -> float:
    """Sigma multiplier (alpha) for a channel, or the global default."""
    return eventDeltaThresholdByChannel.get(channel, eventDeltaThreshold)


stdDevWindowHours = 24  # hours, window for stddev calculation
# reporting tier
normalHeartbeatHours = 3  # hours
quietHeartbeatHours = 12  # hours
burstHeartbeatHours = 0.5  # hours
quietEntryRunLength = 48  # measurements before entering quiet mode
