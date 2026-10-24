"""Decoder unico: Packet grezzi → eventi tipizzati.

Mantiene lo stato del protocollo: impostazioni degli stream (apprese dai comandi START
che scriviamo), orologi per stream, sincronizzazione Mac↔fascia e statistiche.
"""

from __future__ import annotations

from dataclasses import dataclass

from polarlab.protocol import pmd
from polarlab.protocol.channels import Channel, Packet
from polarlab.protocol.events import AccChunk, BatteryEvent, EcgChunk, HrEvent, StreamStarted
from polarlab.protocol.hr_service import parse_hr_measurement
from polarlab.protocol.pmd import MeasurementType, SettingType
from polarlab.protocol.timebase import ClockSync, StreamClock


@dataclass(slots=True)
class DecoderStats:
    packets: int = 0
    bytes: int = 0
    ecg_frames: int = 0
    acc_frames: int = 0
    hr_packets: int = 0
    gaps: int = 0
    missing_samples: int = 0
    errors: int = 0
    last_error: str = ""


class StreamDecoder:
    def __init__(self):
        self.settings: dict[MeasurementType, pmd.StreamSettings] = {}
        self._start_cmds: dict[MeasurementType, bytes] = {}
        self.clocks: dict[MeasurementType, StreamClock] = {}
        self.sync = ClockSync()
        self.stats = DecoderStats()
        self.features: set[MeasurementType] = set()

    def feed(self, packet: Packet) -> list:
        self.stats.packets += 1
        self.stats.bytes += len(packet.data)
        try:
            return self._dispatch(packet)
        except (ValueError, IndexError) as exc:   # PmdProtocolError è un ValueError
            self.stats.errors += 1
            self.stats.last_error = f"{packet.channel.name}: {exc}"
            return []

    def _dispatch(self, p: Packet) -> list:
        ch = p.channel
        if ch == Channel.PMD_DATA:
            return self._on_data(p)
        if ch == Channel.HR:
            m = parse_hr_measurement(p.data)
            self.stats.hr_packets += 1
            # Senza frame PMD non c'è ancora un orologio comune: istante sconosciuto
            dev_t = self.sync.to_device(p.host_t) if self.sync.offset is not None else float("nan")
            return [HrEvent(host_t=p.host_t, device_t=dev_t, bpm=m.bpm, rr_ms=m.rr_ms, contact=m.contact)]
        if ch == Channel.BATTERY:
            return [BatteryEvent(percent=p.data[0])] if p.data else []
        if ch == Channel.PMD_CP_WRITE:
            return self._on_command(p.data)
        if ch == Channel.PMD_CP:
            if p.data and p.data[0] == pmd.FEATURES_ID:
                self.features = pmd.parse_features(p.data)
            return []
        return []

    def _on_command(self, data: bytes) -> list:
        parsed = pmd.parse_start_command(data)
        if parsed is None:
            return []
        mt, s = parsed
        st = pmd.StreamSettings.from_settings(mt, s)
        self.settings[mt] = st
        self._start_cmds[mt] = bytes(data)
        self.clocks[mt] = StreamClock(st.sample_rate)
        return [StreamStarted(stream=mt.name, sample_rate=st.sample_rate, resolution=st.resolution, range=st.range)]

    def _on_data(self, p: Packet) -> list:
        mt = MeasurementType(p.data[0])
        st = self.settings.get(mt) or pmd.DEFAULT_SETTINGS.get(mt)
        frame = pmd.parse_data_frame(p.data, st)
        n = len(frame.samples)
        if n == 0:
            return []
        clock = self.clocks.get(mt)
        if clock is None:
            clock = self.clocks[mt] = StreamClock(st.sample_rate)
        t, missing = clock.push(frame.timestamp_ns, n)
        self.sync.update(p.host_t, float(t[-1]))
        if missing > 0:
            self.stats.gaps += 1
            self.stats.missing_samples += missing
        if mt == MeasurementType.ECG:
            self.stats.ecg_frames += 1
            return [EcgChunk(t=t, uv=frame.samples[:, 0].astype(float), missing_before=missing)]
        if mt == MeasurementType.ACC:
            self.stats.acc_frames += 1
            return [AccChunk(t=t, mg=frame.samples.astype(float), missing_before=missing)]
        return []

    def config_packets(self, host_t: float) -> list[Packet]:
        """Comandi START già visti: aprono ogni registrazione, così il replay è autosufficiente."""
        return [Packet(host_t, Channel.PMD_CP_WRITE, cmd) for cmd in self._start_cmds.values()]

    def sample_rate(self, mt: MeasurementType) -> float:
        clock = self.clocks.get(mt)
        if clock is not None:
            return clock.effective_fs
        st = self.settings.get(mt) or pmd.DEFAULT_SETTINGS[mt]
        return st.sample_rate


def describe_settings(s: dict[SettingType, list[float]]) -> str:
    names = {SettingType.SAMPLE_RATE: "Hz", SettingType.RESOLUTION: "bit", SettingType.RANGE: "G"}
    return ", ".join(f"{'/'.join(str(int(v)) for v in vals)} {names.get(k, k.name)}" for k, vals in s.items())
