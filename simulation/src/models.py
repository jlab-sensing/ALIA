"""
@file       models.py

Shared data types for sensor measurements.

@author     jLab
@author     HARE Lab
"""

from dataclasses import dataclass
from datetime import datetime
import json
import math
import os


@dataclass
class Datum:
    """A single measurement sample.

    ALIA is data-type agnostic: a Datum carries one scalar `value` tagged with
    the `name` of the quantity it measures (e.g. "VWC", "SC", "ST", or a meteo
    channel like "AmbientTemp_C"). The name is filled in by the input
    transformer from whatever the source file calls the channel. A physical
    probe that reports several quantities emits one Datum per quantity at the
    same timestamp, and ALIA makes the keep/drop decision independently for
    each name.
    """

    timestamp: datetime
    value: float
    name: str = ""
    run_length: int = 0

    def is_present(self) -> bool:
        """True when this Datum carries a real reading (non-None, non-NaN)."""
        return self.value is not None and not (
            isinstance(self.value, float) and math.isnan(self.value)
        )


class Sensor(object):
    def __init__(
        self,
        coordinates: [float, float],  # [lat, lon]
        crs: str,
        data: list[Datum],
        depth: float,
        layer: int,
        name: str,
    ):
        self.coordinates = coordinates
        self.crs = crs
        self.data = data
        self.depth = depth
        self.layer = layer
        self.name = name

    def get_value_max(self) -> float:
        return max([datum.value for datum in self.data])
