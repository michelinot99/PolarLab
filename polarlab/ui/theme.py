"""Palette e stile (tema scuro). Colori per ruolo, non per valore.

Serie categoriali e colori di stato dalla palette di riferimento validata per daltonismo;
i colori di stato (qualità, zone DFA) compaiono sempre insieme a un'etichetta testuale.
"""

PAGE = "#0d0d0d"
SURFACE = "#1a1a19"
SURFACE_2 = "#232321"
BORDER = "#2c2c2a"
INK = "#f0efec"
INK_2 = "#c3c2b7"
MUTED = "#898781"
GRID = "#2c2c2a"
AXIS = "#383835"

ECG_TRACE = "#1baf7a"        # acqua: unica serie del monitor ECG
SERIES_1 = "#3987e5"         # blu  (RR dall'ECG, asse verticale ACC)
SERIES_2 = "#d95926"         # arancio (RR della fascia)
SERIES_3 = "#199e70"         # acqua scura

# Griglia "carta millimetrata" ECG, tono rosato tenue
ECG_GRID_MINOR = (208, 59, 59, 28)
ECG_GRID_MAJOR = (208, 59, 59, 70)

GOOD = "#0ca30c"
WARNING = "#fab219"
SERIOUS = "#ec835a"
CRITICAL = "#d03b3b"

FONT_FAMILY = "system-ui"

STYLESHEET = f"""
QMainWindow, QWidget#root {{ background: {PAGE}; }}
QWidget {{ color: {INK}; font-size: 13px; }}
QFrame#card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 12px; }}
QLabel#title {{ color: {MUTED}; font-size: 11px; font-weight: 600; letter-spacing: 1px; }}
QLabel#value {{ color: {INK}; font-size: 34px; font-weight: 600; }}
QLabel#valueSmall {{ color: {INK}; font-size: 22px; font-weight: 600; }}
QLabel#unit {{ color: {INK_2}; font-size: 13px; }}
QLabel#sub {{ color: {INK_2}; font-size: 12px; }}
QLabel#muted {{ color: {MUTED}; font-size: 12px; }}
QLabel#brand {{ color: {INK}; font-size: 17px; font-weight: 700; }}
QLabel#chip {{ border-radius: 9px; padding: 1px 8px; font-size: 11px; font-weight: 600; }}
QPushButton {{
    background: {SURFACE_2}; border: 1px solid {BORDER}; border-radius: 8px;
    padding: 6px 14px; color: {INK};
}}
QPushButton:hover {{ border-color: {MUTED}; }}
QPushButton:disabled {{ color: {MUTED}; }}
QPushButton#primary {{ background: {INK}; color: {PAGE}; border: none; font-weight: 600; }}
QPushButton#primary:hover {{ background: #ffffff; }}
QPushButton#record:checked {{ background: {CRITICAL}; border-color: {CRITICAL}; color: white; font-weight: 600; }}
QComboBox {{
    background: {SURFACE_2}; border: 1px solid {BORDER}; border-radius: 8px; padding: 5px 10px; color: {INK};
}}
QComboBox QAbstractItemView {{ background: {SURFACE_2}; color: {INK}; selection-background-color: {AXIS}; }}
QCheckBox {{ color: {INK_2}; spacing: 6px; }}
QStatusBar {{ background: {PAGE}; color: {MUTED}; border-top: 1px solid {BORDER}; }}
QStatusBar QLabel {{ color: {MUTED}; font-size: 11px; }}
QListWidget {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px; }}
QListWidget::item {{ padding: 8px; }}
QListWidget::item:selected {{ background: {AXIS}; }}
QDialog {{ background: {PAGE}; }}
QToolTip {{ background: {SURFACE_2}; color: {INK}; border: 1px solid {BORDER}; }}
"""


def quality_color(score: float) -> str:
    if score != score:   # nan
        return MUTED
    return GOOD if score >= 85 else WARNING if score >= 65 else SERIOUS if score >= 35 else CRITICAL


def alpha1_color(a1: float) -> str:
    if a1 != a1:
        return MUTED
    return GOOD if a1 >= 0.75 else WARNING if a1 >= 0.5 else CRITICAL
