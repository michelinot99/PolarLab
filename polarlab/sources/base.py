"""Interfaccia comune delle sorgenti: fascia reale, simulatore, replay.

Una sorgente produce solo `Packet` grezzi e aggiornamenti di stato; la decodifica è
sempre la stessa (protocol.decoder), qualunque sia la provenienza dei byte.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from polarlab.protocol.channels import Packet


class SourceState(Enum):
    IDLE = "inattivo"
    SCANNING = "ricerca…"
    CONNECTING = "connessione…"
    CONNECTED = "connesso"
    STREAMING = "in streaming"
    RECONNECTING = "riconnessione…"
    STOPPED = "fermo"
    ERROR = "errore"


@dataclass
class Status:
    state: SourceState
    message: str = ""
    device_name: str = ""
    address: str = ""
    info: dict = field(default_factory=dict)
    streams: dict = field(default_factory=dict)   # es. {"ECG": "130 Hz, 14 bit", "ACC": "100 Hz, ±8 G"}


PacketSink = Callable[[Packet], None]
StatusSink = Callable[[Status], None]


class SensorSource(ABC):
    kind: str = "sorgente"

    @abstractmethod
    async def run(self, on_packet: PacketSink, on_status: StatusSink, stop: asyncio.Event) -> None:
        """Produce pacchetti finché `stop` non viene impostato (gestendo da sé le riconnessioni)."""


async def sleep_or_stop(stop: asyncio.Event, seconds: float) -> bool:
    """Attende `seconds` o lo stop; ritorna True se è stato richiesto lo stop."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False
