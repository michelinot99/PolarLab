"""Metriche HRV su finestra mobile di intervalli NN (già corretti)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np
from scipy import interpolate, signal

LF_BAND = (0.04, 0.15)
HF_BAND = (0.15, 0.40)


@dataclass(slots=True)
class TimeDomain:
    mean_nn: float
    hr: float
    sdnn: float
    rmssd: float
    pnn50: float
    sd1: float
    sd2: float


@dataclass(slots=True)
class FreqDomain:
    lf: float        # ms²
    hf: float        # ms²
    lf_hf: float
    resp_peak_hz: float   # picco nella banda 0.1–1 Hz (aritmia sinusale respiratoria)


class NNWindow:
    """Finestra temporale scorrevole di (t, nn, corretto?)."""

    def __init__(self, window_s: float):
        self.window_s = window_s
        self._data: deque[tuple[float, float, bool]] = deque()

    def push(self, t: float, nn: float, corrected: bool) -> None:
        self._data.append((t, nn, corrected))
        while self._data and t - self._data[0][0] > self.window_s:
            self._data.popleft()

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)

    def arrays(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        if not self._data:
            return np.empty(0), np.empty(0), np.empty(0, bool)
        t, nn, c = zip(*self._data)
        return np.asarray(t), np.asarray(nn), np.asarray(c, bool)

    @property
    def span_s(self) -> float:
        return self._data[-1][0] - self._data[0][0] if len(self._data) > 1 else 0.0


def time_domain(nn: np.ndarray) -> TimeDomain | None:
    if len(nn) < 5:
        return None
    d = np.diff(nn)
    sdnn = float(np.std(nn, ddof=1))
    rmssd = float(np.sqrt(np.mean(d * d)))
    # Poincaré: SD1 = dispersione perpendicolare alla diagonale, SD2 lungo la diagonale
    sd1 = float(np.sqrt(0.5) * np.std(d, ddof=1))
    sd2 = float(np.sqrt(max(0.0, 2 * sdnn ** 2 - sd1 ** 2)))
    mean_nn = float(np.mean(nn))
    return TimeDomain(mean_nn=mean_nn, hr=60000.0 / mean_nn, sdnn=sdnn, rmssd=rmssd,
                      pnn50=float(np.mean(np.abs(d) > 50.0) * 100.0), sd1=sd1, sd2=sd2)


def frequency_domain(t: np.ndarray, nn: np.ndarray, fs_resample: float = 4.0) -> FreqDomain | None:
    """Ricampionamento cubico a 4 Hz + Welch; bande LF/HF classiche (Task Force 1996).

    In corsa la respirazione supera spesso 0.4 Hz e l'HF classica perde significato:
    per questo si riporta anche il picco respiratorio fino a 1 Hz (limitato da Nyquist del battito).
    """
    if len(nn) < 30 or t[-1] - t[0] < 60.0:
        return None
    tu = np.arange(t[0], t[-1], 1.0 / fs_resample)
    y = interpolate.CubicSpline(t, nn)(tu)
    y = signal.detrend(y, type="linear")
    nper = min(len(y), int(256 * fs_resample / 4))
    f, p = signal.welch(y, fs=fs_resample, window="hann", nperseg=nper, noverlap=nper // 2, detrend=False)

    def band(lo, hi):
        m = (f >= lo) & (f < hi)
        return float(np.trapezoid(p[m], f[m])) if m.any() else 0.0

    lf, hf = band(*LF_BAND), band(*HF_BAND)
    beat_nyq = 0.5 * 1000.0 / float(np.mean(nn))
    m = (f >= 0.1) & (f <= min(1.0, 0.9 * beat_nyq))
    peak = float(f[m][np.argmax(p[m])]) if m.any() else float("nan")
    return FreqDomain(lf=lf, hf=hf, lf_hf=lf / hf if hf > 0 else float("nan"), resp_peak_hz=peak)
