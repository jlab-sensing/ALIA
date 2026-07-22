"""Ingest SensorScope meteo-station logs (whitespace-delimited .txt) into the
shared Sensor/Datum model.

Two schemas differ only by an optional SeqNum column after epoch (17 vs 18
cols); the nine measurement channels are always the final nine columns, so we
slice from the end. Emits one Sensor per (file, channel); NaN samples are
dropped per-channel.
"""

import math
import os
from datetime import datetime

from simulation.src.models import Datum, Sensor

# Trailing nine measurement columns (name_unit), in file order.
CHANNELS = [
    "AmbientTemp_C",
    "SurfaceTemp_C",
    "SolarRadiation_Wm2",
    "RelativeHumidity_pct",
    "SoilMoisture_pct",
    "Watermark_kPa",
    "Rain_mm",
    "WindSpeed_ms",
    "WindDirection_deg",
]
N_CHANNELS = len(CHANNELS)


def _parse_float(token: str):
    """Return a float, mapping the literal 'NaN' (and parse errors) to nan."""
    try:
        return float(token)
    except ValueError:
        return float("nan")


def convert_txt_to_sensors(path: str) -> list[Sensor]:
    """Parse one SensorScope .txt log into per-channel Sensors (empty for
    `*-def.txt` sidecars or files without parseable rows)."""
    stem = os.path.splitext(os.path.basename(path))[0]
    # "<deployment>-def.txt" are column-definition sidecars, not data.
    if stem.endswith("-def"):
        return []

    per_channel_data: list[list[Datum]] = [[] for _ in CHANNELS]
    station_id = None

    with open(path, "r") as fp:
        for line in fp:
            tokens = line.split()
            # Need the 7 datetime fields + 9 channels.
            if len(tokens) < 7 + N_CHANNELS:
                continue
            try:
                year, month, day = int(tokens[1]), int(tokens[2]), int(tokens[3])
                hour, minute, second = int(tokens[4]), int(tokens[5]), int(tokens[6])
                ts = datetime(year, month, day, hour, minute, second)
            except (ValueError, IndexError):
                continue  # header line or malformed row

            if station_id is None:
                station_id = tokens[0]

            # Channels are the final nine columns (skips optional SeqNum col).
            values = [_parse_float(t) for t in tokens[-N_CHANNELS:]]
            for ci, value in enumerate(values):
                if math.isnan(value):
                    continue
                per_channel_data[ci].append(
                    Datum(timestamp=ts, value=value, name=CHANNELS[ci])
                )

    sensors: list[Sensor] = []
    for ci, channel in enumerate(CHANNELS):
        data = per_channel_data[ci]
        if not data:
            continue
        sensors.append(
            Sensor(
                coordinates=None,
                crs=None,
                data=data,
                depth=0.0,
                layer=0,
                name=f"{stem}-{channel}",
            )
        )
    return sensors


def get_txt_files_from_dir(path_dir: str) -> list[str]:
    """Recursively return data .txt files under a dir, excluding `*-def.txt`."""
    if not os.path.isdir(path_dir):
        return []
    found = []
    for root, _dirs, files in os.walk(path_dir):
        for f in files:
            if f.endswith(".txt") and not f.endswith("-def.txt"):
                found.append(os.path.join(root, f))
    return sorted(found)
