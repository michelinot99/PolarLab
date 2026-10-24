"""Finestra principale: monitor in diretta (Live) e archivio con l'analisi delle sessioni (Sessioni)."""

from __future__ import annotations

import math
import threading
from pathlib import Path

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QLabel, QMainWindow,
                               QMessageBox, QPushButton, QSizePolicy, QStackedWidget, QStatusBar, QVBoxLayout, QWidget)

from polarlab.analysis.report import Profile
from polarlab.core.engine import Engine
from polarlab.core.pipeline import SESSIONS_DIR, Pipeline
from polarlab.dsp.dfa import alpha1_zone
from polarlab.sources.base import SourceState
from polarlab.ui import theme
from polarlab.ui.widgets.charts import MotionCard, PoincareCard, TachogramCard, TemplateCard
from polarlab.ui.widgets.common import Card, Chip, KeyValueGrid, Tile, fmt
from polarlab.ui.sessions_view import SessionsView
from polarlab.ui.widgets.device_dialog import DeviceDialog
from polarlab.ui.widgets.ecg_sweep import EcgSweep
from polarlab.ui.widgets.settings_dialog import SettingsDialog

SRC_H10, SRC_SIM, SRC_REPLAY = "Polar H10", "Simulatore", "Replay sessione…"
STATE_COLORS = {
    SourceState.STREAMING: theme.GOOD, SourceState.CONNECTED: theme.WARNING, SourceState.CONNECTING: theme.WARNING,
    SourceState.SCANNING: theme.WARNING, SourceState.RECONNECTING: theme.SERIOUS, SourceState.ERROR: theme.CRITICAL,
}


class MainWindow(QMainWindow):
    def __init__(self, sim_speed: float = 1.0):
        super().__init__()
        self.setWindowTitle("PolarLab")
        self.resize(1480, 920)
        self.settings = QSettings("PolarLab", "PolarLab")
        self.address: str | None = self.settings.value("address", None) or None
        self.sim_speed = sim_speed
        self.pipeline: Pipeline | None = None
        self.replay_dir: Path | None = None
        self.replay_start = 0.0
        self.sessions_dir = Path(self.settings.value("sessions_dir", "") or SESSIONS_DIR)
        self._frame = 0
        self._build()
        self.setAcceptDrops(True)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(33)

    # ------------------------------------------------------------------ layout

    def _build(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(16, 12, 16, 8)
        outer.setSpacing(12)
        outer.addLayout(self._toolbar())

        self.pages = QStackedWidget()
        outer.addWidget(self.pages, 1)
        live = QWidget()
        body = QHBoxLayout(live)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(12)
        self.pages.addWidget(live)
        self.sessions = SessionsView(self.sessions_dir, self._profile())
        self.sessions.replay_requested.connect(self.play_session)
        self.pages.addWidget(self.sessions)

        left = QVBoxLayout()
        left.setSpacing(12)
        ecg_card = Card("ECG · derivazione toracica")
        self.view_window = QComboBox()
        for s in (4, 6, 8, 10):
            self.view_window.addItem(f"{s} s", s)
        self.view_window.setCurrentIndex(1)
        self.view_window.currentIndexChanged.connect(lambda _: self.ecg.set_window(self.view_window.currentData()))
        self.gain = QComboBox()
        self.gain.addItem("10 mm/mV", 1.0)
        self.gain.addItem("20 mm/mV", 2.0)
        self.gain.addItem("5 mm/mV", 0.5)
        self.gain.currentIndexChanged.connect(lambda _: self.ecg.set_gain(self.gain.currentData()))
        self.raw = QCheckBox("Segnale grezzo")
        self.notch = QCheckBox("Notch 50 Hz")
        self.notch.toggled.connect(self._on_notch)
        for w in (self.raw, self.notch, self.gain, self.view_window):
            ecg_card.header.addWidget(w)
        self.ecg = EcgSweep(6.0)
        ecg_card.layout_.addWidget(self.ecg)
        left.addWidget(ecg_card, 5)
        self.tacho = TachogramCard()
        left.addWidget(self.tacho, 3)
        row = QHBoxLayout()
        row.setSpacing(12)
        self.motion = MotionCard()
        self.template = TemplateCard()
        row.addWidget(self.motion, 3)
        row.addWidget(self.template, 2)
        left.addLayout(row, 3)
        body.addLayout(left, 33)

        right = QGridLayout()
        right.setSpacing(12)
        self.t_hr = Tile("Frequenza cardiaca", "bpm")
        self.t_rmssd = Tile("RMSSD", "ms", small=True)
        self.t_dfa = Tile("DFA α1", "", small=True, chip=True)
        self.t_quality = Tile("Qualità", "/100", small=True, chip=True)
        self.t_resp = Tile("Respiro", "atti/min", small=True)
        self.t_cad = Tile("Cadenza", "passi/min", small=True)
        self.kv = KeyValueGrid("HRV e fascia", ["SDNN", "pNN50", "SD1 / SD2", "LF / HF", "Artefatti", "Accordo RR",
                                                "Batteria", "Contatto", "Battiti", "Osc. verticale"])
        self.poincare = PoincareCard()
        right.addWidget(self.t_hr, 0, 0, 1, 2)
        right.addWidget(self.t_rmssd, 1, 0)
        right.addWidget(self.t_dfa, 1, 1)
        right.addWidget(self.t_quality, 2, 0)
        right.addWidget(self.t_resp, 2, 1)
        right.addWidget(self.t_cad, 3, 0, 1, 2)
        right.addWidget(self.kv, 4, 0, 1, 2)
        right.addWidget(self.poincare, 5, 0, 1, 2)
        right.setRowStretch(5, 1)
        rw = QWidget()
        rw.setLayout(right)
        rw.setMinimumWidth(380)
        rw.setMaximumWidth(460)
        body.addWidget(rw, 12)

        sb = QStatusBar()
        self.setStatusBar(sb)
        self.sb_left = QLabel("")
        self.sb_right = QLabel("")
        sb.addWidget(self.sb_left, 1)
        sb.addPermanentWidget(self.sb_right)

    def _toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(10)
        brand = QLabel("PolarLab")
        brand.setObjectName("brand")
        bar.addWidget(brand)
        bar.addSpacing(8)
        self.mode = QButtonGroup(self)
        seg = QHBoxLayout()
        seg.setSpacing(0)
        for i, (text, obj) in enumerate((("Live", "segL"), ("Sessioni", "segR"))):
            b = QPushButton(text)
            b.setObjectName(obj)
            b.setCheckable(True)
            self.mode.addButton(b, i)
            seg.addWidget(b)
        self.mode.button(0).setChecked(True)
        self.mode.idClicked.connect(self.show_page)
        bar.addLayout(seg)
        bar.addSpacing(12)
        self.live_bar = QWidget()
        lb = QHBoxLayout(self.live_bar)
        lb.setContentsMargins(0, 0, 0, 0)
        lb.setSpacing(10)
        bar.addWidget(self.live_bar, 1)
        self.sessions_msg = QLabel("")
        self.sessions_msg.setObjectName("muted")
        self.sessions_msg.hide()
        bar.addWidget(self.sessions_msg, 1)
        outer_bar, bar = bar, lb
        self.source = QComboBox()
        self.source.addItems([SRC_H10, SRC_SIM, SRC_REPLAY])
        saved = self.settings.value("source", SRC_H10)
        if saved in (SRC_H10, SRC_SIM):
            self.source.setCurrentText(saved)
        self.source.currentTextChanged.connect(self._on_source_changed)
        bar.addWidget(self.source)
        self.device_btn = QPushButton(self._device_label())
        self.device_btn.clicked.connect(self._choose_device)
        bar.addWidget(self.device_btn)
        self.connect_btn = QPushButton("Connetti")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.clicked.connect(self._toggle_connection)
        bar.addWidget(self.connect_btn)
        self.speed = QComboBox()
        for x in (1, 2, 4, 8, 16):
            self.speed.addItem(f"×{x}", float(x))
        self.speed.setToolTip("Velocità del replay")
        self.speed.setCurrentIndex(max(0, self.speed.findData(float(self.sim_speed))))
        self.speed.currentIndexChanged.connect(self._on_speed)
        bar.addWidget(self.speed)
        self.state_chip = Chip()
        bar.addWidget(self.state_chip)
        self.state_msg = QLabel("")
        self.state_msg.setObjectName("muted")
        self.state_msg.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        bar.addWidget(self.state_msg, 1)
        self.rec_btn = QPushButton("● Registra")
        self.rec_btn.setObjectName("record")
        self.rec_btn.setCheckable(True)
        self.rec_btn.setEnabled(False)
        self.rec_btn.toggled.connect(self._toggle_record)
        bar.addWidget(self.rec_btn)
        self._on_source_changed(self.source.currentText())
        settings_btn = QPushButton("Impostazioni")
        settings_btn.clicked.connect(self._open_settings)
        outer_bar.addWidget(settings_btn)
        return outer_bar

    def _device_label(self) -> str:
        return "Fascia: memorizzata" if self.address else "Fascia: qualsiasi H10"

    # ------------------------------------------------------------------ azioni

    def _on_source_changed(self, text: str) -> None:
        self.device_btn.setVisible(text == SRC_H10)
        self.speed.setVisible(text == SRC_REPLAY)
        if text in (SRC_H10, SRC_SIM):
            self.settings.setValue("source", text)

    def _choose_device(self) -> None:
        dlg = DeviceDialog(self, self.address)
        if dlg.exec():
            self.address = dlg.address
            self.settings.setValue("address", self.address or "")
            self.device_btn.setText(self._device_label())

    def _make_source(self):
        kind = self.source.currentText()
        if kind == SRC_SIM:
            from polarlab.sources.simulator import SimulatedSource
            return SimulatedSource(speed=self.sim_speed)
        if kind == SRC_REPLAY:
            if self.replay_dir is None:
                self.show_page(1)
                self.sessions_msg.setText("Scegli una sessione e premi «▶ Riproduci»")
                return None
            from polarlab.sources.replay import ReplaySource
            src = ReplaySource(self.replay_dir, speed=self.speed.currentData(), start_s=self.replay_start)
            self.replay_dir, self.replay_start = None, 0.0
            return src
        from polarlab.sources.polar_ble import PolarH10Source
        return PolarH10Source(address=self.address)

    def start_source(self, replay_dir: Path | None = None, kind: str | None = None) -> None:
        if kind:
            self.source.setCurrentText(kind)
        self.replay_dir = replay_dir
        if self.pipeline is None:
            self._toggle_connection()

    def _toggle_connection(self) -> None:
        if self.pipeline is not None:
            if self.rec_btn.isChecked():
                self.rec_btn.setChecked(False)
            p, self.pipeline = self.pipeline, None
            threading.Thread(target=p.stop, daemon=True).start()   # lo stop BLE può richiedere qualche secondo
            self.connect_btn.setText("Connetti")
            self.rec_btn.setEnabled(False)
            self.source.setEnabled(True)
            self.device_btn.setEnabled(True)
            return
        src = self._make_source()
        if src is None:
            return
        engine = Engine()
        engine.notch_enabled = self.notch.isChecked()
        self.pipeline = Pipeline(src, engine, sessions_dir=self.sessions_dir)
        self.pipeline.start()
        self.connect_btn.setText("Disconnetti")
        self.rec_btn.setEnabled(True)
        self.source.setEnabled(False)
        self.device_btn.setEnabled(False)

    def _on_notch(self, on: bool) -> None:
        if self.pipeline is not None:
            self.pipeline.engine.notch_enabled = on

    def _toggle_record(self, on: bool) -> None:
        if self.pipeline is None:
            return
        if on:
            self.pipeline.start_recording()
        else:
            self.pipeline.stop_recording()
            self.rec_btn.setText("● Registra")

    # ------------------------------------------------------------------ pagine, sessioni, impostazioni

    def show_page(self, i: int) -> None:
        self.pages.setCurrentIndex(i)
        self.mode.button(i).setChecked(True)
        self.live_bar.setVisible(i == 0)
        self.sessions_msg.setVisible(i == 1)
        self.statusBar().setVisible(i == 0)
        if i == 1:
            self.sessions.refresh()
        else:
            self.sessions_msg.setText("")

    def open_session(self, d: Path) -> None:
        self.show_page(1)
        self.sessions.select(Path(d))

    def open_paths(self, paths: list[Path]) -> None:
        """Zip o cartelle trascinati sulla finestra o sull'icona nel Dock: si importano nell'archivio."""
        self.show_page(1)
        self.sessions.import_paths([Path(p) for p in paths])

    def play_session(self, d: Path, start_s: float = 0.0) -> None:
        if self.pipeline is not None:
            if self.rec_btn.isChecked() and QMessageBox.question(
                    self, "Registrazione in corso", "Interrompere la registrazione per avviare il replay?") \
                    != QMessageBox.Yes:
                return
            self._toggle_connection()
        self.source.setCurrentText(SRC_REPLAY)
        self.replay_dir, self.replay_start = Path(d), start_s
        self.show_page(0)
        self._toggle_connection()

    def _on_speed(self, _=None) -> None:
        src = self.pipeline.source if self.pipeline is not None else None
        if src is not None and hasattr(src, "speed"):
            src.speed = self.speed.currentData()

    def _profile(self) -> Profile:
        def num(key):
            try:
                v = float(self.settings.value(key, 0) or 0)
            except (TypeError, ValueError):
                v = 0.0
            return v or None
        return Profile(hr_max=num("hr_max"), hr_rest=num("hr_rest"))

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self, self._profile(), self.sessions_dir, SESSIONS_DIR)
        if not dlg.exec():
            return
        prof = dlg.profile
        self.settings.setValue("hr_max", prof.hr_max or 0)
        self.settings.setValue("hr_rest", prof.hr_rest or 0)
        self.sessions.set_profile(prof)
        new_dir = dlg.sessions_dir
        if new_dir != self.sessions_dir:
            self.sessions_dir = new_dir
            self.settings.setValue("sessions_dir", "" if new_dir == SESSIONS_DIR else str(new_dir))
            self.sessions.set_sessions_dir(new_dir)

    def dragEnterEvent(self, ev) -> None:
        urls = [u.toLocalFile() for u in ev.mimeData().urls()] if ev.mimeData().hasUrls() else []
        if any(u.endswith(".zip") or Path(u).is_dir() for u in urls if u):
            ev.acceptProposedAction()

    def dropEvent(self, ev) -> None:
        paths = [Path(u.toLocalFile()) for u in ev.mimeData().urls() if u.toLocalFile()]
        if paths:
            self.open_paths(paths)

    # ------------------------------------------------------------------ aggiornamento

    def _refresh(self) -> None:
        self._frame += 1
        p = self.pipeline
        if p is None:
            self.ecg.update_data([], 0, 130.0, [], connected=False)
            self.state_chip.set(SourceState.IDLE.value, theme.MUTED)
            self.state_msg.setText("")
            return
        snap = p.engine.snapshot(ecg_seconds=self.ecg.window_s + 0.5)
        ecg = snap.ecg_raw if self.raw.isChecked() else snap.ecg
        self.ecg.update_data(ecg, snap.ecg_end, snap.ecg_fs, snap.beat_idx, connected=True)
        if self._frame % 2 == 0:
            self.motion.update_data(snap)
        if self._frame % 8 == 0:
            self._update_slow(p, snap)

    def _update_slow(self, p: Pipeline, snap) -> None:
        m = snap.metrics
        st = p.status
        self.state_chip.set(st.state.value, STATE_COLORS.get(st.state, theme.MUTED))
        self.state_msg.setText(" · ".join(x for x in (st.device_name, st.message) if x))

        self.t_hr.set(fmt(m.hr_ecg), f"fascia {fmt(m.hr_strap)} bpm" + (
            f"  ·  RR medio {fmt(m.mean_nn)} ms" if math.isfinite(m.mean_nn) else ""))
        self.t_rmssd.set(fmt(m.rmssd, 1), "ultimi 60 s")
        zone, desc = alpha1_zone(m.dfa_a1)
        self.t_dfa.set(fmt(m.dfa_a1, 2), f"{desc} · {m.dfa_beats} battiti")
        self.t_dfa.chip.set(zone, theme.alpha1_color(m.dfa_a1))
        self.t_quality.set(fmt(m.quality), f"template r = {fmt(m.template_corr, 2)}")
        self.t_quality.chip.set(m.quality_label, theme.quality_color(m.quality))
        self.t_resp.set(fmt(m.resp_brpm), f"fonte: {m.resp_source}")
        self.t_cad.set(fmt(m.cadence_spm), f"intensità movimento {fmt(m.motion_mg)} mG")

        kv = self.kv
        kv.set("SDNN", f"{fmt(m.sdnn, 1)} ms")
        kv.set("pNN50", f"{fmt(m.pnn50, 1)} %")
        kv.set("SD1 / SD2", f"{fmt(m.sd1, 1)} / {fmt(m.sd2, 1)}")
        kv.set("LF / HF", fmt(m.lf_hf, 2))
        kv.set("Artefatti", f"{fmt(m.artifact_pct, 1)} %")
        kv.set("Accordo RR", f"{fmt(m.strap_match_pct)} % · {fmt(m.strap_mae_ms, 1)} ms")
        kv.set("Batteria", f"{m.battery} %" if m.battery is not None else "—")
        kv.set("Contatto", {True: "sì", False: "NO", None: "—"}[m.contact])
        kv.set("Battiti", str(m.beats))
        kv.set("Osc. verticale", f"{fmt(m.vertical_osc_cm, 1)} cm")

        self.tacho.update_data(snap)
        self.poincare.update_data(snap, m.sd1, m.sd2)
        self.template.update_data(snap)

        d = p.decoder.stats
        self.sb_left.setText(
            f"{p.packets_per_s:4.0f} pacchetti/s  ·  {d.bytes / 1024:,.0f} kB  ·  frame ECG {d.ecg_frames}  ACC {d.acc_frames}  "
            f"·  buchi {d.gaps} ({d.missing_samples} campioni)  ·  jitter BLE {p.decoder.sync.jitter_ms:.0f} ms  "
            f"·  errori {d.errors}" + (f" ({d.last_error})" if d.last_error else ""))
        info = st.info or {}
        streams = "  ".join(f"{k}: {v}" for k, v in st.streams.items())
        fw = f"fw {info['firmware']}" if info.get("firmware") else ""
        self.sb_right.setText("  ·  ".join(x for x in (info.get("model", ""), fw, streams) if x))

        if p.recording and p.record_dir is not None:
            el = int(p.record_elapsed)
            self.rec_btn.setText(f"■ {el // 60:02d}:{el % 60:02d}")
        if p.last_export is not None:
            done, p.last_export = p.last_export, None
            self.statusBar().showMessage(f"Sessione salvata ed esportata in {done} · la trovi in Sessioni", 8000)
        if p.export_error:
            QMessageBox.warning(self, "Esportazione", p.export_error)
            p.export_error = None

    def closeEvent(self, ev) -> None:
        self.timer.stop()
        if self.pipeline is not None:
            self.pipeline.stop()
        super().closeEvent(ev)
