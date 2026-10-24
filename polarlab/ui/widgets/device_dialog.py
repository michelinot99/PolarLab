"""Scansione Bluetooth e scelta della fascia."""

from __future__ import annotations

import asyncio

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout

from polarlab.ui import theme


class _ScanWorker(QThread):
    found = Signal(list)
    failed = Signal(str)

    def run(self):
        try:
            from polarlab.sources.polar_ble import scan
            self.found.emit(asyncio.run(scan(timeout=6.0)))
        except Exception as exc:   # Bluetooth spento, permesso negato, ...
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class DeviceDialog(QDialog):
    def __init__(self, parent=None, current: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("Scegli la fascia")
        self.setMinimumSize(420, 320)
        self.address: str | None = current
        lay = QVBoxLayout(self)
        hint = QLabel("Indossa la Polar H10 (elettrodi inumiditi): la fascia si accende solo a contatto con la pelle.")
        hint.setWordWrap(True)
        hint.setObjectName("sub")
        lay.addWidget(hint)
        self.list = QListWidget()
        lay.addWidget(self.list, 1)
        self.state = QLabel("")
        self.state.setObjectName("muted")
        lay.addWidget(self.state)
        row = QHBoxLayout()
        self.scan_btn = QPushButton("Cerca di nuovo")
        self.scan_btn.clicked.connect(self.start_scan)
        self.any_btn = QPushButton("Qualsiasi H10")
        self.any_btn.clicked.connect(self._any)
        ok = QPushButton("Usa questa")
        ok.setObjectName("primary")
        ok.clicked.connect(self._accept)
        row.addWidget(self.scan_btn)
        row.addWidget(self.any_btn)
        row.addStretch(1)
        row.addWidget(ok)
        lay.addLayout(row)
        self.list.itemDoubleClicked.connect(lambda _i: self._accept())
        self._worker: _ScanWorker | None = None
        self.start_scan()

    def start_scan(self):
        if self._worker is not None and self._worker.isRunning():
            return
        self.list.clear()
        self.state.setText("Ricerca in corso (6 s)…")
        self.scan_btn.setEnabled(False)
        self._worker = _ScanWorker()
        self._worker.found.connect(self._on_found)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_found(self, devices):
        self.scan_btn.setEnabled(True)
        if not devices:
            self.state.setText("Nessun dispositivo Polar trovato. Controlla che la fascia sia indossata e non collegata ad altro.")
            return
        self.state.setText(f"{len(devices)} dispositivi trovati")
        for d in devices:
            item = QListWidgetItem(f"{d.name}    ·    segnale {d.rssi} dBm")
            item.setData(256, d.address)
            item.setToolTip(d.address)
            self.list.addItem(item)
        self.list.setCurrentRow(0)

    def _on_failed(self, msg):
        self.scan_btn.setEnabled(True)
        self.state.setText(f"Errore Bluetooth: {msg}")
        self.state.setStyleSheet(f"color: {theme.CRITICAL};")

    def _any(self):
        self.address = None
        self.accept()

    def _accept(self):
        item = self.list.currentItem()
        if item is not None:
            self.address = item.data(256)
            self.accept()

    def closeEvent(self, ev):
        if self._worker is not None:
            self._worker.wait(7000)
        super().closeEvent(ev)
