"""Rilevatore QRS in tempo reale: Pan & Tompkins (1985) adattato a 130 Hz.

Catena: passa-banda 5–15 Hz → derivata a 5 punti → quadrato → integrazione su finestra
mobile di 150 ms (MWI). Sui picchi della MWI si applicano le doppie soglie adattive
(SPKI/NPKI), il periodo refrattario, la discriminazione dell'onda T in base alla pendenza
e la search-back per i battiti mancati.

Il picco R viene poi localizzato sul segnale ECG filtrato (lo stesso che si visualizza)
con interpolazione parabolica: a 130 Hz un campione vale 7.7 ms, troppo per l'HRV
(l'RMSSD in corsa è spesso sotto i 10 ms). L'interpolazione porta l'errore a ~1 ms.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

from polarlab.core.ringbuffer import Ring
from polarlab.dsp.filters import FirFilter, SosFilter, bandpass_sos


@dataclass(slots=True)
class Beat:
    index: float          # posizione sub-campione (indice assoluto nello stream ECG)
    amplitude: float      # µV sul segnale filtrato, con segno
    searchback: bool = False


class QRSDetector:
    def __init__(self, fs: float = 130.0, learning_s: float = 2.0):
        self.fs = fs
        self._bp = SosFilter(bandpass_sos(fs, 5.0, 15.0))
        self._deriv = FirFilter(np.array([2.0, 1.0, 0.0, -1.0, -2.0]) * fs / 8.0)
        self._mwi_n = max(1, round(0.150 * fs))
        self._mwi = FirFilter(np.ones(self._mwi_n) / self._mwi_n)

        cap = int(4 * fs)
        self._x = Ring(cap)        # ECG filtrato per la localizzazione
        self._slope = Ring(cap)    # |derivata| per la discriminazione dell'onda T

        self._learn_n = int(learning_s * fs)
        self._refractory = int(0.200 * fs)
        self._t_wave_win = int(0.360 * fs)
        self._lookahead = int(0.200 * fs)
        self._search_lo = int(0.300 * fs)   # finestra di ricerca R prima del picco MWI
        self._relearn_after = int(3.0 * fs)
        self.reset()

    def reset(self) -> None:
        self._bp.reset()
        self._deriv.reset()
        self._mwi.reset()
        self._x.clear()
        self._slope.clear()
        self._n = 0
        self._begin_learning(0)
        self._last_qrs: int | None = None
        self._last_slope = 0.0
        self._rr_recent: deque[int] = deque(maxlen=8)
        self._rr_selected: deque[int] = deque(maxlen=8)
        self._rr_avg2: float | None = None
        self._noise_peaks: list[tuple[int, float]] = []
        self.polarity = 1.0
        self._polarity_votes = 0.0

    def _begin_learning(self, at: int) -> None:
        self._learning_until = at + self._learn_n
        self._quiet_since = self._learning_until
        self._learn_max = 0.0
        self._learn_sum = 0.0
        self._learn_count = 0
        self.spki = 0.0
        self.npki = 0.0
        self._cand_idx = -1
        self._cand_val = 0.0
        self._prev_val = 0.0

    @property
    def threshold1(self) -> float:
        return self.npki + 0.25 * (self.spki - self.npki)

    @property
    def threshold2(self) -> float:
        return 0.5 * self.threshold1

    # ------------------------------------------------------------------ streaming

    def push(self, x: np.ndarray) -> list[Beat]:
        """`x`: ECG già filtrato 0.5–40 Hz (µV). Ritorna i battiti confermati in questo blocco."""
        x = np.asarray(x, dtype=float)
        if len(x) == 0:
            return []
        n0 = self._n
        self._x.extend(x)
        d = self._deriv.process(self._bp.process(x))
        self._slope.extend(np.abs(d))
        mwi = self._mwi.process(d * d)
        self._n += len(x)

        beats: list[Beat] = []
        for i, v in enumerate(mwi):
            idx = n0 + i
            if idx < self._learning_until:
                self._learn(idx, v)
                continue
            # Ricerca dei massimi locali con 200 ms di "lookahead"
            if v > self._cand_val and (self._cand_idx >= 0 or v > self._prev_val):
                self._cand_idx, self._cand_val = idx, v
            elif self._cand_idx >= 0 and idx - self._cand_idx >= self._lookahead:
                beat = self._classify(self._cand_idx, self._cand_val)
                if beat is not None:
                    beats.append(beat)
                self._cand_idx, self._cand_val = -1, 0.0
            self._prev_val = v

            beat = self._maybe_searchback(idx)
            if beat is not None:
                beats.append(beat)
            if idx - self._quiet_since > self._relearn_after:
                # Nessun battito da 3 s (contatto perso, ampiezza cambiata): riapprendi le soglie
                self._begin_learning(idx + 1)
                self._last_qrs = None
                self._rr_avg2 = None
                self._noise_peaks.clear()
        beats.sort(key=lambda b: b.index)
        return beats

    def _learn(self, idx: int, v: float) -> None:
        self._learn_max = max(self._learn_max, v)
        self._learn_sum += v
        self._learn_count += 1
        if idx == self._learning_until - 1:
            self._quiet_since = idx
            self.spki = self._learn_max / 3.0
            self.npki = 0.5 * self._learn_sum / max(1, self._learn_count)
            seg = self._x.last(min(self._learn_n, len(self._x)))
            if len(seg):
                hi, lo = np.percentile(seg, 99.5), np.percentile(seg, 0.5)
                self.polarity = 1.0 if hi >= -lo else -1.0

    # ------------------------------------------------------------------ classificazione

    def _classify(self, idx: int, val: float) -> Beat | None:
        if self._last_qrs is not None and idx - self._last_qrs < self._refractory:
            self.npki = 0.125 * val + 0.875 * self.npki
            return None
        if val > self.threshold1:
            slope = self._max_slope(idx)
            if (self._last_qrs is not None and idx - self._last_qrs < self._t_wave_win
                    and slope < 0.5 * self._last_slope):
                # Probabile onda T: pendenza troppo bassa rispetto al QRS precedente
                self.npki = 0.125 * val + 0.875 * self.npki
                return None
            self.spki = 0.125 * val + 0.875 * self.spki
            return self._accept(idx, slope, searchback=False)
        self.npki = 0.125 * val + 0.875 * self.npki
        self._noise_peaks.append((idx, val))
        if len(self._noise_peaks) > 64:
            del self._noise_peaks[:-64]
        return None

    def _maybe_searchback(self, now: int) -> Beat | None:
        if self._rr_avg2 is None or self._last_qrs is None:
            return None
        if now - self._last_qrs <= 1.66 * self._rr_avg2:
            return None
        cands = [(i, v) for i, v in self._noise_peaks
                 if v > self.threshold2 and i - self._last_qrs >= self._refractory]
        if not cands:
            return None
        idx, val = max(cands, key=lambda c: c[1])
        self.spki = 0.25 * val + 0.75 * self.spki
        return self._accept(idx, self._max_slope(idx), searchback=True)

    def _accept(self, idx: int, slope: float, searchback: bool) -> Beat | None:
        if self._last_qrs is not None:
            rr = idx - self._last_qrs
            self._rr_recent.append(rr)
            if self._rr_avg2 is None or 0.92 * self._rr_avg2 <= rr <= 1.16 * self._rr_avg2:
                self._rr_selected.append(rr)
                self._rr_avg2 = float(np.mean(self._rr_selected))
            elif len(self._rr_recent) == self._rr_recent.maxlen and all(
                    not (0.92 * self._rr_avg2 <= r <= 1.16 * self._rr_avg2) for r in self._rr_recent):
                # Ritmo cambiato bruscamente: riparti dalla media recente
                self._rr_selected = deque(self._rr_recent, maxlen=8)
                self._rr_avg2 = float(np.mean(self._rr_recent))
        self._last_qrs = idx
        self._quiet_since = idx
        self._last_slope = slope
        self._noise_peaks = [(i, v) for i, v in self._noise_peaks if i > idx]
        return self._locate_r(idx, searchback)

    def _max_slope(self, idx: int) -> float:
        a = max(self._slope.start, idx - self._mwi_n)
        b = min(self._slope.end, idx + 1)
        return float(np.max(self._slope.get(a, b))) if b > a else 0.0

    def _locate_r(self, idx: int, searchback: bool) -> Beat | None:
        a = max(self._x.start, idx - self._search_lo)
        b = min(self._x.end, idx + 1)
        if b - a < 3:
            return None
        seg = self._x.get(a, b)
        s = seg * self.polarity
        k = int(np.argmax(s))
        # Voto di polarità: se l'estremo opposto è stabilmente più ampio, il QRS è invertito
        self._polarity_votes = 0.9 * self._polarity_votes + (1.0 if -s.min() > 1.4 * s.max() else 0.0)
        if self._polarity_votes > 5.0:
            self.polarity *= -1.0
            self._polarity_votes = 0.0
        offset = 0.0
        if 0 < k < len(s) - 1:
            y0, y1, y2 = s[k - 1], s[k], s[k + 1]
            if min(y0, y1, y2) > 0:
                # Interpolazione gaussiana (parabola sui logaritmi): esatta per un picco
                # gaussiano, meno distorta della parabola semplice quando il QRS è stretto
                y0, y1, y2 = np.log(y0), np.log(y1), np.log(y2)
            den = y0 - 2.0 * y1 + y2
            if den < 0:
                offset = float(np.clip(0.5 * (y0 - y2) / den, -0.5, 0.5))
        return Beat(index=a + k + offset, amplitude=float(seg[k]), searchback=searchback)


def detect_offline(x: np.ndarray, fs: float, block: int = 73) -> list[Beat]:
    """Esegue il rilevatore in streaming su un intero segnale (benchmark e test)."""
    det = QRSDetector(fs)
    out: list[Beat] = []
    for i in range(0, len(x), block):
        out.extend(det.push(x[i:i + block]))
    return out
