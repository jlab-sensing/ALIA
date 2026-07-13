"""
@file       pipeline.py

Runs ingested sensor data through both the unfiltered and filtered branches
so they can be compared. The raw branch is preserved untouched so any
mutation inside the algorithm cannot invalidate the comparison.
"""

from copy import deepcopy

from simulation.src import filter
from simulation.src.models import Datum, Sensor


def to_channel_streams(sensors: list[Sensor]) -> list[Sensor]:
    """Split each Sensor into one single-channel Sensor per data type present.

    A physical probe reports several quantities (VWC, SC, ST) as separate
    Datums that share a timestamp; a meteo station reports a single channel.
    ALIA decides keep/drop independently for each stream, so before filtering
    we explode every Sensor into one stream per channel name that actually
    carries data — each is then filtered on its own dynamics (rolling sigma,
    delta, run-length, heartbeat) instead of riding on another channel's
    decision.

    The channel name is appended to the sensor name when the source carried
    more than one channel (so already single-channel inputs, e.g. the meteo
    path, keep their names).
    """
    out: list[Sensor] = []
    for sensor in sensors:
        # Channel names present anywhere in this sensor's series, in first-seen
        # order.
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
