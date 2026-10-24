"""Elementi comuni: card, riquadri numerici, chip di stato, plot con lo stile dell'app."""

from __future__ import annotations

import math

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from polarlab.ui import theme


def fmt(v, digits: int = 0, dash: str = "—") -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return dash
    return f"{v:.{digits}f}"


class Card(QFrame):
    def __init__(self, title: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("card")
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(14, 10, 14, 12)
        self.layout_.setSpacing(4)
        self.header = QHBoxLayout()
        self.header.setSpacing(8)
        self.title = QLabel(title.upper())
        self.title.setObjectName("title")
        self.title.setMinimumWidth(self.title.sizeHint().width())
        self.header.addWidget(self.title)
        self.header.addStretch(1)
        self.layout_.addLayout(self.header)


class Chip(QLabel):
    """Stato = pallino colorato + testo: il colore non è mai l'unico portatore di significato."""

    def __init__(self, text: str = "—"):
        super().__init__(text)
        self.setObjectName("chip")
        self.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.set("—", theme.MUTED)

    def set(self, text: str, color: str) -> None:
        self.setText(f"● {text}")
        self.setStyleSheet(f"color: {color}; background: {theme.SURFACE_2}; border: 1px solid {theme.BORDER};")


class Tile(Card):
    def __init__(self, title: str, unit: str = "", small: bool = False, chip: bool = False):
        super().__init__(title)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.value = QLabel("—")
        self.value.setObjectName("valueSmall" if small else "value")
        self.unit = QLabel(unit)
        self.unit.setObjectName("unit")
        row.addWidget(self.value, 0, Qt.AlignBottom)
        row.addWidget(self.unit, 0, Qt.AlignBottom)
        row.addStretch(1)
        self.layout_.addLayout(row)
        self.sub = QLabel("")
        self.sub.setObjectName("sub")
        self.sub.setWordWrap(True)
        self.layout_.addWidget(self.sub)
        self.chip = Chip() if chip else None
        if self.chip is not None:
            self.header.addWidget(self.chip)

    def set(self, value: str, sub: str | None = None) -> None:
        self.value.setText(value)
        if sub is not None:
            self.sub.setText(sub)


class KeyValueGrid(Card):
    def __init__(self, title: str, keys: list[str]):
        super().__init__(title)
        from PySide6.QtWidgets import QGridLayout
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(3)
        self.values: dict[str, QLabel] = {}
        for i, k in enumerate(keys):
            kl = QLabel(k)
            kl.setObjectName("muted")
            vl = QLabel("—")
            vl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            vl.setStyleSheet(f"color: {theme.INK};")
            grid.addWidget(kl, i // 2, (i % 2) * 2)
            grid.addWidget(vl, i // 2, (i % 2) * 2 + 1)
            self.values[k] = vl
        self.layout_.addLayout(grid)

    def set(self, key: str, text: str) -> None:
        self.values[key].setText(text)


def styled_plot(left_label: str = "", bottom_label: str = "") -> pg.PlotWidget:
    w = pg.PlotWidget(background=theme.SURFACE)
    p = w.getPlotItem()
    p.setMenuEnabled(False)
    p.hideButtons()
    w.setMouseEnabled(x=False, y=False)
    p.showGrid(x=True, y=True, alpha=0.18)
    for name in ("left", "bottom"):
        ax = p.getAxis(name)
        ax.setPen(pg.mkPen(theme.AXIS))
        ax.setTextPen(pg.mkPen(theme.MUTED))
        ax.setStyle(tickLength=-4)
    if left_label:
        p.setLabel("left", left_label, color=theme.MUTED)
    if bottom_label:
        p.setLabel("bottom", bottom_label, color=theme.MUTED)
    return w
