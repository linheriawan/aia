"""Usage window (ARCHITECTURE.md §11): range selector, totals, a daily bar
chart, provider/model and per-session breakdowns, and CSV export.

Everything here is estimated on this device — never authoritative billing.
"""

from __future__ import annotations

import csv
import sqlite3
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta

import pyqtgraph as pg
from PySide6.QtCore import QDate, Signal
from PySide6.QtGui import QCloseEvent, QShowEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from deskai.storage import repositories

_RANGE_OPTIONS = ("Today", "7 days", "30 days", "Custom")
_BREAKDOWN_COLUMNS = ("Provider", "Model", "Input tokens", "Output tokens", "Est. cost")
_SESSION_COLUMNS = ("Session", "Input tokens", "Output tokens", "Est. cost")


def _to_qdate(value: date) -> QDate:
    return QDate(value.year, value.month, value.day)


def _range_bounds(label: str, custom_start: date, custom_end: date) -> tuple[str, str]:
    now = datetime.now(UTC)
    if label == "Today":
        start = datetime.combine(now.date(), time.min, tzinfo=UTC)
        end = now
    elif label == "7 days":
        start = now - timedelta(days=7)
        end = now
    elif label == "30 days":
        start = now - timedelta(days=30)
        end = now
    else:
        start = datetime.combine(custom_start, time.min, tzinfo=UTC)
        end = datetime.combine(custom_end, time.max, tzinfo=UTC)
    return start.isoformat(), end.isoformat()


def _format_cost_totals(rows: list[repositories.UsageRow]) -> str:
    """Sums est_cost per unit — different units (USD, Neurons, …) are shown
    separately rather than added together, since they aren't comparable."""
    totals: dict[str, float] = defaultdict(float)
    has_unpriced = False
    for row in rows:
        if row.est_cost is None:
            has_unpriced = True
            continue
        totals[row.currency or "USD"] += row.est_cost
    parts = [f"{amount:.4f} {unit}" for unit, amount in sorted(totals.items())]
    if has_unpriced:
        parts.append("some models have no price on file")
    return ", ".join(parts) if parts else "no price on file for any usage in this range"


class UsageWindow(QWidget):
    open_session_requested = Signal(str)

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn
        self.setWindowTitle("deskai — Usage")
        self.resize(820, 640)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Estimated on this device — not authoritative billing."))

        range_row = QHBoxLayout()
        self._range_box = QComboBox()
        self._range_box.addItems(_RANGE_OPTIONS)
        self._range_box.currentTextChanged.connect(self._on_range_changed)
        range_row.addWidget(QLabel("Range:"))
        range_row.addWidget(self._range_box)

        self._custom_start = QDateEdit(_to_qdate(date.today() - timedelta(days=30)))
        self._custom_start.setCalendarPopup(True)
        self._custom_start.dateChanged.connect(self.refresh)
        self._custom_end = QDateEdit(_to_qdate(date.today()))
        self._custom_end.setCalendarPopup(True)
        self._custom_end.dateChanged.connect(self.refresh)
        range_row.addWidget(self._custom_start)
        range_row.addWidget(QLabel("to"))
        range_row.addWidget(self._custom_end)

        refresh_button = QPushButton("Refresh")
        refresh_button.clicked.connect(self.refresh)
        export_button = QPushButton("Export CSV")
        export_button.clicked.connect(self._on_export_clicked)
        range_row.addWidget(refresh_button)
        range_row.addWidget(export_button)
        range_row.addStretch(1)
        layout.addLayout(range_row)

        self._totals_label = QLabel("")
        layout.addWidget(self._totals_label)

        self._chart = pg.PlotWidget()
        self._chart.setBackground(None)
        self._chart.setLabel("left", "Total tokens")
        self._chart.setMaximumHeight(200)
        layout.addWidget(self._chart)

        layout.addWidget(QLabel("By provider and model:"))
        self._breakdown_table = QTableWidget(0, len(_BREAKDOWN_COLUMNS))
        self._breakdown_table.setHorizontalHeaderLabels(_BREAKDOWN_COLUMNS)
        header = self._breakdown_table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._breakdown_table)

        layout.addWidget(QLabel("By session (double-click to open):"))
        self._session_table = QTableWidget(0, len(_SESSION_COLUMNS))
        self._session_table.setHorizontalHeaderLabels(_SESSION_COLUMNS)
        session_header = self._session_table.horizontalHeader()
        if session_header is not None:
            session_header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._session_table.cellDoubleClicked.connect(self._on_session_row_double_clicked)
        layout.addWidget(self._session_table)

        self._session_ids: list[str] = []
        self._current_rows: list[repositories.UsageRow] = []

        self._on_range_changed(self._range_box.currentText())

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.refresh()

    def _on_range_changed(self, label: str) -> None:
        is_custom = label == "Custom"
        self._custom_start.setVisible(is_custom)
        self._custom_end.setVisible(is_custom)
        self.refresh()

    def refresh(self) -> None:
        start_date = self._custom_start.date().toPython()
        end_date = self._custom_end.date().toPython()
        assert isinstance(start_date, date) and isinstance(end_date, date)
        start_iso, end_iso = _range_bounds(self._range_box.currentText(), start_date, end_date)
        self._current_rows = repositories.list_usage(self._conn, start_iso, end_iso)

        total_input = sum(r.input_tokens for r in self._current_rows)
        total_output = sum(r.output_tokens for r in self._current_rows)
        cost_text = _format_cost_totals(self._current_rows)
        self._totals_label.setText(
            f"Input tokens: {total_input:,}   Output tokens: {total_output:,}   "
            f"Estimated cost: {cost_text}"
        )

        self._update_chart()
        self._update_breakdown_table()
        self._update_session_table()

    def _update_chart(self) -> None:
        self._chart.clear()
        daily: dict[str, int] = defaultdict(int)
        for row in self._current_rows:
            day = row.ts[:10]
            daily[day] += row.input_tokens + row.output_tokens

        days = sorted(daily)
        if not days:
            return
        values = [daily[d] for d in days]
        bars = pg.BarGraphItem(
            x=list(range(len(days))), height=values, width=0.6, brush="steelblue"
        )
        self._chart.addItem(bars)
        axis = self._chart.getAxis("bottom")
        axis.setTicks([[(i, d[5:]) for i, d in enumerate(days)]])

    def _update_breakdown_table(self) -> None:
        grouped: dict[tuple[str, str], list[repositories.UsageRow]] = defaultdict(list)
        for row in self._current_rows:
            grouped[(row.provider, row.model_id)].append(row)

        self._breakdown_table.setRowCount(0)
        for (provider, model_id), rows in sorted(grouped.items()):
            table_row = self._breakdown_table.rowCount()
            self._breakdown_table.insertRow(table_row)
            self._breakdown_table.setItem(table_row, 0, QTableWidgetItem(provider))
            self._breakdown_table.setItem(table_row, 1, QTableWidgetItem(model_id))
            self._breakdown_table.setItem(
                table_row, 2, QTableWidgetItem(f"{sum(r.input_tokens for r in rows):,}")
            )
            self._breakdown_table.setItem(
                table_row, 3, QTableWidgetItem(f"{sum(r.output_tokens for r in rows):,}")
            )
            self._breakdown_table.setItem(table_row, 4, QTableWidgetItem(_format_cost_totals(rows)))

    def _update_session_table(self) -> None:
        titles = {s.id: s.title for s in repositories.list_sessions(self._conn)}
        grouped: dict[str, list[repositories.UsageRow]] = defaultdict(list)
        for row in self._current_rows:
            if row.session_id:
                grouped[row.session_id].append(row)

        self._session_table.setRowCount(0)
        self._session_ids = []
        for session_id, rows in sorted(grouped.items(), key=lambda kv: titles.get(kv[0], "")):
            table_row = self._session_table.rowCount()
            self._session_table.insertRow(table_row)
            self._session_table.setItem(
                table_row, 0, QTableWidgetItem(titles.get(session_id, session_id))
            )
            self._session_table.setItem(
                table_row, 1, QTableWidgetItem(f"{sum(r.input_tokens for r in rows):,}")
            )
            self._session_table.setItem(
                table_row, 2, QTableWidgetItem(f"{sum(r.output_tokens for r in rows):,}")
            )
            self._session_table.setItem(table_row, 3, QTableWidgetItem(_format_cost_totals(rows)))
            self._session_ids.append(session_id)

    def _on_session_row_double_clicked(self, row: int, _column: int) -> None:
        if 0 <= row < len(self._session_ids):
            self.open_session_requested.emit(self._session_ids[row])

    def _on_export_clicked(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(self, "Export usage to CSV", "usage.csv")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "timestamp",
                    "session_id",
                    "provider",
                    "model_id",
                    "input_tokens",
                    "output_tokens",
                    "triggered_by",
                    "est_cost",
                    "currency",
                ]
            )
            for row in self._current_rows:
                writer.writerow(
                    [
                        row.ts,
                        row.session_id,
                        row.provider,
                        row.model_id,
                        row.input_tokens,
                        row.output_tokens,
                        row.triggered_by,
                        row.est_cost,
                        row.currency,
                    ]
                )

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        self.hide()
