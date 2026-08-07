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
│   ├── dirtviz_transformer.py      # DirtViz .csv → Sensor ingestion
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
│   ├── dirtviz/                # DirtViz CSV exports
│   └── out/<dataset>/          # generated metrics.csv, metrics.json, plots/
```

## Commands

All run from the repo root. Input format is detected from the file extensions,
so the same command works for every dataset.

### Compare — raw vs filtered, per-stream metrics and overlay plots

```sh
python -m simulation.src.compare --dir data/dirtviz            --output data/out/dirtviz
python -m simulation.src.compare --dir data/UCSC_Haybarn_Field --output data/out/UCSC_Haybarn_Field
python -m simulation.src.compare --dir data/fishnet-meteo      --output data/out/fishnet-meteo
python -m simulation.src.compare --dir data/genepi-meteo       --output data/out/genepi-meteo
python -m simulation.src.compare --dir data/plaine_morte-meteo --output data/out/plaine_morte-meteo
python -m simulation.src.compare --dir data/luce_stations_1x   --output data/out/luce_stations_1x
```

| Flag | Meaning |
|---|---|
| `-d / --dir` | directory of input files (searched recursively) |
| `-f / --files` | comma-separated input paths (alternative to `--dir`) |
| `-o / --output` | output directory; defaults to `./out` |
| `--show` | display plots interactively in addition to saving |
| `--format` | `auto` (default), `xlsx`, `sensorscope`, `dirtviz` |

### Benchmark — ALIA vs baselines at equal packet count

```sh
python -m simulation.src.benchmark --dir data/UCSC_Haybarn_Field \
    --output data/out/bench-haybarn --targets 30,60

# faster: matched-budget table only, no frontier sweep
python -m simulation.src.benchmark --dir data/dirtviz \
    --output data/out/bench-dirtviz --targets 5,10 --no-frontier

# long records need truncating; the solver re-runs the filter per stream
python -m simulation.src.benchmark --dir data/luce_stations_1x \
    --output data/out/bench-luce --limit-samples 20000
```

### Transfer — fit a knob on one site, apply it blind to another

```sh
python -m simulation.src.transfer \
    --donor data/fishnet-meteo --holdout data/plaine_morte-meteo \
    --target 10 --output data/out/transfer-fishnet-to-pm

# --min-samples sets the floor on stream length (default 100)
python -m simulation.src.transfer \
    --donor data/fishnet-meteo --holdout data/dirtviz \
    --target 10 --min-samples 100 --output data/out/transfer-fishnet-to-dirtviz
```

### Sweep — compression/error tradeoff across parameter values

```sh
# α per channel
python -m simulation.src.sweep --dir data/UCSC_Haybarn_Field \
    --output data/out/sweep-haybarn --mode alpha --channels VWC,ST,SC

# heartbeat ceiling
python -m simulation.src.sweep --dir data/dirtviz \
    --output data/out/sweep-dirtviz --mode heartbeat

# both axes together
python -m simulation.src.sweep --dir data/fishnet-meteo \
    --output data/out/sweep-fishnet --mode both --alphas 1,2,3,5
```

`--mode` also takes `sensitivity` and `overlay`.

### Ingest and plot only, no filtering

```sh
python simulation/data_transformer.py --dir data/UCSC_Haybarn_Field
```

## Input formats

### SensorScope meteo stations (`.txt`)

`fishnet-meteo`, `genepi-meteo`, `luce_stations_1x`, and `plaine_morte-meteo`
ship as whitespace-delimited SensorScope logs.
`simulation/sensorscope_transformer.py` emits **one `Sensor` per (station file,
channel)** across the nine physical channels (ambient/surface temperature,
solar radiation, relative humidity, soil moisture, watermark, rain, wind speed,
wind direction). Both on-disk schema variants — with and without the extra
sequence-number column — are handled automatically.

### DirtViz exports (`.csv`)

A stacked header — column names, units, source device — above epoch-timestamped
rows:

```
timestamp,temperature
s,°C
TIME,BME280
1737770400,18.035
```

`simulation/dirtviz_transformer.py` emits **one `Sensor` per (export file,
measurement column)**. The metadata rows are optional and vary in number
between exports, so everything between the name row and the first parseable
timestamp is read as metadata. Epoch seconds, epoch milliseconds (declared by a
`ms` unit), and ISO-8601 timestamps are all accepted; blank cells are dropped
rather than carried as NaN.

Column names are mapped onto the channel names the rest of the project already
configures — `temperature` (°C) → `AmbientTemp_C`, `humidity` →
`RelativeHumidity_pct`, `voltage`/`current`/`power` →
`Voltage_V`/`Current_uA`/`Power_uW`. The mapping is unit-checked: a column
whose declared unit contradicts the canonical channel (°F against
`AmbientTemp_C`) falls back to a generic `<name>_<unit>` channel and picks up
`config.defaultSensorResolution`. Two columns measuring the same quantity are
disambiguated by the device row (`<file>-BME280-AmbientTemp_C`).

Window lengths are inferred per stream from its own cadence, so the hourly
DirtViz sampling needs no config change. The `config.py` resolutions for
`Voltage_V`, `Current_uA`, and `Power_uW` are placeholders — set them to your
ADC's actual LSB before reading fidelity numbers on those channels.

### Per-channel streams

A reading may carry several data types at once (a soil probe measures VWC, SC,
and ST at the same timestamp). ALIA makes the keep/drop decision
**independently for each data type**. Before filtering,
`pipeline.to_channel_streams` splits every `Sensor` into one single-channel
`Sensor` per data type present (`<sensor>-VWC`, `<sensor>-SC`, `<sensor>-ST`).
Inputs that are already single-channel pass through with their names intact.

## Outputs

Under `<output>/`:

- `plots/overlay/<logger>.png` — one panel per single-channel stream, raw
  (faded) and filtered (solid) overlaid.
- `metrics.txt` — human-readable table of the same rows as `metrics.csv`.
- `metrics.csv` — one row per `(logger, stream)`: `n_raw`, `compression_ratio`,
  fidelity in the channel's own units (`rmse`, `mae`, `max_err`), plus
  `rmse_over_res` (RMSE as a multiple of sensor resolution — dimensionless and
  comparable across channels) and `mean_latency_hr`.
- `metrics.json` — full diff: sample counts, drops by reason, per-stream
  descriptive stats, mean/std deltas, and both zero-order-hold and
  linear-interp error breakdowns.

## Benchmarking against baselines

`metrics.csv` compares ALIA against transmitting everything. The benchmark asks
the sharper question: at the same packet cost, does ALIA reconstruct the signal
better than a simpler scheme?

Every knob is a priori — a dimensionless multiplier, a sample stride, or a
duration. No method is tuned with a threshold in the channel's physical units,
since such a threshold cannot be chosen without having already seen the target
site.

| Method | What it does | Knob |
|---|---|---|
| `alia` | full algorithm: `Δ = max(α·σ, res)` over a **rolling** σ, + heartbeat backoff | α |
| `sod_commissioned` | send-on-delta with a **frozen** band: `Δ = β·σ` measured over the stream's first `commissioningHours`, then fixed | β |
| `uniform` | transmit every Nth sample | N |
| `alia_events` | ablation: detector only, heartbeat disabled | α |
| `alia_heartbeat` | ablation: heartbeat only, detector disabled | ceiling hours |

`sod_commissioned` is the load-bearing control: its knob is dimensionless like
α, and the only difference from `alia` is whether σ keeps updating, so any gap
between them is what continuous adaptation buys. Every ALIA-derived method
calls `filter.filter` with keyword switches (`fixed_delta`, `alpha`,
`use_events`, `use_heartbeat`) rather than reimplementing the loop.

### Tier 1 — mechanism (oracle-fitted)

Every method, ALIA included, has its knob bisection-fitted per stream to hit the
same compression target. These are upper bounds on each decision rule, **not**
deployment results.

- `matched.csv` / `matched.txt` — one row per (channel, method, target). `hit`
  is achieved ÷ target compression; far from 1.00 means the method could not
  reach that budget at all. `rmse_vs_alia` > 1 means worse than ALIA.
- `frontier.csv`, `plots/frontier/<channel>.png` — each method swept across its
  knob grid, error against compression. Lower and further right is better.

### Tier 2 — deployment (transfer)

Fit **one** knob per channel on the donor site, freeze it, apply it unchanged to
a held-out site. Read `drift` (achieved ÷ target compression; 1.00 = perfect
transfer) and `spread` across methods. Do **not** read `rmse` across methods —
transferred knobs land at different compressions, so a method that undershoots
its budget looks spuriously accurate. `penalty` is the comparable accuracy
number.

`--min-samples` (default 100) drops short streams; anything kept under 500
samples is warned about, since it is mostly σ warm-up. `drift` also conflates
transfer loss with cadence mismatch: a σ-multiplier fitted on 2-minute meteo
data lands far off its target on hourly data.

## Editing the algorithm

The decision logic lives in `simulation/src/filter.py` in `_should_log(...)`,
which returns `(keep: bool, reason: str)`. `prev` is the most recently kept
datum for the sensor (or `None` for the first sample). Rejected points are
recorded in `filter.last_drops` and surfaced by `metrics.diff` as
`drops_by_reason`.

# Algorithm Specifications

For each single-channel stream the filter walks the samples in order and, per
sample, decides keep (transmit) or drop, in priority order:

1. **First sample** — always kept (`reason = "first"`).
2. **Event** — if the detector fires, transmit (`reason = "event"`). Closes any
   open run and resets the heartbeat backoff.
3. **Heartbeat** — otherwise, if time since the last transmission exceeds the
   current backoff interval, transmit anyway (`reason = "heartbeat"`).
4. **Run** — otherwise drop and extend the current run-length-encoded run
   (`reason = "in_run"`); the run count rides on the next transmitted packet.

There is no explicit warm-up hold: the rolling σ fills over the first
`stdDevWindowHours` of samples, using a partial-window σ until then.

## Event detector

The **delta** level guard is the sole event detector: fire when
`|value − prev| ≥ Δ`, where `Δ = max(α·σ, resolution)`. `σ` is a rolling
standard deviation over `stdDevWindowHours`; `α` is `eventDeltaThreshold`
(global default 3, with per-channel overrides in
`eventDeltaThresholdByChannel`); `resolution` is the channel's
`sensorResolution` floor, so calm streams don't fire on noise.

## Heartbeat backoff

Between events the stream still transmits periodically. The required gap since
the last transmission grows the longer the stream stays calm, capped at a
ceiling:

```
interval = min(baseHeartbeatHours · 2^(calm_hours / backoffDoublingHours),
               maxHeartbeatHours)
```

`calm_hours` is wall-clock time since the last event; an event resets it to zero
(back to the responsive `base` cadence). The overlay plots bucket each sample as
`responsive` (< one doubling of calm), `backing`, or `max` (at the ceiling).

## Configurables (`simulation/src/config.py`)

| Name                        | Meaning                                              |
|-----------------------------|------------------------------------------------------|
| `eventDeltaThreshold` (α)   | Sigma multiplier for the delta guard (default 3)     |
| `eventDeltaThresholdByChannel` | Per-channel α overrides                           |
| `sensorResolution`          | Per-channel smallest change worth sending (Δ floor)  |
| `stdDevWindowHours`         | Rolling-σ window for the delta guard (see below)      |
| `baseHeartbeatHours`        | Heartbeat cadence right after an event (responsive)  |
| `backoffDoublingHours`      | Calm hours per doubling of the heartbeat interval    |
| `maxHeartbeatHours`         | Heartbeat interval ceiling once fully backed off     |
| `excludedChannels`          | Channels dropped before filtering                    |

### Sizing `stdDevWindowHours`

The σ window should span at least one full dominant period — a diurnal cycle for
every channel in these datasets. Below that, σ measures the signal's own ramp
instead of its noise, so `Δ = α·σ` widens exactly when the signal is moving
fastest.

Measured at 10× compression, RMSE relative to uniform downsampling (< 1 is
better):

| Channel / site | 2 h | 6 h | 12 h | 24 h | 336 h |
|---|---|---|---|---|---|
| SolarRadiation, plaine_morte | **5.74** | 5.26 | 0.63 | **0.54** | 0.55 |
| SolarRadiation, fishnet | 0.67 | 0.76 | 0.50 | **0.34** | 0.35 |
| SurfaceTemp, plaine_morte | 0.76 | 0.61 | 0.46 | **0.44** | 0.43 |
| AmbientTemp, fishnet | 0.79 | 0.78 | 0.68 | 0.58 | **0.50** |

Both sites are flat past 24 h. Warm-up is O(1) per sample (Welford), so long
windows are cheap. Note that α is not transferable across window sizes —
re-solve it if you change this.

### Excluded channels

`WindDirection_deg` is excluded. It is circular — 0° and 360° are the same
bearing — so the linear error metrics in `metrics.py` are invalid for it: a
359°→1° step is a 2° move that RMSE scores as 358°. Scoring it properly needs
angular differencing (`atan2(sin Δ, cos Δ)`) throughout `reconstruction_error`;
until that exists the channel is dropped in `pipeline.to_channel_streams`.
