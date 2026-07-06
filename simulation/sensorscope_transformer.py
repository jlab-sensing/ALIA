"""
@file       sensorscope_transformer.py

Ingest SensorScope-format meteo station logs (whitespace-delimited .txt) into
the shared `Sensor`/`Datum` model so the ALIA comparison harness can run over
them.

Covers the fishnet-meteo, genepi-meteo, luce (sensorscope-meteo), and
plaine_morte-meteo deployments. Two on-disk schema variants exist:

    17 columns (fishnet, genepi):
        ID Y M D h m s epoch  Tair Tsurf Solar RH SoilMoist Watermark Rain Wspd Wdir
    18 columns (luce, plaine_morte):
        ID Y M D h m s epoch  SeqNum  Tair Tsurf Solar RH SoilMoist Watermark Rain Wspd Wdir

The only difference is an extra "Sequence Number" column after the epoch. In
both layouts the nine physical measurement channels are always the final nine
columns, so we slice from the end and ignore the optional sequence column
entirely — no per-file schema sniffing required.

The ALIA filter and metrics are single-channel: they key off `Datum.value`. So
we emit one `Sensor` per (file, channel), with that channel's reading stored in
`.value` and the channel name in `.name`. Samples whose channel value is NaN
are dropped from that channel's series (other channels at the same timestamp
are unaffected), which keeps the rolling-stats and delta logic clean.

@author     jLab
@author     HARE Lab
"""

import math
import os
from datetime import datetime

from simulation.src.models import Datum, Sensor

# Names + units for the trailing nine measurement columns, in file order.
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
    """Parse one SensorScope .txt log into a list of per-channel Sensors.

    Returns an empty list for the `*-def.txt` schema sidecar files and for any
    file that does not contain parseable measurement rows.
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    # The "<deployment>-def.txt" files are column-definition sidecars, not data.
    if stem.endswith("-def"):
        return []

    # One growing data list per channel; zipped into Sensors at the end.
    per_channel_data: list[list[Datum]] = [[] for _ in CHANNELS]
    station_id = None

    with open(path, "r") as fp:
        for line in fp:
            tokens = line.split()
            # Need at least the 7 datetime fields + the 9 channels.
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

            # The nine physical channels are always the final nine columns,
            # regardless of the optional sequence-number column.
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
        if not data:  # channel never reported a real value in this file
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
    """Return data .txt files under a directory (recursively), excluding the
    `*-def.txt` schema sidecars. Station folders may be passed directly or via
    a parent that contains several of them."""
    if not os.path.isdir(path_dir):
        return []
    found = []
    for root, _dirs, files in os.walk(path_dir):
        for f in files:
            if f.endswith(".txt") and not f.endswith("-def.txt"):
                found.append(os.path.join(root, f))
    return sorted(found)
