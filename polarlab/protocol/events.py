"""Eventi tipizzati prodotti dal decoder e consumati dal motore DSP."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(slots=True)
class EcgChunk:
    t: np.ndarray        # s, orologio fascia
    uv: np.ndarray       # µV
    missing_before: int = 0


@dataclass(slots=True)
class AccChunk:
    t: np.ndarray        # s, orologio fascia
    mg: np.ndarray       # (n, 3) milli-g
    missing_before: int = 0


@dataclass(slots=True)
class HrEvent:
    host_t: float
    device_t: float      # stimato tramite ClockSync (la 0x2A37 non ha timestamp); nan se non ancora sincronizzato
    bpm: int
    rr_ms: list[float] = field(default_factory=list)
    contact: bool | None = None


@dataclass(slots=True)
class BatteryEvent:
    percent: int


@dataclass(slots=True)
class StreamStarted:
    stream: str          # "ECG" | "ACC"
    sample_rate: float
    resolution: int
    range: int | None
