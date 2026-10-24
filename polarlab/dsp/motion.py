"""Movimento dall'accelerometro della fascia: gravità, cadenza, intensità, oscillazione verticale.

La fascia può essere indossata con qualsiasi orientamento: la direzione verticale si
stima dalla gravità (media a bassa frequenza del vettore accelerazione), poi si proietta
il segnale su quell'asse. Nella corsa ogni passo produce un ciclo dell'accelerazione
verticale, quindi la frequenza fondamentale verticale è la cadenza (passi/min).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import signal

from polarlab.core.ringbuffer import Ring
from polarlab.dsp.filters import SosFilter

G_MS2 = 9.80665


@dataclass(slots=True)
class MotionState:
    cadence_spm: float          # passi al minuto (nan se fermo)
    cadence_conf: float         # 0–1, altezza del picco di autocorrelazione
    intensity_mg: float         # RMS dell'accelerazione dinamica
    vertical_osc_cm: float      # escursione verticale del busto per passo (stima)
    up: np.ndarray              # versore verticale (verso l'alto) nel sistema della fascia


class MotionAnalyzer:
    def __init__(self, fs: float, window_s: float = 8.0):
        self.fs = fs
        self.window_s = window_s
        self._buf = Ring(int(window_s * fs) + 1, width=3)
        self._vert = Ring(int(window_s * fs) + 1)
        lp = signal.butter(2, 0.3, fs=fs, output="sos")
        self._lp_axes = [SosFilter(lp) for _ in range(3)]
        self.up = np.array([0.0, 0.0, 1.0])

    def push(self, mg: np.ndarray) -> np.ndarray:
        """Aggiunge campioni (n, 3) in mG; ritorna l'accelerazione verticale dinamica (mG)."""
        mg = np.asarray(mg, dtype=float)
        if len(mg) == 0:
            return np.empty(0)
        # Un accelerometro fermo misura +1 g verso l'alto: la componente lenta indica la verticale
        g = np.column_stack([f.process(mg[:, i]) for i, f in enumerate(self._lp_axes)])
        norm = np.linalg.norm(g, axis=1, keepdims=True)
        up = g / np.maximum(norm, 1e-6)
        self.up = up[-1]
        vert = np.sum(mg * up, axis=1) - norm[:, 0]
        self._buf.extend(mg)
        self._vert.extend(vert)
        return vert

    def recent_vertical(self, seconds: float) -> np.ndarray:
        return self._vert.last(int(seconds * self.fs))

    def update(self) -> MotionState:
        v = self._vert.last(len(self._vert))
        dyn = self._buf.last(len(self._buf))
        intensity = float(np.sqrt(np.mean(np.sum((dyn - dyn.mean(axis=0)) ** 2, axis=1)))) if len(dyn) else 0.0
        cad, conf, osc = float("nan"), 0.0, float("nan")
        if len(v) >= 4 * self.fs and intensity > 80.0:
            cad, conf, period = self._cadence(v)
            if np.isfinite(period):
                osc = self._vertical_oscillation(v, period)
        return MotionState(cadence_spm=cad, cadence_conf=conf, intensity_mg=intensity,
                           vertical_osc_cm=osc, up=self.up.copy())

    def _cadence(self, v: np.ndarray) -> tuple[float, float, float]:
        fs = self.fs
        x = signal.sosfiltfilt(signal.butter(2, [1.0, 6.0], btype="band", fs=fs, output="sos"), v)
        x = x - x.mean()
        ac = signal.correlate(x, x, mode="full", method="fft")[len(x) - 1:]
        if ac[0] <= 0:
            return float("nan"), 0.0, float("nan")
        ac = ac / ac[0]
        lo, hi = int(0.25 * fs), min(int(0.75 * fs), len(ac) - 2)   # 80–240 passi/min
        seg = ac[lo:hi + 1]
        peaks, _ = signal.find_peaks(seg)
        if len(peaks) == 0:
            return float("nan"), 0.0, float("nan")
        k = lo + peaks[np.argmax(seg[peaks])]
        # Se c'è un picco forte a metà ritardo, k è la falcata (2 passi): scegli il passo
        half = int(round(k / 2))
        if half >= lo and ac[half - 1:half + 2].max() > 0.6 * ac[k]:
            k = half - 1 + int(np.argmax(ac[half - 1:half + 2]))
        y0, y1, y2 = ac[k - 1], ac[k], ac[k + 1]
        den = y0 - 2 * y1 + y2
        frac = 0.5 * (y0 - y2) / den if den < 0 else 0.0
        period = (k + frac) / fs
        if ac[k] < 0.3:
            return float("nan"), float(ac[k]), float("nan")
        return 60.0 / period, float(ac[k]), period

    def _vertical_oscillation(self, v: np.ndarray, period: float) -> float:
        """Doppia integrazione dell'accelerazione verticale con passa-alto tra le integrazioni."""
        fs = self.fs
        hp = signal.butter(2, 0.5 / period * 0.5, btype="high", fs=fs, output="sos")
        a = signal.sosfiltfilt(hp, v * G_MS2 / 1000.0)
        vel = signal.sosfiltfilt(hp, np.cumsum(a) / fs)
        pos = signal.sosfiltfilt(hp, np.cumsum(vel) / fs)
        n = int(round(period * fs))
        tail = pos[n:-n] if len(pos) > 3 * n else pos
        if len(tail) < n:
            return float("nan")
        steps = len(tail) // n
        p2p = [np.ptp(tail[i * n:(i + 1) * n]) for i in range(steps)]
        return float(np.median(p2p) * 100.0)
