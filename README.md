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
├── simulation/
│   ├── data_transformer.py         # XLSX → Sensor ingestion
│   ├── sensorscope_transformer.py  # SensorScope .txt → Sensor ingestion
│   └── src/                        # algorithm + comparison harness
│       ├── models.py               # Datum, Sensor
│       ├── config.py               # tunable filter parameters
│       ├── filter.py               # the logging-decision algorithm
│       ├── pipeline.py             # runs raw and filtered branches
│       ├── metrics.py              # per-sensor stats and raw-vs-filtered diff
│       ├── plots.py                # shared plotting helpers
│       ├── sweep.py                # compression/error tradeoff sweeps
│       ├── detector_overlay.py     # per-detector overlay plots
│       └── compare.py              # CLI entry point
├── data/
│   ├── UCSC_Haybarn_Field/     # Haybarn site — logger_*.xlsx files
│   ├── fishnet-meteo/          # SensorScope .txt station logs
│   ├── genepi-meteo/           #   "
│   ├── luce_stations_1x/       #   "
│   ├── plaine_morte-meteo/     #   "
│   └── out/<dataset>/          # generated metrics.csv, metrics.json, plots/
```

## Running the comparison harness

From the repo root:

```sh
python -m simulation.src.compare \
    --dir data/UCSC_Haybarn_Field \
    --output data/out/UCSC_Haybarn_Field
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
`simulation/sensorscope_transformer.py`, which emits **one
`Sensor` per (station file, channel)** — the nine physical channels
(ambient/surface temperature, solar radiation, relative humidity, soil
moisture, watermark, rain, wind speed, wind direction) each become their own
series, each reading stored in `Datum.value` and tagged by `Datum.name`, so the
single-channel ALIA filter and metrics run unchanged. Both on-disk schema variants (with and
without the extra sequence-number column) are handled automatically. To
analyze a station:

```sh
python -m simulation.src.compare \
    --dir data/fishnet-meteo \
    --output data/out/fishnet-meteo
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
- `plots/overlay/<logger>.png` — one panel per single-channel stream, raw (faded) and filtered (solid) overlaid.
- `plots/histogram/<logger>.png` — value distribution of the original (raw) stream.
- `metrics.txt` — human-readable table of the same rows as `metrics.csv`.
- `metrics.csv` — one row per `(logger, stream)`: `n_raw`, `compression_ratio`, and
  reconstruction fidelity in the channel's own units (`rmse`, `mae`, `max_err`),
  plus `rmse_over_res` (RMSE as a multiple of the sensor's resolution, a
  dimensionless cross-channel number that doesn't blow up on zero-mean or
  dead-calm channels the way a mean-/σ-normalized nrmse would) and
  `mean_latency_hr`.
- `metrics.json` — full diff including sample counts, drops by reason, per-stream
  descriptive stats, mean/std deltas, and both zero-order-hold and linear-interp
  error breakdowns.

## Editing the algorithm

The decision logic lives in `simulation/src/filter.py` in `_should_log(...)`, which returns `(keep: bool, reason: str)`. `prev` is the most recently kept datum for the sensor (or `None` for the first sample). Rejected points are recorded in `filter.last_drops` and surfaced by `metrics.diff` as `drops_by_reason` — name reasons clearly so the metrics output is useful.


## Running ingestion only

The original ingest-and-plot path still works:

```sh
python simulation/data_transformer.py --dir data/UCSC_Haybarn_Field
```

# Algorithm Specifications

For each single-channel stream the filter walks the samples in order and, per
sample, decides keep (transmit) or drop. The decision is, in priority order:

1. **First sample** — always kept (`reason = "first"`).
2. **Warm-up** — kept until `numWarmupTx` samples have accumulated, so detectors
   have a baseline (`reason = "warmup"`).
3. **Event** — if the active detector fires, transmit (`reason = "event"`). This
   closes any open run and drops the stream into the **burst** tier.
4. **Heartbeat** — otherwise, if the time since the last transmission exceeds the
   current tier's interval, transmit anyway (`reason = "heartbeat"`).
5. **Run** — otherwise drop and extend the current run-length-encoded run
   (`reason = "in_run"`); the run count rides on the next transmitted packet.

## Event detectors

The active detector is `config.eventDetector`; all three run per channel:

- **`delta`** (current default) — level guard: fire when
  `|value − prev| ≥ Δ`, where `Δ = max(α·σ, resolution)`. `σ` is a rolling
  standard deviation over `stdDevWindowHours`; `α` is `eventDeltaThreshold`
  (global default 3, with per-channel overrides in `eventDeltaThresholdByChannel`);
  `resolution` is the channel's `sensorResolution` floor so calm streams don't
  fire on noise.
- **`ttest`** — Welch's t-test (unequal variance) between the recent and baseline
  delta windows; fire when `p < significance`, floored by the channel resolution
  so a significant-but-trivial shift doesn't fire.
- **`kl`** — histogram KL-divergence between a baseline and a recent window; the
  threshold auto-calibrates per stream to the `(1 − klEventRate)` quantile of
  recent KL values.

## Reporting tiers

Each stream moves between three heartbeat tiers based on its recent activity;
the tier only sets the max quiet interval before a heartbeat transmission:

| Tier   | Entered when                              | Heartbeat (`config`)          |
|--------|-------------------------------------------|-------------------------------|
| Burst  | an event just fired                       | `burstHeartbeatHours` (0.5 h) |
| Normal | default                                   | `normalHeartbeatHours` (1 h)  |
| Quiet  | no event for `quietEntryHours` of samples | `quietHeartbeatHours` (3 h)   |

## Configurables (`simulation/src/config.py`)

| Name                        | Meaning                                              |
|-----------------------------|------------------------------------------------------|
| `eventDetector`             | Active detector: `delta`, `ttest`, or `kl`           |
| `eventDeltaThreshold` (α)   | Sigma multiplier for the `delta` guard (default 3)   |
| `sensorResolution`          | Per-channel smallest change worth sending (Δ floor)  |
| `stdDevWindowHours`         | Rolling-σ window for the `delta` guard               |
| `significance`              | p-value threshold for the `ttest` detector           |
| `numWarmupTx`               | Warm-up samples before any detector may fire         |
| `normalHeartbeatHours`      | Normal-tier max interval between transmissions       |
| `quietHeartbeatHours`       | Quiet-tier max interval                              |
| `burstHeartbeatHours`       | Burst-tier max interval (after an event)             |
| `quietEntryHours`           | Quiet duration before backing off to the quiet tier  |

KL-specific knobs (`klHistogramBins`, `klEventRate`, `klRollingWindow`,
`klBaselineLen`, `klRecentLen`) also live in `config.py`.
