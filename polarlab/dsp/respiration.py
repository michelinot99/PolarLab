"""Frequenza respiratoria da tre sorgenti indipendenti, poi fuse.

1. EDR ampiezza: l'ampiezza del picco R è modulata dal respiro (si sposta l'asse elettrico
   del cuore e cambia l'impedenza del torace).
2. RSA: la frequenza cardiaca accelera in inspirazione e rallenta in espirazione.
3. ACC: l'espansione del torace muove la fascia (componente lenta, < 1 Hz).

Ogni stima è il picco spettrale (Welch) nella banda 0.1–1 Hz (6–60 atti/min), con la sua
"prominenza" (potenza del picco / potenza di banda) come confidenza.
Le serie per battito sono campionate alla frequenza cardiaca, quindi la loro banda utile è
limitata da Nyquist (FC/2): a riposo l'EDR non vede respiri veloci, l'ACC sì.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np
from scipy import interpolate, signal

from polarlab.core.ringbuffer import Ring

BAND = (0.1, 1.0)
FS_RESAMPLE = 4.0


@dataclass(slots=True)
class RespEstimate:
    brpm: float          # atti respiratori al minuto
    confidence: float    # 0–1
    source: str          # "ecg+acc", "ecg", "acc", "—"
    by_source: dict


def _spectral_peak(x: np.ndarray, fs: float, hi: float) -> tuple[float, float]:
    if len(x) < 8 * fs:
        return float("nan"), 0.0
    x = signal.detrend(x)
    nper = min(len(x), int(32 * fs))
    f, p = signal.welch(x, fs=fs, nperseg=nper, noverlap=nper // 2)
    m = (f >= BAND[0]) & (f <= hi)
    if m.sum() < 3 or p[m].sum() <= 0:
        return float("nan"), 0.0
    fm, pm = f[m], p[m]
    k = int(np.argmax(pm))
    # Prominenza: frazione di potenza di banda attorno al picco (±1 bin)
    prom = float(pm[max(0, k - 1):k + 2].sum() / pm.sum())
    if 0 < k < len(pm) - 1:
        y0, y1, y2 = np.log(pm[k - 1:k + 2] + 1e-12)
        den = y0 - 2 * y1 + y2
        off = 0.5 * (y0 - y2) / den if den < 0 else 0.0
        freq = fm[k] + off * (fm[1] - fm[0])
    else:
        freq = fm[k]
    return float(freq), prom


class RespirationEstimator:
    def __init__(self, acc_fs_decimated: float = 10.0, window_s: float = 60.0):
        self.window_s = window_s
        self._beats: deque[tuple[float, float, float]] = deque()   # (t, ampiezza R, RR ms)
        self.acc_fs = acc_fs_decimated
        self._acc = Ring(int(window_s * acc_fs_decimated), width=3)

    def push_beat(self, t: float, amplitude: float, rr_ms: float) -> None:
        self._beats.append((t, amplitude, rr_ms))
        while self._beats and t - self._beats[0][0] > self.window_s:
            self._beats.popleft()

    def push_acc(self, mg_decimated: np.ndarray) -> None:
        self._acc.extend(mg_decimated)

    def estimate(self) -> RespEstimate:
        est: dict[str, tuple[float, float]] = {}
        if len(self._beats) >= 20:
            t, amp, rr = (np.asarray(v) for v in zip(*self._beats))
            beat_nyq = 0.5 * 1000.0 / float(np.median(rr))
            hi = min(BAND[1], 0.85 * beat_nyq)
            if np.all(np.diff(t) > 0) and t[-1] - t[0] > 20:
                tu = np.arange(t[0], t[-1], 1.0 / FS_RESAMPLE)
                for name, y in (("edr", amp), ("rsa", rr)):
                    yu = interpolate.PchipInterpolator(t, y)(tu)
                    est[name] = _spectral_peak(yu, FS_RESAMPLE, hi)
        if len(self._acc) >= 20 * self.acc_fs:
            a = self._acc.last(len(self._acc))
            a = a - a.mean(axis=0)
            # Componente principale del movimento lento del torace
            _, _, vt = np.linalg.svd(a, full_matrices=False)
            pc = a @ vt[0]
            lp = signal.butter(2, BAND[1], fs=self.acc_fs, output="sos")
            est["acc"] = _spectral_peak(signal.sosfiltfilt(lp, pc), self.acc_fs, BAND[1])
        return self._fuse(est)

    @staticmethod
    def _fuse(est: dict[str, tuple[float, float]]) -> RespEstimate:
        valid = {k: v for k, v in est.items() if np.isfinite(v[0]) and v[1] > 0.15}
        by_source = {k: v[0] * 60.0 for k, v in est.items() if np.isfinite(v[0])}
        if not valid:
            return RespEstimate(float("nan"), 0.0, "—", by_source)
        best = max(valid, key=lambda k: valid[k][1])
        f_best = valid[best][0]
        agree = {k: v for k, v in valid.items() if abs(v[0] - f_best) * 60.0 <= 3.0}
        w = np.array([v[1] for v in agree.values()])
        f = float(np.average([v[0] for v in agree.values()], weights=w))
        conf = float(min(1.0, valid[best][1] * (1.0 + 0.3 * (len(agree) - 1))))
        groups = {"edr": "ecg", "rsa": "ecg", "acc": "acc"}
        src = "+".join(sorted({groups[k] for k in agree}))
        return RespEstimate(brpm=f * 60.0, confidence=conf, source=src, by_source=by_source)
