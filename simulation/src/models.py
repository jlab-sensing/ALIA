"""Shared data types for sensor measurements. (jLab, HARE Lab)"""

from dataclasses import dataclass
from datetime import datetime
import json
import math
import os


@dataclass
class Datum:
    """A single measurement sample: one scalar `value` tagged with the `name` of
    the quantity (e.g. "VWC", "AmbientTemp_C"). A multi-quantity probe emits one
    Datum per quantity per timestamp; keep/drop is decided independently per name.
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
