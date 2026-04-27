"""
@file       config.py

Tunable parameters for the filter algorithm. Edit values here.
"""

samplePeriodSec = 10  # seconds
sensorResolution = 3  # REPLACE ME WITH SPECIFICS OF SENSOR
eventDeltaThreshold = 3  # tuned: sensor delta that counts as an event
stdDevWindowHours = 24  # hours, window for stddev calculation
# reporting tier
normalHeartbeatHours = 3  # hours
quietHeartbeatHours = 12  # hours
burstHeartbeatHours = 0.5  # hours
quietEntryRunLength = 48  # measurements before entering quiet mode

w = (stdDevWindowHours * 60 * 60) / samplePeriodSec
