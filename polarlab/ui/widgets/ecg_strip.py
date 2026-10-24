"""Striscia ECG ferma su carta millimetrata, per sfogliare una sessione registrata.

Stesse proporzioni del monitor in diretta (25 mm/s, 10 mm/mV). I picchi R sono marcati
con il colore del tipo di battito e, sopra ogni intervallo, l'RR in millisecondi.
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Signal

from polarlab.ui import theme
from polarlab.ui.widgets.ecg_sweep import BIG_S, BIG_UV, SMALL_S, SMALL_UV, paper_grid, paper_y_span

KIND_COLORS = {"normal": theme.INK_2, "premature": theme.SERIES_2, "flagged": theme.WARNING}
MAX_LABELS = 60


def clock(t: float) -> str:
    t = max(0.0, float(t))
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class EcgStrip(pg.GraphicsLayoutWidget):
    clicked = Signal(float)

    def __init__(self, window_s: float = 10.0):
        super().__init__()
        self.setBackground(theme.SURFACE)
        self.window_s = window_s
        self.gain = 1.0
        self.t0 = 0.0
        self.rep = None
        self.plot = self.addPlot()
        self.plot.hideAxis("left")
        self.plot.setMenuEnabled(False)
        self.plot.hideButtons()
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.getViewBox().setDefaultPadding(0)
        ax = self.plot.getAxis("bottom")
        ax.setPen(pg.mkPen(theme.AXIS))
        ax.setTextPen(pg.mkPen(theme.MUTED))
        ax.tickStrings = lambda values, scale, spacing: [clock(v) for v in values]
        self.ci.setContentsMargins(0, 0, 0, 0)

        self._minor = pg.PlotCurveItem(pen=pg.mkPen(theme.ECG_GRID_MINOR, width=1), connect="pairs")
        self._major = pg.PlotCurveItem(pen=pg.mkPen(theme.ECG_GRID_MAJOR, width=1), connect="pairs")
        self.highlight = pg.LinearRegionItem(movable=False, brush=pg.mkBrush(250, 178, 25, 40),
                                             pen=pg.mkPen(theme.WARNING, width=1))
        self.highlight.hide()
        self.trace = pg.PlotCurveItem(pen=pg.mkPen(theme.ECG_TRACE, width=1.6), connect="finite", antialias=True)
        self.beats = pg.ScatterPlotItem(size=9, symbol="t", pen=None)
        for it in (self._minor, self._major, self.highlight, self.trace, self.beats):
            self.plot.addItem(it)
        self.labels = []
        self.caption = pg.TextItem("", color=theme.MUTED, anchor=(0, 0))
        self.caption.setParentItem(self.plot.getViewBox())
        self.caption.setPos(8, 6)
        self.message = pg.TextItem("", color=theme.INK_2, anchor=(0.5, 0.5))
        self.plot.addItem(self.message)
        self.scene().sigMouseClicked.connect(self._on_click)

    # ------------------------------------------------------------------ API

    def set_report(self, rep) -> None:
        self.rep = rep
        self.highlight.hide()
        self.show_at(0.0, center=False)

    def set_window(self, seconds: float) -> None:
        center = self.t0 + self.window_s / 2
        self.window_s = seconds
        self.show_at(center)

    def set_gain(self, gain: float) -> None:
        self.gain = gain
        self.redraw()

    def mark(self, a: float | None, b: float | None = None) -> None:
        """Evidenzia un evento (istante o intervallo); None toglie l'evidenziazione."""
        if a is None:
            self.highlight.hide()
            return
        b = b if b is not None and b > a else a
        pad = 0.25 if b == a else 0.0
        self.highlight.setRegion((a - pad, b + pad))
        self.highlight.show()

    def show_at(self, t: float, center: bool = True) -> None:
        dur = self.rep.duration_s if self.rep is not None else 0.0
        start = t - self.window_s / 2 if center else t
        self.t0 = float(np.clip(start, 0.0, max(0.0, dur - self.window_s)))
        self.redraw()

    @property
    def center(self) -> float:
        return self.t0 + self.window_s / 2

    # ------------------------------------------------------------------ disegno

    def redraw(self) -> None:
        rep = self.rep
        x0, x1 = self.t0, self.t0 + self.window_s
        vb = self.plot.getViewBox()
        span = paper_y_span(vb.width(), vb.height(), self.window_s, self.gain)
        if rep is None or not len(rep.ecg_t):
            self.trace.setData([], [])
            self.beats.setData([], [])
            self._set_labels([])
            self.message.setText("Nessun ECG in questa sessione")
            self.message.setPos((x0 + x1) / 2, 0)
            self._apply(x0, x1, -span / 2, span / 2)
            return
        self.message.setText("")
        i0, i1 = np.searchsorted(rep.ecg_t, [x0 - 0.05, x1 + 0.05])
        t, y = rep.ecg_t[i0:i1], rep.ecg[i0:i1].astype(float)
        if len(t) > 1:
            # Interrompe la traccia sui buchi di dati invece di unire i punti
            jump = np.flatnonzero(np.diff(t) > 3.0 / rep.ecg_fs)
            if len(jump):
                t = np.insert(t, jump + 1, t[jump] + 1.0 / rep.ecg_fs)
                y = np.insert(y, jump + 1, np.nan)
        self.trace.setData(t, y)
        if len(y) and np.isfinite(y).any():
            lo, hi = np.nanpercentile(y, 1), np.nanpercentile(y, 99.5)
            mid = 0.5 * (lo + hi)
        else:
            mid = 0.0
        y0, y1 = mid - span / 2, mid + span / 2
        self._apply(x0, x1, y0, y1)

        r = (rep.r_t >= x0) & (rep.r_t <= x1)
        rt = rep.r_t[r]
        kinds = self._kinds(rt)
        top = y1 - 0.13 * span          # sotto la didascalia in alto a sinistra
        self.beats.setData(rt, np.full(len(rt), top),
                           brush=[pg.mkBrush(KIND_COLORS[k]) for k in kinds])
        labels = []
        all_r = rep.r_t
        j = np.flatnonzero(r)
        for k, idx in enumerate(j):
            if idx == 0 or len(labels) >= MAX_LABELS:
                continue
            rr = (all_r[idx] - all_r[idx - 1]) * 1000.0
            labels.append(((all_r[idx] + all_r[idx - 1]) / 2, top + 0.01 * span, f"{rr:.0f}", KIND_COLORS[kinds[k]]))
        self._set_labels(labels)
        self.caption.setText(f"{clock(x0)} – {clock(x1)}   ·   25 mm/s · {int(10 * self.gain)} mm/mV   ·   RR in ms")

    def _kinds(self, rt: np.ndarray) -> list[str]:
        """Tipo di ogni picco R: in anticipo (prematuro), corretto dal filtro artefatti, normale."""
        rep = self.rep
        out = []
        if not len(rep.b_t):
            return ["normal"] * len(rt)
        idx = np.clip(np.searchsorted(rep.b_t, rt), 0, len(rep.b_t) - 1)
        for t, i in zip(rt, idx):
            cand = [c for c in (i - 1, i) if 0 <= c < len(rep.b_t) and abs(rep.b_t[c] - t) < 0.02]
            if not cand:
                out.append("normal")
                continue
            c = cand[0]
            if rep.b_kind[c] == "normal":
                out.append("normal")
            elif rep.b_rr[c] < 0.85 * rep.b_med[c]:
                out.append("premature")
            else:
                out.append("flagged")
        return out

    def _set_labels(self, labels) -> None:
        while len(self.labels) < len(labels):
            ti = pg.TextItem("", anchor=(0.5, 1.0))
            self.plot.addItem(ti)
            self.labels.append(ti)
        for ti, (x, y, text, color) in zip(self.labels, labels):
            ti.setText(text, color=color)
            ti.setPos(x, y)
            ti.show()
        for ti in self.labels[len(labels):]:
            ti.hide()

    def _apply(self, x0, x1, y0, y1) -> None:
        self.plot.setRange(xRange=(x0, x1), yRange=(y0, y1), padding=0)
        self._minor.setData(*paper_grid(x0, x1, y0, y1, SMALL_S, SMALL_UV))
        self._major.setData(*paper_grid(x0, x1, y0, y1, BIG_S, BIG_UV))

    def _on_click(self, ev) -> None:
        vb = self.plot.getViewBox()
        if vb.sceneBoundingRect().contains(ev.scenePos()):
            self.clicked.emit(float(vb.mapSceneToView(ev.scenePos()).x()))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if hasattr(self, "trace"):      # pyqtgraph chiama resizeEvent già dentro __init__
            self.redraw()
