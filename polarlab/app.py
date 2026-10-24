"""Avvio dell'app: `python -m polarlab [--sim | --replay DIR] [--speed X]`."""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path


def _qt_plugins_hidden() -> bool:
    """True se i plugin Qt hanno il flag "nascosto" di macOS.

    Succede quando l'ambiente virtuale sta in una cartella che inizia con un punto (`.venv`)
    dentro la Scrivania/Documenti sincronizzati con iCloud: iCloud nasconde tutto il contenuto
    e Qt su macOS salta i plugin nascosti, quindi non trova "cocoa".
    """
    try:
        import PySide6
        cocoa = Path(PySide6.__file__).parent / "Qt" / "plugins" / "platforms" / "libqcocoa.dylib"
        return bool(os.lstat(cocoa).st_flags & stat.UF_HIDDEN)
    except (OSError, AttributeError, ImportError):
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="polarlab", description="Streaming ECG dalla Polar H10")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--sim", action="store_true", help="avvia subito il simulatore")
    g.add_argument("--replay", type=Path, metavar="DIR", help="riproduce una sessione registrata")
    g.add_argument("--h10", action="store_true", help="si collega subito alla fascia")
    ap.add_argument("--speed", type=float, default=1.0, help="velocità di simulatore/replay (default 1)")
    ap.add_argument("--open-session", type=Path, metavar="DIR", help="apre l'analisi di una sessione")
    ap.add_argument("paths", nargs="*", type=Path, help=argparse.SUPPRESS)   # zip aperti con l'app
    ap.add_argument("--screenshot", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--screenshot-after", type=float, default=20.0, help=argparse.SUPPRESS)
    args, _ = ap.parse_known_args(argv)   # macOS può aggiungere argomenti propri (-psn_…)

    if sys.platform == "darwin" and _qt_plugins_hidden():
        print("I plugin Qt sono marcati come nascosti (iCloud su una cartella che inizia con '.').\n"
              "Usa l'ambiente venv.nosync: python3.13 -m venv venv.nosync && "
              "venv.nosync/bin/pip install -r requirements.txt", file=sys.stderr)
        return 1
    import pyqtgraph as pg
    from PySide6.QtCore import QEvent, QTimer
    from PySide6.QtGui import QFont, QIcon
    from PySide6.QtWidgets import QApplication, QMessageBox

    from polarlab.ui import theme
    from polarlab.ui.main_window import SRC_H10, SRC_SIM, MainWindow

    pg.setConfigOptions(antialias=True, foreground=theme.MUTED, background=theme.SURFACE)

    class App(QApplication):
        """Riceve i file aperti dal Finder o trascinati sull'icona nel Dock (QFileOpenEvent)."""

        def __init__(self, argv):
            super().__init__(argv)
            self.window = None
            self.pending: list[Path] = []

        def event(self, ev):
            if ev.type() == QEvent.FileOpen:
                path = Path(ev.file())
                # AppKit consegna come "file da aprire" anche gli argomenti della riga di comando
                # (per esempio il PNG di --screenshot): si accettano solo zip e cartelle di sessione
                if str(path) in sys.argv or not (path.suffix.lower() == ".zip" or path.is_dir()):
                    return True
                if self.window is not None:
                    self.window.open_paths([path])
                else:
                    self.pending.append(path)
                return True
            return super().event(ev)

    app = App(sys.argv[:1])
    app.setApplicationName("PolarLab")
    icon = Path(__file__).resolve().parents[1] / "ios" / "App" / "Resources" / "Assets.xcassets" / \
        "AppIcon.appiconset" / "icon-1024.png"
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    app.setFont(QFont(".AppleSystemUIFont", 13))
    app.setStyleSheet(theme.STYLESHEET)

    win = MainWindow(sim_speed=args.speed)
    win.show()
    app.window = win

    def excepthook(kind, exc, tb):
        # Senza Terminale (app nel Dock) un errore non gestito sarebbe invisibile
        import traceback
        text = "".join(traceback.format_exception(kind, exc, tb))
        print(text, file=sys.stderr, flush=True)
        QMessageBox.critical(win, "Errore", f"{kind.__name__}: {exc}\n\nDettagli nel log di PolarLab.")
    sys.excepthook = excepthook

    if app.pending or args.paths:
        win.open_paths(app.pending + list(args.paths))
    elif args.open_session:
        win.open_session(args.open_session)
    if args.sim:
        win.start_source(kind=SRC_SIM)
    elif args.replay:
        win.play_session(args.replay)
    elif args.h10:
        win.start_source(kind=SRC_H10)

    if args.screenshot:
        def grab():
            win.grab().save(str(args.screenshot))
            win.close()
            app.quit()
        QTimer.singleShot(int(args.screenshot_after * 1000), grab)
    return app.exec()
