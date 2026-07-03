"""The winSpark look — one theme applied at the QApplication level.

Dark navy sidebar on the left, light workspace with white cards on the right,
teal accent — kept as plain QSS so the panels stay logic-only and the whole
appearance can be tuned in one place. Buttons opt into the accent style with
setObjectName("primary").
"""

from __future__ import annotations

import tempfile
from pathlib import Path

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

def _build_stylesheet(chevron_url: str) -> str:
    return f"""
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

/* Fusion + a full custom stylesheet suppresses the native drop-down arrow
   unless we draw one ourselves — without this, an editable combo box (e.g.
   "Choose a chat") renders as a plain text field with no visual cue that
   there's a list to open, which is exactly what "there's no dropdown" looks
   like even though the widget still functions. A CSS border-triangle for
   ::down-arrow doesn't render correctly under Qt's Fusion style (the
   subcontrol enforces its own box, so a "0-size + border" triangle paints as
   a small filled square instead of a point); a data: URI for `image` also
   silently renders nothing (Qt's QSS url() resolver doesn't support it) — a
   real tiny PNG written to a temp file (_make_chevron_url) is what actually
   renders. */
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 28px;
    border-left: 1px solid {_BORDER};
}}
QComboBox::down-arrow {{
    image: url({chevron_url});
    width: 10px;
    height: 6px;
}}
QComboBox QAbstractItemView {{
    background: {_FIELD};
    border: 1px solid {_BORDER};
    selection-background-color: {_ACCENT};
    selection-color: {_ON_ACCENT};
}}

/* Plain lists in the light workspace (e.g. the recent-chats list) — a card
   like everything else, not the dark sidebar look below. */
QListWidget {{
    background: {_CARD};
    color: {_TEXT};
    border: 1px solid {_BORDER};
    border-radius: 8px;
    padding: 4px;
    outline: none;
}}
QListWidget::item {{
    padding: 8px 10px;
    border-radius: 6px;
    margin: 1px 2px;
}}
QListWidget::item:hover:!selected {{ background: #f1f5f9; }}
QListWidget::item:selected {{
    background: {_ACCENT};
    color: {_ON_ACCENT};
}}

/* The left rail specifically — dark navy, scoped by object name so it
   doesn't leak onto ordinary lists (like the recent-chats list) elsewhere. */
QListWidget#appSidebar {{
    background: {SIDEBAR_BG};
    color: {SIDEBAR_TEXT};
    border: none;
    padding: 4px;
}}
QListWidget#appSidebar::item {{
    padding: 10px 12px;
    border-radius: 8px;
    margin: 1px 6px;
}}
QListWidget#appSidebar::item:hover:!selected {{ background: #1c2942; }}
QListWidget#appSidebar::item:selected {{
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

QSplitter::handle:horizontal {{ background: {_BG}; width: 4px; }}
QSplitter::handle:vertical {{ background: {_BORDER}; height: 6px; margin: 4px 0; border-radius: 3px; }}
QSplitter::handle:vertical:hover {{ background: {_ACCENT}; }}
"""


def _make_chevron_url(color: str) -> str:
    """A small downward-triangle PNG, drawn in-memory, for the combo-box
    dropdown arrow (see the ::down-arrow rule). Needs an active
    QApplication/QGuiApplication (QPixmap requires one), so this can't be a
    module-level constant — apply_theme calls it after the QApplication exists.

    Written to a real temp file rather than embedded as a data: URI: Qt's QSS
    `url()` resolver doesn't render data URIs for `image` (confirmed —
    nothing painted), only real paths/Qt-resource URLs, so a data URI silently
    produces an invisible arrow (indistinguishable from no arrow at all)."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap

    width, height = 10, 6
    pixmap = QPixmap(width, height)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    triangle = QPainterPath()
    triangle.moveTo(0, 0)
    triangle.lineTo(width, 0)
    triangle.lineTo(width / 2, height)
    triangle.closeSubpath()
    painter.drawPath(triangle)
    painter.end()

    path = Path(tempfile.gettempdir()) / "winspark_chevron_down.png"
    pixmap.save(str(path), "PNG")
    return path.as_posix()


def apply_theme(app) -> None:
    """Apply the winSpark theme. Fusion gives every platform the same base so
    the stylesheet renders identically everywhere."""
    app.setStyle("Fusion")
    chevron = _make_chevron_url(MUTED_COLOR)
    app.setStyleSheet(_build_stylesheet(chevron))
