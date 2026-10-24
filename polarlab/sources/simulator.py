"""Simulatore della Polar H10 a livello di byte.

Genera ECG e accelerometro realistici per uno scenario (riposo → camminata → corsa → ...)
e li impacchetta esattamente come la fascia: frame PMD con timestamp in ns, notifiche
0x2A37 con RR in 1/1024 s, risposte del Control Point. Il resto dell'app non sa di
parlare con un simulatore.

Modello fisiologico (semplificato ma con le proprietà che contano per gli algoritmi):
- RR = media dalla FC dello scenario × (1 + RSA + rumore frattale). Il rumore è una
  miscela di rumore rosa (DFA α≈1) e bianco (α≈0.5) pesata dall'intensità: così l'α1
  scende con lo sforzo come in Rogers et al. 2021.
- ECG: onde P-Q-R-S-T gaussiane (McSharry et al. 2003), intervalli scalati con √RR,
  ampiezza R modulata dal respiro, battiti ectopici ventricolari occasionali.
- Rumore: deriva respiratoria, EMG proporzionale all'intensità, artefatti di impatto a
  ogni passo, 50 Hz di rete.
- ACC: gravità su un orientamento qualunque + oscillazione verticale alla cadenza,
  oscillazione laterale alla falcata, espansione toracica respiratoria.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import numpy as np

from polarlab.protocol import pmd
from polarlab.protocol.channels import Channel, Packet
from polarlab.protocol.hr_service import encode_hr_measurement
from polarlab.protocol.pmd import MeasurementType, SettingType
from polarlab.sources.base import SensorSource, SourceState, Status, sleep_or_stop

ECG_FS = 130.0
ECG_FRAME = 73       # campioni per frame con MTU 232, come la H10
ACC_FRAME = 36


@dataclass(frozen=True)
class Phase:
    name: str
    duration_s: float
    hr: float
    intensity: float      # 0 riposo … 1 massimale
    cadence: float        # passi/min (0 = fermo)
    resp_brpm: float


DEFAULT_SCENARIO = (
    Phase("Riposo", 90, 62, 0.0, 0, 12),
    Phase("Camminata", 60, 98, 0.15, 112, 18),
    Phase("Corsa facile", 240, 142, 0.45, 166, 28),
    Phase("Ritmo soglia", 150, 168, 0.85, 178, 40),
    Phase("Defaticamento", 90, 118, 0.3, 120, 24),
)


class _PinkNoise:
    """Rumore 1/f come somma di processi AR(1) con costanti di tempo in ottave."""

    def __init__(self, rng: np.random.Generator, octaves: int = 8):
        self.r = 1.0 - 2.0 ** -np.arange(1, octaves + 1)
        self.k = np.sqrt(1.0 - self.r ** 2)
        self.state = rng.standard_normal(octaves)
        self.rng = rng

    def next(self) -> float:
        self.state = self.r * self.state + self.k * self.rng.standard_normal(len(self.r))
        return float(self.state.sum() / np.sqrt(len(self.r)))


def _wave(t: np.ndarray, center: float, width: float, amp: float) -> np.ndarray:
    return amp * np.exp(-0.5 * ((t - center) / width) ** 2)


class H10Simulator:
    def __init__(self, seed: int = 7, scenario=DEFAULT_SCENARIO, acc_fs: float = 100.0,
                 acc_true_fs: float | None = None, ectopic_rate: float = 0.004, drop_rate: float = 0.0, loop: bool = True,
                 device_t0: float = 813_000_000.0, host_t0: float | None = None):
        self.rng = np.random.default_rng(seed)
        self.scenario = scenario
        self.acc_fs = acc_fs
        # Frequenza reale del quarzo: la H10 a "100 Hz" di accelerometro ne produce ~101.9
        self.acc_true_fs = acc_fs if acc_true_fs is None else acc_true_fs
        self.ectopic_rate = ectopic_rate
        self.drop_rate = drop_rate
        self.loop = loop
        self.dev_t0 = device_t0
        self.host_t0 = time.time() if host_t0 is None else host_t0
        self.t = 0.0                       # tempo simulato dall'inizio (s)
        self._pink = _PinkNoise(self.rng)
        p0 = scenario[0]
        self._hr, self._int, self._cad, self._resp = p0.hr, p0.intensity, p0.cadence, p0.resp_brpm
        # Fasi di respiro e passo come funzioni continue del tempo (ribasate a ogni advance)
        self._tref, self._rp0, self._sp0 = 0.0, 0.0, 0.0
        self._rf, self._sf = self._resp / 60.0, self._cad / 60.0
        self.beats: list[tuple[float, float, bool]] = []   # (t R, rr, ectopico) generati
        self.true_r: list[float] = []                       # istanti R (orologio fascia)
        self._next_beat = 0.6
        self._compensatory = False
        self._ecg_n = 0
        self._acc_n = 0
        self._ecg_buf: list[float] = []
        self._acc_buf: list[np.ndarray] = []
        self._hr_next = 1.0
        self._hr_last_beat = 0
        self._up = np.array([-0.25, -0.93, 0.27])
        self._up /= np.linalg.norm(self._up)
        self._fwd = np.cross(self._up, [1.0, 0.0, 0.0])
        self._fwd /= np.linalg.norm(self._fwd)
        self._lat = np.cross(self._up, self._fwd)

    # ------------------------------------------------------------------ scenario

    def phase_at(self, t: float) -> Phase:
        total = sum(p.duration_s for p in self.scenario)
        tt = t % total if self.loop else min(t, total - 1e-6)
        for p in self.scenario:
            if tt < p.duration_s:
                return p
            tt -= p.duration_s
        return self.scenario[-1]

    def _relax(self, t: float, dt: float) -> None:
        p = self.phase_at(t)
        a = 1.0 - np.exp(-dt / 25.0)
        self._hr += a * (p.hr - self._hr)
        self._int += a * (p.intensity - self._int)
        self._cad += min(1.0, 4 * a) * (p.cadence - self._cad)
        self._resp += a * (p.resp_brpm - self._resp)

    # ------------------------------------------------------------------ battiti

    def _generate_beats_until(self, t_end: float) -> None:
        while self._next_beat < t_end:
            tb = self._next_beat
            prev_t = self.beats[-1][0] if self.beats else tb
            self._relax(tb, self.beats[-1][1] if self.beats else 0.0)
            rr_mean = 60.0 / self._hr
            I = self._int
            rsa = (0.05 * (1 - I) ** 2 + 0.003) * np.sin(self._resp_phase(tb))
            sd = 0.03 * (1 - I) ** 1.5 + 0.004
            noise = sd * ((1 - I) * self._pink.next() + (0.15 + 0.9 * I * I) * self.rng.standard_normal())
            rr = rr_mean * (1.0 + rsa + noise)
            ectopic = False
            if self._compensatory:
                rr = rr_mean * 1.3
                self._compensatory = False
            elif self.rng.random() < self.ectopic_rate:
                rr = rr_mean * 0.68
                ectopic = True
                self._compensatory = True
            rr = float(np.clip(rr, 0.27, 2.0))
            t_r = prev_t + rr if self.beats else tb
            self.beats.append((t_r, rr, ectopic))
            self.true_r.append(self.dev_t0 + t_r)
            self._next_beat = t_r + 0.01

    def _ecg(self, ts: np.ndarray) -> np.ndarray:
        x = np.zeros_like(ts)
        lo, hi = ts[0] - 0.7, ts[-1] + 0.5
        for tb, rr, ect in reversed(self.beats):
            if tb < lo:
                break
            if tb > hi:
                continue
            u = ts - tb
            s = float(np.sqrt(np.clip(rr, 0.3, 1.5)))
            amp = 1.0 + 0.08 * np.sin(self._resp_phase(tb))
            if ect:
                x += _wave(u, -0.01, 0.030, 1400 * amp) + _wave(u, 0.06, 0.035, -450) + _wave(u, 0.30 * s, 0.07, -380)
            else:
                x += (_wave(u, -0.17 * s, 0.024, 110) + _wave(u, -0.032, 0.010, -110)
                      + _wave(u, 0.0, 0.0125, 1150 * amp) + _wave(u, 0.034, 0.012, -300)
                      + _wave(u, 0.26 * s, 0.055 * s, 290))
        I = self._int
        x += 70.0 * np.sin(self._resp_phase(ts))                                # deriva respiratoria
        x += self.rng.standard_normal(len(ts)) * (7.0 + 35.0 * I)               # EMG
        x += 12.0 * np.sin(2 * np.pi * 50.0 * ts)                              # rete 50 Hz
        if self._sf > 1.0:
            x += 90.0 * I * np.maximum(np.cos(self._step_phase(ts)), 0) ** 8   # impatti del passo
        return x

    def _resp_phase(self, t):
        return self._rp0 + 2 * np.pi * self._rf * (t - self._tref)

    def _step_phase(self, t):
        return self._sp0 + 2 * np.pi * self._sf * (t - self._tref)

    def _rebase(self, t: float) -> None:
        self._rp0, self._sp0 = self._resp_phase(t), self._step_phase(t)
        self._tref = t
        self._rf, self._sf = self._resp / 60.0, self._cad / 60.0

    def _acc(self, ts: np.ndarray) -> np.ndarray:
        n = len(ts)
        phase = self._step_phase(ts)
        cad = self._sf * 60.0
        running = np.clip((cad - 60.0) / 100.0, 0.0, 1.0)
        a_v = (150 + 750 * running) if cad > 60 else 0.0
        vert = a_v * (0.9 * np.cos(phase) + 0.35 * np.cos(2 * phase + 0.5) + 0.15 * np.cos(3 * phase + 1.0))
        lat = (0.25 * a_v) * np.sin(phase / 2.0)
        fwd = 6.0 * np.sin(self._resp_phase(ts)) + 0.2 * a_v * np.sin(phase + 0.8)
        mg = (1000.0 * self._up + vert[:, None] * self._up + lat[:, None] * self._lat + fwd[:, None] * self._fwd)
        return mg + self.rng.standard_normal((n, 3)) * 4.0

    # ------------------------------------------------------------------ pacchetti

    def _host(self, t: float) -> float:
        # Orario di arrivo "come se" fosse in tempo reale, anche quando il simulatore è accelerato:
        # così la sincronizzazione degli orologi e le registrazioni restano coerenti.
        latency = 0.012 + abs(self.rng.normal(0, 0.008))
        return self.host_t0 + t + latency

    def _ns(self, t: float) -> int:
        return int(round((self.dev_t0 + t) * 1e9))

    def startup_packets(self) -> list[Packet]:
        h = self.host_t0
        ecg_cmd = pmd.build_start(MeasurementType.ECG, {SettingType.SAMPLE_RATE: 130, SettingType.RESOLUTION: 14})
        acc_cmd = pmd.build_start(MeasurementType.ACC, {SettingType.SAMPLE_RATE: int(self.acc_fs),
                                                        SettingType.RESOLUTION: 16, SettingType.RANGE: 8})
        return [
            Packet(h, Channel.BATTERY, bytes([87])),
            Packet(h, Channel.PMD_CP, pmd.H10_FEATURES),
            Packet(h, Channel.PMD_CP_WRITE, pmd.build_get_settings(MeasurementType.ECG)),
            Packet(h, Channel.PMD_CP, pmd.encode_settings_response(MeasurementType.ECG, pmd.H10_AVAILABLE[MeasurementType.ECG])),
            Packet(h, Channel.PMD_CP_WRITE, ecg_cmd),
            Packet(h, Channel.PMD_CP, pmd.encode_control_response(pmd.ControlOp.START, MeasurementType.ECG)),
            Packet(h, Channel.PMD_CP_WRITE, acc_cmd),
            Packet(h, Channel.PMD_CP, pmd.encode_control_response(pmd.ControlOp.START, MeasurementType.ACC)),
        ]

    def advance(self, dt: float) -> list[Packet]:
        """Avanza il tempo simulato di `dt` secondi e ritorna i pacchetti prodotti."""
        t_end = self.t + dt
        out: list[Packet] = []
        self._rebase(self.t)
        self._generate_beats_until(t_end + 1.0)

        n_ecg = int(np.floor(t_end * ECG_FS)) - self._ecg_n
        if n_ecg > 0:
            ts = (self._ecg_n + 1 + np.arange(n_ecg)) / ECG_FS
            self._ecg_buf.extend(self._ecg(ts))
            self._ecg_n += n_ecg
            while len(self._ecg_buf) >= ECG_FRAME:
                frame, self._ecg_buf = self._ecg_buf[:ECG_FRAME], self._ecg_buf[ECG_FRAME:]
                t_last = (self._ecg_n - len(self._ecg_buf)) / ECG_FS
                if self.rng.random() >= self.drop_rate:
                    data = pmd.encode_data_frame(MeasurementType.ECG, self._ns(t_last), np.round(frame).astype(int))
                    out.append(Packet(self._host(t_last), Channel.PMD_DATA, data))

        n_acc = int(np.floor(t_end * self.acc_true_fs)) - self._acc_n
        if n_acc > 0:
            ts = (self._acc_n + 1 + np.arange(n_acc)) / self.acc_true_fs
            self._acc_buf.extend(self._acc(ts))
            self._acc_n += n_acc
            while len(self._acc_buf) >= ACC_FRAME:
                frame, self._acc_buf = self._acc_buf[:ACC_FRAME], self._acc_buf[ACC_FRAME:]
                t_last = (self._acc_n - len(self._acc_buf)) / self.acc_true_fs
                data = pmd.encode_data_frame(MeasurementType.ACC, self._ns(t_last), np.round(np.array(frame)).astype(int))
                out.append(Packet(self._host(t_last), Channel.PMD_DATA, data))

        while self._hr_next <= t_end:
            done = [b for b in self.beats[self._hr_last_beat:] if b[0] <= self._hr_next]
            self._hr_last_beat += len(done)
            recent = [b[1] for b in self.beats[max(0, self._hr_last_beat - 4):self._hr_last_beat]]
            bpm = int(round(60.0 / np.mean(recent))) if recent else 0
            data = encode_hr_measurement(bpm, [b[1] * 1000.0 for b in done], contact=True)
            out.append(Packet(self._host(self._hr_next), Channel.HR, data))
            self._hr_next += 1.0

        self.t = t_end
        out.sort(key=lambda p: p.host_t)
        return out

    def true_beats_between(self, a: float, b: float) -> np.ndarray:
        r = np.asarray(self.true_r)
        return r[(r >= a) & (r <= b)]


class SimulatedSource(SensorSource):
    kind = "Simulatore"

    def __init__(self, speed: float = 1.0, seed: int = 7, drop_rate: float = 0.0):
        self.speed = speed
        self.seed = seed
        self.drop_rate = drop_rate
        self.sim: H10Simulator | None = None

    async def run(self, on_packet, on_status, stop: asyncio.Event) -> None:
        on_status(Status(SourceState.CONNECTING, "simulatore"))
        if await sleep_or_stop(stop, 0.4):
            return
        self.sim = sim = H10Simulator(seed=self.seed, drop_rate=self.drop_rate)
        for p in sim.startup_packets():
            on_packet(p)
        info = {"manufacturer": "Polar Electro Oy (simulato)", "model": "H10", "firmware": "sim"}
        on_status(Status(SourceState.STREAMING, sim.phase_at(0).name, device_name="Polar H10 SIM",
                         address="simulatore", info=info,
                         streams={"ECG": "130 Hz, 14 bit", "ACC": f"{int(sim.acc_fs)} Hz, ±8 G"}))
        t_start = time.monotonic()
        last_phase = ""
        while not stop.is_set():
            await asyncio.sleep(0.04)
            target = (time.monotonic() - t_start) * self.speed
            for p in sim.advance(target - sim.t):
                on_packet(p)
            phase = sim.phase_at(sim.t).name
            if phase != last_phase:
                last_phase = phase
                on_status(Status(SourceState.STREAMING, f"scenario: {phase}", device_name="Polar H10 SIM",
                                 address="simulatore", info=info,
                                 streams={"ECG": "130 Hz, 14 bit", "ACC": f"{int(sim.acc_fs)} Hz, ±8 G"}))
        on_status(Status(SourceState.STOPPED, "simulatore fermato"))
