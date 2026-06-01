"""
@file       config.py

Tunable parameters for the filter algorithm. Edit values here.
"""

samplePeriodSec = 600  # seconds (10 min — matches the UCSC Haybarn ZL6 cadence)
sensorResolution = 0.02  # TEROS-10/12 stated VWC accuracy (m³/m³)
eventDeltaThreshold = 3  # tuned: sensor delta that counts as an event
stdDevWindowHours = 24  # hours, window for stddev calculation
# reporting tier
normalHeartbeatHours = 3  # hours
quietHeartbeatHours = 12  # hours
burstHeartbeatHours = 0.5  # hours
quietEntryRunLength = 48  # measurements before entering quiet mode



numSamplesPerDay = (stdDevWindowHours * 60 * 60) // samplePeriodSec
