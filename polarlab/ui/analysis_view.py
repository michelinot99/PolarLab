"""Schermata di analisi di una sessione: panoramica dell'intera corsa, centrata sul cuore.

Dall'alto: riquadri riassuntivi, cronologia (FC, α1, RR, cadenza, qualità con asse X
condiviso), esploratore ECG, eventi da controllare con morfologia e Poincaré, zone,
soglie e recupero, dati della registrazione.
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QGridLayout, QHBoxLayout, QHeaderView,
                               QLabel, QPushButton, QScrollArea, QScrollBar, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from polarlab.analysis import report as R
from polarlab.dsp.dfa import ANT_ALPHA1, AET_ALPHA1
from polarlab.ui import theme
from polarlab.ui.widgets.common import Card, Chip, KeyValueGrid, Tile, fmt, styled_plot
from polarlab.ui.widgets.ecg_strip import EcgStrip, clock

WEEKDAYS = ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica")
MONTHS = ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre",
          "ottobre", "novembre", "dicembre")
DISCLAIMER = ("PolarLab è uno strumento sperimentale per l'allenamento, non un dispositivo medico. Gli eventi "
              "segnalati sono indicazioni per rivedere il tracciato, non diagnosi: una fascia toracica registra "
              "una sola derivazione. Per sintomi o dubbi, mostra il tracciato a un medico.")


def session_start(meta: dict, directory: Path) -> datetime | None:
    try:
        return datetime.fromisoformat(meta["started"])
    except (KeyError, ValueError, TypeError):
        try:
            return datetime.strptime(directory.name[:17], "%Y-%m-%d_%H%M%S")
        except ValueError:
            return None


def long_date(d: datetime | None) -> str:
    if d is None:
        return "Sessione"
    return f"{WEEKDAYS[d.weekday()].capitalize()} {d.day} {MONTHS[d.month - 1]} {d.year} · {d:%H:%M}"


def duration_text(s: float) -> str:
    s = int(round(s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h} h {m:02d} min"
    return f"{m} min {sec:02d} s" if m else f"{sec} s"


class TimeAxis(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):
        return [clock(v) for v in values]


class Timeline(pg.GraphicsLayoutWidget):
    """Grafici impilati con lo stesso asse dei tempi; un clic sceglie l'istante dell'esploratore ECG."""

    picked = Signal(float)

    def __init__(self):
        super().__init__()
        self.setBackground(theme.SURFACE)
        self.ci.setContentsMargins(4, 4, 8, 4)
        self.ci.setSpacing(2)
        self.plots: list[pg.PlotItem] = []
        self.cursors: list[pg.InfiniteLine] = []
        self.shades: list = []
        specs = [("FC bpm", 3), ("DFA α1", 2), ("RR ms", 2), ("Cadenza", 1.4), ("Qualità", 1.1)]
        for i, (label, stretch) in enumerate(specs):
            p = self.addPlot(row=i, col=0, axisItems={"bottom": TimeAxis("bottom")})
            p.setMenuEnabled(False)
            p.hideButtons()
            p.setMouseEnabled(x=True, y=False)
            p.showGrid(x=True, y=True, alpha=0.15)
            for name in ("left", "bottom"):
                ax = p.getAxis(name)
                ax.setPen(pg.mkPen(theme.AXIS))
                ax.setTextPen(pg.mkPen(theme.MUTED))
                ax.setStyle(tickLength=-4)
            p.getAxis("left").setWidth(52)
            p.setLabel("left", label, color=theme.MUTED)
            if i < len(specs) - 1:
                p.getAxis("bottom").setStyle(showValues=False)
            if self.plots:
                p.setXLink(self.plots[0])
            self.ci.layout.setRowStretchFactor(i, int(stretch * 10))
            cur = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen(theme.INK_2, width=1, style=Qt.DashLine))
            p.addItem(cur, ignoreBounds=True)
            self.cursors.append(cur)
            self.plots.append(p)
        hr, a1, rr, cad, q = self.plots
        lg = hr.addLegend(offset=(-8, -4), labelTextColor=theme.INK_2, labelTextSize="9pt",
                          brush=pg.mkBrush(26, 26, 25, 200), pen=pg.mkPen(theme.BORDER))
        lg.setColumnCount(3)
        self.hr_ecg = hr.plot(pen=pg.mkPen(theme.SERIES_1, width=1.8), name="FC dall'ECG")
        self.hr_strap = hr.plot(pen=pg.mkPen(217, 89, 38, 150, width=1), name="FC della fascia")
        self.ev_dots = pg.ScatterPlotItem(size=9, pen=pg.mkPen(theme.SURFACE, width=1))
        hr.addItem(self.ev_dots)
        lg.addItem(self.ev_dots, "evento da controllare")
        self.zone_lines: list[pg.InfiniteLine] = []
        self.a1 = a1.plot(pen=pg.mkPen(theme.SERIES_3, width=1.8))
        for v, txt in ((AET_ALPHA1, "VT1"), (ANT_ALPHA1, "VT2")):
            a1.addItem(pg.InfiniteLine(v, angle=0, pen=pg.mkPen(theme.MUTED, width=1, style=Qt.DashLine),
                                       label=txt, labelOpts={"color": theme.MUTED, "position": 0.02}))
        a1.setYRange(0.2, 1.5, padding=0)
        self.nn = rr.plot(pen=pg.mkPen(theme.SERIES_1, width=1.2))
        self.flag = pg.ScatterPlotItem(size=8, symbol="x", pen=pg.mkPen(theme.WARNING, width=1.5), brush=None)
        rr.addItem(self.flag)
        self.cad = cad.plot(pen=None, symbol="o", symbolSize=2.5, symbolPen=None,
                            symbolBrush=pg.mkBrush(57, 135, 229, 200))
        self.q = q.plot(pen=pg.mkPen(theme.GOOD, width=1.2), fillLevel=0, brush=pg.mkBrush(12, 163, 12, 40))
        q.setYRange(0, 105, padding=0)
        for p in self.plots:
            p.getViewBox().setDefaultPadding(0.02)
        self.scene().sigMouseClicked.connect(self._on_click)

    def set_report(self, rep: R.SessionReport, profile: R.Profile, events: list[R.Event]) -> None:
        for p, s in self.shades:
            p.removeItem(s)
        self.shades = []
        for a, b in rep.active:
            for p in self.plots:
                reg = pg.LinearRegionItem((a, b), movable=False, brush=pg.mkBrush(255, 255, 255, 9), pen=pg.mkPen(None))
                reg.setZValue(-20)
                p.addItem(reg, ignoreBounds=True)
                self.shades.append((p, reg))
        t = rep.m_t
        self.hr_ecg.setData(t, rep.hr_ecg, connect="finite")
        self.hr_strap.setData(t, rep.hr_strap, connect="finite")
        a1 = np.where(R._dfa_mask(rep), rep.dfa_a1, np.nan)
        self.a1.setData(t, a1, connect="finite")
        self.nn.setData(rep.b_t, rep.b_nn)
        flagged = rep.b_kind != "normal"
        self.flag.setData(rep.b_t[flagged], rep.b_rr[flagged])
        self.cad.setData(t, rep.cadence)
        self.q.setData(t, np.nan_to_num(rep.quality, nan=0.0))
        if len(rep.b_nn):
            lo, hi = np.percentile(rep.b_nn, [0.5, 99.5])
            self.plots[2].setYRange(lo - 30, hi + 30, padding=0)
        cad = rep.cadence[np.isfinite(rep.cadence)]
        self.plots[3].setYRange(*(np.percentile(cad, [1, 99]) + [-10, 10]) if len(cad) else (0, 200), padding=0)
        self.set_profile(rep, profile, events)
        dur = max(rep.duration_s, 1.0)
        for p in self.plots:
            p.setLimits(xMin=-0.02 * dur, xMax=dur * 1.02, minXRange=5.0)
        self.plots[0].setXRange(0, dur, padding=0.01)

    def set_profile(self, rep: R.SessionReport, profile: R.Profile, events: list[R.Event]) -> None:
        hr = self.plots[0]
        for ln in self.zone_lines:
            hr.removeItem(ln)
        self.zone_lines = []
        zones = R.hr_zones(rep, profile)
        if zones:
            for name, lo, hi, _ in zones[1:]:
                ln = pg.InfiniteLine(lo, angle=0, pen=pg.mkPen(theme.AXIS, width=1, style=Qt.DotLine))
                ln.setToolTip(f"inizio {name}: {lo:.0f} bpm")
                hr.addItem(ln, ignoreBounds=True)
                self.zone_lines.append(ln)
        hs = R.hr_series(rep)
        pts = [(e.t, R._at(rep, hs, e.t), e.level) for e in events if e.level != "info"]
        pts = [(t, v, lv) for t, v, lv in pts if math.isfinite(v)]
        self.ev_dots.setData([p[0] for p in pts], [p[1] for p in pts],
                             brush=[pg.mkBrush(theme.LEVEL_COLORS[p[2]]) for p in pts])
        ok = np.isfinite(hs)
        if ok.any():
            lo, hi = np.nanpercentile(hs[ok], [0.5, 99.8])
            hr.setYRange(lo - 8, hi + 12, padding=0)

    def set_cursor(self, t: float) -> None:
        for c in self.cursors:
            c.setValue(t)

    def _on_click(self, ev) -> None:
        if ev.button() != Qt.LeftButton:
            return
        for p in self.plots:
            vb = p.getViewBox()
            if vb.sceneBoundingRect().contains(ev.scenePos()):
                self.picked.emit(float(vb.mapSceneToView(ev.scenePos()).x()))
                return


class AnalysisView(QScrollArea):
    replay_requested = Signal(object, float)     # cartella, istante di partenza
    reanalyze_requested = Signal(object)
    reveal_requested = Signal(object)

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.rep: R.SessionReport | None = None
        self.profile = R.Profile()
        self.events: list[R.Event] = []
        self._shown_events: list[R.Event] = []
        page = QWidget()
        page.setObjectName("root")
        self.setWidget(page)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 4, 8, 12)
        lay.setSpacing(12)
        lay.addLayout(self._header())
        lay.addLayout(self._tiles())
        tl = Card("Cronologia · trascina o usa la rotella per lo zoom, clic per aprire l'ECG in quel punto")
        self.timeline = Timeline()
        self.timeline.setMinimumHeight(470)
        self.timeline.picked.connect(self._goto)
        tl.layout_.addWidget(self.timeline)
        lay.addWidget(tl)
        lay.addWidget(self._ecg_card())
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(self._events_card(), 5)
        row.addWidget(self._morph_card(), 3)
        row.addWidget(self._poincare_card(), 3)
        lay.addLayout(row)
        row2 = QHBoxLayout()
        row2.setSpacing(12)
        row2.addWidget(self._zones_card(), 1)
        row2.addWidget(self._threshold_card(), 1)
        row2.addWidget(self._recovery_card(), 1)
        lay.addLayout(row2)
        self.kv = KeyValueGrid("Dati della registrazione", [
            "Fascia", "Firmware", "Registrata con", "Durata", "Pacchetti", "Buchi nei dati", "Errori decoder",
            "Contatto perso", "Battiti rilevati", "Intervalli corretti", "Accordo RR con la fascia", "Batteria",
            "Respiro medio", "Oscillazione verticale"])
        lay.addWidget(self.kv)
        note = QLabel(DISCLAIMER)
        note.setObjectName("disclaimer")
        note.setWordWrap(True)
        lay.addWidget(note)
        lay.addStretch(1)

    # ------------------------------------------------------------------ costruzione

    def _header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        self.title = QLabel("")
        self.title.setObjectName("h1")
        row.addWidget(self.title)
        self.chip_src = Chip()
        self.chip_dur = Chip()
        row.addWidget(self.chip_src)
        row.addWidget(self.chip_dur)
        row.addStretch(1)
        play = QPushButton("▶ Riproduci")
        play.setObjectName("primary")
        play.setToolTip("Rivedi la sessione nel monitor in diretta, dall'inizio")
        play.clicked.connect(lambda: self.rep and self.replay_requested.emit(self.rep.dir, 0.0))
        here = QPushButton("Riproduci da qui")
        here.setToolTip("Riparte dal punto mostrato nell'esploratore ECG")
        here.clicked.connect(lambda: self.rep and self.replay_requested.emit(self.rep.dir, max(0.0, self.ecg.t0 - 5)))
        finder = QPushButton("Mostra nel Finder")
        finder.clicked.connect(lambda: self.rep and self.reveal_requested.emit(self.rep.dir))
        again = QPushButton("Rianalizza")
        again.setToolTip("Rielabora il log grezzo con gli algoritmi attuali e rigenera le CSV")
        again.clicked.connect(lambda: self.rep and self.reanalyze_requested.emit(self.rep.dir))
        for b in (play, here, finder, again):
            row.addWidget(b)
        self.title.setMinimumWidth(0)
        return row

    def _tiles(self) -> QGridLayout:
        g = QGridLayout()
        g.setSpacing(12)
        self.t_hr = Tile("Frequenza cardiaca media", "bpm")
        self.t_thr = Tile("Soglie stimate (DFA α1)", "bpm", chip=True)
        self.t_rec = Tile("Recupero", "bpm")
        self.t_hrv = Tile("HRV a riposo · RMSSD", "ms")
        self.t_ev = Tile("Da controllare", "", chip=True)
        self.t_q = Tile("Qualità dei dati", "%", chip=True)
        self.t_mov = Tile("Passi", "")
        tiles = (self.t_hr, self.t_ev, self.t_thr, self.t_rec, self.t_hrv, self.t_q, self.t_mov)
        for i, t in enumerate(tiles):
            t.sub.setMinimumHeight(32)
            t.setMinimumWidth(0)
            t.title.setMinimumWidth(0)
            g.addWidget(t, i // 4, i % 4)
        for c in range(4):
            g.setColumnStretch(c, 1)
        return g

    def _ecg_card(self) -> Card:
        c = Card("Esploratore ECG")
        self.prev_ev = QPushButton("◀ evento")
        self.next_ev = QPushButton("evento ▶")
        self.prev_ev.clicked.connect(lambda: self._step_event(-1))
        self.next_ev.clicked.connect(lambda: self._step_event(+1))
        self.win = QComboBox()
        for s in (5, 10, 20, 30):
            self.win.addItem(f"{s} s", s)
        self.win.setCurrentIndex(1)
        self.win.currentIndexChanged.connect(lambda _: self._set_window(self.win.currentData()))
        self.gain = QComboBox()
        self.gain.addItem("10 mm/mV", 1.0)
        self.gain.addItem("20 mm/mV", 2.0)
        self.gain.addItem("5 mm/mV", 0.5)
        self.gain.currentIndexChanged.connect(lambda _: self.ecg.set_gain(self.gain.currentData()))
        png = QPushButton("Esporta PNG")
        png.setToolTip("Salva l'immagine della striscia, per esempio da mostrare a un medico")
        png.clicked.connect(self._export_png)
        legend = QLabel(f"<span style='color:{theme.INK_2}'>▼</span> normale  "
                        f"<span style='color:{theme.SERIES_2}'>▼</span> in anticipo  "
                        f"<span style='color:{theme.WARNING}'>▼</span> corretto")
        legend.setObjectName("muted")
        for w in (legend, self.prev_ev, self.next_ev, self.gain, self.win, png):
            c.header.addWidget(w)
        self.ecg = EcgStrip(10.0)
        self.ecg.setMinimumHeight(260)
        self.ecg.clicked.connect(lambda t: self.timeline.set_cursor(t))
        c.layout_.addWidget(self.ecg)
        self.scroll = QScrollBar(Qt.Horizontal)
        self.scroll.valueChanged.connect(self._on_scroll)
        c.layout_.addWidget(self.scroll)
        return c

    def _events_card(self) -> Card:
        c = Card("Eventi da controllare")
        self.ev_filter = QComboBox()
        self.ev_filter.addItem("Cuore e ritmo", "heart")
        self.ev_filter.addItem("Tutti, anche i dati", "all")
        self.ev_filter.currentIndexChanged.connect(lambda _: self._fill_events())
        c.header.addWidget(self.ev_filter)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["ORA", "EVENTO", "DETTAGLIO"])
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setMinimumHeight(260)
        self.table.cellClicked.connect(lambda r, _: self._open_event(r))
        self.table.currentCellChanged.connect(lambda r, *_: r >= 0 and self._open_event(r))
        c.layout_.addWidget(self.table)
        self.ev_empty = QLabel("")
        self.ev_empty.setObjectName("sub")
        self.ev_empty.setWordWrap(True)
        c.layout_.addWidget(self.ev_empty)
        return c

    def _morph_card(self) -> Card:
        c = Card("Forma del battito")
        self.morph = styled_plot("µV", "ms dal picco R")
        p = self.morph.getPlotItem()
        p.addLegend(offset=(-6, 4), labelTextColor=theme.INK_2, labelTextSize="9pt",
                    brush=pg.mkBrush(26, 26, 25, 200), pen=pg.mkPen(theme.BORDER))
        self.morph_n = p.plot(pen=pg.mkPen(theme.ECG_TRACE, width=2.5), name="battito normale (mediana)")
        self.morph_e = p.plot(pen=pg.mkPen(theme.SERIES_2, width=2), name="battiti in anticipo")
        c.layout_.addWidget(self.morph)
        self.morph_sub = QLabel("")
        self.morph_sub.setObjectName("sub")
        self.morph_sub.setWordWrap(True)
        c.layout_.addWidget(self.morph_sub)
        return c

    def _poincare_card(self) -> Card:
        c = Card("Poincaré · intera sessione")
        self.poin = styled_plot("RRₙ₊₁ ms", "RRₙ ms")
        p = self.poin.getPlotItem()
        self.poin_id = p.plot(pen=pg.mkPen(theme.AXIS, width=1, style=Qt.DashLine))
        self.poin_dots = pg.ScatterPlotItem(size=4, pen=None, brush=pg.mkBrush(57, 135, 229, 110))
        self.poin_flag = pg.ScatterPlotItem(size=7, symbol="x", pen=pg.mkPen(theme.SERIES_2, width=1.5), brush=None)
        p.addItem(self.poin_dots)
        p.addItem(self.poin_flag)
        self.poin.setAspectLocked(True)
        c.layout_.addWidget(self.poin)
        return c

    def _zones_card(self) -> Card:
        c = Card("Tempo nelle zone")
        self.zones_plot = styled_plot("", "minuti")
        self.zones_plot.getPlotItem().getAxis("left").setWidth(110)
        c.layout_.addWidget(self.zones_plot)
        self.zones_sub = QLabel("")
        self.zones_sub.setObjectName("sub")
        self.zones_sub.setWordWrap(True)
        c.layout_.addWidget(self.zones_sub)
        return c

    def _threshold_card(self) -> Card:
        c = Card("α1 e frequenza cardiaca")
        self.thr_plot = styled_plot("DFA α1", "FC bpm")
        p = self.thr_plot.getPlotItem()
        self.thr_dots = pg.ScatterPlotItem(size=5, pen=None, brush=pg.mkBrush(25, 158, 112, 160))
        p.addItem(self.thr_dots)
        self.thr_fit = p.plot(pen=pg.mkPen(theme.INK_2, width=1.5))
        for v in (AET_ALPHA1, ANT_ALPHA1):
            p.addItem(pg.InfiniteLine(v, angle=0, pen=pg.mkPen(theme.MUTED, width=1, style=Qt.DashLine)))
        self.thr_marks = pg.ScatterPlotItem(size=11, symbol="d", pen=pg.mkPen(theme.SURFACE), brush=pg.mkBrush(theme.INK))
        p.addItem(self.thr_marks)
        c.layout_.addWidget(self.thr_plot)
        self.thr_sub = QLabel("")
        self.thr_sub.setObjectName("sub")
        self.thr_sub.setWordWrap(True)
        c.layout_.addWidget(self.thr_sub)
        return c

    def _recovery_card(self) -> Card:
        c = Card("Recupero dopo lo sforzo")
        self.rec_plot = styled_plot("bpm", "s dal picco")
        p = self.rec_plot.getPlotItem()
        self.rec_curve = p.plot(pen=pg.mkPen(theme.SERIES_1, width=2))
        self.rec_marks = pg.ScatterPlotItem(size=9, pen=pg.mkPen(theme.SURFACE), brush=pg.mkBrush(theme.INK))
        p.addItem(self.rec_marks)
        p.addItem(pg.InfiniteLine(0, angle=90, pen=pg.mkPen(theme.AXIS, width=1, style=Qt.DashLine)))
        c.layout_.addWidget(self.rec_plot)
        self.rec_sub = QLabel("")
        self.rec_sub.setObjectName("sub")
        self.rec_sub.setWordWrap(True)
        c.layout_.addWidget(self.rec_sub)
        return c

    # ------------------------------------------------------------------ dati

    def set_report(self, rep: R.SessionReport, profile: R.Profile) -> None:
        self.rep, self.profile = rep, profile
        self.events = R.events(rep, profile)
        start = session_start(rep.meta, rep.dir)
        self.title.setText(long_date(start))
        self.chip_src.set("iPhone" if rep.is_iphone else "Mac", theme.SERIES_1)
        self.chip_dur.set(duration_text(rep.duration_s), theme.INK_2)
        self.timeline.set_report(rep, profile, self.events)
        self.ecg.set_report(rep)
        self._sync_scroll()
        self._fill_tiles()
        self._fill_events()
        self._fill_morph()
        self._fill_poincare()
        self._fill_zones()
        self._fill_thresholds()
        self._fill_recovery()
        self._fill_kv()
        first = next((e for e in self.events if e.level != "info"), None)
        self._goto(first.t if first else self._default_ecg_time(), mark=first)
        self.verticalScrollBar().setValue(0)

    def set_profile(self, profile: R.Profile) -> None:
        if self.rep is None:
            return
        self.profile = profile
        self.events = R.events(self.rep, profile)
        self.timeline.set_profile(self.rep, profile, self.events)
        self._fill_tiles()
        self._fill_events()
        self._fill_zones()

    def _default_ecg_time(self) -> float:
        rep = self.rep
        if len(rep.active):
            a, b = rep.active[0]
            return float(min(a + 60.0, (a + b) / 2))
        return min(30.0, rep.duration_s / 2)

    def _fill_tiles(self) -> None:
        rep = self.rep
        hs = R.hr_stats(rep)
        self.t_hr.set(fmt(hs["avg"]), f"max {fmt(hs['max'])} · min {fmt(hs['min'])} bpm")
        th = R.thresholds(rep)
        if th and (math.isfinite(th["vt1"]) or math.isfinite(th["vt2"])):
            self.t_thr.set(f"{fmt(th['vt1'])} · {fmt(th['vt2'])}", f"VT1 · VT2 · R² {th['r2']:.2f}")
            ok = th["vt1_reliable"] or th["vt2_reliable"]
            self.t_thr.chip.set("affidabile" if th["vt1_reliable"] and th["vt2_reliable"] else
                                "parziale" if ok else "indicativa", theme.GOOD if ok else theme.WARNING)
        elif th:
            self.t_thr.set("—", f"legame α1–FC troppo debole (R² {th['r2']:.2f}): serve uno sforzo crescente")
            self.t_thr.chip.set("non stimabile", theme.MUTED)
        else:
            self.t_thr.set("—", "servono ≥ 10 min di sforzo crescente con segnale pulito")
            self.t_thr.chip.set("—", theme.MUTED)
        rec = R.recovery(rep)
        if rec and rec["drops"]:
            dt = 60 if 60 in rec["drops"] else max(rec["drops"])
            others = " · ".join(f"{k} s −{v:.0f}" for k, v in sorted(rec["drops"].items()) if k != dt)
            self.t_rec.set(f"−{rec['drops'][dt]:.0f}", f"in {dt} s dal picco di {rec['hr_peak']:.0f} bpm"
                           + (f" · {others}" if others else ""))
        else:
            self.t_rec.set("—", "registra 1–2 min da fermo dopo lo sforzo")
        rh = R.rest_hrv(rep)
        if rh:
            td, a, b = rh
            self.t_hrv.set(fmt(td.rmssd), f"SDNN {td.sdnn:.0f} ms · FC {td.hr:.0f} · {(b - a) / 60:.0f} min da fermo")
        else:
            act = R.activity(rep)
            self.t_hrv.set("—", f"nessun tratto da fermo prima del movimento"
                           + (f" · in corsa {act['rmssd_active']:.0f} ms" if math.isfinite(act["rmssd_active"]) else ""))
        n_s = sum(e.level == "serious" for e in self.events)
        n_w = sum(e.level == "warning" for e in self.events)
        n_i = sum(e.level == "info" for e in self.events)
        self.t_ev.set(str(n_s + n_w), f"{n_s} da rivedere · {n_w} da notare · {n_i} sui dati")
        if n_s:
            self.t_ev.chip.set("da rivedere", theme.CRITICAL)
        elif n_w:
            self.t_ev.chip.set("da notare", theme.WARNING)
        else:
            self.t_ev.chip.set("nulla di rilevante", theme.GOOD)
        dq = R.data_quality(rep)
        self.t_q.set(fmt(dq["good_pct"]), f"tempo con segnale buono · artefatti {fmt(dq['artifact_pct'], 1)}% · "
                     f"buchi {rep.stats.get('gaps', 0)}")
        qm = dq["quality_median"]
        self.t_q.chip.set("ottima" if qm >= 85 else "buona" if qm >= 65 else "scarsa" if qm >= 35 else "inutilizzabile"
                          if math.isfinite(qm) else "—", theme.quality_color(qm))
        act = R.activity(rep)
        self.t_mov.set(str(act["steps"]) if act["steps"] else "—",
                       f"cadenza {fmt(act['cadence'])} passi/min · {duration_text(act['active_s'])} in movimento"
                       if act["active_s"] else "nessun tratto in movimento")

    def _fill_events(self) -> None:
        heart = self.ev_filter.currentData() == "heart"
        evs = [e for e in self.events if not (heart and e.level == "info")]
        self._shown_events = evs
        self.table.blockSignals(True)
        self.table.setRowCount(len(evs))
        for r, e in enumerate(evs):
            when = clock(e.t) + (f"–{clock(e.end)}" if e.end is not None and e.end - e.t >= 1 else "")
            items = [QTableWidgetItem(when), QTableWidgetItem(f"● {e.title}"), QTableWidgetItem(e.detail)]
            items[1].setForeground(pg.mkColor(theme.LEVEL_COLORS[e.level]))
            items[1].setToolTip(theme.LEVEL_LABELS[e.level])
            items[0].setForeground(pg.mkColor(theme.INK_2))
            items[2].setToolTip(e.detail)
            for c, it in enumerate(items):
                self.table.setItem(r, c, it)
        self.table.blockSignals(False)
        hidden = len(self.events) - len(evs)
        if not evs:
            self.ev_empty.setText("Nessun battito prematuro, pausa, salto di frequenza o irregolarità del ritmo."
                                  + (f" {hidden} segnalazioni sulla qualità dei dati: scegli «Tutti»." if hidden else ""))
        else:
            self.ev_empty.setText(f"Clic su un evento per vederlo nell'ECG."
                                  + (f" Nascoste {hidden} segnalazioni sui dati." if hidden else ""))

    def _fill_morph(self) -> None:
        rep = self.rep
        if len(rep.tpl_normal):
            t = rep.tpl_t * 1000.0
            self.morph_n.setData(t, rep.tpl_normal)
            self.morph_e.setData(t, rep.tpl_ectopic) if len(rep.tpl_ectopic) else self.morph_e.setData([], [])
            ys = np.concatenate([rep.tpl_normal, rep.tpl_ectopic])
            pad = 0.08 * float(np.ptp(ys))
            self.morph.setRange(xRange=(t[0], t[-1]), yRange=(ys.min() - pad, ys.max() + 3 * pad), padding=0)
            n = int(np.sum(rep.b_kind == "normal"))
            extra = ""
            if len(rep.ect_corr):
                extra = (f" · {len(rep.ect_corr)} battiti in anticipo, somiglianza del QRS mediana "
                         f"r = {np.nanmedian(rep.ect_corr):.2f}")
            self.morph_sub.setText(f"Mediana di {min(n, 1500)} battiti normali, filtrata a fase zero{extra}.")
        else:
            self.morph_n.setData([], [])
            self.morph_e.setData([], [])
            self.morph_sub.setText("Troppi pochi battiti per il battito medio.")

    def _fill_poincare(self) -> None:
        rep = self.rep
        nn = rep.b_rr
        if len(nn) < 3:
            self.poin_dots.setData([], [])
            self.poin_flag.setData([], [])
            return
        x, y = nn[:-1], nn[1:]
        ok = (rep.b_kind[:-1] == "normal") & (rep.b_kind[1:] == "normal")
        idx = np.flatnonzero(ok)
        if len(idx) > 4000:
            idx = idx[np.linspace(0, len(idx) - 1, 4000).astype(int)]
        self.poin_dots.setData(x[idx], y[idx])
        self.poin_flag.setData(x[~ok], y[~ok])
        lo, hi = float(np.percentile(nn, 0.5)) - 40, float(np.percentile(nn, 99.5)) + 40
        self.poin_id.setData([lo, hi], [lo, hi])
        self.poin.setRange(xRange=(lo, hi), yRange=(lo, hi), padding=0)

    def _fill_zones(self) -> None:
        p = self.zones_plot.getPlotItem()
        p.clear()
        rows: list[tuple[str, float, str]] = []
        zones = R.hr_zones(self.rep, self.profile)
        colors = ["#3987e5", "#199e70", "#fab219", "#ec835a", "#d03b3b"]
        if zones:
            for (name, lo, hi, sec), col in zip(zones, colors):
                rng = f"< {hi:.0f}" if lo == 0 else f"≥ {lo:.0f}" if not math.isfinite(hi) else f"{lo:.0f}–{hi:.0f}"
                rows.append((f"{name} {rng}", sec / 60.0, col))
        az = R.alpha1_zones(self.rep)
        for (name, sec), col in zip(az.items(), (theme.GOOD, theme.WARNING, theme.CRITICAL)):
            rows.append((f"α1 {name}", sec / 60.0, col))
        rows = rows[::-1]
        if rows:
            ys = np.arange(len(rows))
            bars = pg.BarGraphItem(x0=np.zeros(len(rows)), y=ys, height=0.62, width=[r[1] for r in rows],
                                   brushes=[pg.mkBrush(r[2]) for r in rows], pens=[pg.mkPen(None)] * len(rows))
            p.addItem(bars)
            p.getAxis("left").setTicks([[(i, r[0]) for i, r in enumerate(rows)]])
            top = max(r[1] for r in rows)
            p.setYRange(-0.6, len(rows) - 0.4, padding=0)
            p.setXRange(0, max(top * 1.2, 1.0), padding=0)
            for i, r in enumerate(rows):
                ti = pg.TextItem(f"{r[1]:.1f}" if r[1] < 10 else f"{r[1]:.0f}", color=theme.INK_2, anchor=(0, 0.5))
                ti.setPos(r[1], i)
                p.addItem(ti)
        if zones:
            base = "riserva cardiaca (Karvonen)" if self.profile.hr_rest else "% della FC max"
            self.zones_sub.setText(f"Zone FC su {base}: FC max {self.profile.hr_max:.0f}. "
                                   "Zone α1 solo con artefatti < 5% e segnale buono.")
        else:
            self.zones_sub.setText("Imposta la FC max in Impostazioni per vedere le zone di frequenza. "
                                   "Zone α1 solo con artefatti < 5% e segnale buono.")

    def _fill_thresholds(self) -> None:
        th = R.thresholds(self.rep)
        m = R._dfa_mask(self.rep)
        hs = R.hr_series(self.rep)
        idx = np.flatnonzero(m)[::5]
        self.thr_dots.setData(hs[idx], self.rep.dfa_a1[idx])
        if th:
            xs = np.array([th["hr"].min(), th["hr"].max()])
            pad = 15
            xs = np.array([xs[0] - pad, xs[1] + pad])
            self.thr_fit.setData(xs, th["intercept"] + th["slope"] * xs)
            pts = [(th[k], v) for k, v in (("vt1", AET_ALPHA1), ("vt2", ANT_ALPHA1)) if math.isfinite(th[k])]
            self.thr_marks.setData([p[0] for p in pts], [p[1] for p in pts])
            def word(k):
                return "affidabile" if th[k + "_reliable"] else "da confermare"
            if not (math.isfinite(th["vt1"]) or math.isfinite(th["vt2"])):
                self.thr_fit.setData([], [])
                self.thr_sub.setText(f"Su {len(th['hr'])} punti α1 e FC sono poco legati (R² {th['r2']:.2f}): nessuna "
                                     "soglia stimabile. Il metodo funziona in una corsa a intensità crescente.")
            else:
                self.thr_sub.setText(
                f"Retta di regressione su {len(th['hr'])} punti (R² {th['r2']:.2f}). "
                f"VT1 (α1 = 0.75): {fmt(th['vt1'])} bpm, {word('vt1')}. VT2 (α1 = 0.5): {fmt(th['vt2'])} bpm, "
                f"{word('vt2')}. Il metodo funziona meglio in una corsa a intensità crescente.")
        else:
            self.thr_fit.setData([], [])
            self.thr_marks.setData([], [])
            self.thr_sub.setText("Stima non disponibile: servono almeno ~10 minuti di α1 valido con frequenza che "
                                 "sale di 15 bpm o più (per esempio un riscaldamento progressivo).")
        p = self.thr_plot.getPlotItem()
        if len(idx):
            p.enableAutoRange()
        else:
            p.setRange(xRange=(60, 190), yRange=(0.2, 1.5), padding=0)

    def _fill_recovery(self) -> None:
        rep = self.rep
        rec = R.recovery(rep)
        if not rec or not rec["drops"]:
            self.rec_curve.setData([], [])
            self.rec_marks.setData([], [])
            self.rec_sub.setText("La registrazione finisce subito dopo lo sforzo: per misurare il recupero "
                                 "lascia registrare 1–2 minuti da fermo." if rec else
                                 "Nessuno sforzo seguito da una pausa in questa sessione.")
            return
        if not rec:
            self.rec_curve.setData([], [])
            self.rec_marks.setData([], [])
            self.rec_sub.setText("Nessuno sforzo seguito da una pausa in questa sessione.")
            return
        hs = R._rolling_median(R.hr_series(rep), 3)
        m = (rep.m_t >= rec["t_peak"] - 30) & (rep.m_t <= rec["t_peak"] + 180)
        self.rec_curve.setData(rep.m_t[m] - rec["t_peak"], hs[m], connect="finite")
        pts = [(0.0, rec["hr_peak"])] + [(k, rec["hr_peak"] - v) for k, v in sorted(rec["drops"].items())]
        self.rec_marks.setData([p[0] for p in pts], [p[1] for p in pts])
        if rec["drops"]:
            txt = " · ".join(f"dopo {k} s: −{v:.0f} bpm" for k, v in sorted(rec["drops"].items()))
            self.rec_sub.setText(f"Picco {rec['hr_peak']:.0f} bpm al termine del movimento ({clock(rec['t_peak'])}). "
                                 f"{txt}. Un calo di oltre 12 bpm nel primo minuto è considerato nella norma.")
        else:
            self.rec_sub.setText("La registrazione finisce subito dopo lo sforzo: per misurare il recupero "
                                 "lascia registrare 1–2 minuti da fermo.")
        self.rec_plot.getPlotItem().enableAutoRange()

    def _fill_kv(self) -> None:
        rep, meta = self.rep, self.rep.meta
        info = meta.get("device_info", {}) or {}
        dq = R.data_quality(rep)
        k = dq["kinds"]
        st = rep.stats
        kv = self.kv
        kv.set("Fascia", meta.get("device") or "—")
        kv.set("Firmware", info.get("firmware", "—"))
        kv.set("Registrata con", "iPhone" if rep.is_iphone else f"Mac ({meta.get('source', '—')})")
        kv.set("Durata", duration_text(rep.duration_s))
        kv.set("Pacchetti", f"{st.get('packets', 0):,}".replace(",", "."))
        miss = st.get("missing_samples", 0)
        kv.set("Buchi nei dati", f"{st.get('gaps', 0)} ({miss} campioni, {miss / rep.ecg_fs:.1f} s)")
        kv.set("Errori decoder", str(st.get("errors", 0)))
        kv.set("Contatto perso", f"{dq['contact_lost_s']:.0f} s" if dq["contact_lost_s"] else "mai")
        kv.set("Battiti rilevati", f"{dq['beats']:,}".replace(",", "."))
        kv.set("Intervalli corretti", f"{fmt(dq['artifact_pct'], 1)} % · ect {k['ectopic']} · l/c {k['longshort']} · "
                                      f"manc {k['missed']} · extra {k['extra']}")
        kv.set("Accordo RR con la fascia", f"{fmt(dq['strap_match'])} % · {fmt(dq['strap_mae'], 1)} ms")
        b = dq["battery"]
        kv.set("Batteria", f"{b[0]} → {b[1]} %" if b else "—")
        resp = rep.resp[np.isfinite(rep.resp)]
        kv.set("Respiro medio", f"{np.median(resp):.0f} atti/min" if len(resp) else "—")
        act = R.activity(rep)
        kv.set("Oscillazione verticale", f"{fmt(act['vosc'], 1)} cm")

    # ------------------------------------------------------------------ navigazione

    def _set_window(self, seconds: float) -> None:
        self.ecg.set_window(seconds)
        self._sync_scroll()

    def _sync_scroll(self) -> None:
        if self.rep is None:
            return
        self.scroll.blockSignals(True)
        maxv = int(max(0.0, self.rep.duration_s - self.ecg.window_s) * 10)
        self.scroll.setRange(0, maxv)
        self.scroll.setPageStep(int(self.ecg.window_s * 10))
        self.scroll.setSingleStep(int(self.ecg.window_s * 2.5))
        self.scroll.setValue(int(self.ecg.t0 * 10))
        self.scroll.blockSignals(False)

    def _on_scroll(self, v: int) -> None:
        self.ecg.show_at(v / 10.0, center=False)
        self.timeline.set_cursor(self.ecg.center)

    def _goto(self, t: float, mark: R.Event | None = None) -> None:
        self.ecg.show_at(t)
        if mark is not None:
            self.ecg.mark(mark.t, mark.end)
        else:
            self.ecg.mark(None)
        self.timeline.set_cursor(t)
        self._sync_scroll()

    def _open_event(self, row: int) -> None:
        if 0 <= row < len(self._shown_events):
            e = self._shown_events[row]
            self._goto(e.t if e.end is None else min(e.t + self.ecg.window_s / 2 - 1, (e.t + e.end) / 2), mark=e)

    def _step_event(self, direction: int) -> None:
        evs = self._shown_events
        if not evs:
            return
        c = self.ecg.center
        if direction > 0:
            nxt = next((i for i, e in enumerate(evs) if e.t > c + 0.5), None)
        else:
            nxt = next((i for i in range(len(evs) - 1, -1, -1) if evs[i].t < c - 0.5), None)
        if nxt is not None:
            self.table.selectRow(nxt)
            self._open_event(nxt)

    def _export_png(self) -> None:
        if self.rep is None:
            return
        name = f"ECG_{self.rep.dir.name}_{clock(self.ecg.t0).replace(':', '')}.png"
        path, _ = QFileDialog.getSaveFileName(self, "Esporta striscia ECG", str(Path.home() / "Desktop" / name),
                                              "Immagine PNG (*.png)")
        if path:
            self.ecg.grab().save(path)
