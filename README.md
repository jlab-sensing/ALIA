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
- `-d / --dir` — directory of input files (searched recursively).
- `-f / --files` — comma-separated input paths (alternative to `--dir`).
- `-o / --output` — output directory; defaults to `./out`.
- `--show` — display plots interactively in addition to saving.
- `--format` — `auto` (default), `xlsx`, or `sensorscope`. `auto` picks
  SensorScope meteo (`.txt`) vs Haybarn loggers (`.xlsx`) from the inputs.

### SensorScope meteo stations

The `fishnet-meteo`, `genepi-meteo`, `luce_stations_1x`, and
`plaine_morte-meteo` deployments ship as whitespace-delimited SensorScope
`.txt` logs. They are ingested by
`data/UCSC_Haybarn_Field/sensorscope_transformer.py`, which emits **one
`Sensor` per (station file, channel)** — the nine physical channels
(ambient/surface temperature, solar radiation, relative humidity, soil
moisture, watermark, rain, wind speed, wind direction) each become their own
series, with the reading stored in `Datum.VWC` so the single-channel ALIA
filter and metrics run unchanged. Both on-disk schema variants (with and
without the extra sequence-number column) are handled automatically. To
analyze a station:

```sh
python -m simulation.src.compare \
    --dir data/UCSC_Haybarn_Field/data/fishnet-meteo \
    --output data/UCSC_Haybarn_Field/out/fishnet-meteo
```

`metrics.csv` then carries one row per `(station, channel)`, and each station
gets a plot with one raw-vs-filtered subplot per channel. Note the filter
`config.py` defaults (sample period, sensor resolution) are tuned for the
Haybarn VWC sensors; the σ-based event threshold adapts per channel, but
revisit `samplePeriodSec`/`sensorResolution` if you tune against meteo data.

### Per-channel streams

A reading may carry several data types at once (a soil probe measures VWC, soil
conductivity, and soil temperature at the same timestamp). ALIA makes the
keep/drop decision **independently for each data type** — SC and ST are filtered
on their own dynamics, not kept or dropped because VWC moved. Before filtering,
`pipeline.to_channel_streams` splits every `Sensor` into one single-channel
`Sensor` per data type present (named `<sensor>-VWC`, `<sensor>-SC`,
`<sensor>-ST`), and the rest of the pipeline runs over those streams unchanged.
Inputs that are already single-channel (the meteo path) pass through with their
names intact.

Outputs land in `<output>/`:
- `plots/<logger>.png` — one panel per single-channel stream, raw (faded) and filtered (solid) overlaid.
- `metrics.csv` — one row per `(logger, stream)`: sample counts, drop rate, mean/std deltas for that stream.
- `metrics.json` — full diff including drops by reason and per-stream descriptive stats.

## Editing the algorithm

The decision logic lives in `simulation/src/filter_algo.py` in `_should_log(datum, prev, sensor)`, which returns `(keep: bool, reason: str)`. `prev` is the most recently kept datum for the sensor (or `None` for the first sample); `sensor` exposes `depth`, `coordinates`, and `name` for context. Rejected points are recorded in `filter_algo.last_drops` and surfaced by `metrics.diff` as `drops_by_reason` — name reasons clearly so the metrics output is useful.


## Running ingestion only

The original ingest-and-plot path still works:

```sh
python data/UCSC_Haybarn_Field/data_transformer.py --dir data/UCSC_Haybarn_Field/data
```

# Algorithm Specifications
## Configurables:
α: Confidence Interval (Usually just 3)
Δmin: Sensor Resolutino
ω: Sampling raet x desired time for delta window
Tmax: Time for heartbeat (Max amount of time without Tx)
Δburst: Required Δ from previous value to enter burst mode
θquiet: Required RLE count to enter quiet mode
Tburst: Time for heartbeat in Burst mode (Max amount of time without Tx in Burst mode)
Tquiet: Time for heartbeat in Quiet mode (Max amount of time without Tx in Quiet mode)

## Reporting Tiers (Subject to Change)
| Tier        | Tmax | Tquiet | Tburst | θquiet |
|-------------|------|--------|--------|--------|
|  Responsive | 1 H  | 6 H    | 15 min | 24     |
| Standard    | 3 H  | 12 H   | 30 min | 48     |
| Low Power   | 6 H  | 24 H   | 60 min | 96     |

### Rule 1: Delta Supression 
Is new value - prev value > Δ? If so, transmit. 
    - Δ = α x σ over ω

### Rule 2: Run Length Encoding (RLE)
Is new value - prev value < Δ? If so, add to a new packet and begin incrementing data for every value new value seen < Δ. 

### Rule 3: Heartbeat
Send current value if time since last transmission > Tmax
    - Does NOT stop current RLE Run

### Rule 4: Burst Mode
Is new value - prev value > Δburst? If so, enter burst mode
    - If burst mode, change Tmax to Tburst

### Rule 5: Quiet Mode
Is RLE count > θquiet? If so, enter quiet mode
    - If quiet mode, change Tmax to Tquiet 
