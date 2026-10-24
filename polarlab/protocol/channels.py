"""Pacchetto grezzo: l'unità che passa tra sorgente, registrazione e decoder.

Ogni sorgente (BLE, simulatore, replay) produce solo `Packet`. Il decoder è unico,
quindi simulatore e replay esercitano esattamente lo stesso percorso della fascia reale.
"""

from dataclasses import dataclass
from enum import IntEnum


class Channel(IntEnum):
    HR = 1             # notifica 0x2A37
    BATTERY = 2        # lettura/notifica 0x2A19
    PMD_CP = 3         # risposta (indication) del PMD Control Point
    PMD_CP_WRITE = 4   # comando scritto da noi sul PMD Control Point
    PMD_DATA = 5       # notifica PMD Data (frame ECG/ACC)


@dataclass(slots=True, frozen=True)
class Packet:
    host_t: float      # secondi, orologio del Mac (time.time()) all'arrivo
    channel: Channel
    data: bytes
