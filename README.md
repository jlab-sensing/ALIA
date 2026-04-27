# ALIA

ALIA (Adaptive Latency-Integrated Algorithm)

## Setup

```sh
python -m venv .
source bin/activate
pip install -r requirements.txt
```

## Layout

```
ALIA/
├── simulation/src/             # algorithm + comparison harness
│   ├── models.py               # Datum, Sensor
│   ├── filter_algo.py          # the logging-decision algorithm
│   ├── pipeline.py             # runs raw and filtered branches
│   ├── metrics.py              # per-sensor stats and raw-vs-filtered diff
│   └── compare.py              # CLI entry point
└── data/UCSC_Haybarn_Field/
    ├── data_transformer.py     # site-specific XLSX → Sensor ingestion
    └── data/                   # XLSX logger files
```

## Running the comparison harness

From the repo root:

```sh
python -m simulation.src.compare \
    --dir data/UCSC_Haybarn_Field/data \
    --output data/UCSC_Haybarn_Field/out
```

Flags:
- `-d / --dir` — directory of XLSX logger files.
- `-f / --files` — comma-separated XLSX paths (alternative to `--dir`).
- `-o / --output` — output directory; defaults to `./out`.
- `--show` — display plots interactively in addition to saving.

Outputs land in `<output>/`:
- `plots/<logger>.png` — raw (faded) and filtered (solid) overlaid on the same axes for VWC, SC, ST.
- `metrics.csv` — one row per `(logger, sensor)`: sample counts, drop rate, VWC mean/std deltas.
- `metrics.json` — full diff including drops by reason and per-sensor descriptive stats.

## Editing the algorithm

The decision logic lives in `simulation/src/filter_algo.py` in `_should_log(datum, prev, sensor)`, which returns `(keep: bool, reason: str)`. `prev` is the most recently kept datum for the sensor (or `None` for the first sample); `sensor` exposes `depth`, `coordinates`, and `name` for context. Rejected points are recorded in `filter_algo.last_drops` and surfaced by `metrics.diff` as `drops_by_reason` — name reasons clearly so the metrics output is useful.

Until `_should_log` is edited, every datum is kept and the raw and filtered branches are identical.

## Running ingestion only

The original ingest-and-plot path still works:

```sh
python data/UCSC_Haybarn_Field/data_transformer.py --dir data/UCSC_Haybarn_Field/data
```
