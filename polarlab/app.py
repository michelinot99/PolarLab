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
    ap.add_argument("--screenshot", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--screenshot-after", type=float, default=20.0, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if sys.platform == "darwin" and _qt_plugins_hidden():
        print("I plugin Qt sono marcati come nascosti (iCloud su una cartella che inizia con '.').\n"
              "Usa l'ambiente venv.nosync: python3.13 -m venv venv.nosync && "
              "venv.nosync/bin/pip install -r requirements.txt", file=sys.stderr)
        return 1
    import pyqtgraph as pg
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    from polarlab.ui import theme
    from polarlab.ui.main_window import SRC_H10, SRC_REPLAY, SRC_SIM, MainWindow

    pg.setConfigOptions(antialias=True, foreground=theme.MUTED, background=theme.SURFACE)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("PolarLab")
    app.setFont(QFont(".AppleSystemUIFont", 13))
    app.setStyleSheet(theme.STYLESHEET)

    win = MainWindow(sim_speed=args.speed)
    win.show()
    if args.sim:
        win.start_source(kind=SRC_SIM)
    elif args.replay:
        win.start_source(replay_dir=args.replay, kind=SRC_REPLAY)
    elif args.h10:
        win.start_source(kind=SRC_H10)

    if args.screenshot:
        def grab():
            win.grab().save(str(args.screenshot))
            win.close()
            app.quit()
        QTimer.singleShot(int(args.screenshot_after * 1000), grab)
    return app.exec()
