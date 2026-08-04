"""Tunable parameters for the filter algorithm. Edit values here."""

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
    "WindDirection_deg": 1.0,  # wind direction (°) — see excludedChannels
}
defaultSensorResolution = 0.02  # fallback for unlisted channels


def resolution_for(channel: str) -> float:
    return sensorResolution.get(channel, defaultSensorResolution)


excludedChannels = {"WindDirection_deg"}


eventDeltaThreshold = 3  # global default
eventDeltaThresholdByChannel = {
    "AmbientTemp_C": 1.0,
    "SurfaceTemp_C": 1.0,
    "RelativeHumidity_pct": 1.0,
    "SolarRadiation_Wm2": 1.5,
    "WindSpeed_ms": 1.5,
    "Rain_mm": 1.0,
    "VWC": 5.0,
}


def event_multiplier_for(channel: str) -> float:
    return eventDeltaThresholdByChannel.get(channel, eventDeltaThreshold)


stdDevWindowHours = 2
#   interval = min(baseHeartbeatours * 2**(calm_hours / backoffDoublingHours),
#                  maxHeartbeatHours)
# calm_hours = wall-clock since the last event; reset to 0 on each event.
baseHeartbeatHours = 6.0
backoffDoublingHours = 24.0
maxHeartbeatHours = 24.0
