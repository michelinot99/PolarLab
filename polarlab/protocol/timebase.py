"""Base dei tempi: timestamp per campione, perdita di pacchetti, allineamento orologi.

La H10 marca ogni frame con il timestamp (ns) dell'ultimo campione. Gli istanti dei
campioni precedenti si ricostruiscono all'indietro con il periodo di campionamento
*effettivo*, stimato dai timestamp stessi: così si segue la deriva del quarzo della fascia.
"""

from __future__ import annotations

from collections import deque

import numpy as np

POLAR_EPOCH_UNIX = 946_684_800  # 2000-01-01T00:00:00Z: epoch dei timestamp Polar

# Tolleranza oltre la quale un salto di timestamp non è più deriva ma perdita di campioni
_CONTINUITY_TOL = 0.03


class StreamClock:
    """Ricostruisce gli istanti dei campioni di uno stream PMD e rileva i buchi."""

    def __init__(self, nominal_fs: float):
        self.nominal_fs = float(nominal_fs)
        self.dt = 1.0 / self.nominal_fs
        self._prev_last: float | None = None

    @property
    def effective_fs(self) -> float:
        return 1.0 / self.dt

    def reset(self) -> None:
        self._prev_last = None
        self.dt = 1.0 / self.nominal_fs

    def push(self, timestamp_ns: int, n: int) -> tuple[np.ndarray, int]:
        """Ritorna (istanti dei campioni in secondi di orologio fascia, campioni mancanti prima del frame)."""
        t_last = timestamp_ns * 1e-9
        missing = 0
        if self._prev_last is not None and n > 0:
            span = t_last - self._prev_last
            est = span / n
            if abs(est - self.dt) <= _CONTINUITY_TOL * self.dt:
                # Frame contiguo: aggiorna lentamente il periodo effettivo
                self.dt += 0.05 * (est - self.dt)
            elif span > 0:
                missing = max(0, int(round(span / self.dt)) - n)
            else:
                # Timestamp non monotono (riconnessione o reset della fascia)
                missing = -1
        self._prev_last = t_last
        t = t_last - self.dt * np.arange(n - 1, -1, -1, dtype=np.float64)
        return t, missing


class ClockSync:
    """Stima l'offset orologio Mac − orologio fascia.

    L'offset minimo osservato corrisponde al pacchetto arrivato con la latenza più
    bassa: è lo stimatore classico (stile NTP) robusto ai ritardi del Bluetooth.
    Una finestra mobile permette di seguire la deriva tra i due quarzi.
    """

    def __init__(self, window_s: float = 60.0):
        self.window_s = window_s
        self._obs: deque[tuple[float, float]] = deque()
        self.offset: float | None = None
        self.jitter_ms: float = 0.0

    def update(self, host_t: float, device_t: float) -> None:
        off = host_t - device_t
        self._obs.append((host_t, off))
        while self._obs and host_t - self._obs[0][0] > self.window_s:
            self._obs.popleft()
        offs = [o for _, o in self._obs]
        self.offset = min(offs)
        self.jitter_ms = (float(np.median(offs)) - self.offset) * 1e3

    def to_device(self, host_t: float) -> float:
        return host_t - (self.offset or 0.0)

    def to_host(self, device_t: float) -> float:
        return device_t + (self.offset or 0.0)
