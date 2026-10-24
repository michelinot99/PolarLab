"""Grafici secondari: tacogramma RR, Poincaré, battito medio, accelerazione verticale."""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt

from polarlab.ui import theme
from polarlab.ui.widgets.common import Card, styled_plot


def _legend(plot: pg.PlotItem) -> pg.LegendItem:
    lg = plot.addLegend(offset=(-8, 6), labelTextColor=theme.INK_2, labelTextSize="9pt",
                        brush=pg.mkBrush(26, 26, 25, 200), pen=pg.mkPen(theme.BORDER))
    lg.setColumnCount(3)
    return lg


class TachogramCard(Card):
    """Serie RR nel tempo: RR dall'ECG (corretti), RR calcolati dalla fascia, artefatti."""

    def __init__(self, minutes: float = 5.0):
        super().__init__("Tacogramma RR")
        self.seconds = minutes * 60
        self.w = styled_plot("ms")
        p = self.w.getPlotItem()
        _legend(p)
        self.nn = p.plot(pen=pg.mkPen(theme.SERIES_1, width=2), name="RR dall'ECG (corretti)")
        self.strap = pg.ScatterPlotItem(size=4, pen=None, brush=pg.mkBrush(217, 89, 38, 150))
        p.addItem(self.strap)
        p.legend.addItem(self.strap, "RR della fascia")
        self.art = pg.ScatterPlotItem(size=10, symbol="x", pen=pg.mkPen(theme.WARNING, width=2), brush=None)
        p.addItem(self.art)
        # La serie principale sopra i punti della fascia, gli artefatti sopra tutto
        self.strap.setZValue(1)
        self.nn.setZValue(2)
        self.art.setZValue(3)
        p.legend.addItem(self.art, "artefatto corretto")
        self.layout_.addWidget(self.w)

    def update_data(self, s) -> None:
        if len(s.rr_t):
            self.nn.setData(s.rr_t, s.rr_nn)
            bad = ~s.rr_ok
            self.art.setData(s.rr_t[bad], s.rr_raw[bad])
            lo = np.percentile(s.rr_nn, 1) - 25
            hi = np.percentile(s.rr_nn, 99) + 25
            hi += 0.3 * (hi - lo)   # spazio per la legenda
            self.w.setYRange(lo, hi, padding=0)
        else:
            self.nn.setData([], [])
            self.art.setData([], [])
        if len(s.strap_t):
            self.strap.setData(s.strap_t, s.strap_rr)
        self.w.setXRange(max(0.0, s.now - self.seconds), max(s.now, 30.0), padding=0.01)


class PoincareCard(Card):
    def __init__(self):
        super().__init__("Poincaré")
        self.w = styled_plot("RRₙ₊₁ ms", "RRₙ ms")
        p = self.w.getPlotItem()
        self.ident = p.plot(pen=pg.mkPen(theme.AXIS, width=1, style=Qt.DashLine))
        self.dots = pg.ScatterPlotItem(size=6, pen=pg.mkPen(theme.SURFACE, width=1),
                                       brush=pg.mkBrush(57, 135, 229, 170))
        p.addItem(self.dots)
        self.ellipse = p.plot(pen=pg.mkPen(theme.INK_2, width=1.5))
        self.w.setAspectLocked(True)
        self.layout_.addWidget(self.w)

    def update_data(self, s, sd1: float, sd2: float) -> None:
        nn = s.rr_nn[s.rr_ok][-300:] if len(s.rr_nn) else np.empty(0)
        if len(nn) < 3:
            self.dots.setData([], [])
            self.ellipse.setData([], [])
            return
        x, y = nn[:-1], nn[1:]
        self.dots.setData(x, y)
        lo, hi = float(min(x.min(), y.min())) - 20, float(max(x.max(), y.max())) + 20
        self.ident.setData([lo, hi], [lo, hi])
        if np.isfinite(sd1) and np.isfinite(sd2):
            m = float(nn.mean())
            th = np.linspace(0, 2 * np.pi, 80)
            # SD2 lungo la diagonale, SD1 perpendicolare
            u, v = sd2 * np.cos(th), sd1 * np.sin(th)
            self.ellipse.setData(m + (u - v) / np.sqrt(2), m + (u + v) / np.sqrt(2))
        self.w.setRange(xRange=(lo, hi), yRange=(lo, hi), padding=0)


class TemplateCard(Card):
    """Ultimi battiti sovrapposti (tenui) e battito medio (in evidenza)."""

    def __init__(self):
        super().__init__("Battito medio")
        self.w = styled_plot("µV", "ms dal picco R")
        p = self.w.getPlotItem()
        self.recent = [p.plot(pen=pg.mkPen(240, 239, 236, 38, width=1)) for _ in range(12)]
        self.mean = p.plot(pen=pg.mkPen(theme.ECG_TRACE, width=2.5))
        self.layout_.addWidget(self.w)

    def update_data(self, s) -> None:
        if s.template is None:
            for c in self.recent + [self.mean]:
                c.setData([], [])
            return
        t = s.template_t * 1000.0
        for c, seg in zip(self.recent, list(s.template_recent)[-12:] + [None] * 12):
            c.setData(t, seg) if seg is not None else c.setData([], [])
        self.mean.setData(t, s.template)


class MotionCard(Card):
    def __init__(self):
        super().__init__("Accelerazione verticale")
        self.w = styled_plot("mG", "s")
        p = self.w.getPlotItem()
        self.curve = p.plot(pen=pg.mkPen(theme.SERIES_1, width=1.5))
        self.layout_.addWidget(self.w)

    def update_data(self, s, seconds: float = 6.0) -> None:
        if not len(s.acc_t):
            self.curve.setData([], [])
            return
        m = s.acc_t >= s.acc_t[-1] - seconds
        self.curve.setData(s.acc_t[m], s.vert[m])
        self.w.setXRange(s.acc_t[-1] - seconds, s.acc_t[-1], padding=0)
        peak = max(150.0, float(np.percentile(np.abs(s.vert[m]), 99)) * 1.15)
        self.w.setYRange(-peak, peak, padding=0)
