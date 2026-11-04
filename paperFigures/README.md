# Paper figures — Results & Evaluation

Every figure is a vector PDF (use this in LaTeX) plus a 300-dpi PNG preview.
All are sized for a two-column layout (`\textwidth` ≈ 7 in) and share one
colour / marker / line style per method, defined in
`simulation/src/plots.py` (`METHOD_STYLE`).

| File | What it shows | Suggested placement |
|---|---|---|
| `fig1_error_frontier` | RMSE vs. compression for ALIA, send-on-delta and uniform, one panel per dataset | main result |
| `fig2_silence_frontier` | worst-case silence (longest gap between packets) vs. compression | main result, paired with fig 1 |
| `fig3_reconstruction` | 30 days of Haybarn soil moisture: ALIA vs. send-on-delta at the same 30× packet budget | qualitative example |
| `fig6_alia_overlay` | the fig 3 Haybarn window, ALIA alone, plus three zooms (dry spell / wetting front / drainage) where raw vs. reconstruction is visible | Results (showcase) |
| `fig7_threshold` | 4 days of FishNet air temperature: δ(t) over time, plus zooms on its widest and narrowest stretch showing the ±δ band around the held value | Results (how the threshold adapts) |
| `fig4_ablation` | ALIA vs. ALIA with no heartbeat / no event detector at a 10× budget; RMSE relative to uniform, silence on a broken linear axis | ablation |
| `fig5_sensitivity_*` | one-at-a-time parameter sensitivity, indexed to the default | robustness (main text or appendix) |
| `table_summary.tex` | 10× budget: RMSE relative to uniform + worst-case silence, one row per dataset × channel, best per column bold | main-text results table (`table*`; needs `booktabs`, `multirow`) |
| `table_matched_full.tex` / `table_matched.csv` | every number at 10× and 30×: RMSE, max error, mean latency, worst-case silence | appendix (long; wrap in `longtable` or split) |

## Regenerating

From the repo root. The benchmark and sweep runs are slow, so run them once;
`paper_figures` only re-reads their CSVs (it re-runs the filter only for fig 3).

```sh
# 1. matched-budget table + frontiers (latency now included)
python -m simulation.src.benchmark --dir data/fishnet-meteo      --output data/out/paper/bench-fishnet      --targets 10,30 --jobs 4
python -m simulation.src.benchmark --dir data/plaine_morte-meteo --output data/out/paper/bench-plaine_morte --targets 10,30 --jobs 4
python -m simulation.src.benchmark --dir data/UCSC_Haybarn_Field --output data/out/paper/bench-haybarn      --targets 10,30 --jobs 4 --channels VWC,ST

# 2. parameter sensitivity
python -m simulation.src.sweep --dir data/fishnet-meteo      --output data/out/paper/sens-fishnet --mode sensitivity --channels AmbientTemp_C,RelativeHumidity_pct
python -m simulation.src.sweep --dir data/UCSC_Haybarn_Field --output data/out/paper/sens-haybarn --mode sensitivity --channels VWC,ST

# 3. figures + table
python -m simulation.src.paper_figures --bench data/out/paper --output paperFigures
```

Panels, datasets and the overlay stream/window are set at the top of
`simulation/src/paper_figures.py` and through its flags (`--overlay-stream`,
`--overlay-days`, `--overlay-target`).

## Reading the figures — caveats to state in the text

- **Oracle-fitted budgets.** In the benchmark, every method (ALIA included) has
  its knob bisection-fitted per stream to hit the same packet budget. The
  numbers are therefore an upper bound for each decision rule, not a deployment
  result; say so, and point to `transfer.py` for the blind-transfer setting.
- **Budget not met (†) / not usable (‡).** † marks a method that could not get
  down to the budget (sent more packets than allowed); ‡ marks one that could
  not use it (even its least-compressing setting sent far fewer packets). Both
  are excluded from bolding, and fig 4 labels ‡ bars.
- **Wind speed is excluded** from every figure and table
  (`EXCLUDED_CHANNELS` in `paper_figures.py`): its 0.5 m/s resolution floor
  stops ALIA at ~27×, so it has no equal-cost comparison at 10×.
- **Worst-case silence** is the longest delay before the receiver learns about a
  sample (`metrics.transmission_latency`, max over every stream of the channel).
  That is the quantity the heartbeat bounds and RMSE does not see. Logger
  outages (raw gaps > 3 sample periods; FishNet has some up to 26 h) are
  subtracted (`exclude_outages=True`), since no method can transmit data that
  was never recorded. Without that, every method showed ~26.7 h.
- **Sensitivity baseline** uses the global α from `config.py` with per-channel
  overrides dropped (that is how `sweep.py --mode sensitivity` works), so its
  operating point differs from the benchmark's.

## Draft captions

**Fig. 1.** Reconstruction error (RMSE, zero-order hold) against compression
ratio. Each method has one knob, swept over a fixed a-priori grid: ALIA's α
(heartbeat held at its defaults: base 6 h, doubling 24 h, cap 24 h; σ window
12 h), send-on-delta's β (band = β × σ of the first 24 h, frozen), uniform's
stride N. Every other setting is held at its default, and the runs are
deterministic, so each point is one exact value, not a noisy estimate. The
defaults' own influence is what Fig. 5 measures. Log–log axes; lower-right is
better. On Haybarn VWC ALIA's curve starts at ~22×: even at α → 0 the sensor
resolution floor (0.001 m³/m³) suppresses sub-resolution changes.

**Fig. 2.** Worst-case silence: the longest a receiver waits between packets,
against compression ratio (logger outages excluded). Same sweeps as Fig. 1, so
points at the same x spend the same packet budget. Uniform has the shortest
silences by construction (it sends on a clock) but the highest error (Fig. 1).
Send-on-delta sends only on change, so on calm signals it goes silent: 16–39 h
on FishNet at 10×, and on Haybarn VWC ~2,500 h (it goes quiet for most of the
dry season). ALIA's heartbeat caps its silence: it stays at ≤ 24 h on Haybarn
at every compression, and 7–15 h on FishNet at 10×.

**Fig. 3.** Thirty days of UCSC Haybarn soil moisture (one TEROS-10 probe) at a
matched 30× packet budget; both methods send about the same number of packets in
this window (149 vs. 147). Grey line: raw samples. Coloured: what the receiver
reconstructs from the transmitted packets (ticks along the bottom; heavy black
ticks are ALIA heartbeats). Shaded: each stretch with no packet for more than 24 h.
Before the Feb 16 storm, send-on-delta goes 120 h without a packet, while ALIA's
heartbeat keeps the receiver updated at least once every 24 h; both track the
storm itself equally well.

**Fig. 4.** Ablation on FishNet at a 10× packet budget. Each variant's knob is
fitted per stream so it sends at most one packet per 10 raw samples (the same
number of packets as uniform sending every 10th sample), so differences come
from *which* samples are sent, not how many. (a) RMSE divided by uniform
downsampling's RMSE at that budget (dashed line = 1). Removing the heartbeat
barely changes error (0.68 vs 0.68 on air temperature); removing the event
detector leaves a periodic sender no better than uniform (0.88–1.00). (b)
Longest gap between packets, linear axis broken at 18–35 h. Dashed: uniform.
Without the heartbeat, silence grows to 12 h (humidity) and 39 h (solar);
with it, ALIA stays ≤ 15 h. 10× is the lighter of the two benchmarked budgets
(10×, 30×); every variant met it on all four FishNet channels. The 30× numbers
are in the appendix table.

**Fig. 5.** Sensitivity of compression, RMSE and mean latency to each parameter
swept alone, as percent change from the default (dotted line). Default: α = 1
(the global value, per-channel overrides dropped), base heartbeat 6 h, backoff
doubling 24 h, heartbeat cap 24 h, σ window 12 h; the same defaults for both
datasets. Note this is not the benchmark's operating point: α = 1 compresses
FishNet air temperature ~56×, not 10×.

**Fig. 6.** ALIA on thirty days of UCSC Haybarn soil moisture at a 30× packet
budget (α fitted so the full record compresses 30×): 149 of 4324 samples sent
in the month shown. (a) Whole window; boxes mark the zooms. Grey: raw samples.
Blue: the receiver's zero-order-hold reconstruction. Ticks: event packets
(blue) and heartbeats (black). (b) Dry spell: the signal drifts by less than
the event threshold, so heartbeats do most of the sending and the held value
lags the slow drying by at most 0.001 m³/m³. (c) Wetting front: events fire
densely (19 of 87 samples) and the reconstruction follows the 0.08 m³/m³ rise
in steps, worst error 0.006. (d) Drainage: events are evenly spaced as the
signal crosses one threshold-width at a time; error stays ≤ 0.001.

**Fig. 7.** How ALIA's event threshold adapts, four days of FishNet air
temperature (α = 0.17, 10× budget). (a) Threshold δ(t) = max(ασ_t, r), σ over
a trailing 12 h window; ticks are event packets. (b), (c) Six-hour windows at
the widest and narrowest δ, in temperature units: the held value (last packet),
the ±δ band around it, and the raw samples. An event fires exactly where the
raw signal leaves the band. After a noisy afternoon the band is ~0.6 °C wide
and fluctuations inside it are ignored (8 events in 6 h); after a calm night it
shrinks to ~0.16 °C, so the morning warm-up is sent densely (40 events in 6 h).
