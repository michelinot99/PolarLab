"""Motore di analisi: consuma gli eventi del decoder e mantiene stato e metriche.

È sincrono e senza thread propri: lo si può guidare da un test, dal replay offline o
dalla pipeline in tempo reale. La UI ne legge uno `Snapshot` (copia coerente sotto lock).
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import asdict, dataclass, field

import numpy as np

from polarlab.core.ringbuffer import Ring
from polarlab.dsp import dfa, hrv
from polarlab.dsp.filters import EcgFilter, UniformResampler
from polarlab.dsp.motion import MotionAnalyzer
from polarlab.dsp.qrs import QRSDetector
from polarlab.dsp.quality import QualityMonitor
from polarlab.dsp.respiration import RespirationEstimator
from polarlab.dsp.rr_clean import INVALID, NORMAL, RRCleaner
from polarlab.dsp.template import BeatTemplate
from polarlab.protocol.events import AccChunk, BatteryEvent, EcgChunk, HrEvent, StreamStarted

NAN = float("nan")
MAX_PADDED_GAP_S = 1.0     # buchi più brevi si riempiono, più lunghi azzerano il rilevatore
ECG_HISTORY_S = 30.0
ACC_HISTORY_S = 12.0
RR_HISTORY_S = 900.0


@dataclass
class Metrics:
    hr_ecg: float = NAN
    hr_strap: float = NAN
    mean_nn: float = NAN
    rmssd: float = NAN
    sdnn: float = NAN
    pnn50: float = NAN
    sd1: float = NAN
    sd2: float = NAN
    lf: float = NAN
    hf: float = NAN
    lf_hf: float = NAN
    dfa_a1: float = NAN
    dfa_beats: int = 0
    artifact_pct: float = NAN
    quality: float = NAN
    quality_label: str = "—"
    template_corr: float = NAN
    strap_mae_ms: float = NAN
    strap_match_pct: float = NAN
    resp_brpm: float = NAN
    resp_source: str = "—"
    resp_conf: float = 0.0
    cadence_spm: float = NAN
    motion_mg: float = NAN
    vertical_osc_cm: float = NAN
    contact: bool | None = None
    battery: int | None = None
    beats: int = 0


@dataclass
class Snapshot:
    now: float = 0.0
    ecg_fs: float = 130.0
    ecg_end: int = 0
    ecg_t: np.ndarray = field(default_factory=lambda: np.empty(0))
    ecg: np.ndarray = field(default_factory=lambda: np.empty(0))
    ecg_raw: np.ndarray = field(default_factory=lambda: np.empty(0))
    beat_idx: np.ndarray = field(default_factory=lambda: np.empty(0))
    beat_amp: np.ndarray = field(default_factory=lambda: np.empty(0))
    rr_t: np.ndarray = field(default_factory=lambda: np.empty(0))
    rr_raw: np.ndarray = field(default_factory=lambda: np.empty(0))
    rr_nn: np.ndarray = field(default_factory=lambda: np.empty(0))
    rr_ok: np.ndarray = field(default_factory=lambda: np.empty(0, bool))
    strap_t: np.ndarray = field(default_factory=lambda: np.empty(0))
    strap_rr: np.ndarray = field(default_factory=lambda: np.empty(0))
    acc_t: np.ndarray = field(default_factory=lambda: np.empty(0))
    acc: np.ndarray = field(default_factory=lambda: np.empty((0, 3)))
    vert: np.ndarray = field(default_factory=lambda: np.empty(0))
    template_t: np.ndarray = field(default_factory=lambda: np.empty(0))
    template: np.ndarray | None = None
    template_recent: list = field(default_factory=list)
    metrics: Metrics = field(default_factory=Metrics)


class Engine:
    def __init__(self, keep_log: bool = False):
        self.lock = threading.Lock()
        self.keep_log = keep_log
        self.beat_log: list[dict] = []
        self.metrics_log: list[dict] = []
        self.metrics = Metrics()
        self.t0: float | None = None
        self._setup_ecg(130.0)
        self._setup_acc(200.0)
        self._strap_rr: deque[tuple[float, float]] = deque()
        self._rr: deque[tuple[float, float, float, bool]] = deque()
        self._hrv = hrv.NNWindow(60.0)
        self._hrv_long = hrv.NNWindow(300.0)
        self._dfa = hrv.NNWindow(120.0)
        self._quality = QualityMonitor()
        self._resp = RespirationEstimator()
        self._next_tick: float | None = None
        self._tick_count = 0

    # ------------------------------------------------------------------ setup

    def _setup_ecg(self, fs: float) -> None:
        self.ecg_fs = fs
        cap = int(ECG_HISTORY_S * fs)
        self._ecg_t = Ring(cap)
        self._ecg_raw = Ring(cap)
        self._ecg_bp = Ring(cap)
        self._ecg_disp = Ring(cap)
        self._filter = EcgFilter(fs)
        self._qrs = QRSDetector(fs)
        self._template = BeatTemplate(fs)
        self._cleaner = RRCleaner()
        self._pending_template: list = []
        self._beats: deque[tuple[float, float]] = deque(maxlen=256)   # (indice, ampiezza)
        self._last_beat_t: float | None = None
        self._last_beat_idx: float = -1.0
        self._gap_index: int = -1     # indice ECG fino al quale i dati sono riempiti

    def _setup_acc(self, fs: float) -> None:
        self.acc_fs = fs
        cap = int(ACC_HISTORY_S * fs)
        self._acc_t = Ring(cap)
        self._acc = Ring(cap, width=3)
        self._vert = Ring(cap)
        self._motion = MotionAnalyzer(fs)
        self._acc_rs = UniformResampler(fs)
        self._decim = max(1, int(round(fs / 10.0)))
        self._decim_buf = np.empty((0, 3))

    @property
    def notch_enabled(self) -> bool:
        return self._filter.notch_enabled

    @notch_enabled.setter
    def notch_enabled(self, value: bool) -> None:
        with self.lock:
            self._filter.notch_enabled = value

    # ------------------------------------------------------------------ eventi

    def handle(self, ev) -> None:
        with self.lock:
            if isinstance(ev, EcgChunk):
                self._on_ecg(ev)
            elif isinstance(ev, AccChunk):
                self._on_acc(ev)
            elif isinstance(ev, HrEvent):
                self._on_hr(ev)
            elif isinstance(ev, BatteryEvent):
                self.metrics.battery = ev.percent
            elif isinstance(ev, StreamStarted):
                if ev.stream == "ECG" and ev.sample_rate != self.ecg_fs:
                    self._setup_ecg(ev.sample_rate)
                elif ev.stream == "ACC" and ev.sample_rate != self.acc_fs:
                    self._setup_acc(ev.sample_rate)

    def _on_ecg(self, ev: EcgChunk) -> None:
        t, x = ev.t, ev.uv
        if self.t0 is None:
            self.t0 = float(t[0])
        if ev.missing_before:
            dt = 1.0 / self.ecg_fs
            if 0 < ev.missing_before * dt <= MAX_PADDED_GAP_S and len(self._ecg_raw):
                # Riempie il buco con l'ultimo valore: l'indice dei campioni resta = tempo
                n = ev.missing_before
                last_v = self._ecg_raw.at(self._ecg_raw.end - 1)
                t = np.concatenate([t[0] - dt * np.arange(n, 0, -1), t])
                x = np.concatenate([np.full(n, last_v), x])
                self._gap_index = self._ecg_raw.end + n
            else:
                self._reset_beat_chain()
        bp, disp = self._filter.process(x)
        self._ecg_t.extend(t)
        self._ecg_raw.extend(x)
        self._ecg_bp.extend(bp)
        self._ecg_disp.extend(disp)

        for b in self._qrs.push(bp):
            self._on_beat(b)
        self._update_templates()

        now = float(t[-1])
        if self._next_tick is None:
            self._next_tick = now + 1.0
        if now >= self._next_tick:
            self._next_tick += 1.0
            if now - self._next_tick > 5.0:
                self._next_tick = now + 1.0
            self._quality.push_ecg_second(self._ecg_raw.last(int(self.ecg_fs)))
            self._tick(now)

    def _reset_beat_chain(self) -> None:
        self._filter.reset()
        self._qrs.reset()
        self._cleaner.reset()
        self._pending_template.clear()
        self._last_beat_t = None
        self._last_beat_idx = -1.0
        self._gap_index = -1
        self._ecg_t.clear()
        self._ecg_raw.clear()
        self._ecg_bp.clear()
        self._ecg_disp.clear()
        self._beats.clear()

    def _index_to_time(self, idx: float) -> float:
        i = int(math.floor(idx))
        i = min(max(i, self._ecg_t.start), self._ecg_t.end - 1)
        return float(self._ecg_t.at(i)) + (idx - i) / self.ecg_fs

    def _on_beat(self, b) -> None:
        t = self._index_to_time(b.index)
        self._beats.append((b.index, b.amplitude))
        self.metrics.beats += 1
        if self._last_beat_t is not None:
            rr = (t - self._last_beat_t) * 1000.0
            # Intervallo che attraversa un tratto riempito (pacchetti persi): non affidabile
            invalid = self._last_beat_idx < self._gap_index or rr <= 0
            self._quality.ours.append(rr)
            if not invalid:
                self._resp.push_beat(t, abs(b.amplitude), rr)
            for nn in self._cleaner.push(t, rr, invalid=invalid):
                self._on_nn(nn)
        self._last_beat_t = t
        self._last_beat_idx = b.index
        self._pending_template.append(b)

    def _on_nn(self, nn) -> None:
        ok = nn.kind == NORMAL
        self._rr.append((nn.t, nn.rr, nn.nn, ok))
        while self._rr and nn.t - self._rr[0][0] > RR_HISTORY_S:
            self._rr.popleft()
        if nn.kind != INVALID:
            for w in (self._hrv, self._hrv_long, self._dfa):
                w.push(nn.t, nn.nn, not ok)
        if self.keep_log:
            self.beat_log.append({"t": round(nn.t - (self.t0 or 0.0), 4), "rr_ms": round(nn.rr, 2),
                                  "nn_ms": round(nn.nn, 2), "kind": nn.kind})

    def _update_templates(self) -> None:
        keep = []
        for b in self._pending_template:
            if b.index + self._template.post + 2 >= self._ecg_bp.end:
                keep.append(b)
                continue
            a = int(math.floor(b.index)) - self._template.pre - 1
            if a < self._ecg_bp.start:
                continue
            seg_src = self._ecg_bp.get(a, a + self._template.length + 3)
            seg = self._template.segment(seg_src, b.index - a)
            if seg is not None:
                self._quality.push_corr(self._template.add(seg))
        self._pending_template = keep

    def _on_acc(self, ev: AccChunk) -> None:
        if self.t0 is None:
            self.t0 = float(ev.t[0])
        if ev.missing_before:
            self._acc_rs.reset()      # non si interpola attraverso un buco
        # Da qui in poi l'accelerometro è esattamente a acc_fs, qualunque sia il quarzo della fascia
        t, mg = self._acc_rs.push(ev.t, ev.mg)
        if not len(t):
            return
        vert = self._motion.push(mg)
        self._acc_t.extend(t)
        self._acc.extend(mg)
        self._vert.extend(vert)
        buf = np.concatenate([self._decim_buf, mg])
        n = (len(buf) // self._decim) * self._decim
        if n:
            self._resp.push_acc(buf[:n].reshape(-1, self._decim, 3).mean(axis=1))
        self._decim_buf = buf[n:]

    def _on_hr(self, ev: HrEvent) -> None:
        self.metrics.hr_strap = float(ev.bpm)
        self.metrics.contact = ev.contact
        self._quality.contact = ev.contact
        t = ev.device_t
        # Gli RR della notifica precedono l'istante di arrivo: si ricostruiscono all'indietro
        acc = 0.0
        times = []
        for rr in reversed(ev.rr_ms):
            times.append(t - acc / 1000.0)
            acc += rr
        for tt, rr in zip(reversed(times), ev.rr_ms):
            if math.isfinite(tt):
                self._strap_rr.append((tt, rr))
            self._quality.strap.append(rr)
        while self._strap_rr and t - self._strap_rr[0][0] > RR_HISTORY_S:
            self._strap_rr.popleft()

    # ------------------------------------------------------------------ metriche

    def _tick(self, now: float) -> None:
        self._tick_count += 1
        m = self.metrics
        _, nn, corrected = self._hrv.arrays()
        td = hrv.time_domain(nn)
        if td is not None:
            m.mean_nn, m.rmssd, m.sdnn, m.pnn50, m.sd1, m.sd2 = td.mean_nn, td.rmssd, td.sdnn, td.pnn50, td.sd1, td.sd2
            m.artifact_pct = float(np.mean(corrected) * 100.0)
        if len(nn) >= 3:
            m.hr_ecg = 60000.0 / float(np.median(nn[-5:]))
        if self._last_beat_t is not None and now - self._last_beat_t > 4.0:
            m.hr_ecg = NAN

        q = self._quality.evaluate()
        m.quality, m.quality_label, m.template_corr = q.score, q.label, q.template_corr
        m.strap_mae_ms, m.strap_match_pct = q.strap_mae_ms, q.strap_match_pct

        if self._tick_count % 5 == 0:
            _, dnn, _ = self._dfa.arrays()
            m.dfa_beats = len(dnn)
            m.dfa_a1 = dfa.dfa_alpha(dnn) if self._dfa.span_s >= 60.0 else NAN
            tl, nl, _ = self._hrv_long.arrays()
            fd = hrv.frequency_domain(tl, nl)
            if fd is not None:
                m.lf, m.hf, m.lf_hf = fd.lf, fd.hf, fd.lf_hf
        if self._tick_count % 2 == 0:
            r = self._resp.estimate()
            m.resp_brpm, m.resp_source, m.resp_conf = r.brpm, r.source, r.confidence
        if len(self._acc):
            ms = self._motion.update()
            m.cadence_spm, m.motion_mg, m.vertical_osc_cm = ms.cadence_spm, ms.intensity_mg, ms.vertical_osc_cm

        if self.keep_log:
            row = {k: v for k, v in asdict(m).items() if k not in ("contact",)}
            row["t"] = now - (self.t0 or 0.0)
            self.metrics_log.append(row)

    # ------------------------------------------------------------------ snapshot

    def snapshot(self, ecg_seconds: float = 12.0, rr_seconds: float = 300.0) -> Snapshot:
        with self.lock:
            t0 = self.t0 or 0.0
            s = Snapshot(metrics=Metrics(**asdict(self.metrics)), ecg_fs=self.ecg_fs, ecg_end=self._ecg_raw.end)
            n = min(len(self._ecg_raw), int(ecg_seconds * self.ecg_fs))
            if n:
                s.ecg_t = self._ecg_t.last(n) - t0
                s.ecg = self._ecg_disp.last(n).copy()
                s.ecg_raw = self._ecg_raw.last(n).copy()
                s.now = float(s.ecg_t[-1])
            lo = self._ecg_raw.end - n
            beats = [(i, a) for i, a in self._beats if i >= lo]
            if beats:
                s.beat_idx, s.beat_amp = (np.asarray(v) for v in zip(*beats))
            rr = [r for r in self._rr if r[0] - t0 >= s.now - rr_seconds]
            if rr:
                t, raw, nn, ok = zip(*rr)
                s.rr_t, s.rr_raw, s.rr_nn, s.rr_ok = np.asarray(t) - t0, np.asarray(raw), np.asarray(nn), np.asarray(ok)
            st = [r for r in self._strap_rr if r[0] - t0 >= s.now - rr_seconds]
            if st:
                t, rr_s = zip(*st)
                s.strap_t, s.strap_rr = np.asarray(t) - t0, np.asarray(rr_s)
            na = min(len(self._acc), int(10 * self.acc_fs))
            if na:
                s.acc_t = self._acc_t.last(na) - t0
                s.acc = self._acc.last(na).copy()
                s.vert = self._vert.last(na).copy()
                if not n:
                    s.now = float(s.acc_t[-1])
            s.template_t = self._template.t_axis
            if self._template.mean is not None:
                s.template = self._template.mean.copy()
                s.template_recent = [r.copy() for r in self._template.recent]
            return s

    def finish(self) -> None:
        """Fine sessione offline: emette gli ultimi intervalli rimasti in coda al correttore."""
        with self.lock:
            for nn in self._cleaner.flush():
                self._on_nn(nn)
