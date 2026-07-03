"""The winSpark look — one theme applied at the QApplication level.

Dark navy sidebar on the left, light workspace with white cards on the right,
teal accent — kept as plain QSS so the panels stay logic-only and the whole
appearance can be tuned in one place. Buttons opt into the accent style with
setObjectName("primary").
"""

from __future__ import annotations

# Palette (navy sidebar / light content / teal accent)
SIDEBAR_BG = "#0f172a"       # dark navy (left rail, status bar)
SIDEBAR_TEXT = "#cbd5e1"
SIDEBAR_MUTED = "#7c8aa0"
_BG = "#f1f5f9"              # workspace background
_CARD = "#ffffff"            # cards / group boxes
_FIELD = "#ffffff"           # inputs
_BORDER = "#e2e8f0"
_TEXT = "#0f172a"
MUTED_COLOR = "#64748b"
_ACCENT = "#14b8a6"          # teal
_ACCENT_HOVER = "#0d9488"
_ON_ACCENT = "#ffffff"

STYLESHEET = f"""
QMainWindow, QWidget {{
    background: {_BG};
    color: {_TEXT};
    font-size: 10pt;
}}

QLabel {{ background: transparent; }}

QGroupBox {{
    background: {_CARD};
    border: 1px solid {_BORDER};
    border-radius: 10px;
    margin-top: 16px;
    padding: 12px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 6px;
    color: {MUTED_COLOR};
    font-weight: 600;
}}

QPushButton {{
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 6px;
    padding: 6px 14px;
}}
QPushButton:hover {{ background: #f1f5f9; }}
QPushButton:pressed {{ background: #e2e8f0; }}
QPushButton:disabled {{ color: #94a3b8; background: #f8fafc; border-color: {_BORDER}; }}

QPushButton#primary {{
    background: {_ACCENT};
    border: 1px solid {_ACCENT};
    color: {_ON_ACCENT};
    font-weight: 600;
}}
QPushButton#primary:hover {{ background: {_ACCENT_HOVER}; border-color: {_ACCENT_HOVER}; }}
QPushButton#primary:disabled {{ background: #f8fafc; color: #94a3b8; border-color: {_BORDER}; }}

QLineEdit, QPlainTextEdit, QComboBox {{
    background: {_FIELD};
    border: 1px solid #cbd5e1;
    border-radius: 6px;
    padding: 6px 8px;
    selection-background-color: {_ACCENT};
    selection-color: {_ON_ACCENT};
}}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{ border-color: {_ACCENT}; }}
QLineEdit:disabled, QComboBox:disabled {{ color: #94a3b8; background: #f8fafc; }}
QLineEdit[readOnly="true"], QPlainTextEdit[readOnly="true"] {{ background: #f8fafc; }}

QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox QAbstractItemView {{
    background: {_FIELD};
    border: 1px solid {_BORDER};
    selection-background-color: {_ACCENT};
    selection-color: {_ON_ACCENT};
}}

/* The left rail — dark navy like the sidebar it lives in. */
QListWidget {{
    background: {SIDEBAR_BG};
    color: {SIDEBAR_TEXT};
    border: none;
    padding: 4px;
    outline: none;
}}
QListWidget::item {{
    padding: 10px 12px;
    border-radius: 8px;
    margin: 1px 6px;
}}
QListWidget::item:hover:!selected {{ background: #1c2942; }}
QListWidget::item:selected {{
    background: {_ACCENT};
    color: {_ON_ACCENT};
}}

QTableWidget {{
    background: {_CARD};
    border: 1px solid {_BORDER};
    border-radius: 8px;
    gridline-color: #eef2f7;
}}
QHeaderView::section {{
    background: #f8fafc;
    border: none;
    border-bottom: 1px solid {_BORDER};
    padding: 6px 8px;
    color: {MUTED_COLOR};
    font-weight: 600;
}}
QTableWidget::item {{ padding: 4px 6px; }}
QTableWidget::item:selected {{ background: {_ACCENT}; color: {_ON_ACCENT}; }}

QScrollArea {{ border: none; background: transparent; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #cbd5e1; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #94a3b8; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #cbd5e1; border-radius: 4px; min-width: 30px; }}
QScrollBar::handle:horizontal:hover {{ background: #94a3b8; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QStatusBar {{ background: {SIDEBAR_BG}; color: {SIDEBAR_MUTED}; }}
QStatusBar::item {{ border: none; }}

QSplitter::handle {{ background: {_BG}; }}
"""


def apply_theme(app) -> None:
    """Apply the winSpark theme. Fusion gives every platform the same base so
    the stylesheet renders identically everywhere."""
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
