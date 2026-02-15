"""Translation queue panel — live view of batch progress and translation log."""

import time
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QHeaderView, QLabel, QAbstractItemView, QPushButton, QComboBox,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor


# Status icons and colors
_STATUS = {
    "queued":      ("\u23f3", QColor("#9399b2")),   # hourglass, overlay2 (readable)
    "translating": ("\u2699", QColor("#89b4fa")),   # gear, blue
    "done":        ("\u2714", QColor("#a6e3a1")),   # check, green
    "error":       ("\u2718", QColor("#f38ba8")),   # cross, red
    "skipped":     ("\u23ed", QColor("#6c7086")),   # skip, dim gray
    "tm":          ("\u267b", QColor("#cba6f7")),   # recycle, mauve (translation memory)
    "glossary":    ("\U0001f4d6", QColor("#f9e2af")),  # book, yellow (glossary prefill)
}


class QueuePanel(QWidget):
    """Live translation queue showing pending, in-progress, and completed entries."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries = []         # list of entry objects in queue order
        self._entry_rows = {}      # entry_id -> row index
        self._start_time = 0.0
        self._done_count = 0
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # Header row
        header = QHBoxLayout()
        self._summary_label = QLabel("No batch running")
        self._summary_label.setStyleSheet("font-weight: bold;")
        header.addWidget(self._summary_label)
        header.addStretch()

        # Filter dropdown
        self._filter_combo = QComboBox()
        self._filter_combo.addItems(["All", "Queued", "Done", "Error", "TM/Glossary"])
        self._filter_combo.currentTextChanged.connect(self._apply_filter)
        header.addWidget(QLabel("Show:"))
        header.addWidget(self._filter_combo)

        # Clear button
        clear_btn = QPushButton("Clear Log")
        clear_btn.clicked.connect(self.clear)
        header.addWidget(clear_btn)

        layout.addLayout(header)

        # Queue table
        self._table = QTableWidget()
        self._table.setColumnCount(6)
        self._table.setHorizontalHeaderLabels([
            "", "File", "Field", "Original", "Translation", "Source"
        ])
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(0, 30)
        self._table.setColumnWidth(1, 120)
        self._table.setColumnWidth(2, 100)
        self._table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Stretch)
        self._table.setColumnWidth(5, 80)

        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setDefaultSectionSize(24)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setStyleSheet("""
            QTableWidget {
                background-color: #1e1e2e;
                alternate-background-color: #24243a;
                color: #cdd6f4;
                gridline-color: #313244;
                border: 1px solid #313244;
                font-size: 12px;
            }
            QTableWidget::item {
                padding: 2px 4px;
            }
            QTableWidget::item:selected {
                background-color: #45475a;
                color: #cdd6f4;
            }
            QHeaderView::section {
                background-color: #181825;
                color: #a6adc8;
                border: 1px solid #313244;
                padding: 3px 6px;
                font-weight: bold;
                font-size: 11px;
            }
        """)
        layout.addWidget(self._table)

        # Stats row
        stats = QHBoxLayout()
        self._eta_label = QLabel("")
        stats.addWidget(self._eta_label)
        stats.addStretch()
        self._speed_label = QLabel("")
        stats.addWidget(self._speed_label)
        layout.addLayout(stats)

    # ── Public API ──────────────────────────────────────────────────

    def load_queue(self, entries: list):
        """Populate the queue with entries about to be translated."""
        self.clear()
        self._entries = list(entries)
        self._start_time = time.time()
        self._done_count = 0

        self._table.setRowCount(len(entries))
        for i, entry in enumerate(entries):
            self._entry_rows[entry.id] = i

            # Status icon
            icon, color = _STATUS["queued"]
            status_item = QTableWidgetItem(icon)
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            status_item.setData(Qt.ItemDataRole.UserRole, "queued")
            self._table.setItem(i, 0, status_item)

            # File — subdued color for metadata columns
            dim = QColor("#7f849c")  # overlay1
            file_item = QTableWidgetItem(entry.file)
            file_item.setToolTip(entry.id)
            file_item.setForeground(dim)
            self._table.setItem(i, 1, file_item)

            # Field
            field_item = QTableWidgetItem(entry.field or "")
            field_item.setForeground(dim)
            self._table.setItem(i, 2, field_item)

            # Original (truncated) — bright so you can read it
            orig = (entry.original or "")[:120].replace("\n", " ")
            self._table.setItem(i, 3, QTableWidgetItem(orig))

            # Translation (empty until done)
            self._table.setItem(i, 4, QTableWidgetItem(""))

            # Source (how it was translated)
            self._table.setItem(i, 5, QTableWidgetItem(""))

        self._update_summary()

    def mark_entry_done(self, entry_id: str, translation: str,
                        source: str = "LLM"):
        """Mark an entry as translated and show the result."""
        row = self._entry_rows.get(entry_id)
        if row is None:
            return

        self._done_count += 1
        icon, color = _STATUS["done"]
        status_item = self._table.item(row, 0)
        if status_item:
            status_item.setText(icon)
            status_item.setData(Qt.ItemDataRole.UserRole, "done")

        # Translation text
        trans_text = (translation or "")[:120].replace("\n", " ")
        trans_item = self._table.item(row, 4)
        if trans_item:
            trans_item.setText(trans_text)
            trans_item.setForeground(color)

        # Source label
        source_item = self._table.item(row, 5)
        if source_item:
            source_item.setText(source)
            if source == "TM":
                s_icon, s_color = _STATUS["tm"]
                source_item.setForeground(s_color)
            elif source == "Glossary":
                s_icon, s_color = _STATUS["glossary"]
                source_item.setForeground(s_color)
            else:
                source_item.setForeground(color)

        self._update_summary()
        self._apply_filter(self._filter_combo.currentText())

        # Auto-scroll to the latest completed entry
        self._table.scrollToItem(
            self._table.item(row, 0),
            QAbstractItemView.ScrollHint.PositionAtCenter,
        )

    def mark_entry_error(self, entry_id: str, error_msg: str):
        """Mark an entry as failed."""
        row = self._entry_rows.get(entry_id)
        if row is None:
            return

        icon, color = _STATUS["error"]
        status_item = self._table.item(row, 0)
        if status_item:
            status_item.setText(icon)
            status_item.setData(Qt.ItemDataRole.UserRole, "error")

        trans_item = self._table.item(row, 4)
        if trans_item:
            trans_item.setText(f"ERROR: {error_msg[:80]}")
            trans_item.setForeground(color)

        source_item = self._table.item(row, 5)
        if source_item:
            source_item.setText("Error")
            source_item.setForeground(color)

    def mark_prefill(self, entry_id: str, translation: str,
                     source: str = "TM"):
        """Mark an entry as pre-filled by TM or glossary (not LLM)."""
        self.mark_entry_done(entry_id, translation, source=source)

    def mark_batch_finished(self):
        """Called when the entire batch completes."""
        elapsed = time.time() - self._start_time if self._start_time else 0
        total = len(self._entries)
        self._summary_label.setText(
            f"Batch complete: {self._done_count}/{total} translated "
            f"in {self._format_time(elapsed)}"
        )
        self._eta_label.setText("")
        if elapsed > 0 and self._done_count > 0:
            self._speed_label.setText(
                f"Average: {elapsed / self._done_count:.1f}s/entry"
            )

    def clear(self):
        """Clear the queue display."""
        self._table.setRowCount(0)
        self._entries = []
        self._entry_rows = {}
        self._done_count = 0
        self._start_time = 0
        self._summary_label.setText("No batch running")
        self._eta_label.setText("")
        self._speed_label.setText("")

    # ── Internal ────────────────────────────────────────────────────

    def _update_summary(self):
        """Update the summary label with progress and ETA."""
        total = len(self._entries)
        if total == 0:
            return

        elapsed = time.time() - self._start_time if self._start_time else 0
        remaining = total - self._done_count

        self._summary_label.setText(
            f"Translating: {self._done_count}/{total} "
            f"({remaining} remaining)"
        )

        if self._done_count > 0 and elapsed > 0:
            rate = elapsed / self._done_count
            eta = rate * remaining
            self._eta_label.setText(
                f"ETA: {self._format_time(eta)} "
                f"| Elapsed: {self._format_time(elapsed)}"
            )
            self._speed_label.setText(f"{rate:.1f}s/entry")

    def _apply_filter(self, filter_text: str):
        """Show/hide rows based on filter selection."""
        for row in range(self._table.rowCount()):
            status_item = self._table.item(row, 0)
            if not status_item:
                continue
            status = status_item.data(Qt.ItemDataRole.UserRole)
            source_item = self._table.item(row, 5)
            source = source_item.text() if source_item else ""

            visible = True
            if filter_text == "Queued":
                visible = status == "queued"
            elif filter_text == "Done":
                visible = status == "done"
            elif filter_text == "Error":
                visible = status == "error"
            elif filter_text == "TM/Glossary":
                visible = source in ("TM", "Glossary")

            self._table.setRowHidden(row, not visible)

    @staticmethod
    def _format_time(seconds: float) -> str:
        """Format seconds into human-readable time."""
        if seconds < 60:
            return f"{seconds:.0f}s"
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        if minutes < 60:
            return f"{minutes}m {secs}s"
        hours = minutes // 60
        mins = minutes % 60
        return f"{hours}h {mins}m"
