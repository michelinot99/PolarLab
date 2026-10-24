"""Monitor ECG a scorrimento ("sweep"), come in un monitor clinico.

La traccia si riscrive da sinistra a destra con una barra di cancellazione davanti al
punto di scrittura: il segnale vecchio resta fermo e l'occhio legge facilmente la
morfologia. La griglia rispetta le proporzioni della carta ECG standard (25 mm/s,
10 mm/mV): un quadretto grande = 0.2 s × 0.5 mV, sempre quadrato, qualunque sia la
dimensione della finestra.
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg

from polarlab.ui import theme

SMALL_S, BIG_S = 0.04, 0.2          # 1 mm e 5 mm a 25 mm/s
SMALL_UV, BIG_UV = 100.0, 500.0     # 1 mm e 5 mm a 10 mm/mV
ERASE_S = 0.12


def paper_grid(x0: float, x1: float, y0: float, y1: float, step_t: float, step_v: float):
    """Segmenti (per `connect="pairs"`) della griglia da carta ECG nel rettangolo dato."""
    xs = np.arange(np.ceil(x0 / step_t - 1e-6) * step_t, x1 + 1e-9, step_t)
    ys = np.arange(np.floor(y0 / step_v) * step_v, y1 + step_v, step_v)
    vx = np.repeat(xs, 2)
    vy = np.tile([ys[0], ys[-1]], len(xs))
    hx = np.tile([x0, x1], len(ys))
    hy = np.repeat(ys, 2)
    return np.concatenate([vx, hx]), np.concatenate([vy, hy])


def paper_y_span(width_px: float, height_px: float, window_s: float, gain: float) -> float:
    """Ampiezza verticale (µV) che rende quadrati i quadretti: 25 mm/s e 10·gain mm/mV."""
    px_per_s = max(1.0, width_px) / window_s
    return max(1.0, height_px) * BIG_UV / (BIG_S * px_per_s) / gain


class EcgSweep(pg.GraphicsLayoutWidget):
    def __init__(self, window_s: float = 6.0):
        super().__init__()
        self.setBackground(theme.SURFACE)
        self.window_s = window_s
        self.gain = 1.0                      # 1 = 10 mm/mV, 2 = 20 mm/mV
        self.plot = self.addPlot()
        self.plot.hideAxis("left")
        self.plot.hideAxis("bottom")
        self.plot.setMenuEnabled(False)
        self.plot.hideButtons()
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.getViewBox().setDefaultPadding(0)
        self.ci.setContentsMargins(0, 0, 0, 0)

        self._minor = pg.PlotCurveItem(pen=pg.mkPen(theme.ECG_GRID_MINOR, width=1), connect="pairs")
        self._major = pg.PlotCurveItem(pen=pg.mkPen(theme.ECG_GRID_MAJOR, width=1), connect="pairs")
        self.trace = pg.PlotCurveItem(pen=pg.mkPen(theme.ECG_TRACE, width=2), connect="finite", antialias=True)
        self.beats = pg.ScatterPlotItem(size=9, symbol="t", pen=None, brush=pg.mkBrush(theme.INK_2))
        for it in (self._minor, self._major, self.trace, self.beats):
            self.plot.addItem(it)

        self.caption = pg.TextItem("", color=theme.MUTED, anchor=(0, 0))
        self.caption.setParentItem(self.plot.getViewBox())
        self.caption.setPos(8, 6)
        self.message = pg.TextItem("", color=theme.INK_2, anchor=(0.5, 0.5))
        self.plot.addItem(self.message)

        self._center = 0.0
        self._grid_key = None
        self._update_caption()

    def set_window(self, seconds: float) -> None:
        self.window_s = seconds
        self._grid_key = None

    def set_gain(self, gain: float) -> None:
        self.gain = gain
        self._grid_key = None
        self._update_caption()

    def _update_caption(self) -> None:
        self.caption.setText(f"25 mm/s · {int(10 * self.gain)} mm/mV")

    def _y_span(self) -> float:
        vb = self.plot.getViewBox()
        return paper_y_span(vb.width(), vb.height(), self.window_s, self.gain)

    def _rebuild_grid(self, y0: float, y1: float) -> None:
        self._minor.setData(*paper_grid(0.0, self.window_s, y0, y1, SMALL_S, SMALL_UV))
        self._major.setData(*paper_grid(0.0, self.window_s, y0, y1, BIG_S, BIG_UV))

    def update_data(self, ecg: np.ndarray, end_index: int, fs: float,
                    beat_idx: np.ndarray, connected: bool) -> None:
        n_win = max(10, int(round(self.window_s * fs)))
        span = self._y_span()
        if len(ecg) < 2:
            self.trace.setData([], [])
            self.beats.setData([], [])
            self.message.setText("In attesa dell'ECG…" if connected else "Nessuna sorgente collegata")
            self.message.setPos(self.window_s / 2, self._center)
            self._apply_range(span)
            return
        self.message.setText("")
        ecg = ecg[-n_win:]
        n = len(ecg)
        idx = np.arange(end_index - n, end_index)
        pos = idx % n_win
        y = np.full(n_win, np.nan)
        y[pos] = ecg
        head = end_index % n_win
        erase = (head + np.arange(int(ERASE_S * fs))) % n_win
        y[erase] = np.nan
        x = np.arange(n_win) / fs
        self.trace.setData(x, y)

        # Ricentra solo quando il segnale esce dalla fascia centrale (niente "ballo" della griglia)
        lo, hi = np.nanpercentile(ecg, 1), np.nanpercentile(ecg, 99.5)
        mid = 0.5 * (lo + hi)
        if abs(mid - self._center) > 0.2 * span or hi > self._center + 0.45 * span or lo < self._center - 0.45 * span:
            self._center = mid
        self._apply_range(span)

        if len(beat_idx):
            b = beat_idx[beat_idx >= end_index - n]
            bx = (b % n_win) / fs
            top = self._center + 0.40 * span
            self.beats.setData(bx, np.full(len(bx), top))
        else:
            self.beats.setData([], [])

    def _apply_range(self, span: float) -> None:
        y0, y1 = self._center - span / 2, self._center + span / 2
        self.plot.setRange(xRange=(0, self.window_s), yRange=(y0, y1), padding=0)
        key = (round(y0 / SMALL_UV), round(y1 / SMALL_UV), self.window_s)
        if key != self._grid_key:
            self._grid_key = key
            self._rebuild_grid(y0, y1)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._grid_key = None
