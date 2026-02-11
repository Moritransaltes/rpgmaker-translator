"""Translation table widget — main workspace for reviewing and editing translations.

Uses QTableView + QAbstractTableModel for virtual scrolling — only visible
rows are rendered, so 24k+ entries load instantly.
"""

import re

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableView,
    QLineEdit, QComboBox, QLabel, QMenu, QAbstractItemView, QHeaderView,
    QInputDialog, QTextEdit, QSplitter, QGroupBox,
)
from PyQt6.QtCore import (
    pyqtSignal, Qt, QTimer, QAbstractTableModel, QModelIndex,
)
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
    "reviewed":     QColor(30, 70, 40),       # dark green
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

_COLUMN_HEADERS = ["", "File", "Field", "Original (JP)", "Translation (EN)"]


class TranslationTableModel(QAbstractTableModel):
    """Model backing the translation table — provides data on demand."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: list[TranslationEntry] = []
        self._dark_mode = True

    @property
    def _status_colors(self):
        return STATUS_COLORS_DARK if self._dark_mode else STATUS_COLORS_LIGHT

    def set_entries(self, entries: list):
        self.beginResetModel()
        self._entries = entries
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return len(self._entries)

    def columnCount(self, parent=QModelIndex()):
        return 5

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        if row < 0 or row >= len(self._entries):
            return None

        entry = self._entries[row]

        if role == Qt.ItemDataRole.DisplayRole or role == Qt.ItemDataRole.EditRole:
            if col == COL_STATUS:
                return STATUS_ICONS.get(entry.status, "")
            elif col == COL_FILE:
                return entry.file
            elif col == COL_FIELD:
                return entry.field
            elif col == COL_ORIGINAL:
                return entry.original
            elif col == COL_TRANSLATION:
                return entry.translation

        elif role == Qt.ItemDataRole.BackgroundRole:
            return self._status_colors.get(entry.status, QColor(255, 255, 255))

        elif role == Qt.ItemDataRole.TextAlignmentRole:
            if col == COL_STATUS:
                return Qt.AlignmentFlag.AlignCenter

        return None

    def setData(self, index: QModelIndex, value, role=Qt.ItemDataRole.EditRole):
        if not index.isValid() or role != Qt.ItemDataRole.EditRole:
            return False
        row, col = index.row(), index.column()
        if col != COL_TRANSLATION or row < 0 or row >= len(self._entries):
            return False

        entry = self._entries[row]
        new_text = str(value)
        entry.translation = new_text
        if new_text.strip():
            entry.status = "translated"
        else:
            entry.status = "untranslated"
        # Emit change for the entire row (status icon + colors changed too)
        self.dataChanged.emit(
            self.index(row, 0), self.index(row, self.columnCount() - 1)
        )
        return True

    def flags(self, index: QModelIndex):
        base = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
        if index.column() == COL_TRANSLATION:
            return base | Qt.ItemFlag.ItemIsEditable
        return base

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _COLUMN_HEADERS[section] if section < len(_COLUMN_HEADERS) else ""
        return None

    # ── Helpers for external updates ──────────────────────────────

    def entry_at(self, row: int) -> TranslationEntry | None:
        if 0 <= row < len(self._entries):
            return self._entries[row]
        return None

    def refresh_row(self, row: int):
        """Notify the view that a row's data changed."""
        if 0 <= row < len(self._entries):
            self.dataChanged.emit(
                self.index(row, 0), self.index(row, self.columnCount() - 1)
            )

    def refresh_all(self):
        """Notify the view that all visible data may have changed (e.g. dark mode toggle)."""
        if self._entries:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._entries) - 1, self.columnCount() - 1),
            )


class TranslationTable(QWidget):
    """Table view for browsing, editing, and managing translations."""

    translate_requested = pyqtSignal(list)    # List of entry IDs to translate
    retranslate_correction = pyqtSignal(str, str)  # entry_id, user correction hint
    variant_requested = pyqtSignal(str)       # entry_id — request translation variants
    polish_requested = pyqtSignal(list)       # List of entry IDs to polish
    status_changed = pyqtSignal()             # Emitted when any status changes
    glossary_add = pyqtSignal(str, str, str)  # jp_term, en_term, "project"|"general"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all_entries = []      # full project (never file-filtered)
        self._entries = []           # current file-filtered subset (or all)
        self._visible_entries = []   # after search + status filter
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
        self.search_edit.setPlaceholderText("Search all entries (ignores codes)...")
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

        # ── Table (QTableView + model) ────────────────────────────
        self._model = TranslationTableModel(self)
        self.table = QTableView()
        self.table.setModel(self._model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
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
        self.orig_editor.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.orig_editor.customContextMenuRequested.connect(self._show_orig_context_menu)
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
        self.table.selectionModel().currentRowChanged.connect(self._on_row_selected)

        # Detect edits via model's dataChanged (from in-table editing)
        self._model.dataChanged.connect(self._on_model_data_changed)

        # ── Stats bar ──────────────────────────────────────────────
        self.stats_label = QLabel("No entries loaded")
        layout.addWidget(self.stats_label)

    def set_dark_mode(self, dark: bool):
        """Switch row colors between dark and light palettes."""
        self._dark_mode = dark
        self._model._dark_mode = dark
        self._model.refresh_all()

    def set_entries(self, entries: list):
        """Load full project entries into the table."""
        self._all_entries = entries
        self._entries = entries
        self._apply_filter()

    def filter_by_file(self, entries: list):
        """Show only entries from a specific file (file tree click).

        The full project list is kept so text search can span all files.
        """
        self._entries = entries
        self._apply_filter()

    def clear_file_filter(self):
        """Remove file filter — show all project entries."""
        self._entries = self._all_entries
        self._apply_filter()

    def refresh(self):
        """Re-apply current filters (after external data changes)."""
        self._apply_filter()

    def _schedule_filter(self):
        """Debounce search — wait 250ms after last keystroke before filtering."""
        self._filter_timer.start()

    @staticmethod
    def _strip_codes(text: str) -> str:
        """Remove control codes from text for search matching."""
        return _CODE_RE.sub("", text)

    def _apply_filter(self):
        """Filter visible entries by search text and status.

        When a search query is active, searches ALL project entries
        (ignoring file tree filter) so you can find text across the
        entire game.  Control codes are stripped before matching.
        """
        query = self.search_edit.text().lower()
        status = self.status_filter.currentText().lower()

        # Search all entries when query is active, file-filtered otherwise
        source = self._all_entries if query else self._entries

        self._visible_entries = []
        for e in source:
            if status != "all" and e.status != status:
                continue
            if query:
                orig_clean = self._strip_codes(e.original).lower()
                trans_clean = self._strip_codes(e.translation).lower()
                if query not in orig_clean and query not in trans_clean:
                    continue
            self._visible_entries.append(e)

        self._model.set_entries(self._visible_entries)
        self._update_stats()

    def _on_model_data_changed(self, top_left, bottom_right, roles=None):
        """Handle edits made via the table's inline editor."""
        self._update_stats()
        self.status_changed.emit()

    def update_entry(self, entry_id: str, translation: str):
        """Update a specific entry's translation (called after LLM translates)."""
        for row, entry in enumerate(self._visible_entries):
            if entry.id == entry_id:
                entry.translation = translation
                entry.status = "translated"
                self._model.refresh_row(row)
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
        rows = set(idx.row() for idx in self.table.selectionModel().selectedRows())
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

        polish_action = QAction("Polish Grammar", self)
        polish_action.triggered.connect(self._polish_selected)
        menu.addAction(polish_action)

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

        menu.addSeparator()

        add_proj_glossary = QAction("Add to Project Glossary...", self)
        add_proj_glossary.triggered.connect(lambda: self._add_row_to_glossary("project"))
        menu.addAction(add_proj_glossary)

        add_gen_glossary = QAction("Add to General Glossary...", self)
        add_gen_glossary.triggered.connect(lambda: self._add_row_to_glossary("general"))
        menu.addAction(add_gen_glossary)

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

    def _polish_selected(self):
        """Emit signal to polish grammar of selected entries."""
        ids = self.get_selected_entry_ids()
        if ids:
            self.polish_requested.emit(ids)

    def _set_status(self, status: str):
        """Set status for all selected rows."""
        rows = sorted(set(idx.row() for idx in self.table.selectionModel().selectedRows()))
        for row in rows:
            if row < len(self._visible_entries):
                entry = self._visible_entries[row]
                entry.status = status
                self._model.refresh_row(row)
        self._update_stats()
        self.status_changed.emit()

    def _copy_original(self):
        """Copy original text to translation column for selected rows."""
        rows = sorted(set(idx.row() for idx in self.table.selectionModel().selectedRows()))
        for row in rows:
            if row < len(self._visible_entries):
                entry = self._visible_entries[row]
                entry.translation = entry.original
                entry.status = "translated"
                self._model.refresh_row(row)
        self._update_stats()
        self.status_changed.emit()

    def _add_row_to_glossary(self, glossary_type: str):
        """Add selected row's original→translation as a glossary entry."""
        ids = self.get_selected_entry_ids()
        if not ids:
            return
        # Use the first selected entry
        entry = None
        for e in self._visible_entries:
            if e.id == ids[0]:
                entry = e
                break
        if not entry:
            return
        jp_term = entry.original.strip()
        en_prefill = entry.translation.strip()
        # Strip control codes from both for cleaner glossary entries
        jp_term = self._strip_codes(jp_term)
        en_prefill = self._strip_codes(en_prefill)
        label = "Project" if glossary_type == "project" else "General"
        en_term, ok = QInputDialog.getText(
            self, f"Add to {label} Glossary",
            f"Japanese: {jp_term}\n\nEnglish translation:",
            text=en_prefill,
        )
        if ok and en_term.strip() and jp_term:
            self.glossary_add.emit(jp_term, en_term.strip(), glossary_type)

    # ── Editor panel handlers ─────────────────────────────────────

    def _on_row_selected(self, current: QModelIndex, previous: QModelIndex):
        """When a row is clicked, load its text into the editor panel."""
        row = current.row()
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

        # Sync back to the model (refreshes colors + status icon)
        self._model.refresh_row(row)
        self.status_changed.emit()

    def _show_editor_context_menu(self, pos):
        """Right-click menu on translation editor — glossary add + insert codes."""
        # Start with the standard text-edit menu (copy, paste, undo, etc.)
        menu = self.trans_editor.createStandardContextMenu()

        # Glossary add from selected EN text
        selected = self.trans_editor.textCursor().selectedText().strip()
        if selected:
            menu.addSeparator()
            proj_action = QAction(f'Add "{selected}" to Project Glossary...', self)
            proj_action.triggered.connect(lambda: self._add_to_glossary_from_trans(selected, "project"))
            menu.addAction(proj_action)
            gen_action = QAction(f'Add "{selected}" to General Glossary...', self)
            gen_action.triggered.connect(lambda: self._add_to_glossary_from_trans(selected, "general"))
            menu.addAction(gen_action)

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

    def _show_orig_context_menu(self, pos):
        """Right-click menu on original editor — glossary add from JP selection."""
        menu = self.orig_editor.createStandardContextMenu()
        selected = self.orig_editor.textCursor().selectedText().strip()
        if selected:
            menu.addSeparator()
            proj_action = QAction(f'Add "{selected}" to Project Glossary...', self)
            proj_action.triggered.connect(lambda: self._add_to_glossary_from_orig(selected, "project"))
            menu.addAction(proj_action)
            gen_action = QAction(f'Add "{selected}" to General Glossary...', self)
            gen_action.triggered.connect(lambda: self._add_to_glossary_from_orig(selected, "general"))
            menu.addAction(gen_action)
        menu.exec(self.orig_editor.mapToGlobal(pos))

    def _add_to_glossary_from_orig(self, jp_term: str, glossary_type: str):
        """Prompt for EN translation and emit glossary_add signal."""
        # Pre-fill with selected text from translation editor if any
        prefill = self.trans_editor.textCursor().selectedText().strip()
        en_term, ok = QInputDialog.getText(
            self, f"Add to {'Project' if glossary_type == 'project' else 'General'} Glossary",
            f"Japanese: {jp_term}\n\nEnglish translation:",
            text=prefill,
        )
        if ok and en_term.strip():
            self.glossary_add.emit(jp_term, en_term.strip(), glossary_type)

    def _add_to_glossary_from_trans(self, en_term: str, glossary_type: str):
        """Prompt for JP original and emit glossary_add signal."""
        # Pre-fill with selected text from original editor if any
        prefill = self.orig_editor.textCursor().selectedText().strip()
        jp_term, ok = QInputDialog.getText(
            self, f"Add to {'Project' if glossary_type == 'project' else 'General'} Glossary",
            f"English: {en_term}\n\nJapanese original:",
            text=prefill,
        )
        if ok and jp_term.strip():
            self.glossary_add.emit(jp_term.strip(), en_term, glossary_type)

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
