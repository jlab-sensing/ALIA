"""Ingest DirtViz CSV exports into the shared Sensor/Datum model.

A DirtViz export is a wide CSV with a stacked header: the first row names the
columns, and one or two metadata rows follow before the data starts —

    timestamp,temperature      <- column names
    s,°C                       <- units
    TIME,BME280                <- source device
    1737770400,18.035          <- data

The metadata rows are optional and their count varies by export, so anything
between the name row and the first parseable timestamp is treated as metadata
(units first, device second). Column 0 is the time axis; every other column is
a measurement channel.

Emits one Sensor per (file, column), matching the SensorScope path, so the
single-channel filter and metrics run unchanged. Blank cells are dropped rather
than carried as NaN.
"""

import csv
import math
import os
import re
from datetime import datetime, timezone

from simulation.src.models import Datum, Sensor

# Column names that mean a channel this project already configures. Mapping to
# the canonical name (rather than the source's own label) is what lets a DirtViz
# cell act as donor or holdout in `transfer.py`, which pairs sites by channel
# name.
#
# Each entry is a list of (accepted units, canonical name), first match wins;
# `None` accepts any unit. A column whose unit matches no rule falls back to the
# generic `<name>_<unit>` slug rather than being silently mislabelled -- which
# matters more than it sounds, because the same quantity arrives in different
# units from different DirtViz endpoints. `/teros/` reports VWC in percent and
# EC in uS/cm, while the Haybarn TEROS-12 channels `VWC`/`SC` are m3/m3 and
# dS/m; collapsing them together would apply a resolution off by 100-1000x.
_UNITS_C = ("c", "degc", "celsius")
_UNITS_PCT = ("%", "pct", "percent", "rh")
_CANONICAL = {
    # Air / surface temperature.
    "temperature": [(_UNITS_C, "AmbientTemp_C")],
    "air temperature": [(_UNITS_C, "AmbientTemp_C")],
    "ambient temperature": [(_UNITS_C, "AmbientTemp_C")],
    "surface temperature": [(_UNITS_C, "SurfaceTemp_C")],
    # `/teros/` calls soil temperature `temp`; same units as ST, so it merges.
    "temp": [(_UNITS_C, "ST")],
    "soil temperature": [(_UNITS_C, "ST")],
    # Atmosphere.
    "humidity": [(_UNITS_PCT, "RelativeHumidity_pct")],
    "relative humidity": [(_UNITS_PCT, "RelativeHumidity_pct")],
    "pressure": [(("hpa", "mbar"), "Pressure_hPa")],
    "co2": [(("ppm",), "CO2_ppm")],
    # Electrical. `/power/` reports mV / uA / uW as `v` / `i` / `p`.
    "v": [(("mv",), "Voltage_mV"), (("v", "volt", "volts"), "Voltage_V")],
    "voltage": [(("mv",), "Voltage_mV"), (("v", "volt", "volts"), "Voltage_V")],
    "i": [(("ua", "microamp", "microamps"), "Current_uA")],
    "current": [(("ua", "microamp", "microamps"), "Current_uA")],
    "p": [(("uw", "microwatt", "microwatts"), "Power_uW")],
    "power": [(("uw", "microwatt", "microwatts"), "Power_uW")],
    # Soil. Percent and m3/m3 are kept apart on purpose (see above).
    "vwc": [(_UNITS_PCT, "VWC_pct"), (("m3/m3", "m3m3", ""), "VWC")],
    "soil moisture": [(_UNITS_PCT, "VWC_pct"), (("m3/m3", "m3m3", ""), "VWC")],
    "volumetric water content": [
        (_UNITS_PCT, "VWC_pct"),
        (("m3/m3", "m3m3", ""), "VWC"),
    ],
    "raw_vwc": [(None, "VWC_raw")],  # uncalibrated counts; unit is literally "raw"
    "ec": [(("us/cm", "uscm"), "EC_uScm"), (("ds/m", "dsm"), "SC")],
    "conductivity": [(("us/cm", "uscm"), "EC_uScm"), (("ds/m", "dsm"), "SC")],
    "bulk ec": [(("ds/m", "dsm", ""), "SC")],
}

# Time-axis column labels, and unit strings meaning "epoch milliseconds".
_TIME_NAMES = {"timestamp", "time", "ts", "datetime", "date"}
_MS_UNITS = {"ms", "msec", "millisecond", "milliseconds"}


def _norm(text: str) -> str:
    """Lowercase, strip the degree sign and surrounding punctuation/space."""
    return text.strip().strip("()[]").replace("°", "").replace("µ", "u").lower()


def _slug(text: str) -> str:
    """Filesystem/identifier-safe token; runs of other characters collapse."""
    return re.sub(r"_+", "_", re.sub(r"[^0-9A-Za-z]+", "_", text.strip())).strip("_")


def _channel_name(name: str, unit: str) -> str:
    """Canonical channel name for a column, else a `<name>_<unit>` slug."""
    rules = _CANONICAL.get(_norm(name))
    if rules:
        normalized = _norm(unit)
        for accepted_units, canonical in rules:
            if accepted_units is None or normalized in accepted_units:
                return canonical
        # A missing unit means "unspecified", not "wrong unit" — take the name
        # at its word and use the first rule. Only a unit that is present and
        # unrecognised (°F against a °C channel) forces the generic fallback.
        if not unit.strip():
            return rules[0][1]
    unit_slug = _slug(unit.replace("°", "").replace("%", "pct").replace("µ", "u"))
    return f"{_slug(name)}_{unit_slug}" if unit_slug else _slug(name)


def _parse_timestamp(token: str, unit: str):
    """Epoch seconds/milliseconds or an ISO-8601 string -> naive UTC datetime.

    Naive because every other transformer in this project emits naive
    datetimes, and `filter.py` subtracts timestamps across them.
    """
    token = token.strip()
    if not token:
        return None
    try:
        epoch = float(token)
    except ValueError:
        try:
            return datetime.fromisoformat(token.replace("Z", "+00:00")).replace(
                tzinfo=None
            )
        except ValueError:
            return None
    # Trust the declared unit; fall back to magnitude when it is absent (any
    # plausible second-valued epoch is well under 1e11).
    if _norm(unit) in _MS_UNITS or (not _norm(unit) and epoch > 1e11):
        epoch /= 1000.0
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_float(token: str) -> float:
    try:
        return float(token)
    except ValueError:
        return float("nan")


def convert_csv_to_sensors(path: str) -> list[Sensor]:
    """Parse one DirtViz CSV export into per-channel Sensors (empty if the file
    has no header or no parseable data rows)."""
    stem = os.path.splitext(os.path.basename(path))[0]

    with open(path, "r", newline="") as fp:
        rows = [r for r in csv.reader(fp) if any(cell.strip() for cell in r)]
    if len(rows) < 2:
        return []

    header = [cell.strip() for cell in rows[0]]
    # Everything between the header and the first parseable timestamp is
    # metadata: units, then device. Peek at the time unit as we go, since the
    # unit row is what tells us whether the epoch is seconds or milliseconds.
    meta: list[list[str]] = []
    first_data = len(rows)
    for i, row in enumerate(rows[1:], 1):
        time_unit = meta[0][0] if meta and meta[0] else ""
        if _parse_timestamp(row[0] if row else "", time_unit) is not None:
            first_data = i
            break
        meta.append([cell.strip() for cell in row])

    def _meta(index: int, column: int) -> str:
        row = meta[index] if index < len(meta) else []
        return row[column] if column < len(row) else ""

    time_unit = _meta(0, 0)

    # Channel columns: everything but the time axis. A DirtViz export always
    # leads with it, but match on the name too in case a variant reorders.
    channel_cols = [
        c
        for c in range(len(header))
        if c != 0 and _norm(header[c]) not in _TIME_NAMES
    ]
    if not channel_cols:
        return []

    channels = {c: _channel_name(header[c], _meta(0, c)) for c in channel_cols}
    # Two columns can canonicalize to the same channel (two probes of the same
    # quantity); the device row disambiguates the Sensor names.
    duplicated = {
        ch for ch in channels.values() if list(channels.values()).count(ch) > 1
    }

    per_column: dict[int, list[Datum]] = {c: [] for c in channel_cols}
    for row in rows[first_data:]:
        ts = _parse_timestamp(row[0] if row else "", time_unit)
        if ts is None:
            continue  # a stray metadata or malformed row
        for c in channel_cols:
            if c >= len(row) or not row[c].strip():
                continue
            value = _parse_float(row[c])
            if math.isnan(value):
                continue
            per_column[c].append(Datum(timestamp=ts, value=value, name=channels[c]))

    sensors: list[Sensor] = []
    for c in channel_cols:
        data = per_column[c]
        if not data:
            continue
        channel = channels[c]
        device = _slug(_meta(1, c))
        suffix = f"{device}-{channel}" if (device and channel in duplicated) else channel
        sensors.append(
            Sensor(
                coordinates=None,
                crs=None,
                data=data,
                depth=0.0,
                layer=0,
                name=f"{stem}-{suffix}",
            )
        )
    return sensors


def get_csv_files_from_dir(path_dir: str) -> list[str]:
    """Recursively return .csv files under a dir."""
    if not os.path.isdir(path_dir):
        return []
    found = []
    for root, _dirs, files in os.walk(path_dir):
        for f in files:
            if f.endswith(".csv"):
                found.append(os.path.join(root, f))
    return sorted(found)
