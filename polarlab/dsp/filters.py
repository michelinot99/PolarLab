"""Filtri IIR/FIR causali con stato, adatti allo streaming a blocchi."""

from __future__ import annotations

import math

import numpy as np
from scipy import signal


class SosFilter:
    """Filtro IIR in sezioni del secondo ordine; lo stato persiste tra un blocco e l'altro."""

    def __init__(self, sos: np.ndarray):
        self.sos = np.asarray(sos, dtype=float)
        self._zi: np.ndarray | None = None

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=float)
        if len(x) == 0:
            return x
        if self._zi is None:
            # Stato iniziale a regime per un ingresso costante: niente transitorio all'avvio
            self._zi = signal.sosfilt_zi(self.sos) * x[0]
        y, self._zi = signal.sosfilt(self.sos, x, zi=self._zi)
        return y

    def reset(self) -> None:
        self._zi = None


class FirFilter:
    def __init__(self, taps: np.ndarray):
        self.b = np.asarray(taps, dtype=float)
        self._zi = np.zeros(len(self.b) - 1)

    def process(self, x: np.ndarray) -> np.ndarray:
        y, self._zi = signal.lfilter(self.b, 1.0, np.asarray(x, dtype=float), zi=self._zi)
        return y

    def reset(self) -> None:
        self._zi[:] = 0


def bandpass_sos(fs: float, lo: float, hi: float, order: int = 2) -> np.ndarray:
    return signal.butter(order, [lo, hi], btype="bandpass", fs=fs, output="sos")


def notch_sos(fs: float, f0: float = 50.0, q: float = 30.0) -> np.ndarray:
    b, a = signal.iirnotch(f0, q, fs=fs)
    return signal.tf2sos(b, a)


class EcgFilter:
    """Filtro di visualizzazione: passa-banda 0.5–40 Hz + notch rete opzionale (50 Hz in Italia).

    Il passa-banda rimuove la deriva della linea di base (respiro, movimento) e il rumore
    muscolare ad alta frequenza mantenendo la morfologia diagnostica del QRS.
    """

    def __init__(self, fs: float, lo: float = 0.5, hi: float = 40.0, mains_hz: float = 50.0):
        self.fs = fs
        self._bp = SosFilter(bandpass_sos(fs, lo, min(hi, 0.45 * fs)))
        self._notch = SosFilter(notch_sos(fs, mains_hz)) if mains_hz < fs / 2 else None
        self.notch_enabled = False

    def process(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Ritorna (passa-banda, visualizzato). Il primo alimenta il rilevatore QRS."""
        bp = self._bp.process(x)
        notched = self._notch.process(bp) if self._notch is not None else bp
        return bp, (notched if self.notch_enabled else bp)

    def reset(self) -> None:
        self._bp.reset()
        if self._notch is not None:
            self._notch.reset()


def zero_phase_ecg(x: np.ndarray, fs: float, lo: float = 0.5, hi: float = 40.0) -> np.ndarray:
    """Versione a fase zero (offline): nessuna distorsione della morfologia."""
    return signal.sosfiltfilt(bandpass_sos(fs, lo, min(hi, 0.45 * fs)), x)


class UniformResampler:
    """Riporta uno stream su una griglia uniforme a `fs`, usando i timestamp dei campioni.

    Il quarzo della fascia non va esattamente alla frequenza dichiarata (la H10 a "100 Hz"
    di accelerometro ne produce ~101.9): filtri e stime costruiti su `fs` nominale
    sbaglierebbero della stessa percentuale. L'interpolazione lineare basta per segnali
    con banda molto sotto fs/2 (movimento, respiro).
    """

    def __init__(self, fs: float):
        self.dt = 1.0 / fs
        self.reset()

    def reset(self) -> None:
        self._t0: float | None = None      # istante del primo punto della griglia
        self._k = 0                        # indice del prossimo punto da emettere
        self._t_prev: float | None = None
        self._x_prev: np.ndarray | None = None

    def push(self, t: np.ndarray, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        t, x = np.asarray(t, dtype=float), np.asarray(x, dtype=float)
        if len(t) == 0:
            return t, x
        if self._t_prev is not None:
            # L'ultimo campione del blocco precedente fa da ponte per interpolare
            t, x = np.concatenate([[self._t_prev], t]), np.concatenate([self._x_prev, x])
        if self._t0 is None:
            self._t0 = float(t[0])
        n_end = int(math.floor((t[-1] - self._t0) / self.dt + 1e-9)) + 1
        grid = self._t0 + self.dt * np.arange(self._k, max(self._k, n_end))
        self._k = max(self._k, n_end)
        self._t_prev, self._x_prev = float(t[-1]), x[-1:]
        if x.ndim == 1:
            return grid, np.interp(grid, t, x)
        return grid, np.column_stack([np.interp(grid, t, x[:, j]) for j in range(x.shape[1])])
