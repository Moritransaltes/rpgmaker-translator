"""Translation table widget — main workspace for reviewing and editing translations."""

import re

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QLineEdit, QComboBox, QLabel, QMenu, QAbstractItemView, QHeaderView,
    QInputDialog, QTextEdit, QSplitter, QGroupBox,
)
from PyQt6.QtCore import pyqtSignal, Qt, QTimer
from PyQt6.QtGui import QColor, QAction, QTextCursor

from ..project_model import TranslationEntry

# Same regex as ollama_client — matches RPG Maker control codes
_CODE_RE = re.compile(
    r'\\[A-Za-z]+\[\d*\]'
    r'|\\[{}$.|!><^]'
    r'|<[^>]+>'
)


# Status colors — light mode
STATUS_COLORS_LIGHT = {
    "untranslated": QColor(255, 230, 230),   # light red
    "translated":   QColor(255, 255, 210),   # light yellow
    "reviewed":     QColor(210, 255, 210),   # light green
    "skipped":      QColor(230, 230, 230),   # light gray
}

# Status colors — dark mode (muted, readable with light text)
STATUS_COLORS_DARK = {
    "untranslated": QColor(80, 40, 40),      # dark red
    "translated":   QColor(70, 65, 30),      # dark yellow
    "reviewed":     QColor(30, 70, 40),      # dark green
    "skipped":      QColor(50, 50, 55),      # dark gray
}

STATUS_ICONS = {
    "untranslated": "\u25cb",  # ○
    "translated":   "\u25d0",  # ◐
    "reviewed":     "\u25cf",  # ●
    "skipped":      "\u2014",  # —
}

# Column indices
COL_STATUS = 0
COL_FILE = 1
COL_FIELD = 2
COL_ORIGINAL = 3
COL_TRANSLATION = 4


class TranslationTable(QWidget):
    """Table view for browsing, editing, and managing translations."""

    translate_requested = pyqtSignal(list)    # List of entry IDs to translate
    retranslate_correction = pyqtSignal(str, str)  # entry_id, user correction hint
    variant_requested = pyqtSignal(str)       # entry_id — request translation variants
    status_changed = pyqtSignal()             # Emitted when any status changes

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries = []
        self._visible_entries = []
        self._updating = False
        self._dark_mode = True  # Match main_window default
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(250)  # 250ms debounce
        self._filter_timer.timeout.connect(self._apply_filter)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # ── Filter bar ─────────────────────────────────────────────
        filter_row = QHBoxLayout()

        filter_row.addWidget(QLabel("Search:"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter by text...")
        self.search_edit.textChanged.connect(self._schedule_filter)
        filter_row.addWidget(self.search_edit)

        filter_row.addWidget(QLabel("Status:"))
        self.status_filter = QComboBox()
        self.status_filter.addItems(["All", "Untranslated", "Translated", "Reviewed", "Skipped"])
        self.status_filter.currentTextChanged.connect(self._apply_filter)
        filter_row.addWidget(self.status_filter)

        layout.addLayout(filter_row)

        # ── Vertical splitter: table on top, editor on bottom ─────
        vsplit = QSplitter(Qt.Orientation.Vertical)

        # ── Table ──────────────────────────────────────────────────
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["", "File", "Field", "Original (JP)", "Translation (EN)"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.cellChanged.connect(self._on_cell_changed)
        self.table.setWordWrap(True)

        # Column widths
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(COL_STATUS, 30)
        header.setSectionResizeMode(COL_FILE, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_FIELD, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_ORIGINAL, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_TRANSLATION, QHeaderView.ResizeMode.Stretch)

        vsplit.addWidget(self.table)

        # ── Editor panel ───────────────────────────────────────────
        editor_widget = QWidget()
        editor_layout = QHBoxLayout(editor_widget)
        editor_layout.setContentsMargins(0, 0, 0, 0)

        # Left: original (read-only)
        orig_group = QGroupBox("Original (JP)")
        orig_box = QVBoxLayout(orig_group)
        self.orig_editor = QTextEdit()
        self.orig_editor.setReadOnly(True)
        self.orig_editor.setAcceptRichText(False)
        self.orig_editor.setPlaceholderText("Select a row to view original text...")
        orig_box.addWidget(self.orig_editor)
        editor_layout.addWidget(orig_group)

        # Right: translation (editable, with code-insert right-click menu)
        trans_group = QGroupBox("Translation (EN) — editable")
        trans_box = QVBoxLayout(trans_group)
        self.trans_editor = QTextEdit()
        self.trans_editor.setAcceptRichText(False)
        self.trans_editor.setPlaceholderText("Select a row to edit translation...")
        self.trans_editor.textChanged.connect(self._on_editor_changed)
        self.trans_editor.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.trans_editor.customContextMenuRequested.connect(self._show_editor_context_menu)
        trans_box.addWidget(self.trans_editor)
        editor_layout.addWidget(trans_group)

        vsplit.addWidget(editor_widget)

        # Default split: 70% table, 30% editor
        vsplit.setStretchFactor(0, 7)
        vsplit.setStretchFactor(1, 3)

        layout.addWidget(vsplit)

        # Track current selection
        self._selected_row = -1
        self.table.currentCellChanged.connect(self._on_row_selected)

        # ── Stats bar ──────────────────────────────────────────────
        self.stats_label = QLabel("No entries loaded")
        layout.addWidget(self.stats_label)

    @property
    def _status_colors(self):
        return STATUS_COLORS_DARK if self._dark_mode else STATUS_COLORS_LIGHT

    def set_dark_mode(self, dark: bool):
        """Switch row colors between dark and light palettes."""
        self._dark_mode = dark
        if self._visible_entries:
            self._refresh_table()

    def set_entries(self, entries: list):
        """Load entries into the table."""
        self._entries = entries
        self._apply_filter()

    def _schedule_filter(self):
        """Debounce search — wait 250ms after last keystroke before filtering."""
        self._filter_timer.start()

    def _apply_filter(self):
        """Filter visible entries by search text and status."""
        query = self.search_edit.text().lower()
        status = self.status_filter.currentText().lower()

        self._visible_entries = []
        for e in self._entries:
            if status != "all" and e.status != status:
                continue
            if query and query not in e.original.lower() and query not in e.translation.lower():
                continue
            self._visible_entries.append(e)

        self._refresh_table()

    def _refresh_table(self):
        """Rebuild the table rows from visible entries."""
        self._updating = True
        self.table.setUpdatesEnabled(False)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self._visible_entries))

        read_only = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled

        for row, entry in enumerate(self._visible_entries):
            color = self._status_colors.get(entry.status, QColor(255, 255, 255))

            # Status icon
            status_item = QTableWidgetItem(STATUS_ICONS.get(entry.status, ""))
            status_item.setFlags(read_only)
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            status_item.setBackground(color)
            self.table.setItem(row, COL_STATUS, status_item)

            # File
            file_item = QTableWidgetItem(entry.file)
            file_item.setFlags(read_only)
            file_item.setBackground(color)
            self.table.setItem(row, COL_FILE, file_item)

            # Field
            field_item = QTableWidgetItem(entry.field)
            field_item.setFlags(read_only)
            field_item.setBackground(color)
            self.table.setItem(row, COL_FIELD, field_item)

            # Original (read-only)
            orig_item = QTableWidgetItem(entry.original)
            orig_item.setFlags(read_only)
            orig_item.setBackground(color)
            self.table.setItem(row, COL_ORIGINAL, orig_item)

            # Translation (editable)
            trans_item = QTableWidgetItem(entry.translation)
            trans_item.setBackground(color)
            self.table.setItem(row, COL_TRANSLATION, trans_item)

        self.table.setUpdatesEnabled(True)
        self._updating = False
        self._update_stats()

    def _on_cell_changed(self, row: int, col: int):
        """Handle manual edits in the translation column."""
        if self._updating or col != COL_TRANSLATION:
            return
        if 0 <= row < len(self._visible_entries):
            entry = self._visible_entries[row]
            new_text = self.table.item(row, col).text()
            entry.translation = new_text
            if new_text.strip():
                entry.status = "translated"
            else:
                entry.status = "untranslated"
            self._update_row_color(row, entry)
            self.status_changed.emit()

    def _update_row_color(self, row: int, entry: TranslationEntry):
        """Update the background color and status icon for a row."""
        color = self._status_colors.get(entry.status, QColor(255, 255, 255))
        self.table.item(row, COL_STATUS).setText(STATUS_ICONS.get(entry.status, ""))
        for col in range(5):
            item = self.table.item(row, col)
            if item:
                item.setBackground(color)

    def update_entry(self, entry_id: str, translation: str):
        """Update a specific entry's translation (called after LLM translates)."""
        for row, entry in enumerate(self._visible_entries):
            if entry.id == entry_id:
                entry.translation = translation
                entry.status = "translated"
                self._updating = True
                self.table.item(row, COL_TRANSLATION).setText(translation)
                self._update_row_color(row, entry)
                self._updating = False
                # Also update editor panel if this row is selected
                if row == self._selected_row:
                    self.trans_editor.blockSignals(True)
                    self.trans_editor.setPlainText(translation)
                    self.trans_editor.blockSignals(False)
                break
        self._update_stats()
        self.status_changed.emit()

    def get_selected_entry_ids(self) -> list:
        """Return IDs of currently selected entries."""
        rows = set(idx.row() for idx in self.table.selectedIndexes())
        return [self._visible_entries[r].id for r in sorted(rows) if r < len(self._visible_entries)]

    def _show_context_menu(self, pos):
        """Right-click context menu."""
        menu = QMenu(self)

        translate_action = QAction("Translate Selected", self)
        translate_action.triggered.connect(self._translate_selected)
        menu.addAction(translate_action)

        retranslate_action = QAction("Retranslate with Correction...", self)
        retranslate_action.triggered.connect(self._retranslate_with_correction)
        menu.addAction(retranslate_action)

        variant_action = QAction("Show Variants (3 options)...", self)
        variant_action.triggered.connect(self._request_variants)
        menu.addAction(variant_action)

        menu.addSeparator()

        review_action = QAction("Mark as Reviewed", self)
        review_action.triggered.connect(lambda: self._set_status("reviewed"))
        menu.addAction(review_action)

        skip_action = QAction("Mark as Skipped", self)
        skip_action.triggered.connect(lambda: self._set_status("skipped"))
        menu.addAction(skip_action)

        unmark_action = QAction("Mark as Untranslated", self)
        unmark_action.triggered.connect(lambda: self._set_status("untranslated"))
        menu.addAction(unmark_action)

        menu.addSeparator()

        copy_action = QAction("Copy Original to Translation", self)
        copy_action.triggered.connect(self._copy_original)
        menu.addAction(copy_action)

        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _translate_selected(self):
        """Emit signal to translate selected entries."""
        ids = self.get_selected_entry_ids()
        if ids:
            self.translate_requested.emit(ids)

    def _retranslate_with_correction(self):
        """Ask user what's wrong and emit signal for a single-entry retranslation."""
        ids = self.get_selected_entry_ids()
        if not ids:
            return
        # Only retranslate the first selected entry
        entry_id = ids[0]
        correction, ok = QInputDialog.getText(
            self, "Retranslate with Correction",
            "What's wrong with the current translation?\n"
            "(e.g. 'wrong pronoun, should be she/her' or 'speaker is talking to someone, use you')",
        )
        if ok and correction.strip():
            self.retranslate_correction.emit(entry_id, correction.strip())

    def _request_variants(self):
        """Emit signal to generate translation variants for the first selected entry."""
        ids = self.get_selected_entry_ids()
        if ids:
            self.variant_requested.emit(ids[0])

    def _set_status(self, status: str):
        """Set status for all selected rows."""
        rows = sorted(set(idx.row() for idx in self.table.selectedIndexes()))
        for row in rows:
            if row < len(self._visible_entries):
                entry = self._visible_entries[row]
                entry.status = status
                self._update_row_color(row, entry)
        self._update_stats()
        self.status_changed.emit()

    def _copy_original(self):
        """Copy original text to translation column for selected rows."""
        rows = sorted(set(idx.row() for idx in self.table.selectedIndexes()))
        self._updating = True
        for row in rows:
            if row < len(self._visible_entries):
                entry = self._visible_entries[row]
                entry.translation = entry.original
                entry.status = "translated"
                self.table.item(row, COL_TRANSLATION).setText(entry.original)
                self._update_row_color(row, entry)
        self._updating = False
        self._update_stats()
        self.status_changed.emit()

    # ── Editor panel handlers ─────────────────────────────────────

    def _on_row_selected(self, row: int, col: int, prev_row: int, prev_col: int):
        """When a row is clicked, load its text into the editor panel."""
        if row < 0 or row >= len(self._visible_entries):
            self._selected_row = -1
            self.orig_editor.clear()
            self.trans_editor.clear()
            return

        self._selected_row = row
        entry = self._visible_entries[row]

        # Block signals while loading to avoid feedback loop
        self.trans_editor.blockSignals(True)
        self.orig_editor.setPlainText(entry.original)
        self.trans_editor.setPlainText(entry.translation)
        self.trans_editor.blockSignals(False)

    def _on_editor_changed(self):
        """Save edits from the translation editor back to the entry and table."""
        row = self._selected_row
        if row < 0 or row >= len(self._visible_entries):
            return

        entry = self._visible_entries[row]
        new_text = self.trans_editor.toPlainText()
        entry.translation = new_text

        if new_text.strip():
            entry.status = "translated"
        else:
            entry.status = "untranslated"

        # Sync back to the table cell
        self._updating = True
        self.table.item(row, COL_TRANSLATION).setText(new_text)
        self._update_row_color(row, entry)
        self._updating = False
        self.status_changed.emit()

    def _show_editor_context_menu(self, pos):
        """Right-click menu on translation editor — insert codes from original."""
        # Start with the standard text-edit menu (copy, paste, undo, etc.)
        menu = self.trans_editor.createStandardContextMenu()

        row = self._selected_row
        if row < 0 or row >= len(self._visible_entries):
            menu.exec(self.trans_editor.mapToGlobal(pos))
            return

        entry = self._visible_entries[row]
        orig_codes = _CODE_RE.findall(entry.original)
        if not orig_codes:
            menu.exec(self.trans_editor.mapToGlobal(pos))
            return

        menu.addSeparator()

        # Find which codes are missing from the translation
        trans_text = self.trans_editor.toPlainText()
        missing = [c for c in orig_codes if c not in trans_text]

        if missing:
            restore_action = QAction(f"Restore {len(missing)} Missing Code(s)", self)
            restore_action.triggered.connect(lambda: self._restore_missing_codes(missing))
            menu.addAction(restore_action)
            menu.addSeparator()

        # List each code from the original for individual insertion
        for code in orig_codes:
            label = f"Insert {code}"
            if code in missing:
                label += "  (missing)"
            action = QAction(label, self)
            action.triggered.connect(lambda checked, c=code: self._insert_code_at_cursor(c))
            menu.addAction(action)

        menu.exec(self.trans_editor.mapToGlobal(pos))

    def _insert_code_at_cursor(self, code: str):
        """Insert a control code at the current cursor position in the translation editor."""
        cursor = self.trans_editor.textCursor()
        cursor.insertText(code)
        self.trans_editor.setTextCursor(cursor)

    def _restore_missing_codes(self, missing_codes: list):
        """Insert all missing codes at the start of the translation."""
        cursor = self.trans_editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        cursor.insertText("".join(missing_codes))
        self.trans_editor.setTextCursor(cursor)

    def _update_stats(self):
        """Update the stats label."""
        total = len(self._visible_entries)
        translated = sum(1 for e in self._visible_entries if e.status in ("translated", "reviewed"))
        reviewed = sum(1 for e in self._visible_entries if e.status == "reviewed")
        self.stats_label.setText(
            f"Showing {total} entries  |  "
            f"Translated: {translated}  |  Reviewed: {reviewed}"
        )
