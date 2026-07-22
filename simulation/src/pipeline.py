"""Run ingested sensor data through both branches for comparison. The raw
branch is kept untouched so algorithm mutations can't invalidate it."""

from copy import deepcopy

from simulation.src import filter
from simulation.src.models import Datum, Sensor


def to_channel_streams(sensors: list[Sensor]) -> list[Sensor]:
    """Split each Sensor into one single-channel Sensor per data type present.

    ALIA decides keep/drop independently per stream, so a multi-channel probe
    (VWC, SC, ST) is exploded into one stream per channel before filtering. The
    channel name is appended to the sensor name only when the source was
    multi-channel (single-channel meteo inputs keep their names).
    """
    out: list[Sensor] = []
    for sensor in sensors:
        # Channel names present anywhere in this series, first-seen order.
        seen: list[str] = []
        for datum in sensor.data:
            if datum.is_present() and datum.name not in seen:
                seen.append(datum.name)
        multi = len(seen) > 1
        for channel in seen:
            data = [
                Datum(timestamp=d.timestamp, value=d.value, name=d.name)
                for d in sensor.data
                if d.name == channel and d.is_present()
            ]
            if not data:
                continue
            out.append(
                Sensor(
                    coordinates=sensor.coordinates,
                    crs=sensor.crs,
                    data=data,
                    depth=sensor.depth,
                    layer=sensor.layer,
                    name=f"{sensor.name}-{channel}" if multi else sensor.name,
                )
            )
    return out


def run_both(sensors: list[Sensor]) -> dict[str, list[Sensor]]:
    """Return {"raw": <untouched copy>, "filtered": <algorithm output>}."""
    raw = deepcopy(sensors)
    filtered = filter.filter(deepcopy(sensors))
    return {"raw": raw, "filtered": filtered}
