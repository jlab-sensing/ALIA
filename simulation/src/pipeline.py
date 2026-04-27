"""
@file       pipeline.py

Runs ingested sensor data through both the unfiltered and filtered branches
so they can be compared. The raw branch is preserved untouched so any
mutation inside the algorithm cannot invalidate the comparison.
"""

from copy import deepcopy

from simulation.src import filter_algo
from simulation.src.models import Sensor


def run_both(sensors: list[Sensor]) -> dict[str, list[Sensor]]:
    """Return {"raw": <untouched copy>, "filtered": <algorithm output>}."""
    raw = deepcopy(sensors)
    filtered = filter_algo.filter(deepcopy(sensors))
    return {"raw": raw, "filtered": filtered}
