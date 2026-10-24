"""Polar Measurement Data (PMD): comandi del Control Point e decodifica dei frame dati.

Riferimento: Polar BLE SDK (technical_documentation/Polar_Measurement_Data_Specification.pdf)
e docs/PROTOCOL.md in questo progetto, con esempi esadecimali.

Formato di un frame PMD Data:
    [0]     tipo di misura (0x00 ECG, 0x02 ACC, ...)
    [1..8]  timestamp uint64 LE, ns, riferito all'ULTIMO campione del frame
    [9]     tipo di frame: bit 7 = compresso (delta), bit 0..6 = formato del campione
    [10..]  payload
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from enum import IntEnum

import numpy as np


class MeasurementType(IntEnum):
    ECG = 0
    PPG = 1
    ACC = 2
    PPI = 3
    GYRO = 5
    MAG = 6


class ControlOp(IntEnum):
    GET_SETTINGS = 0x01
    START = 0x02
    STOP = 0x03


class SettingType(IntEnum):
    SAMPLE_RATE = 0
    RESOLUTION = 1
    RANGE = 2
    RANGE_MILLIUNIT = 3
    CHANNELS = 4
    FACTOR = 5


# Dimensione in byte di un singolo valore per ogni tipo di impostazione
SETTING_VALUE_SIZE = {
    SettingType.SAMPLE_RATE: 2,
    SettingType.RESOLUTION: 2,
    SettingType.RANGE: 2,
    SettingType.RANGE_MILLIUNIT: 4,
    SettingType.CHANNELS: 1,
    SettingType.FACTOR: 4,
}


class PmdError(IntEnum):
    SUCCESS = 0
    INVALID_OP_CODE = 1
    INVALID_MEASUREMENT_TYPE = 2
    NOT_SUPPORTED = 3
    INVALID_LENGTH = 4
    INVALID_PARAMETER = 5
    ALREADY_IN_STATE = 6
    INVALID_RESOLUTION = 7
    INVALID_SAMPLE_RATE = 8
    INVALID_RANGE = 9
    INVALID_MTU = 10
    INVALID_NUMBER_OF_CHANNELS = 11
    INVALID_STATE = 12
    DEVICE_IN_CHARGER = 13


FEATURES_ID = 0x0F          # primo byte della lettura del Control Point
CONTROL_RESPONSE_ID = 0xF0  # primo byte di ogni risposta del Control Point
COMPRESSED_FLAG = 0x80
HEADER_SIZE = 10

# Numero di canali per tipo di misura
CHANNELS = {MeasurementType.ECG: 1, MeasurementType.ACC: 3, MeasurementType.GYRO: 3, MeasurementType.MAG: 3}


class PmdProtocolError(ValueError):
    pass


# --------------------------------------------------------------------------- comandi

def parse_features(data: bytes) -> set[MeasurementType]:
    """Lettura del Control Point → tipi di misura supportati (bitmask nel byte 1)."""
    if len(data) < 2 or data[0] != FEATURES_ID:
        raise PmdProtocolError(f"risposta features inattesa: {data.hex()}")
    mask = int.from_bytes(data[1:3], "little")
    return {mt for mt in MeasurementType if mask & (1 << mt.value)}


def build_get_settings(mt: MeasurementType) -> bytes:
    return bytes([ControlOp.GET_SETTINGS, mt])


def build_start(mt: MeasurementType, settings: dict[SettingType, int]) -> bytes:
    """Comando di avvio: op, tipo, poi TLV [tipo impostazione][count=1][valore LE]."""
    out = bytearray([ControlOp.START, mt])
    for st in sorted(settings):
        out += bytes([st, 1]) + int(settings[st]).to_bytes(SETTING_VALUE_SIZE[st], "little")
    return bytes(out)


def build_stop(mt: MeasurementType) -> bytes:
    return bytes([ControlOp.STOP, mt])


def parse_settings_tlv(data: bytes) -> dict[SettingType, list[float]]:
    """TLV delle impostazioni (risposta a GET_SETTINGS o parametri di START)."""
    out: dict[SettingType, list[float]] = {}
    i = 0
    while i + 2 <= len(data):
        try:
            st = SettingType(data[i])
        except ValueError:
            raise PmdProtocolError(f"tipo di impostazione sconosciuto 0x{data[i]:02x} in {data.hex()}") from None
        count = data[i + 1]
        size = SETTING_VALUE_SIZE[st]
        i += 2
        if i + count * size > len(data):
            raise PmdProtocolError(f"TLV troncato: {data.hex()}")
        values: list[float] = []
        for _ in range(count):
            raw = data[i:i + size]
            values.append(struct.unpack("<f", raw)[0] if st == SettingType.FACTOR else int.from_bytes(raw, "little"))
            i += size
        out[st] = values
    return out


def parse_start_command(data: bytes) -> tuple[MeasurementType, dict[SettingType, int]] | None:
    """Ricava le impostazioni da un comando START che abbiamo scritto (serve al decoder e al replay)."""
    if len(data) < 2 or data[0] != ControlOp.START:
        return None
    tlv = parse_settings_tlv(data[2:])
    return MeasurementType(data[1]), {k: int(v[0]) for k, v in tlv.items()}


@dataclass(slots=True)
class ControlResponse:
    op: int
    meas_type: int
    error: PmdError
    more_frames: bool
    params: bytes

    @property
    def ok(self) -> bool:
        return self.error in (PmdError.SUCCESS, PmdError.ALREADY_IN_STATE)

    def settings(self) -> dict[SettingType, list[float]]:
        return parse_settings_tlv(self.params) if self.op == ControlOp.GET_SETTINGS else {}


def parse_control_response(data: bytes) -> ControlResponse:
    """Risposta del Control Point: F0 <op> <tipo> <errore> [<more>] [parametri]."""
    if len(data) < 4 or data[0] != CONTROL_RESPONSE_ID:
        raise PmdProtocolError(f"risposta Control Point inattesa: {data.hex()}")
    try:
        error = PmdError(data[3])
    except ValueError:
        raise PmdProtocolError(f"codice di errore sconosciuto {data[3]}") from None
    more = len(data) > 4 and data[4] != 0
    return ControlResponse(op=data[1], meas_type=data[2], error=error, more_frames=more, params=bytes(data[5:]))


def choose_settings(available: dict[SettingType, list[float]],
                    wanted: dict[SettingType, int]) -> dict[SettingType, int]:
    """Per ogni impostazione disponibile sceglie il valore più vicino a quello desiderato."""
    chosen: dict[SettingType, int] = {}
    for st, values in available.items():
        if st == SettingType.FACTOR or not values:
            continue
        target = wanted.get(st)
        best = values[0] if target is None else min(values, key=lambda v: abs(v - target))
        chosen[st] = int(best)
    return chosen


# --------------------------------------------------------------------------- frame dati

@dataclass(slots=True)
class StreamSettings:
    sample_rate: float
    resolution: int = 16
    range: int | None = None
    channels: int = 1
    factor: float = 1.0

    @classmethod
    def from_settings(cls, mt: MeasurementType, s: dict[SettingType, int]) -> "StreamSettings":
        return cls(
            sample_rate=float(s.get(SettingType.SAMPLE_RATE, DEFAULT_SETTINGS[mt].sample_rate)),
            resolution=int(s.get(SettingType.RESOLUTION, DEFAULT_SETTINGS[mt].resolution)),
            range=s.get(SettingType.RANGE),
            channels=int(s.get(SettingType.CHANNELS, CHANNELS.get(mt, 1))),
        )


DEFAULT_SETTINGS = {
    MeasurementType.ECG: StreamSettings(sample_rate=130, resolution=14, channels=1),
    MeasurementType.ACC: StreamSettings(sample_rate=200, resolution=16, range=8, channels=3),
}


@dataclass(slots=True)
class DataFrame:
    meas_type: MeasurementType
    timestamp_ns: int          # istante dell'ultimo campione, orologio della fascia
    frame_type: int
    compressed: bool
    samples: np.ndarray = field(repr=False)   # (n, canali), interi nell'unità nativa (µV o mG)


def _signed(values: np.ndarray, bits: int) -> np.ndarray:
    values = values.astype(np.int64)
    sign = 1 << (bits - 1)
    return (values ^ sign) - sign


def _decode_le(payload: bytes, size: int) -> np.ndarray:
    """Interi little-endian con segno di `size` byte (1, 2, 3 o 4)."""
    n = len(payload) // size
    raw = np.frombuffer(payload[: n * size], dtype=np.uint8).reshape(n, size).astype(np.int64)
    value = np.zeros(n, dtype=np.int64)
    for k in range(size):
        value |= raw[:, k] << (8 * k)
    return _signed(value, 8 * size)


def _decode_raw(payload: bytes, sample_bytes: int, channels: int) -> np.ndarray:
    step = sample_bytes * channels
    if len(payload) % step:
        raise PmdProtocolError(f"payload di {len(payload)} byte non multiplo di {step}")
    return _decode_le(payload, sample_bytes).reshape(-1, channels)


def _decode_delta(payload: bytes, resolution: int, channels: int) -> np.ndarray:
    """Frame compressi: campione di riferimento + blocchi di delta impacchettati a bit (LSB first)."""
    ref_size = math.ceil(resolution / 8)
    ref_len = ref_size * channels
    if len(payload) < ref_len:
        raise PmdProtocolError("frame delta senza campione di riferimento")
    ref = _decode_le(payload[:ref_len], ref_size)
    out = [ref.reshape(1, channels)]
    i = ref_len
    while i + 2 <= len(payload):
        bits, count = payload[i], payload[i + 1]
        i += 2
        nbytes = math.ceil(bits * channels * count / 8)
        if i + nbytes > len(payload) or bits == 0:
            raise PmdProtocolError("blocco delta troncato o non valido")
        packed = np.frombuffer(payload[i:i + nbytes], dtype=np.uint8)
        flat = np.unpackbits(packed, bitorder="little")[: bits * channels * count]
        weights = 1 << np.arange(bits, dtype=np.int64)
        deltas = _signed(flat.reshape(-1, bits).astype(np.int64) @ weights, bits).reshape(count, channels)
        out.append(out[-1][-1] + np.cumsum(deltas, axis=0))
        i += nbytes
    return np.concatenate(out, axis=0)


def parse_data_frame(data: bytes, settings: StreamSettings | None = None) -> DataFrame:
    if len(data) < HEADER_SIZE:
        raise PmdProtocolError(f"frame troppo corto ({len(data)} byte)")
    try:
        mt = MeasurementType(data[0])
    except ValueError:
        raise PmdProtocolError(f"tipo di misura sconosciuto {data[0]}") from None
    ts = int.from_bytes(data[1:9], "little")
    ftype_byte = data[9]
    compressed = bool(ftype_byte & COMPRESSED_FLAG)
    ftype = ftype_byte & 0x7F
    payload = bytes(data[HEADER_SIZE:])
    s = settings or DEFAULT_SETTINGS.get(mt) or StreamSettings(sample_rate=0)
    channels = CHANNELS.get(mt, s.channels)

    if compressed:
        samples = _decode_delta(payload, s.resolution, channels)
    elif mt == MeasurementType.ECG:
        if ftype != 0:
            raise PmdProtocolError(f"frame ECG di tipo {ftype} non supportato")
        samples = _decode_raw(payload, 3, 1)
    elif mt in (MeasurementType.ACC, MeasurementType.GYRO, MeasurementType.MAG):
        if ftype not in (0, 1, 2):
            raise PmdProtocolError(f"frame {mt.name} di tipo {ftype} non supportato")
        samples = _decode_raw(payload, ftype + 1, channels)
    else:
        raise PmdProtocolError(f"decodifica di {mt.name} non implementata")

    if s.factor != 1.0:
        samples = np.round(samples * s.factor).astype(np.int64)
    return DataFrame(meas_type=mt, timestamp_ns=ts, frame_type=ftype, compressed=compressed, samples=samples)


# --------------------------------------------------------------------------- encoder (simulatore e test)

def encode_data_frame(mt: MeasurementType, timestamp_ns: int, samples: np.ndarray, frame_type: int | None = None) -> bytes:
    """Frame non compresso, nello stesso formato della H10 (ECG int24, ACC int16 di default)."""
    samples = np.asarray(samples, dtype=np.int64)
    if samples.ndim == 1:
        samples = samples[:, None]
    if frame_type is None:
        frame_type = 0 if mt == MeasurementType.ECG else 1
    size = 3 if mt == MeasurementType.ECG else frame_type + 1
    lo, hi = -(1 << (8 * size - 1)), (1 << (8 * size - 1)) - 1
    flat = np.clip(samples.reshape(-1), lo, hi) & ((1 << (8 * size)) - 1)
    payload = ((flat[:, None] >> (8 * np.arange(size))) & 0xFF).astype(np.uint8).tobytes()
    return bytes([mt]) + timestamp_ns.to_bytes(8, "little") + bytes([frame_type]) + payload


def encode_delta_frame(mt: MeasurementType, timestamp_ns: int, samples: np.ndarray, resolution: int,
                       frame_type: int = 1, block: int = 16) -> bytes:
    """Frame compresso a delta (usato nei test per verificare il decoder)."""
    samples = np.asarray(samples, dtype=np.int64)
    if samples.ndim == 1:
        samples = samples[:, None]
    ref_size = math.ceil(resolution / 8)
    payload = bytearray()
    for v in samples[0]:
        payload += (int(v) & ((1 << (8 * ref_size)) - 1)).to_bytes(ref_size, "little")
    deltas = np.diff(samples, axis=0)
    for start in range(0, len(deltas), block):
        chunk = deltas[start:start + block]
        peak = int(np.max(np.abs(chunk))) if chunk.size else 0
        bits = max(2, peak.bit_length() + 1)
        flat = (chunk.reshape(-1) & ((1 << bits) - 1)).astype(np.int64)
        bitarr = ((flat[:, None] >> np.arange(bits)) & 1).astype(np.uint8).reshape(-1)
        payload += bytes([bits, len(chunk)]) + np.packbits(bitarr, bitorder="little").tobytes()
    return bytes([mt]) + timestamp_ns.to_bytes(8, "little") + bytes([frame_type | COMPRESSED_FLAG]) + bytes(payload)


def encode_settings_response(mt: MeasurementType, available: dict[SettingType, list[int]]) -> bytes:
    """Risposta F0 01 ... a GET_SETTINGS, come la invia la H10."""
    out = bytearray([CONTROL_RESPONSE_ID, ControlOp.GET_SETTINGS, mt, PmdError.SUCCESS, 0])
    for st in sorted(available):
        vals = available[st]
        out += bytes([st, len(vals)])
        for v in vals:
            out += int(v).to_bytes(SETTING_VALUE_SIZE[st], "little")
    return bytes(out)


def encode_control_response(op: ControlOp, mt: MeasurementType, error: PmdError = PmdError.SUCCESS) -> bytes:
    return bytes([CONTROL_RESPONSE_ID, op, mt, error, 0])


# Impostazioni annunciate da una H10 reale (firmware 3.x/5.x)
H10_AVAILABLE = {
    MeasurementType.ECG: {SettingType.SAMPLE_RATE: [130], SettingType.RESOLUTION: [14]},
    MeasurementType.ACC: {
        SettingType.SAMPLE_RATE: [25, 50, 100, 200],
        SettingType.RESOLUTION: [16],
        SettingType.RANGE: [2, 4, 8],
    },
}
H10_FEATURES = bytes([FEATURES_ID, (1 << MeasurementType.ECG) | (1 << MeasurementType.ACC), 0x00])
