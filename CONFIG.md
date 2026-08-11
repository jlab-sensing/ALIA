# Configuring ALIA

Everything tunable is in `simulation/src/config.py`.

ALIA sends a sample when it moves more than `Δ = max(α·σ, resolution)`, and
otherwise at least once per heartbeat interval. So there are only three
questions: how small a change is *real* (resolution), how big a change is
*interesting* (window and α), and how stale data may get when nothing happens
(heartbeat).

## The knobs

| Knob | How to pick it |
|---|---|
| `sensorResolution` | Datasheet. Add an entry per channel; unlisted ones fall back to `defaultSensorResolution` (0.02), a guess. |
| `stdDevWindowHours` | One full dominant period — 24 h for anything diurnal. See below. |
| `eventDeltaThreshold` (α) | **The dial.** Pick off the table below to hit a packet budget. Per-channel overrides in `eventDeltaThresholdByChannel`. |
| `maxHeartbeatHours` | Longest you can go without hearing from the node. A requirement, not a tuning target. |
| `baseHeartbeatHours` | How fast you want data while something is happening. |
| `backoffDoublingHours` | How fast you relax once it stops. Leave at 24 h. |
| `errorThreshold` | Error size that would actually matter downstream. Reporting only — does not affect filtering. |
| `samplePeriodSec` | Ignore. Only a fallback for streams too short to measure their own cadence. |

## α — the only real decision

`gt_bme280` air temperature (raw 5-minute series, 6687 samples), 24 h window.
Pick the row whose error you can live with (`vs_unif` is RMSE relative to
uniform downsampling at the *same* packet count — below 1.0 means ALIA is
earning its keep):

| α | saved | RMSE (°C) | max err | err > 0.5 | vs_unif |
|---|---|---|---|---|---|
| 0.5 | 96.2% | 0.169 | 0.60 | 11 (0.2%) | 0.60 |
| 1.0 | 98.0% | 0.297 | 1.23 | 784 (11.7%) | 0.68 |
| 1.5 | 98.5% | 0.424 | 1.67 | 1488 (22.3%) | 0.77 |
| 2.0 | 98.8% | 0.555 | 1.62 | 1768 (26.4%) | 0.93 |
| 3.0 | 99.4% | 0.917 | 2.17 | 3154 (47.2%) | 1.04 |
| 5.0 | 99.5% | 0.925 | 2.25 | 3197 (47.8%) | 0.99 |

Past α ≈ 2 the savings barely move while the error keeps climbing and `vs_unif`
crosses 1.0 — beyond that point you would do better taking every Nth sample.
That knee is the operating point worth having.

Generate this for your own data with
`sweep --dir data/<yours> --mode alpha`.

## `stdDevWindowHours` — one dominant period, minimum

The most consequential value in the file, and it has a right answer rather than
a preference. Below one full cycle, σ measures the signal's own ramp instead of
its noise, so `Δ` widens exactly when the signal is moving fastest.

Same stream at α = 1.0, sweeping the window:

| window | 2 h | 6 h | 12 h | 24 h | 48 h | 72 h |
|---|---|---|---|---|---|---|
| saved | 83.4% | 92.7% | 96.6% | 98.0% | 98.2% | 98.3% |
| vs_unif | 0.75 | 0.93 | 0.76 | **0.68** | 0.66 | 0.60 |

The window is specified in **hours, not samples** — `filter._window_size`
converts using each stream's measured cadence. That conversion is the trap: at
5-minute cadence a 2 h window is 24 samples and works fine, but on hourly data
the same 2 h is **2 samples**, σ collapses, `Δ` pins to the resolution floor,
and the filter transmits almost everything. Always check
`hours · 3600 / cadence` is ≥ ~10 samples. Your record should also span several
windows, since the first is warm-up.

**α is not transferable across window sizes**; change one and re-pick the other.

## The heartbeat is a backstop, not a schedule

Each sample, `filter.py:176` asks: did the value move ≥ `Δ`? → send as `event`,
reset the calm clock. Otherwise, has it been ≥ `interval` since the last packet?
→ send as `heartbeat`. Otherwise drop.

`interval` climbs the longer the stream stays calm, where `calm_hours` is time
since the last **event** (heartbeats do not reset it):

```
interval = min(baseHeartbeatHours · 2^(calm_hours / backoffDoublingHours),
               maxHeartbeatHours)
```

At the defaults that is 6 h → 12 h after a day of calm → 24 h after two days.

**Reaching the ceiling takes 48 h of unbroken calm.** If the detector fires more
often than that, the climb never finishes and the ceiling never applies:

| `gt_bme280` temperature | α = 1.0 | α = 3.0 |
|---|---|---|
| events | 99 | 9 |
| saved, ceiling 12 h | 98.0% | 99.2% |
| saved, ceiling 96 h | 98.0% | 99.6% |

At α = 1.0 every ceiling from 12 h up gives identical output — the detector
fires often enough that the interval never climbs to the cap. At α = 3.0 the
ceiling is load-bearing: it is what stops the stream going dark.

**Caveat on this particular stream:** its max gap reads 52–56 h even at a 6 h
ceiling, which looks like the ceiling failing. It is not — the heartbeat can
only fire on a sample that exists, and this cell has multi-day outages
(6687 samples where 23 days at 5 min would be ~6600, with holes). So a max gap
far *above* the ceiling means missing data, not a misconfigured heartbeat.

So raising α does not just add error — it hands control of your sample rate from
the detector to the heartbeat. The two cannot be picked independently.

## Bringing up a new stream

1. **Ingest it** and read the stream names in `metrics.txt`. Channel names are
   what the config lookups key on, so a stream named `temperature_F` silently
   gets the fallback resolution and the global α. Fix naming first.
2. **`sensorResolution`** from the datasheet.
3. **`stdDevWindowHours`** = one dominant period, then run the two checks above.
4. **Heartbeat trio** from requirements.
5. **Sweep α** and pick a row.
6. **Read the overlay plot** as a diagnostic (below), then confirm against the
   baselines with `benchmark`. If `uniform` is beating you, something upstream is
   misconfigured — usually the window.

| What the panel shows | What it means |
|---|---|
| `events ≫ heartbeats` | Healthy — the detector is driving. |
| `heartbeats ≫ events` | Detector inert. α too high, resolution too coarse, or window wrong — you have built a periodic sampler. |
| `saved` near 0% | σ collapsed. Window too short, or α too small. |
| max gap == `maxHeartbeatHours` | Ceiling binding — the heartbeat, not the detector, sets your rate. |
| max gap ≫ `maxHeartbeatHours` | Not a config problem: the source has data outages. |

Starting point for DirtViz (raw 5-minute pulls): `stdDevWindowHours = 24`,
`AmbientTemp_C` α = 1.0, heartbeat 6/24/24, `errorThreshold = 0.5`,
`AmbientTemp_C` resolution 0.01. That gives 98.0% saved at RMSE 0.30 °C.

## Gotcha: where a knob has to be set

`filter.py` does `from config import stdDevWindowHours, ...`, binding a **copy**
at import. Editing `config.py` is fine, but patching at runtime from a script
requires setting `filter.stdDevWindowHours`, not `config.stdDevWindowHours`.

Affected: `stdDevWindowHours`, the three heartbeat values, `samplePeriodSec`.
Read live off `config`: `eventDeltaThreshold`, `sensorResolution`,
`errorThreshold`, `excludedChannels`. `sweep.py` routes these correctly already
(see `_CONFIG_KNOBS`). One knob lives outside `config.py`: `commissioningHours`
in `baselines.py`, which only affects the benchmark controls.
