"""Small reusable Qt widgets/helpers for the winSpark UI."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
)


def make_table(columns: list[str], stretch_col: int = 0) -> QTableWidget:
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setSectionResizeMode(stretch_col, QHeaderView.Stretch)
    return table


def fill_table(table: QTableWidget, rows: list[list[str]]) -> None:
    table.setRowCount(len(rows))
    for r, values in enumerate(rows):
        for c, value in enumerate(values):
            table.setItem(r, c, QTableWidgetItem(value))


class StatusCheck(QLabel):
    """A tiny status indicator: neutral / green tick / red cross with a message.
    Its state is readable (`state`, `message`) so panel logic is testable."""

    def __init__(self) -> None:
        super().__init__()
        self.state = "neutral"  # neutral | ok | bad | busy
        self.message = ""
        self.clear_status()

    def clear_status(self) -> None:
        self.state = "neutral"
        self.message = ""
        self.setText("")
        self.setStyleSheet("")

    def set_busy(self, text: str) -> None:
        self.state = "busy"
        self.message = text
        self.setText(text)
        self.setStyleSheet("color: #666;")

    def set_ok(self, text: str) -> None:
        self.state = "ok"
        self.message = text
        self.setText(f"✓  {text}")
        self.setStyleSheet("color: #1a7f37; font-weight: 600;")

    def set_bad(self, text: str) -> None:
        self.state = "bad"
        self.message = text
        self.setText(f"✗  {text}")
        self.setStyleSheet("color: #b3261e;")
