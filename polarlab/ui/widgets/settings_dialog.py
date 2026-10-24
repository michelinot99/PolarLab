"""Impostazioni: profilo dell'atleta (per le zone) e cartella delle sessioni."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QSpinBox, QVBoxLayout)

from polarlab.analysis.report import Profile


class SettingsDialog(QDialog):
    def __init__(self, parent, profile: Profile, sessions_dir: Path, default_dir: Path):
        super().__init__(parent)
        self.setWindowTitle("Impostazioni")
        self.setMinimumWidth(520)
        self.default_dir = default_dir
        lay = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(10)
        self.hr_max = QSpinBox()
        self.hr_max.setRange(0, 240)
        self.hr_max.setSpecialValueText("non impostata")
        self.hr_max.setSuffix(" bpm")
        self.hr_max.setValue(int(profile.hr_max or 0))
        self.hr_rest = QSpinBox()
        self.hr_rest.setRange(0, 120)
        self.hr_rest.setSpecialValueText("non impostata")
        self.hr_rest.setSuffix(" bpm")
        self.hr_rest.setValue(int(profile.hr_rest or 0))
        form.addRow("FC massima", self.hr_max)
        form.addRow("FC a riposo", self.hr_rest)
        hint = QLabel("Con la FC massima l'analisi mostra il tempo nelle 5 zone (% della FC max); con anche "
                      "la FC a riposo le zone usano la riserva cardiaca (Karvonen). La FC max serve anche a "
                      "segnalare frequenze oltre il tuo massimo.")
        hint.setObjectName("sub")
        hint.setWordWrap(True)
        form.addRow("", hint)
        row = QHBoxLayout()
        self.dir = QLineEdit(str(sessions_dir))
        pick = QPushButton("Scegli…")
        pick.clicked.connect(self._pick)
        reset = QPushButton("Predefinita")
        reset.clicked.connect(lambda: self.dir.setText(str(self.default_dir)))
        row.addWidget(self.dir, 1)
        row.addWidget(pick)
        row.addWidget(reset)
        form.addRow("Cartella sessioni", row)
        lay.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _pick(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Cartella delle sessioni", self.dir.text())
        if d:
            self.dir.setText(d)

    @property
    def profile(self) -> Profile:
        return Profile(hr_max=float(self.hr_max.value()) or None, hr_rest=float(self.hr_rest.value()) or None)

    @property
    def sessions_dir(self) -> Path:
        return Path(self.dir.text()).expanduser()
