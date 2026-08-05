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


# Rolling-sigma window. Must span at least one full dominant period (a diurnal
# cycle for every channel here). Below that, sigma measures the *signal's own
# ramp* rather than its noise, so the threshold widens exactly when the signal
# is moving fastest -- the detector goes blind through sunrise while chattering
# all night.
#
# Measured at 10x compression, error relative to uniform downsampling (<1 is
# better). The cliff sits between 6 h and 12 h, i.e. exactly where the window
# starts to outspan the sunrise ramp:
#
#   solar, plaine_morte:   2h 5.74   6h 5.26  |  12h 0.63   24h 0.54   336h 0.55
#   solar, fishnet:        2h 0.67   6h 0.76  |  12h 0.50   24h 0.34   336h 0.35
#   surfacetemp, both:     2h ~0.75           |            24h ~0.50
#
# Worst-case error falls 3-13x across all channels. Both sites are flat past
# 24 h (fishnet gains a little more out to 168 h; plaine_morte gains nothing),
# so one diurnal cycle is the defensible default.
stdDevWindowHours = 24
#   interval = min(baseHeartbeatours * 2**(calm_hours / backoffDoublingHours),
#                  maxHeartbeatHours)
# calm_hours = wall-clock since the last event; reset to 0 on each event.
baseHeartbeatHours = 6.0
backoffDoublingHours = 24.0
maxHeartbeatHours = 24.0
