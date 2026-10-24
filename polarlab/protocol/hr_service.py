"""Heart Rate Measurement (0x2A37), Bluetooth SIG Heart Rate Service.

Byte 0 (flag):
    bit 0    formato HR: 0 = uint8, 1 = uint16
    bit 1    contatto rilevato (valido solo se bit 2 = 1)
    bit 2    rilevamento contatto supportato
    bit 3    Energy Expended presente (uint16, kJ)
    bit 4    intervalli RR presenti (uint16 ciascuno, unità di 1/1024 s)
"""

from __future__ import annotations

from dataclasses import dataclass, field

RR_UNIT_MS = 1000.0 / 1024.0


@dataclass(slots=True)
class HrMeasurement:
    bpm: int
    rr_ms: list[float] = field(default_factory=list)
    contact: bool | None = None       # None = la fascia non riporta il contatto
    energy_kj: int | None = None


def parse_hr_measurement(data: bytes) -> HrMeasurement:
    if len(data) < 2:
        raise ValueError(f"misura HR troppo corta: {data.hex()}")
    flags = data[0]
    i = 1
    if flags & 0x01:
        bpm = int.from_bytes(data[i:i + 2], "little")
        i += 2
    else:
        bpm = data[i]
        i += 1
    contact = bool(flags & 0x02) if flags & 0x04 else None
    energy = None
    if flags & 0x08:
        energy = int.from_bytes(data[i:i + 2], "little")
        i += 2
    rr: list[float] = []
    if flags & 0x10:
        while i + 2 <= len(data):
            rr.append(int.from_bytes(data[i:i + 2], "little") * RR_UNIT_MS)
            i += 2
    return HrMeasurement(bpm=bpm, rr_ms=rr, contact=contact, energy_kj=energy)


def encode_hr_measurement(bpm: int, rr_ms: list[float] = (), contact: bool | None = True,
                          energy_kj: int | None = None) -> bytes:
    flags = 0
    body = bytearray()
    if bpm > 255:
        flags |= 0x01
        body += int(bpm).to_bytes(2, "little")
    else:
        body.append(int(bpm))
    if contact is not None:
        flags |= 0x04 | (0x02 if contact else 0)
    if energy_kj is not None:
        flags |= 0x08
        body += int(energy_kj).to_bytes(2, "little")
    if rr_ms:
        flags |= 0x10
        for rr in rr_ms:
            body += int(round(rr / RR_UNIT_MS)).to_bytes(2, "little")
    return bytes([flags]) + bytes(body)
