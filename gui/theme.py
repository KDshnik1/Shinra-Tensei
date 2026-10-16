"""Светлая и тёмная темы.

Палитра (QPalette) и таблица стилей задаются ВМЕСТЕ из одного набора цветов,
поэтому текст всегда контрастен фону — независимо от темы Windows.
"""
from string import Template

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPalette
from core.i18n import tr

THEME_MODES = [(tr('Системная'), "system"), (tr('Светлая'), "light"), (tr('Тёмная'), "dark")]

COLORS = {
    "light": dict(
        bg="#f4f6f9", surface="#ffffff", surface2="#eef2f7", alt_row="#f7f9fc",
        text="#1d2b3a", text_muted="#34495e", placeholder="#8a97a5", disabled="#9aa7b4",
        border="#d5dce5", grid="#eef1f5", input_bg="#ffffff", input_disabled="#f0f2f5",
        input_border="#b9c6d4", hover="#e3effb", selection="#cfe3f8", selection_text="#0d1b2a",
        accent="#1976d2", accent_hover="#1e88e5", accent_text="#ffffff", title="#0b4f8a",
        progress_bg="#f7f9fb", chunk="#43a047",
        banner1="#0b4f8a", banner2="#1e88e5", banner_sub="#d6e6f7",
        light="#ffffff", mid="#c5ced8", dark="#9aa7b4", shadow="#6b7785",
    ),
    "dark": dict(
        bg="#1e1f22", surface="#2b2d31", surface2="#34373d", alt_row="#303338",
        text="#e6e8eb", text_muted="#b4bcc6", placeholder="#7d8590", disabled="#6b7480",
        border="#41454d", grid="#383b41", input_bg="#1f2125", input_disabled="#26282c",
        input_border="#50555e", hover="#3a4049", selection="#2f5f93", selection_text="#ffffff",
        accent="#3b8eea", accent_hover="#5aa2f0", accent_text="#ffffff", title="#7cb7ff",
        progress_bg="#1f2125", chunk="#4caf50",
        banner1="#0d2f52", banner2="#1b5fa8", banner_sub="#b8cfe8",
        light="#4a4f57", mid="#3a3e45", dark="#18191b", shadow="#000000",
    ),
}

STATUS = {
    "light": {"ok": "#2e7d32", "warn": "#ef6c00", "error": "#c62828",
              "stopped": "#6b7785", "running": "#1565c0", "idle": "#34495e"},
    "dark": {"ok": "#66bb6a", "warn": "#ffa726", "error": "#ef5350",
             "stopped": "#9aa4ae", "running": "#64b5f6", "idle": "#c9d1d9"},
}

_QSS = Template("""
QMainWindow, QDialog { background: $bg; }
#Banner { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 $banner1, stop:1 $banner2); }
#BannerTitle { color: #ffffff; font-size: 17pt; font-weight: 600; background: transparent; }
#BannerSub { color: $banner_sub; background: transparent; }

QToolBar { background: $surface; border: none; border-bottom: 1px solid $border;
           spacing: 2px; padding: 4px; }
QToolButton { padding: 5px 9px; border-radius: 4px; color: $text; background: transparent; }
QToolButton:hover { background: $hover; }
QToolButton:disabled { color: $disabled; }

QGroupBox { border: 1px solid $border; border-radius: 6px; margin-top: 14px;
            background: $surface; padding: 10px 8px 8px 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px;
                   color: $title; font-weight: 600; }

QPushButton { padding: 6px 14px; border: 1px solid $input_border; border-radius: 4px;
              background: $surface; color: $text; }
QPushButton:hover { background: $hover; }
QPushButton:pressed { background: $selection; color: $selection_text; }
QPushButton:disabled { color: $disabled; border-color: $border; }
QPushButton#Primary { background: $accent; color: $accent_text; border-color: $accent; }
QPushButton#Primary:hover { background: $accent_hover; }

QTableWidget, QListWidget { background: $surface; alternate-background-color: $alt_row;
    color: $text; gridline-color: $grid; border: 1px solid $border;
    selection-background-color: $selection; selection-color: $selection_text; }
QHeaderView::section { background: $surface2; color: $text_muted; padding: 6px; border: none;
    border-right: 1px solid $border; border-bottom: 1px solid $border; font-weight: 600; }
QTableCornerButton::section { background: $surface2; border: none; }

QProgressBar { border: 1px solid $input_border; border-radius: 3px; text-align: center;
               background: $progress_bg; color: $text; min-height: 16px; }
QProgressBar::chunk { background: $chunk; border-radius: 2px; }

QPlainTextEdit { background: $surface; color: $text; border: 1px solid $border;
                 font-family: Consolas, monospace; font-size: 9pt;
                 selection-background-color: $selection; selection-color: $selection_text; }
QStatusBar { background: $surface; color: $text_muted; border-top: 1px solid $border; }
QSplitter::handle { background: $bg; }
QToolTip { background: $surface2; color: $text; border: 1px solid $border; padding: 4px; }
QMenu { background: $surface; color: $text; border: 1px solid $border; }
QMenu::item:selected { background: $selection; color: $selection_text; }
""")

_current = "light"


def current() -> str:
    return _current


def color(key: str) -> str:
    return COLORS[_current][key]


def status_color(kind: str) -> str:
    return STATUS[_current].get(kind, STATUS[_current]["idle"])


def _set_scheme(app, scheme):
    """Qt 6.8+: сообщает Windows тему окна (тёмный заголовок и т.п.)."""
    try:
        app.styleHints().setColorScheme(scheme)
    except AttributeError:
        pass


def system_is_dark(app) -> bool:
    _set_scheme(app, Qt.ColorScheme.Unknown)          # сброс к системной
    try:
        sch = app.styleHints().colorScheme()
        if sch == Qt.ColorScheme.Dark:
            return True
        if sch == Qt.ColorScheme.Light:
            return False
    except AttributeError:
        pass
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return val == 0
    except Exception:
        return False


def _palette(c: dict) -> QPalette:
    p = QPalette()
    R, G = QPalette.ColorRole, QPalette.ColorGroup
    roles = {
        R.Window: c["bg"], R.WindowText: c["text"], R.Base: c["input_bg"],
        R.AlternateBase: c["alt_row"], R.ToolTipBase: c["surface2"], R.ToolTipText: c["text"],
        R.PlaceholderText: c["placeholder"], R.Text: c["text"], R.Button: c["surface"],
        R.ButtonText: c["text"], R.BrightText: "#ff5252", R.Link: c["accent"],
        R.Highlight: c["selection"], R.HighlightedText: c["selection_text"],
        R.Light: c["light"], R.Midlight: c["mid"], R.Mid: c["mid"],
        R.Dark: c["dark"], R.Shadow: c["shadow"],
    }
    for role, col in roles.items():
        p.setColor(role, QColor(col))
    for role in (R.Text, R.ButtonText, R.WindowText, R.PlaceholderText):
        p.setColor(G.Disabled, role, QColor(c["disabled"]))
    p.setColor(G.Disabled, R.Base, QColor(c["input_disabled"]))
    p.setColor(G.Disabled, R.Button, QColor(c["input_disabled"]))
    return p


def apply(app, mode: str) -> str:
    """mode: system | light | dark. Возвращает фактически применённую тему."""
    global _current
    if mode == "system":
        name = "dark" if system_is_dark(app) else "light"
    else:
        name = mode if mode in COLORS else "light"
        _set_scheme(app, Qt.ColorScheme.Dark if name == "dark" else Qt.ColorScheme.Light)
    _current = name
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    app.setPalette(_palette(COLORS[name]))
    app.setStyleSheet(_QSS.substitute(COLORS[name]))
    return name
