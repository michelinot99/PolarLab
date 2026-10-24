"""Indice di qualità del segnale (0–100) da più fonti indipendenti."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

SATURATION_UV = 20_000.0


@dataclass(slots=True)
class Quality:
    score: float                 # 0–100
    label: str
    template_corr: float         # mediana della correlazione battito-template
    strap_mae_ms: float          # |RR nostro − RR fascia| medio dopo allineamento
    strap_match_pct: float       # % di battiti entro 10 ms
    contact: bool | None
    flat_or_saturated: bool


def align_rr(ours: np.ndarray, strap: np.ndarray, max_lag: int = 6) -> tuple[float, float]:
    """Confronta due serie RR senza timestamp comuni cercando lo sfasamento (in battiti) migliore.

    La 0x2A37 non ha timestamp e arriva ~1 s dopo: si allineano le sequenze con il
    lag che minimizza l'errore medio. Ritorna (MAE in ms, % entro 10 ms).
    """
    best = (float("nan"), float("nan"))
    if len(ours) < 8 or len(strap) < 8:
        return best
    best_mae = np.inf
    for lag in range(-max_lag, max_lag + 1):
        # lag > 0: i nostri battiti più recenti non sono ancora arrivati dalla fascia
        a = ours[: len(ours) - lag] if lag > 0 else ours
        b = strap[: len(strap) + lag] if lag < 0 else strap
        n = min(len(a), len(b))
        if n < 6:
            continue
        a, b = a[-n:], b[-n:]
        err = np.abs(a - b)
        mae = float(np.mean(err))
        if mae < best_mae:
            best_mae = mae
            best = (mae, float(np.mean(err <= 10.0) * 100.0))
    return best


class QualityMonitor:
    def __init__(self):
        self.corrs: deque[float] = deque(maxlen=20)
        self.ours: deque[float] = deque(maxlen=40)
        self.strap: deque[float] = deque(maxlen=40)
        self.contact: bool | None = None
        self._flat = False

    def push_corr(self, c: float) -> None:
        if np.isfinite(c):
            self.corrs.append(c)

    def push_ecg_second(self, x: np.ndarray) -> None:
        if len(x):
            self._flat = bool(np.std(x) < 5.0 or np.mean(np.abs(x) > SATURATION_UV) > 0.05)

    def evaluate(self) -> Quality:
        corr = float(np.median(self.corrs)) if self.corrs else float("nan")
        mae, match = align_rr(np.asarray(self.ours), np.asarray(self.strap))
        parts, weights = [], []
        if np.isfinite(corr):
            parts.append(np.clip((corr - 0.5) / 0.45, 0, 1))
            weights.append(0.5)
        if np.isfinite(match):
            parts.append(match / 100.0)
            weights.append(0.35)
        if not parts:
            # Nessun battito ancora valutato: niente punteggio, solo eventuale allarme contatto
            bad = self._flat or self.contact is False
            return Quality(score=0.0 if bad else float("nan"), label="inutilizzabile" if bad else "—",
                           template_corr=corr, strap_mae_ms=mae, strap_match_pct=match,
                           contact=self.contact, flat_or_saturated=self._flat)
        parts.append(0.0 if self._flat or self.contact is False else 1.0)
        weights.append(0.15)
        score = 100.0 * float(np.average(parts, weights=weights))
        if self._flat or self.contact is False:
            score = min(score, 10.0)
        label = "ottima" if score >= 85 else "buona" if score >= 65 else "scarsa" if score >= 35 else "inutilizzabile"
        return Quality(score=score, label=label, template_corr=corr, strap_mae_ms=mae, strap_match_pct=match,
                       contact=self.contact, flat_or_saturated=self._flat)
