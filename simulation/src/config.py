"""Tunable parameters for the filter algorithm. Edit values here."""

samplePeriodSec = 600  # seconds
sensorResolution = {
    "VWC": 0.001,  # TEROS-10/12 volumetric water content (m³/m³)
    "SC": 0.001,  # TEROS-12 bulk electrical conductivity (dS/m)
    "ST": 0.1,  # TEROS-12 soil temperature (°C)
    "AmbientTemp_C": 0.01,  # SHT75 air temperature (°C)
    "SurfaceTemp_C": 0.0625,  # TN901 surface/IR temperature (°C); data steps
    "SolarRadiation_Wm2": 0.53,  # Davis solar irradiance (W/m²); data steps
    "RelativeHumidity_pct": 0.05,  # SHT75 relative humidity (%)
    "WindSpeed_ms": 0.45,  # Davis anemometer, 1 mph (m/s)
    "Rain_mm": 0.2,  # Davis rain collector (mm)
    "SoilMoisture_pct": 0.1,  # Decagon EC-5 soil moisture (%)
    "WindDirection_deg": 1.0,  # wind direction (°) — see excludedChannels
    "Pressure_hPa": 0.01,  # barometric pressure (hPa)
    "CO2_ppm": 1.0,  # CO2 concentration (ppm)
    "Voltage_V": 0.0001,  # cell potential (V)
    "Voltage_mV": 0.1,  # cell potential from /power/ (mV)
    "Current_uA": 0.1,  # cell current (µA)
    "Power_uW": 0.1,  # cell power (µW)
    "VWC_pct": 0.1,  # volumetric water content (%)
    "VWC_raw": 1.0,  # uncalibrated TEROS counts
    "EC_uScm": 1.0,  # bulk electrical conductivity (µS/cm)
}
defaultSensorResolution = 0.02  # fallback for unlisted channels


def resolution_for(channel: str) -> float:
    return sensorResolution.get(channel, defaultSensorResolution)


excludedChannels = {"WindDirection_deg"}


eventDeltaThreshold = 1  # global default
eventDeltaThresholdByChannel = {
    "AmbientTemp_C": 0.05,
    "SurfaceTemp_C": 1.0,
    "RelativeHumidity_pct": 1.0,
    "SolarRadiation_Wm2": 1.5,
    "WindSpeed_ms": 1.5,
    "Rain_mm": 1.0,
    "VWC": 5.0,
    "VWC_pct": 5.0,  # same TEROS-12 as VWC, reported in percent
    "Pressure_hPa": 1.0,  # slow and diurnal, like the other met channels
}


def event_multiplier_for(channel: str) -> float:
    return eventDeltaThresholdByChannel.get(channel, eventDeltaThreshold)


#
#   solar, plaine_morte:   2h 5.74   6h 5.26  |  12h 0.63   24h 0.54   336h 0.55
#   solar, fishnet:        2h 0.67   6h 0.76  |  12h 0.50   24h 0.34   336h 0.35
#   surfacetemp, both:     2h ~0.75           |            24h ~0.50
#
# Worst-case error falls 3-13x across all channels. Both sites are flat past
# 24 h (fishnet gains a little more out to 168 h; plaine_morte gains nothing),
# so one diurnal cycle is the defensible default.
stdDevWindowHours = 12
#   interval = min(baseHeartbeatours * 2**(calm_hours / backoffDoublingHours),
#                  maxHeartbeatHours)
# calm_hours = wall-clock since the last event; reset to 0 on each event.
baseHeartbeatHours = 6.0
backoffDoublingHours = 24.0
maxHeartbeatHours = 24.0
