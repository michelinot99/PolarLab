# Algorithms

All processors work **in streaming**: they receive the block of samples that just arrived
(`push`) and keep their internal state. The cost per sample is constant, and the windowed
metrics (HRV, DFA, breathing, cadence) are recomputed at 0.2–1 Hz. This is the structure the
iPhone needs to avoid draining the battery during a run.

```
ECG µV ─► band-pass 0.5–40 Hz ─┬─► (notch 50 Hz) ─► monitor
                               ├─► Pan-Tompkins QRS ─► sub-sample R peak ─► RR
                               └─► average beat / correlation ─► quality
RR ─► artefact correction (Lipponen-Tarvainen) ─► NN ─► HRV (60 s), LF/HF (5 min), DFA α1 (2 min)
RR + R amplitude + ACC ─► breathing
ACC ─► vertical from gravity ─► cadence, vertical oscillation, intensity
Strap RR (0x2A37) ─► comparison with our RR ─► quality
```

The analysis of a whole recorded session, built on top of these processors, is described in
[ANALYSIS.md](ANALYSIS.md).

## Filters — `dsp/filters.py`
* Butterworth band-pass 0.5–40 Hz (order 2 per side), causal, SOS with state.
  0.5 Hz removes the drift from breathing and movement, 40 Hz the muscle noise.
* Optional IIR notch at 50 Hz (Q = 30). At a 130 Hz sample rate the 50 Hz mains hum is inside
  the band and therefore visible.
* The initial state `sosfilt_zi · x[0]` avoids the start-up transient.
* `zero_phase_ecg` (filtfilt) is for offline morphology analysis.

## QRS detection — `dsp/qrs.py`
Pan & Tompkins (IEEE TBME 1985), adapted to 130 Hz:
1. band-pass 5–15 Hz → 5-point derivative → square → 150 ms moving-window integration;
2. local maxima of the MWI confirmed after 200 ms without a higher value;
3. adaptive thresholds `TH1 = NPKI + 0.25 (SPKI − NPKI)` and `TH2 = TH1/2`, learned in the first 2 s;
4. 200 ms refractory period; a candidate within 360 ms whose slope is < 50% of the previous
   QRS is a T wave;
5. search-back: if no beat arrives within 1.66 × the mean RR, the highest noise peak above
   TH2 is accepted;
6. automatic re-learning after 3 s without beats (strap taken off and put back on);
7. **R-peak localisation** on the 0.5–40 Hz signal in a 300 ms window before the MWI peak,
   with learned polarity (corrected by majority if the QRS is inverted), plus
   **3-point Gaussian interpolation** (a parabola on the logarithms).

At 130 Hz one sample is 7.7 ms: without interpolation the quantisation error (±3.8 ms) would
be comparable to the RMSSD while running (3–10 ms).
On the simulator the robust (MAD) jitter of the R peak is ~0.3 ms (`tests/test_dsp.py`).
`scripts/validate_qrs.py` measures Se and +P on MIT-BIH resampled to 130 Hz (22 October 2026):

| Record | Beats | Se | +P | Notes |
|---|---|---|---|---|
| 100 | 931* | 100.00% | 100.00% | clean sinus rhythm |
| 105 | 1525* | 99.87% | 99.80% | heavy noise and artefacts |
| 203 | 2960 | 94.97% | 96.56% | atrial fibrillation + ventricular tachycardia, multiform ectopic beats |

\* partial download (slow network), evaluated on the downloaded part. Position error:
median +2.4 ms (a constant delay, which cancels out in RR), IQR 2.5 ms.
In record 203 about 20% of the wide ventricular beats are missed during VT episodes. Halving
the thresholds in irregular rhythm or relaxing the T-wave rule only shifts the Se/+P trade-off
(tried: Se 96.1% but +P 93.9%), so the original Pan-Tompkins stays. For a chest strap in sinus
rhythm, the relevant cases are 100 and 105.

## RR artefact correction — `dsp/rr_clean.py`
Lipponen & Tarvainen, J Med Eng Technol 2019 (the algorithm used by Kubios):
* `dRR` normalised by 5.2 × the quartile deviation over 91 beats;
* difference from the median over 11 beats (`mRR`, negative values doubled);
* classification with the S11/S12 (ectopic) and S21/S22 (long/short) regions and the
  "missed beat" (RR/2 ≈ median) and "extra beat" (RR + next RR ≈ median) tests.

Causal version: each interval is classified as soon as the next 2 arrive, with statistics
computed on the window that ends there. Corrections: missed beat → two intervals of RR/2;
extra beat → the two are merged; ectopic/long-short → local median.
A minimum threshold of 15 ms applies to both thresholds, so that with the very low variability
of running, normal beats are not flagged for a few milliseconds.

Intervals that span a packet gap (filled for continuity) are marked `invalid` and excluded
from HRV.

## HRV — `dsp/hrv.py`
* Time domain over 60 s: mean NN, SDNN, RMSSD, pNN50; Poincaré SD1 = √½·sd(ΔNN),
  SD2 = √(2·SDNN² − SD1²).
* Frequency domain over 5 min: cubic spline at 4 Hz, detrend, Welch (Hann), LF 0.04–0.15 Hz,
  HF 0.15–0.40 Hz in ms². While running, breathing often exceeds 0.4 Hz, so the "classic" HF
  loses its meaning: this is why the respiratory peak is reported too.
  (Resampling + Welch was chosen over Lomb-Scargle, the initial plan, because it directly
  gives powers in ms² with standard normalisation.)

## DFA α1 — `dsp/dfa.py`
Peng et al. 1995; in sport, Rogers et al. 2021 (Front. Physiol.).
Integration of the NN series, non-overlapping windows from 4 to 16 beats taken from both the
start and the end, linear detrend, log-log slope of F(n). 2-minute moving window, recomputed
every 5 s.
* α1 ≥ 0.75 → below the aerobic threshold (VT1)
* 0.5 – 0.75 → between VT1 and VT2
* < 0.5 → above the anaerobic threshold

It is very sensitive to artefacts: above 3–5% corrected beats the value is not reliable
(the percentage is shown as "Artefatti").
Validation: white noise → 0.5, 1/f noise → 1.0, Brownian noise → 1.5 (`tests/test_dsp.py`).

## Signal quality — `dsp/quality.py`, `dsp/template.py`
* **Average beat**: mean of the beats aligned on the R peak, with sub-sample alignment
  (the first 8 as an arithmetic mean, then an exponential mean α = 0.08 only for beats with
  r ≥ 0.7). It shows the P-QRS-T morphology even when each single beat is noisy.
* **Beat-template correlation** (median of the last 20 beats).
* **Agreement with the strap**: the H10 computes its own RR intervals with an internal
  algorithm. Since 0x2A37 has no timestamp, the two sequences are aligned by looking for the
  lag (±6 beats) with the minimum mean error. Two independent estimates that agree are the best
  sign of a correct RR.
* Flat/saturated signal and the contact flag of 0x2A37.
* Score 0–100 = 50% correlation + 35% agreement + 15% contact.

## Breathing — `dsp/respiration.py`
Three estimates, each as a spectral peak (Welch) in 0.1–1 Hz over 60 s, with prominence as
confidence:
1. **EDR**: modulation of the R amplitude;
2. **RSA**: modulation of the RR intervals (respiratory sinus arrhythmia);
3. **ACC**: principal component of the slow chest movement (decimated to 10 Hz).

Per-beat series are sampled at the heart rate, so they only see breaths up to HR/2: at rest,
with HR 60, no more than 30 breaths/min. The ACC has no such limit.
Fusion: the most prominent estimate is chosen and those agreeing within 3 breaths/min are averaged.

## Motion — `dsp/motion.py`
* The ACC arrives already resampled to the nominal rate using the strap's timestamps
  (`UniformResampler`, linear interpolation): the H10 crystal runs ~2% fast.
* Vertical = direction of the slow (< 0.3 Hz) component of the acceleration vector: a still
  accelerometer measures +1 g upwards, whatever the orientation of the strap.
* Cadence: autocorrelation of the vertical acceleration (filtered 1–6 Hz) over 8 s, peak
  between 0.25 and 0.75 s (80–240 steps/min) with parabolic interpolation. If there is a strong
  peak at half the lag, the first one was the stride (2 steps) and the step is used.
* Vertical oscillation: double integration with a high-pass before and after each
  integration (to avoid drift), median of the peak-to-peak per step. It is an **estimate**:
  compare it with a reference source (Garmin HRM-Pro, Stryd) before trusting it.

## References
* Pan J, Tompkins WJ. A real-time QRS detection algorithm. IEEE TBME 1985.
* Lipponen JA, Tarvainen MP. A robust algorithm for HRV time series artefact correction
  using novel beat classification. J Med Eng Technol 2019.
* Peng CK et al. Quantification of scaling exponents and crossover phenomena in
  nonstationary heartbeat time series. Chaos 1995.
* Rogers B et al. A new detection method defining the aerobic threshold for endurance
  exercise and training prescription based on fractal correlation properties of HRV.
  Front Physiol 2021.
* Task Force ESC/NASPE. Heart rate variability: standards of measurement. Circulation 1996.
* McSharry PE et al. A dynamical model for generating synthetic ECG signals. IEEE TBME 2003.
* Charlton PH et al. Breathing rate estimation from the ECG and PPG: a review. IEEE Rev
  Biomed Eng 2018.
