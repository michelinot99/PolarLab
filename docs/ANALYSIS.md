# Session analysis

What the Mac app computes when you select a recorded session, and how. The code is in
[`polarlab/analysis/report.py`](../polarlab/analysis/report.py); the screen is
[`polarlab/ui/analysis_view.py`](../polarlab/ui/analysis_view.py).

> These are training metrics and pointers for reviewing the ECG trace, **not diagnoses**.
> A chest strap records a single lead, at 130 Hz,
> while you move. Show anything that worries you to a doctor.

## Pipeline

```
raw.plab ─► StreamDecoder ─► Engine (keep_log)      same code as the live monitor
              │                 ├─ beat_log    every RR / NN interval with its artefact class
              │                 ├─ metrics_log 1 Hz: HR, HRV, α1, quality, cadence, breathing…
              │                 └─ rpeak_log   time and amplitude of every detected R peak
              ├─ ECG, ACC samples, strap HR notifications, data gaps
              ▼
         SessionReport  ─►  zero-phase ECG, median beats, steps and moving segments
              │
              ├─ analysis.npz   arrays (≈5–6 MB per hour, mostly the filtered ECG)
              └─ analysis.json  cache key + summary shown in the session list
```

* One pass over the raw log costs about 0.25% of the recording time (≈10 s for one hour on
  an Apple Silicon Mac). It runs in a background thread; the result is cached in the session
  folder.
* The cache key is `ANALYSIS_VERSION`, plus the size and modification time of `raw.plab`.
  Changing the algorithms means bumping `ANALYSIS_VERSION`.
* Values that depend on your profile (max HR, resting HR) are computed when the screen is
  drawn, from the cached arrays, so changing the settings needs no re-analysis.
* **Rianalizza** (Re-analyse) clears the cache, regenerates the CSV files with the current
  algorithms and rebuilds the report.

## Heart rate
* The 1 Hz series is the engine's HR from our own R peaks (median of the last 5 NN
  intervals). Where our detector has no beats, the strap's HR fills in.
* Average = mean over time. Maximum and minimum are taken on a 5-second rolling median, so
  a single mis-detected beat cannot set a record.
* **Zones** (needs *FC massima*, max HR, in the settings): Z1 < 60%, Z2 60–70%, Z3 70–80%,
  Z4 80–90%, Z5 ≥ 90%. Percentages are of max HR, or of heart-rate reserve (Karvonen) if a
  resting HR is set too.

## Thresholds from DFA α1
* α1 is valid only when artefacts are < 5% and signal quality ≥ 65 (same rule as the
  literature: DFA is very sensitive to corrected beats).
* Minutes in α1 zones: ≥ 0.75 (below VT1), 0.5–0.75 (between VT1 and VT2), < 0.5 (above VT2).
* **HR at VT1/VT2** (Rogers et al. 2021): linear regression α1 = a + b·HR on one valid point
  every 5 s (α1 updates every 5 s). Requirements: at least 12 points, an HR range of at least
  15 bpm, a negative slope, and **R² ≥ 0.3**; otherwise no threshold is shown.
  A threshold is labelled *affidabile* (reliable) when R² ≥ 0.5 and the target α1 lies
  within the observed α1 values (±0.1). Otherwise it is extrapolated and shown as
  *da confermare* (to be confirmed).
  The method works best on a run with gradually increasing intensity (a progressive warm-up
  or a ramp). An easy steady run usually gives a weak relationship and no estimate.

## Movement
* Steps and moving segments come from the **magnitude** of the acceleration, which does not
  depend on how the strap is worn. It is resampled to 100 Hz and band-passed 0.8–5 Hz.
* Moving = RMS per second > 120 mG, smoothed with a 5-s median. Gaps ≤ 10 s are merged, and
  segments shorter than 15 s are dropped.
* Steps = peaks of the band-passed magnitude within moving segments. The threshold is
  max(100 mG, 35% of the segment's 95th percentile), with at least 0.25 s between steps
  (≤ 240 steps/min). Cadence = steps / moving time.
  Check: 62 counted steps on a 21-s run in place where 60 were counted by hand.
* **Cardiac drift**: in the longest moving segment, if it is ≥ 20 min, the first 10% is
  skipped as warm-up. The rest is split in two halves:
  drift = HR(2nd half) / HR(1st half) − 1. Cadence of both halves is shown alongside, because
  a faster pace also raises HR.

## Recovery
After the last moving segment of at least 20 s, the peak HR is searched from 5 s before
to 15 s after the end of movement (HR keeps rising for a few seconds after you stop). The drop
is measured 30, 60 and 120 s after the peak (median of ±2 s). It is only available when the
recording continues long enough after you stop. A drop above ~12 bpm in the first minute is
commonly considered normal (Cole et al. 1999).

## Resting HRV
RMSSD and SDNN on normal NN intervals in the still period before the first movement (up to
5 min, ending 10 s before moving). The period must last at least 60 s; if you never move,
the whole session is used. RMSSD while moving is the mean of the 60-s RMSSD inside moving
segments.

## Events to review

Every event has a time, an end (for episodes), a level and a short explanation. Levels:
**da rivedere** (review: red), **da notare** (note: amber), **dati** (data quality: grey,
hidden by default).

| Event | Rule | Level |
|---|---|---|
| Premature beat | see below | note |
| Run of premature beats | ≥ 2 premature beats in the same episode | review |
| Frequent premature beats | ≥ 10 premature beats and ≥ 1% of all beats | note; review if ≥ 10% |
| Pause | raw RR > 2 s, not across a data gap | review |
| Very high HR | 5-s median HR above max HR + 5 (or 200 bpm if max HR is not set) for ≥ 10 s, with good signal | review |
| Abrupt HR rise/fall | 3-s median HR changes by > 30 bpm within 5 s, signal good at both ends, movement intensity within ×1.5, not within 2 min after an effort (for drops) | note |
| Irregular rhythm under effort | 30-s windows (15-s step) with HR > 100, good signal, at least 20 beats, and ≥ 25% of consecutive RR pairs differing by > 8% | review |
| Data gap | ECG samples lost between packets | data |
| Contact lost | strap reported no skin contact | data |
| Poor signal | quality < 35 for ≥ 10 s | data |
| Probable artefact | flagged interval without the premature-beat pattern, or with poor signal | data |

### Premature beats
The Lipponen–Tarvainen classifier (see [ALGORITHMS.md](ALGORITHMS.md)) marks irregular
intervals as `ectopic` or `longshort`. A premature beat usually produces a short interval
followed by a longer (compensatory) one, and the classifier may mark either. So:

1. consecutive marked intervals form an **episode** (plus the interval right after it);
2. the episode is a premature beat if it contains an interval **shorter than 85%** of the
   local median of normal RR (±5 beats) **followed by a longer one** (> 105%), and the signal
   quality at that moment is ≥ 65;
3. otherwise it is reported as a *probable artefact* (detection error, motion), in the data
   category.

For each premature beat the report gives:
* how early it came (`1 − RR / local median`);
* the **QRS shape compared with your normal beat**: the Pearson correlation, from −100 to
  +200 ms around the R peak, between that beat and the median of up to 1500 normal beats
  (zero-phase filtered). r ≥ 0.85 reads "similar to normal beats", lower values "different".
  A narrow beat with normal shape is typical of beats starting above the ventricles, a wide
  beat with a different shape of beats starting in the ventricles. A single lead at 130 Hz is
  not enough to tell them apart with certainty.

The **Forma del battito** (beat shape) chart overlays the median normal beat and the median
of the premature beats.

### Why these thresholds
While running, sinus rhythm is very regular (beat-to-beat changes of 1–2%), so widespread
irregularity is unusual and worth a look at the trace. At rest, normal sinus arrhythmia
(the heart speeding up when breathing in) easily exceeds 8%, which is why the irregularity
check only runs above 100 bpm. Isolated premature beats add only 2–3 "different" pairs per
30-s window and do not trigger it.

On the simulator (which injects 1% "ventricular" ectopic beats with a wide, different QRS),
the report finds all 14 of 14 injected beats with "different shape" (r ≈ 0.45), and raises no
pause, jump or irregularity events (`tests/test_analysis.py`).

## Limitations
* One lead, 130 Hz, during motion: P waves are often not visible, and QRS width cannot be
  measured precisely.
* Artefacts from strap movement can mimic extra beats; check the trace in the ECG explorer.
* Thresholds from α1 depend heavily on the protocol of the run and on signal quality.
* Recovery depends on what you do after stopping (standing, walking, sitting).

## References
* Rogers B et al. A new detection method defining the aerobic threshold for endurance
  exercise and training prescription based on fractal correlation properties of HRV.
  Front Physiol 2021.
* Lipponen JA, Tarvainen MP. A robust algorithm for HRV time series artefact correction
  using novel beat classification. J Med Eng Technol 2019.
* Cole CR et al. Heart-rate recovery immediately after exercise as a predictor of mortality.
  N Engl J Med 1999.
* Karvonen MJ et al. The effects of training on heart rate; a longitudinal study.
  Ann Med Exp Biol Fenn 1957.
