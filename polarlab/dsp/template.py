"""Battito medio (ensemble average allineato sul picco R) e correlazione battito-template.

La media su molti battiti allineati abbatte il rumore non correlato (√N) e rende
visibile la morfologia P-QRS-T anche durante la corsa. La correlazione di ogni nuovo
battito con il template è un ottimo indice di qualità del segnale.
"""

from __future__ import annotations

from collections import deque

import numpy as np

PRE_S = 0.25
POST_S = 0.45


class BeatTemplate:
    def __init__(self, fs: float, init_beats: int = 8, alpha: float = 0.08, min_corr: float = 0.7):
        self.fs = fs
        self.pre = int(round(PRE_S * fs))
        self.post = int(round(POST_S * fs))
        self.length = self.pre + self.post + 1
        self.t_axis = (np.arange(self.length) - self.pre) / fs
        self.init_beats = init_beats
        self.alpha = alpha
        self.min_corr = min_corr
        self.reset()

    def reset(self) -> None:
        self.mean: np.ndarray | None = None
        self._count = 0
        self.recent: deque[np.ndarray] = deque(maxlen=12)

    def segment(self, x: np.ndarray, r_index_in_x: float) -> np.ndarray | None:
        """Estrae il segmento centrato sul picco R con allineamento sub-campione."""
        pos = r_index_in_x + np.arange(-self.pre, self.post + 1)
        if pos[0] < 0 or pos[-1] > len(x) - 1:
            return None
        return np.interp(pos, np.arange(len(x)), x)

    def add(self, seg: np.ndarray) -> float:
        """Aggiorna il template; ritorna la correlazione del battito col template precedente."""
        self.recent.append(seg)
        if self.mean is None:
            self.mean = seg.copy()
            self._count = 1
            return float("nan")
        corr = float(np.corrcoef(seg, self.mean)[0, 1])
        if self._count < self.init_beats:
            self._count += 1
            self.mean += (seg - self.mean) / self._count
        elif corr >= self.min_corr:
            self.mean += self.alpha * (seg - self.mean)
        return corr
