"""Archivio delle sessioni: elenco leggero a sinistra, analisi della sessione scelta a destra.

L'elenco legge solo `meta.json` e il riepilogo in cache (`analysis.json`): pochi kB per
sessione. I dati veri si caricano, in un thread, solo quando si seleziona una sessione.
"""

from __future__ import annotations

import shutil
import zipfile
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMenu,
                               QMessageBox, QProgressBar, QPushButton, QStackedWidget, QVBoxLayout, QWidget)

from polarlab.analysis import report as R
from polarlab.core.session import RAW_NAME
from polarlab.ui.analysis_view import AnalysisView, duration_text, session_start

DOWNLOADS = Path.home() / "Downloads"
MEMORY_CACHE = 3
SHORT_DAYS = ("lun", "mar", "mer", "gio", "ven", "sab", "dom")
SHORT_MONTHS = ("gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic")


def is_session(d: Path) -> bool:
    return d.is_dir() and (d / RAW_NAME).exists()


def list_sessions(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted((d for d in root.iterdir() if is_session(d)), key=lambda d: d.name, reverse=True)


def _zip_session_root(zf: zipfile.ZipFile) -> str | None:
    """Cartella dentro lo zip che contiene raw.plab ("" se raw.plab è alla radice)."""
    for name in zf.namelist():
        p = Path(name)
        if p.name == RAW_NAME and not p.is_absolute() and ".." not in p.parts and len(p.parts) <= 2:
            return str(p.parent) if len(p.parts) == 2 else ""
    return None


def zip_session_name(path: Path) -> str | None:
    try:
        with zipfile.ZipFile(path) as zf:
            root = _zip_session_root(zf)
    except (OSError, zipfile.BadZipFile):
        return None
    if root is None:
        return None
    return root or path.stem.removeprefix("PolarLab_")


def import_path(src: Path, sessions_dir: Path) -> Path:
    """Copia nell'archivio uno zip esportato dall'iPhone o una cartella di sessione; ritorna la destinazione."""
    src = Path(src)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        if not is_session(src):
            raise ValueError(f"{src.name} non contiene {RAW_NAME}")
        if src.resolve().parent == sessions_dir.resolve():
            return src
        dest = _free_name(sessions_dir, src.name, src / RAW_NAME)
        if not dest.exists():
            shutil.copytree(src, dest)
        return dest
    with zipfile.ZipFile(src) as zf:
        root = _zip_session_root(zf)
        if root is None:
            raise ValueError(f"{src.name} non è una sessione PolarLab (manca {RAW_NAME})")
        name = root or src.stem.removeprefix("PolarLab_")
        prefix = f"{root}/" if root else ""
        members = [m for m in zf.infolist() if m.filename.startswith(prefix) and not m.is_dir()]
        raw_size = next(m.file_size for m in members if Path(m.filename).name == RAW_NAME)
        dest = _free_name(sessions_dir, name, raw_size)
        if dest.exists():
            return dest
        tmp = sessions_dir / f".import-{name}"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        for m in members:
            rel = Path(m.filename[len(prefix):])
            if rel.is_absolute() or ".." in rel.parts or len(rel.parts) != 1:
                continue        # solo i file della sessione, niente percorsi strani
            with zf.open(m) as fi, open(tmp / rel.name, "wb") as fo:
                shutil.copyfileobj(fi, fo)
        tmp.rename(dest)
        return dest


def _free_name(root: Path, name: str, raw) -> Path:
    """Stesso nome se è la stessa registrazione (stessa dimensione di raw.plab), altrimenti un suffisso."""
    size = raw if isinstance(raw, int) else Path(raw).stat().st_size
    for i in range(1, 100):
        d = root / (name if i == 1 else f"{name}-{i}")
        if not d.exists():
            return d
        try:
            if (d / RAW_NAME).stat().st_size == size:
                return d
        except OSError:
            pass
    raise OSError("troppe sessioni con lo stesso nome")


def list_label(d: Path) -> tuple[str, str]:
    meta = R.read_meta(d)
    start = session_start(meta, d)
    title = (f"{SHORT_DAYS[start.weekday()]} {start.day} {SHORT_MONTHS[start.month - 1]} {start.year} · {start:%H:%M}"
             if start else d.name)
    parts = []
    head = R.cached_summary(d)
    s = head["summary"] if head else meta.get("summary") or {}
    dur = (head or {}).get("duration_s") or meta.get("duration_s")
    if dur:
        parts.append(duration_text(float(dur)))
    parts.append("iPhone" if "iOS" in str(meta.get("app", "")) else "Mac")
    hr = s.get("avg_hr")
    if isinstance(hr, (int, float)) and hr == hr:
        parts.append(f"FC {hr:.0f}")
    ev = s.get("events") if isinstance(s.get("events"), dict) else None
    if ev:
        n = ev.get("serious", 0) + ev.get("warning", 0)
        parts.append(f"⚠ {n}" if n else "✓")
    return title, "  ·  ".join(parts)


class _Loader(QThread):
    done = Signal(int, object, str)

    def __init__(self, req: int, directory: Path, fresh: bool):
        super().__init__()
        self.req, self.directory, self.fresh = req, directory, fresh

    def run(self):
        try:
            rep = R.reanalyze(self.directory) if self.fresh else R.analyze(self.directory)
            self.done.emit(self.req, rep, "")
        except Exception as exc:   # file rovinato, permessi, ...
            self.done.emit(self.req, None, f"{type(exc).__name__}: {exc}")


class SessionsView(QWidget):
    replay_requested = Signal(object, float)

    def __init__(self, sessions_dir: Path, profile: R.Profile):
        super().__init__()
        self.sessions_dir = sessions_dir
        self.profile = profile
        self.settings = QSettings("PolarLab", "PolarLab")
        self._cache: OrderedDict[Path, R.SessionReport] = OrderedDict()
        self._req = 0
        self._loaders: list[_Loader] = []
        self._current: Path | None = None
        self._pending_select: Path | None = None

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        lay.addWidget(self._sidebar())
        self.stack = QStackedWidget()
        self.empty = self._message_page()
        self.loading = self._loading_page()
        self.analysis = AnalysisView()
        self.analysis.replay_requested.connect(self.replay_requested)
        self.analysis.reanalyze_requested.connect(lambda d: self._load(d, fresh=True))
        self.analysis.reveal_requested.connect(self.reveal)
        for w in (self.empty, self.loading, self.analysis):
            self.stack.addWidget(w)
        lay.addWidget(self.stack, 1)

        self.watcher = QFileSystemWatcher(self)
        self.watcher.directoryChanged.connect(lambda _: self._refresh_timer.start())
        self._refresh_timer = QTimer(self, singleShot=True, interval=600)
        self._refresh_timer.timeout.connect(self.refresh)
        self.set_sessions_dir(sessions_dir)

    # ------------------------------------------------------------------ costruzione

    def _sidebar(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(300)
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        head = QHBoxLayout()
        title = QLabel("SESSIONI")
        title.setObjectName("title")
        head.addWidget(title)
        head.addStretch(1)
        imp = QPushButton("Importa…")
        imp.setToolTip("Zip condiviso dall'iPhone o cartella di una sessione. Puoi anche trascinarli sulla finestra.")
        imp.clicked.connect(self._choose_import)
        head.addWidget(imp)
        v.addLayout(head)

        self.banner = QFrame()
        self.banner.setObjectName("banner")
        b = QVBoxLayout(self.banner)
        b.setContentsMargins(10, 8, 10, 8)
        self.banner_text = QLabel("")
        self.banner_text.setWordWrap(True)
        b.addWidget(self.banner_text)
        br = QHBoxLayout()
        go = QPushButton("Importa")
        go.setObjectName("primary")
        go.clicked.connect(self._import_downloads)
        no = QPushButton("Ignora")
        no.clicked.connect(self._dismiss_downloads)
        br.addWidget(go)
        br.addWidget(no)
        br.addStretch(1)
        b.addLayout(br)
        self.banner.hide()
        v.addWidget(self.banner)

        self.list = QListWidget()
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        self.list.currentItemChanged.connect(self._on_select)
        v.addWidget(self.list, 1)
        self.count = QLabel("")
        self.count.setObjectName("muted")
        self.count.setWordWrap(True)
        v.addWidget(self.count)
        return w

    def _message_page(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addStretch(1)
        self.empty_text = QLabel("")
        self.empty_text.setAlignment(Qt.AlignCenter)
        self.empty_text.setWordWrap(True)
        self.empty_text.setObjectName("sub")
        v.addWidget(self.empty_text)
        v.addStretch(2)
        return w

    def _loading_page(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addStretch(1)
        self.loading_text = QLabel("Analisi in corso…")
        self.loading_text.setAlignment(Qt.AlignCenter)
        self.loading_text.setObjectName("sub")
        v.addWidget(self.loading_text)
        bar = QProgressBar()
        bar.setRange(0, 0)
        bar.setTextVisible(False)
        bar.setFixedWidth(280)
        v.addWidget(bar, 0, Qt.AlignCenter)
        v.addStretch(2)
        return w

    # ------------------------------------------------------------------ elenco

    def set_sessions_dir(self, d: Path) -> None:
        self.sessions_dir = Path(d)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        if self.watcher.directories():
            self.watcher.removePaths(self.watcher.directories())
        self.watcher.addPath(str(self.sessions_dir))
        self._cache.clear()
        self.refresh()

    def refresh(self) -> None:
        keep = self._pending_select or self._current
        self.list.blockSignals(True)
        self.list.clear()
        sessions = list_sessions(self.sessions_dir)
        for d in sessions:
            title, sub = list_label(d)
            it = QListWidgetItem(f"{title}\n{sub}")
            it.setData(Qt.UserRole, str(d))
            it.setToolTip(str(d))
            self.list.addItem(it)
        self.list.blockSignals(False)
        n = len(sessions)
        self.count.setText(f"{n} {'sessione' if n == 1 else 'sessioni'} in {self.sessions_dir}" if n else "")
        if not sessions:
            self.empty_text.setText("Nessuna sessione registrata.\n\nSull'iPhone apri una sessione → Condividi → "
                                    "AirDrop al Mac, poi trascina lo zip qui (o usa «Importa…»).\n"
                                    "Le registrazioni fatte sul Mac compaiono qui da sole.")
            self.stack.setCurrentWidget(self.empty)
        elif keep is not None:
            self.select(keep, load=self._current != keep)
        elif self._current is None:
            self.empty_text.setText("Seleziona una sessione a sinistra.")
            self.stack.setCurrentWidget(self.empty)
        self._check_downloads()

    def select(self, d: Path, load: bool = True) -> None:
        d = Path(d)
        for i in range(self.list.count()):
            if Path(self.list.item(i).data(Qt.UserRole)) == d:
                self.list.blockSignals(not load)
                self.list.setCurrentRow(i)
                self.list.blockSignals(False)
                self._pending_select = None
                return
        self._pending_select = d

    def _update_item(self, d: Path) -> None:
        for i in range(self.list.count()):
            it = self.list.item(i)
            if Path(it.data(Qt.UserRole)) == d:
                title, sub = list_label(d)
                it.setText(f"{title}\n{sub}")

    def _on_select(self, item: QListWidgetItem | None, _prev=None) -> None:
        if item is None:
            return
        self._load(Path(item.data(Qt.UserRole)))

    # ------------------------------------------------------------------ caricamento

    def _load(self, d: Path, fresh: bool = False) -> None:
        self._current = d
        self._req += 1
        if fresh:
            self._cache.pop(d, None)
        elif d in self._cache:
            self._cache.move_to_end(d)
            self._show(self._cache[d])
            return
        self.loading_text.setText("Rianalisi con gli algoritmi attuali…" if fresh else
                                  "Analisi in corso…" if R.cached_summary(d) is None else "Caricamento…")
        self.stack.setCurrentWidget(self.loading)
        ld = _Loader(self._req, d, fresh)
        ld.done.connect(self._loaded)
        ld.finished.connect(lambda: self._loaders.remove(ld) if ld in self._loaders else None)
        self._loaders.append(ld)
        ld.start()

    def _loaded(self, req: int, rep, err: str) -> None:
        if rep is not None:
            self._cache[rep.dir] = rep
            self._cache.move_to_end(rep.dir)
            while len(self._cache) > MEMORY_CACHE:
                self._cache.popitem(last=False)
            self._update_item(rep.dir)
        if req != self._req:
            return             # nel frattempo è stata scelta un'altra sessione
        if rep is None:
            self.empty_text.setText(f"Impossibile analizzare la sessione:\n{err}")
            self.stack.setCurrentWidget(self.empty)
            return
        self._show(rep)

    def _show(self, rep) -> None:
        self.analysis.set_report(rep, self.profile)
        self.stack.setCurrentWidget(self.analysis)

    def set_profile(self, profile: R.Profile) -> None:
        self.profile = profile
        self.analysis.set_profile(profile)

    # ------------------------------------------------------------------ importazione

    def import_paths(self, paths: list[Path]) -> None:
        done, errors = [], []
        for p in paths:
            try:
                done.append(import_path(Path(p), self.sessions_dir))
            except (OSError, ValueError, zipfile.BadZipFile, StopIteration) as exc:
                errors.append(f"{Path(p).name}: {exc}")
        if done:
            self._pending_select = done[-1]
            self._current = None
            self.refresh()
        if errors:
            QMessageBox.warning(self, "Importazione", "\n".join(errors))

    def _choose_import(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Importa sessioni", str(DOWNLOADS),
                                                "Sessioni PolarLab (*.zip);;Tutti i file (*)")
        if files:
            self.import_paths([Path(f) for f in files])

    def _new_downloads(self) -> list[Path]:
        if not DOWNLOADS.exists():
            return []
        dismissed = set(self.settings.value("dismissed_zips", []) or [])
        out = []
        try:
            zips = sorted(DOWNLOADS.glob("PolarLab_*.zip"))
        except OSError:      # accesso a Download non consentito
            return []
        for z in zips:
            if z.name in dismissed:
                continue
            name = zip_session_name(z)
            if name and not (self.sessions_dir / name).exists():
                out.append(z)
        return out

    def _check_downloads(self) -> None:
        new = self._new_downloads()
        if new:
            n = len(new)
            self.banner_text.setText(f"{n} {'nuova sessione' if n == 1 else 'nuove sessioni'} dall'iPhone "
                                     f"in Download")
        self.banner.setVisible(bool(new))

    def _import_downloads(self) -> None:
        self.import_paths(self._new_downloads())
        self._check_downloads()

    def _dismiss_downloads(self) -> None:
        dismissed = set(self.settings.value("dismissed_zips", []) or [])
        dismissed |= {z.name for z in self._new_downloads()}
        self.settings.setValue("dismissed_zips", sorted(dismissed))
        self.banner.hide()

    # ------------------------------------------------------------------ azioni

    def _menu(self, pos) -> None:
        it = self.list.itemAt(pos)
        if it is None:
            return
        d = Path(it.data(Qt.UserRole))
        m = QMenu(self)
        m.addAction("Riproduci", lambda: self.replay_requested.emit(d, 0.0))
        m.addAction("Mostra nel Finder", lambda: self.reveal(d))
        m.addAction("Rianalizza", lambda: (self.select(d), self._load(d, fresh=True)))
        m.addSeparator()
        m.addAction("Sposta nel Cestino…", lambda: self._trash(d))
        m.exec(self.list.mapToGlobal(pos))

    def reveal(self, d: Path) -> None:
        import subprocess
        subprocess.Popen(["open", "-R", str(d / RAW_NAME)])

    def _trash(self, d: Path) -> None:
        from PySide6.QtCore import QFile
        title, _ = list_label(d)
        if QMessageBox.question(self, "Sposta nel Cestino",
                                f"Spostare nel Cestino la sessione «{title}»?\nLa puoi recuperare dal Cestino.") \
                != QMessageBox.Yes:
            return
        if not QFile.moveToTrash(str(d)):
            QMessageBox.warning(self, "Cestino", f"Impossibile spostare {d.name} nel Cestino.")
            return
        self._cache.pop(d, None)
        if self._current == d:
            self._current = None
        self.refresh()
