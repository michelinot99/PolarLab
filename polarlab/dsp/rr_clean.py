"""Correzione degli artefatti RR: Lipponen & Tarvainen (2019), in versione causale.

Lipponen JA, Tarvainen MP. "A robust algorithm for heart rate variability time series
artefact correction using novel beat classification". J Med Eng Technol 2019.
È l'algoritmo usato da Kubios HRV.

Ogni intervallo viene classificato con 2 battiti di ritardo (servono RR[n+1], RR[n+2]);
le statistiche (quartile deviation su 91 battiti, mediana su 11) sono calcolate su una
finestra che termina lì, quindi l'algoritmo funziona in tempo reale.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

ALPHA = 5.2
C1, C2 = 0.13, 0.17
QD_WINDOW = 91
MED_WINDOW = 11
# Soglie minime (ms): in corsa la variabilità è così bassa che la sola quartile deviation
# porterebbe a scartare battiti normali per differenze di pochi millisecondi.
MIN_TH1_MS = 15.0
MIN_TH2_MS = 15.0
WARMUP = 15        # intervalli di storia prima di applicare la classificazione completa

NORMAL, ECTOPIC, MISSED, EXTRA, LONGSHORT, INVALID = "normal", "ectopic", "missed", "extra", "longshort", "invalid"


@dataclass(slots=True)
class NN:
    t: float          # s, istante del battito che chiude l'intervallo
    rr: float         # ms, intervallo originale
    nn: float         # ms, intervallo corretto (uguale a rr se normale)
    kind: str


def _qd(x: np.ndarray) -> float:
    q1, q3 = np.percentile(x, [25, 75])
    return 0.5 * (q3 - q1)


class RRCleaner:
    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self._t: deque[float] = deque(maxlen=QD_WINDOW + 3)
        self._rr: deque[float] = deque(maxlen=QD_WINDOW + 3)
        self._forced: deque[bool] = deque(maxlen=QD_WINDOW + 3)
        self._pending = 0          # quanti intervalli in coda non sono ancora classificati
        self._skip_next = False    # dopo un battito "extra" il successivo è già stato fuso

    def push(self, t: float, rr_ms: float, invalid: bool = False) -> list[NN]:
        """Aggiunge un intervallo; ritorna gli intervalli classificati (con 2 battiti di ritardo)."""
        self._t.append(t)
        self._rr.append(rr_ms)
        self._forced.append(invalid)
        self._pending += 1
        out: list[NN] = []
        while self._pending >= 3:
            k = len(self._rr) - self._pending   # indice nella deque dell'intervallo da classificare
            self._pending -= 1
            if self._skip_next:
                self._skip_next = False
                continue
            out.extend(self._classify(k))
        return out

    def flush(self) -> list[NN]:
        """Fine registrazione: emette gli ultimi intervalli senza classificarli."""
        out = []
        n = len(self._rr)
        for k in range(n - self._pending, n):
            if not self._skip_next:
                out.append(NN(self._t[k], self._rr[k], self._rr[k], INVALID if self._forced[k] else NORMAL))
            self._skip_next = False
        self._pending = 0
        return out

    def _classify(self, k: int) -> list[NN]:
        rr = np.fromiter(self._rr, dtype=float)
        t, x = self._t[k], rr[k]
        if self._forced[k]:
            return [NN(t, x, float(np.median(rr[max(0, k - 5):k + 1])), INVALID)]
        if k < WARMUP:
            # Poca storia: quartile deviation inaffidabile. Si scartano solo gli errori grossolani.
            med = float(np.median(rr[:k + 3]))
            if abs(x - med) > 0.3 * med:
                return [NN(t, x, med, LONGSHORT)]
            return [NN(t, x, x, NORMAL)]

        drr = np.diff(rr, prepend=rr[0])
        hist = slice(max(1, k - QD_WINDOW + 3), k + 3)
        th1 = max(ALPHA * _qd(np.abs(drr[hist])), MIN_TH1_MS)
        drrs = drr / th1

        med = float(np.median(rr[max(0, k - MED_WINDOW + 3):k + 3]))
        lo = max(0, k - QD_WINDOW + 3)
        meds = np.array([np.median(rr[max(0, j - MED_WINDOW + 3):j + 3]) for j in range(lo, k + 1)])
        mrr_hist = rr[lo:k + 1] - meds
        mrr_hist = np.where(mrr_hist < 0, 2 * mrr_hist, mrr_hist)
        th2 = max(ALPHA * _qd(np.abs(mrr_hist)), MIN_TH2_MS)
        mrr = x - med
        mrrs = (2 * mrr if mrr < 0 else mrr) / th2

        d0, dm1, dp1, dp2 = drrs[k], drrs[k - 1], drrs[k + 1], drrs[k + 2]
        s12 = max(dm1, dp1) if d0 > 0 else min(dm1, dp1)
        s22 = min(dp1, dp2) if d0 >= 0 else max(dp1, dp2)

        if abs(d0) > 1:
            if (d0 > 1 and s12 < -C1 * d0 - C2) or (d0 < -1 and s12 > -C1 * d0 + C2):
                return [NN(t, x, med, ECTOPIC)]
        if not (abs(d0) > 1 or abs(mrrs) > 3):
            return [NN(t, x, x, NORMAL)]

        long_beat = d0 > 1 and s22 < -1
        short_beat = d0 < -1 and s22 > 1
        if not (long_beat or short_beat or abs(mrrs) > 3):
            return [NN(t, x, x, NORMAL)]
        # Battito mancato: l'intervallo è circa il doppio della mediana
        if long_beat and abs(x / 2 - med) < th2:
            return [NN(t - x / 2000.0, x / 2, x / 2, MISSED), NN(t, x / 2, x / 2, MISSED)]
        # Battito in più: questo intervallo + il successivo ≈ mediana → si fondono
        if short_beat and abs(x + rr[k + 1] - med) < th2:
            self._skip_next = True
            return [NN(self._t[k + 1], x + rr[k + 1], x + rr[k + 1], EXTRA)]
        return [NN(t, x, med, LONGSHORT)]
