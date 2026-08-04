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
│       ├── baselines.py            # comparison methods + matched-budget solver
│       ├── benchmark.py            # ALIA vs baselines at equal packet count
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

## Benchmarking against baselines

`metrics.csv` compares ALIA against transmitting **everything**, which can only
ever show that sending fewer packets costs fewer packets. `benchmark.py` answers
the question that actually decides whether the algorithm is worth anything:
**at the same number of packets, does ALIA reconstruct the signal better than a
naive scheme?**

```sh
python -m simulation.src.benchmark \
    --dir data/UCSC_Haybarn_Field \
    --output data/out/bench-haybarn
```

Methods compared (all defined in `baselines.py`):

| Method | What it does | Knob |
|---|---|---|
| `alia` | full algorithm: `Δ = max(α·σ, res)` + heartbeat backoff | α |
| `deadband` | classic send-on-delta: transmit when `\|x − last_sent\| ≥ c` | c |
| `deadband_hb` | same fixed `c`, plus ALIA's heartbeat backoff | c |
| `uniform` | transmit every Nth sample | N |
| `alia_events` | ablation: detector only, heartbeat disabled | α |
| `alia_heartbeat` | ablation: heartbeat only, detector disabled | ceiling hours |

`deadband_hb` is the load-bearing control: it differs from `alia` *only* in that
the threshold does not track σ, so any gap between them is precisely what
adaptivity buys. `alia_heartbeat` necessarily degenerates to periodic
transmission — with no events the calm clock never resets and the interval pins
at the ceiling — which is why it tracks `uniform` closely.

Every ALIA-derived method calls `filter.filter` with keyword switches
(`fixed_delta`, `alpha`, `use_events`, `use_heartbeat`) rather than
reimplementing the loop, so run-length encoding, the flush packet and the
backoff curve are identical across methods and any difference is attributable to
the decision rule alone.

Outputs:
- `matched.csv` / `matched.txt` — every method solved (by bisection) to spend
  **no more** packets than ALIA spent on that stream, then scored.
  `rmse_vs_alia` > 1 means the baseline is worse than ALIA at equal cost;
  < 1 means ALIA is losing.
- `frontier.csv` and `plots/frontier/<channel>.png` — each method swept across
  its own knob grid, error against compression, so the comparison is visible at
  every operating point rather than at one. Lower and further right is better.

Errors pool across the streams of a channel by summing squared error and sample
counts before taking the root, so long streams are not double-counted. Flags:
`--limit-samples N` truncates each stream (the solver re-runs the filter dozens
of times per stream, so the half-million-sample `luce` records need it), and
`--no-frontier` produces only the matched-budget table, which is ~2.5× faster.

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
2. **Event** — if the detector fires, transmit (`reason = "event"`). This closes
   any open run and resets the heartbeat backoff.
3. **Heartbeat** — otherwise, if the time since the last transmission exceeds the
   current backoff interval, transmit anyway (`reason = "heartbeat"`).
4. **Run** — otherwise drop and extend the current run-length-encoded run
   (`reason = "in_run"`); the run count rides on the next transmitted packet.

There is no explicit warm-up hold: the rolling σ backing the detector fills over
the first `stdDevWindowHours` of samples, using a partial-window σ until then.

## Event detector

The **delta** level guard is the sole event detector (hardcoded in `filter.py`):
fire when `|value − prev| ≥ Δ`, where `Δ = max(α·σ, resolution)`. `σ` is a
rolling standard deviation over `stdDevWindowHours`; `α` is `eventDeltaThreshold`
(global default 3, with per-channel overrides in `eventDeltaThresholdByChannel`);
`resolution` is the channel's `sensorResolution` floor so calm streams don't fire
on noise. Delta wins because its firing rule *is* the reconstruction objective —
"has the held value drifted too far?" — so it sits on the accuracy-per-packet
frontier at every operating point.

To pick α per channel, use the delta α sweep:

```sh
python -m simulation.src.sweep \
    --dir data/UCSC_Haybarn_Field \
    --output data/out/sweep-haybarn \
    --mode alpha --channels VWC,ST,SC
```

## Heartbeat backoff

Between events, the stream still transmits periodically so data can't go stale.
The required gap since the last transmission **grows the longer the stream stays
calm**, capped at a ceiling:

```
interval = min(baseHeartbeatHours · 2^(calm_hours / backoffDoublingHours),
               maxHeartbeatHours)
```

`calm_hours` is the wall-clock time since the last event; an event resets it to
zero (back to the responsive `base` cadence). This replaced an older fixed
three-tier scheme (burst/normal/quiet) whose burst tier could never fire and
whose normal tier was a vestigial transient — a diagnostic sweep showed only the
"quiet" tier did any work, so it collapsed into this single backoff curve. For
reporting, the overlay plots bucket each sample as `responsive` (< one doubling
of calm), `backing`, or `max` (interval at the ceiling).

## Configurables (`simulation/src/config.py`)

| Name                        | Meaning                                              |
|-----------------------------|------------------------------------------------------|
| `eventDeltaThreshold` (α)   | Sigma multiplier for the delta guard (default 3)     |
| `eventDeltaThresholdByChannel` | Per-channel α overrides                           |
| `sensorResolution`          | Per-channel smallest change worth sending (Δ floor)  |
| `stdDevWindowHours`         | Rolling-σ window for the delta guard                 |
| `baseHeartbeatHours`        | Heartbeat cadence right after an event (responsive)  |
| `backoffDoublingHours`      | Calm hours per doubling of the heartbeat interval    |
| `maxHeartbeatHours`         | Heartbeat interval ceiling once fully backed off     |
| `excludedChannels`          | Channels dropped before filtering (see below)        |

### Excluded channels

`WindDirection_deg` is excluded. It is **circular** — 0° and 360° are the same
bearing — so the linear error metrics in `metrics.py` are invalid for it: a
359°→1° step is a 2° move that RMSE scores as 358°, which is why the channel
previously reported RMSE around 56–72° with `max_err` of 358.6°. Those numbers
were measuring wraparound, not reconstruction quality. Scoring the channel
properly needs angular differencing (`atan2(sin Δ, cos Δ)`) throughout
`reconstruction_error`; until that exists the channel is dropped in
`pipeline.to_channel_streams` rather than reported wrong.
