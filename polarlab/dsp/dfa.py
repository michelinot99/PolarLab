"""Detrended Fluctuation Analysis a breve termine (α1, scale 4–16 battiti).

Peng et al. 1995. Per lo sport: Rogers B. et al. 2021 ("A New Detection Method Defining
the Aerobic Threshold for Endurance Exercise and Training Prescription Based on Fractal
Correlation Properties of Heart Rate Variability"): α1 ≈ 0.75 alla soglia aerobica
(VT1), α1 ≈ 0.5 vicino alla soglia anaerobica (VT2). Serve una finestra di ~2 minuti e
una quota di artefatti inferiore al 3–5%: per questo si usano NN corretti e se ne
riporta la percentuale.
"""

from __future__ import annotations

import numpy as np

AET_ALPHA1 = 0.75
ANT_ALPHA1 = 0.50


def fluctuation(rr: np.ndarray, scales: np.ndarray) -> np.ndarray:
    """F(n) per ogni scala: RMS dei residui dopo detrend lineare in finestre non sovrapposte.

    Le finestre si prendono sia dall'inizio sia dalla fine della serie, così si usano
    tutti i campioni anche quando la lunghezza non è multipla di n.
    """
    y = np.cumsum(rr - np.mean(rr))
    out = np.full(len(scales), np.nan)
    for i, n in enumerate(scales):
        nseg = len(y) // n
        if nseg < 2:
            continue
        segs = np.concatenate([y[: nseg * n].reshape(nseg, n), y[len(y) - nseg * n:].reshape(nseg, n)])
        x = np.arange(n) - (n - 1) / 2.0
        centered = segs - segs.mean(axis=1, keepdims=True)
        slope = centered @ x / (x @ x)
        resid = centered - slope[:, None] * x
        out[i] = np.sqrt(np.mean(resid * resid))
    return out


def dfa_alpha(rr: np.ndarray, n_min: int = 4, n_max: int = 16) -> float:
    rr = np.asarray(rr, dtype=float)
    if len(rr) < 2 * n_max:
        return float("nan")
    scales = np.arange(n_min, n_max + 1)
    f = fluctuation(rr, scales)
    ok = np.isfinite(f) & (f > 0)
    if ok.sum() < 3:
        return float("nan")
    return float(np.polyfit(np.log(scales[ok]), np.log(f[ok]), 1)[0])


def alpha1_zone(alpha1: float) -> tuple[str, str]:
    """(etichetta breve, descrizione) della zona di intensità."""
    if not np.isfinite(alpha1):
        return "—", "servono ~2 min di battiti puliti"
    if alpha1 >= AET_ALPHA1:
        return "aerobico", "sotto la soglia aerobica (VT1)"
    if alpha1 >= ANT_ALPHA1:
        return "tra soglie", "tra VT1 e VT2"
    return "sopra VT2", "oltre la soglia anaerobica"
