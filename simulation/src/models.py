"""
@file       models.py

Shared data types for sensor measurements.

@author     jLab
@author     HARE Lab
"""

from dataclasses import dataclass
from datetime import datetime
import json
import os


@dataclass
class Datum:
    timestamp: datetime
    VWC: float
    SC: float = None
    ST: float = None
    run_length: int = 0


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

    def get_vwc_max(self) -> float:
        return max([datum.VWC for datum in self.data])
