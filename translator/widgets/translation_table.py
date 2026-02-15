"""Translation table widget — main workspace for reviewing and editing translations.

Uses QTableView + QAbstractTableModel for virtual scrolling — only visible
rows are rendered, so 24k+ entries load instantly.
"""

import re

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableView,
    QLineEdit, QComboBox, QLabel, QMenu, QAbstractItemView, QHeaderView,
    QInputDialog, QMessageBox, QTextEdit, QSplitter, QGroupBox, QCheckBox,
    QPushButton, QTreeWidget, QTreeWidgetItem,
)
from PyQt6.QtCore import (
    pyqtSignal, Qt, QTimer, QAbstractTableModel, QModelIndex,
)
from PyQt6.QtGui import QColor, QAction, QTextCursor, QShortcut, QKeySequence

from ..project_model import TranslationEntry
from .. import CONTROL_CODE_RE, JAPANESE_RE

_CODE_RE = CONTROL_CODE_RE  # local alias
_JAPANESE_RE = JAPANESE_RE


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

_COLUMN_HEADERS = ["", "File", "Event", "Original (JP)", "Translation (EN)"]


def _extract_event_context(entry_id: str) -> str:
    """Extract the event context from an entry ID for display.

    Examples:
        "CommonEvents.json/CE169(リブパイズリ)/dialog_64" → "CE169"
        "Map001.json/Ev3(EV003)/p0/dialog_5"              → "Ev3/p0"
        "Troops.json/Troop5(ゴブリン)/p0/dialog_1"         → "Troop5/p0"
        "Actors.json/1/name"                                → "1"
        "Map001.json/displayName"                           → ""
    """
    parts = entry_id.split("/")
    if len(parts) < 3:
        return ""
    # Middle parts = event context (between filename and entry type)
    middle = parts[1:-1]
    # Strip event names in parentheses for brevity: CE169(リブパイズリ) → CE169
    cleaned = []
    for part in middle:
        paren = part.find("(")
        cleaned.append(part[:paren] if paren > 0 else part)
    return "/".join(cleaned)


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
                return _extract_event_context(entry.id)
            elif col == COL_ORIGINAL:
                return entry.original
            elif col == COL_TRANSLATION:
                return entry.translation

        elif role == Qt.ItemDataRole.ToolTipRole:
            if col == COL_FIELD:
                return f"{entry.field} — {entry.id}"

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
        self.search_edit.setPlaceholderText("Search (use + for AND, e.g. Alice+she)...")
        self.search_edit.textChanged.connect(self._schedule_filter)
        filter_row.addWidget(self.search_edit)

        filter_row.addWidget(QLabel("Status:"))
        self.status_filter = QComboBox()
        self.status_filter.addItems(["All", "Untranslated", "Translated", "Reviewed", "Skipped"])
        self.status_filter.currentTextChanged.connect(self._apply_filter)
        filter_row.addWidget(self.status_filter)

        filter_row.addWidget(QLabel("Field:"))
        self.field_filter = QComboBox()
        self.field_filter.addItems([
            "All Fields",
            "Dialogue",
            "Choices",
            "Scroll Text",
            "Names",
            "Descriptions",
            "Messages / Battle",
            "System / Terms",
            "Plugin Commands",
            "Plugin Params",
            "Map Names",
        ])
        self.field_filter.setToolTip(
            "Filter entries by field type.\n"
            "Useful for reviewing all choices, spotting plugin commands, etc."
        )
        self.field_filter.currentTextChanged.connect(self._apply_filter)
        filter_row.addWidget(self.field_filter)

        filter_row.addWidget(QLabel("Speaker:"))
        self.speaker_filter = QComboBox()
        self.speaker_filter.addItem("All Speakers")
        self.speaker_filter.setToolTip("Filter dialogue by speaker (from event headers)")
        self.speaker_filter.currentTextChanged.connect(self._apply_filter)
        filter_row.addWidget(self.speaker_filter)

        self.jp_check = QCheckBox("JP in translation")
        self.jp_check.setToolTip("Show only entries where the translation still contains Japanese characters")
        self.jp_check.stateChanged.connect(self._apply_filter)
        filter_row.addWidget(self.jp_check)

        layout.addLayout(filter_row)

        # ── Find & Replace bar (hidden by default, Ctrl+H to toggle) ─
        self._replace_bar = QWidget()
        replace_row = QHBoxLayout(self._replace_bar)
        replace_row.setContentsMargins(0, 0, 0, 0)

        replace_row.addWidget(QLabel("Find:"))
        self._find_edit = QLineEdit()
        self._find_edit.setPlaceholderText("Text to find in translations...")
        self._find_edit.returnPressed.connect(self._replace_next)
        replace_row.addWidget(self._find_edit)

        replace_row.addWidget(QLabel("Replace:"))
        self._replace_edit = QLineEdit()
        self._replace_edit.setPlaceholderText("Replace with...")
        self._replace_edit.returnPressed.connect(self._replace_next)
        replace_row.addWidget(self._replace_edit)

        self._replace_one_btn = QPushButton("Replace")
        self._replace_one_btn.setToolTip("Replace in current match and advance to next")
        self._replace_one_btn.clicked.connect(self._replace_current_and_next)
        replace_row.addWidget(self._replace_one_btn)

        self._replace_all_btn = QPushButton("Replace All")
        self._replace_all_btn.setToolTip("Replace all occurrences in all translations")
        self._replace_all_btn.clicked.connect(self._replace_all)
        replace_row.addWidget(self._replace_all_btn)

        self._replace_status = QLabel("")
        replace_row.addWidget(self._replace_status)

        close_btn = QPushButton("\u00d7")  # ×
        close_btn.setFixedWidth(24)
        close_btn.setToolTip("Close Find & Replace")
        close_btn.clicked.connect(self._hide_replace_bar)
        replace_row.addWidget(close_btn)

        self._replace_bar.hide()
        layout.addWidget(self._replace_bar)

        # Ctrl+H shortcut
        shortcut = QShortcut(QKeySequence("Ctrl+H"), self)
        shortcut.activated.connect(self.show_replace_bar)

        # Track position for one-by-one replacement
        self._replace_index = 0

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

        # ── Editor + Context panel ─────────────────────────────────
        editor_split = QSplitter(Qt.Orientation.Vertical)

        # Top: side-by-side editors
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

        editor_split.addWidget(editor_widget)

        # Bottom: event context pane — mini side-by-side JP|EN table
        context_group = QGroupBox("Event Context")
        context_box = QVBoxLayout(context_group)
        context_box.setContentsMargins(4, 4, 4, 4)
        self.context_tree = QTreeWidget()
        self.context_tree.setHeaderLabels(["Speaker", "Original (JP)", "Translation (EN)"])
        self.context_tree.setColumnCount(3)
        self.context_tree.setRootIsDecorated(False)
        self.context_tree.setAlternatingRowColors(True)
        self.context_tree.setWordWrap(True)
        self.context_tree.itemClicked.connect(self._on_context_item_clicked)
        self.context_tree.itemChanged.connect(self._on_context_item_edited)
        # Column sizing: speaker narrow, JP and EN share remaining space equally
        ctx_header = self.context_tree.header()
        ctx_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        ctx_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        ctx_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        context_box.addWidget(self.context_tree)
        editor_split.addWidget(context_group)

        # Editor split: 60% editors, 40% context
        editor_split.setStretchFactor(0, 6)
        editor_split.setStretchFactor(1, 4)

        vsplit.addWidget(editor_split)

        # Default split: 70% table, 30% editor+context
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
        self._populate_speaker_filter(entries)
        self._apply_filter()

    def _populate_speaker_filter(self, entries: list):
        """Extract unique speaker names from entry contexts and populate dropdown."""
        speakers = set()
        for e in entries:
            if not e.context:
                continue
            m = re.search(r'\[Speaker:\s*(.+?)\]', e.context)
            if m:
                speakers.add(m.group(1).strip())
        self.speaker_filter.blockSignals(True)
        current = self.speaker_filter.currentText()
        self.speaker_filter.clear()
        self.speaker_filter.addItem("All Speakers")
        self.speaker_filter.addItem("(No speaker)")
        for name in sorted(speakers):
            self.speaker_filter.addItem(name)
        # Restore previous selection if still valid
        idx = self.speaker_filter.findText(current)
        if idx >= 0:
            self.speaker_filter.setCurrentIndex(idx)
        self.speaker_filter.blockSignals(False)

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

    # Field filter dropdown → set of matching entry.field values
    _FIELD_FILTER_MAP = {
        "All Fields":        None,  # no filtering
        "Dialogue":          {"dialog"},
        "Choices":           {"choice"},
        "Scroll Text":       {"scroll_text"},
        "Names":             {"name", "nickname", "change_name", "change_nickname"},
        "Descriptions":      {"description", "profile", "change_profile"},
        "Messages / Battle": {"message1", "message2", "message3", "message4"},
        "System / Terms":    {"gameTitle", "elements", "skillTypes",
                              "weaponTypes", "armorTypes", "equipTypes"},  # + terms.*
        "Plugin Commands":   {"plugin_command"},
        "Plugin Params":     {"plugin_param"},
        "Map Names":         {"displayName"},
    }

    def _apply_filter(self):
        """Filter visible entries by search text, status, field type, and QA checks.

        When a search query or field filter is active, searches ALL project
        entries (ignoring file tree filter) so you can find text across the
        entire game.  Control codes are stripped before matching.
        """
        query = self.search_edit.text().lower()
        status = self.status_filter.currentText().lower()
        field_label = self.field_filter.currentText()
        field_set = self._FIELD_FILTER_MAP.get(field_label)
        jp_only = self.jp_check.isChecked()
        speaker = self.speaker_filter.currentText()
        speaker_active = speaker not in ("All Speakers", "")

        # Search all entries when query, field filter, speaker, or JP filter is active
        use_all = query or jp_only or field_set is not None or speaker_active
        source = self._all_entries if use_all else self._entries

        self._visible_entries = []
        for e in source:
            if status != "all" and e.status != status:
                continue
            if field_set is not None:
                if field_label == "System / Terms":
                    # Match gameTitle + any terms.* field
                    if e.field not in field_set and not e.field.startswith("terms."):
                        continue
                else:
                    if e.field not in field_set:
                        continue
            if speaker_active:
                if speaker == "(No speaker)":
                    # Match entries with no speaker tag in context
                    if e.context and re.search(r'\[Speaker:', e.context):
                        continue
                else:
                    # Match entries with this specific speaker
                    if not e.context or f"[Speaker: {speaker}]" not in e.context:
                        continue
            if query:
                orig_clean = self._strip_codes(e.original).lower()
                trans_clean = self._strip_codes(e.translation).lower()
                # Also search raw text so control codes like \N[1] are findable
                orig_raw = e.original.lower()
                trans_raw = (e.translation or "").lower()
                combined = orig_clean + " " + trans_clean + " " + orig_raw + " " + trans_raw
                # Support + as AND separator: "\n[1]+she" matches both terms
                terms = [t.strip() for t in query.split("+") if t.strip()]
                if not all(t in combined for t in terms):
                    continue
            if jp_only:
                # Only show entries where the translation contains Japanese
                if not e.translation or not _JAPANESE_RE.search(e.translation):
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

        swap_f2m = QAction("Swap Pronouns (she/her \u2192 he/him)", self)
        swap_f2m.triggered.connect(lambda: self._swap_pronouns("f2m"))
        menu.addAction(swap_f2m)

        swap_m2f = QAction("Swap Pronouns (he/him \u2192 she/her)", self)
        swap_m2f.triggered.connect(lambda: self._swap_pronouns("m2f"))
        menu.addAction(swap_m2f)

        menu.addSeparator()

        set_speaker = QAction("Set Speaker...", self)
        set_speaker.triggered.connect(self._set_speaker)
        menu.addAction(set_speaker)

        clear_speaker = QAction("Clear Speaker", self)
        clear_speaker.triggered.connect(self._clear_speaker)
        menu.addAction(clear_speaker)

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

    # ── Pronoun swap ───────────────────────────────────────────────

    @staticmethod
    def _apply_pronoun_swap(text: str, direction: str) -> str:
        """Swap gendered pronouns and gendered nouns in *text*.

        *direction* is ``"f2m"`` (she→he) or ``"m2f"`` (he→she).
        Order matters: longer / compound forms are replaced first so that
        e.g. ``herself`` isn't partially matched by the ``her`` rule.
        """
        if direction == "f2m":
            # she/her → he/him  (order: compounds first)
            text = re.sub(r'\bherself\b', 'himself', text)
            text = re.sub(r'\bHerself\b', 'Himself', text)
            text = re.sub(r'\bhers\b', 'his', text)
            text = re.sub(r'\bHers\b', 'His', text)
            text = re.sub(r"\bshe's\b", "he's", text)
            text = re.sub(r"\bShe's\b", "He's", text)
            # "her" before a lowercase word → possessive "his"
            text = re.sub(r'\bher(\s+[a-z])', r'his\1', text)
            text = re.sub(r'\bHer(\s+[a-z])', r'His\1', text)
            # "her" in all other positions → object "him"
            text = re.sub(r'\bher\b', 'him', text)
            text = re.sub(r'\bHer\b', 'Him', text)
            # Simple subject
            text = re.sub(r'\bshe\b', 'he', text)
            text = re.sub(r'\bShe\b', 'He', text)
            # Gendered nouns
            text = re.sub(r'\bgirls\b', 'guys', text)
            text = re.sub(r'\bGirls\b', 'Guys', text)
            text = re.sub(r'\bgirl\b', 'guy', text)
            text = re.sub(r'\bGirl\b', 'Guy', text)
            text = re.sub(r'\bwoman\b', 'man', text)
            text = re.sub(r'\bWoman\b', 'Man', text)
            text = re.sub(r'\bwomen\b', 'men', text)
            text = re.sub(r'\bWomen\b', 'Men', text)
            text = re.sub(r'\blady\b', 'gentleman', text)
            text = re.sub(r'\bLady\b', 'Gentleman', text)
            text = re.sub(r'\bmother\b', 'father', text)
            text = re.sub(r'\bMother\b', 'Father', text)
            text = re.sub(r'\bsister\b', 'brother', text)
            text = re.sub(r'\bSister\b', 'Brother', text)
            text = re.sub(r'\bdaughter\b', 'son', text)
            text = re.sub(r'\bDaughter\b', 'Son', text)
            text = re.sub(r'\bwife\b', 'husband', text)
            text = re.sub(r'\bWife\b', 'Husband', text)
            text = re.sub(r'\bheroine\b', 'hero', text)
            text = re.sub(r'\bHeroine\b', 'Hero', text)
        else:
            # he/him → she/her  (order: compounds first)
            text = re.sub(r'\bhimself\b', 'herself', text)
            text = re.sub(r'\bHimself\b', 'Herself', text)
            text = re.sub(r"\bhe's\b", "she's", text)
            text = re.sub(r"\bHe's\b", "She's", text)
            # "his" before a lowercase word → possessive "her"
            text = re.sub(r'\bhis(\s+[a-z])', r'her\1', text)
            text = re.sub(r'\bHis(\s+[a-z])', r'Her\1', text)
            # "his" standalone → "hers"
            text = re.sub(r'\bhis\b', 'hers', text)
            text = re.sub(r'\bHis\b', 'Hers', text)
            # "him" → "her"
            text = re.sub(r'\bhim\b', 'her', text)
            text = re.sub(r'\bHim\b', 'Her', text)
            # Simple subject
            text = re.sub(r'\bhe\b', 'she', text)
            text = re.sub(r'\bHe\b', 'She', text)
            # Gendered nouns
            text = re.sub(r'\bguys\b', 'girls', text)
            text = re.sub(r'\bGuys\b', 'Girls', text)
            text = re.sub(r'\bguy\b', 'girl', text)
            text = re.sub(r'\bGuy\b', 'Girl', text)
            text = re.sub(r'\bmen\b', 'women', text)
            text = re.sub(r'\bMen\b', 'Women', text)
            text = re.sub(r'\bman\b', 'woman', text)
            text = re.sub(r'\bMan\b', 'Woman', text)
            text = re.sub(r'\bgentleman\b', 'lady', text)
            text = re.sub(r'\bGentleman\b', 'Lady', text)
            text = re.sub(r'\bfather\b', 'mother', text)
            text = re.sub(r'\bFather\b', 'Mother', text)
            text = re.sub(r'\bbrother\b', 'sister', text)
            text = re.sub(r'\bBrother\b', 'Sister', text)
            text = re.sub(r'\bson\b', 'daughter', text)
            text = re.sub(r'\bSon\b', 'Daughter', text)
            text = re.sub(r'\bhusband\b', 'wife', text)
            text = re.sub(r'\bHusband\b', 'Wife', text)
            text = re.sub(r'\bhero\b', 'heroine', text)
            text = re.sub(r'\bHero\b', 'Heroine', text)
        return text

    def _swap_pronouns(self, direction: str):
        """Swap gendered pronouns in selected rows' translations."""
        rows = sorted(set(idx.row() for idx in self.table.selectionModel().selectedRows()))
        changed = 0
        for row in rows:
            if row >= len(self._visible_entries):
                continue
            entry = self._visible_entries[row]
            if not entry.translation:
                continue
            new_text = self._apply_pronoun_swap(entry.translation, direction)
            if new_text != entry.translation:
                entry.translation = new_text
                self._model.refresh_row(row)
                changed += 1
        if changed:
            self._update_stats()
            self.status_changed.emit()
        label = "she/her \u2192 he/him" if direction == "f2m" else "he/him \u2192 she/her"
        QMessageBox.information(
            self, "Pronoun Swap",
            f"Swapped pronouns ({label}) in {changed} of {len(rows)} selected entries."
        )

    # ── Speaker tagging ──────────────────────────────────────────

    def _set_speaker(self):
        """Assign a speaker name to selected entries' context."""
        rows = sorted(set(idx.row() for idx in self.table.selectionModel().selectedRows()))
        if not rows:
            return

        # Collect known speakers from existing contexts
        speakers = set()
        for e in self._all_entries:
            if e.context:
                m = re.search(r'\[Speaker:\s*(.+?)\]', e.context)
                if m:
                    speakers.add(m.group(1).strip())
        # Also collect actor names from DB entries (translated or original)
        for e in self._all_entries:
            if e.field == "name" and e.file == "Actors.json":
                name = (e.translation or e.original).strip()
                if name:
                    speakers.add(name)

        items = sorted(speakers)
        name, ok = QInputDialog.getItem(
            self, "Set Speaker",
            f"Speaker name for {len(rows)} selected entries:",
            items, 0, True,  # editable=True
        )
        if not ok or not name.strip():
            return

        name = name.strip()
        changed = 0
        for row in rows:
            if row >= len(self._visible_entries):
                continue
            entry = self._visible_entries[row]
            if entry.context and re.search(r'\[Speaker:', entry.context):
                entry.context = re.sub(
                    r'\[Speaker:\s*.+?\]', f'[Speaker: {name}]', entry.context
                )
            else:
                prefix = f"[Speaker: {name}]"
                entry.context = f"{prefix}\n{entry.context}" if entry.context else prefix
            changed += 1

        if changed:
            self._populate_speaker_filter(self._all_entries)
            self.status_changed.emit()
        QMessageBox.information(
            self, "Set Speaker",
            f'Set speaker to "{name}" for {changed} entries.'
        )

    def _clear_speaker(self):
        """Remove speaker tag from selected entries' context."""
        rows = sorted(set(idx.row() for idx in self.table.selectionModel().selectedRows()))
        if not rows:
            return

        changed = 0
        for row in rows:
            if row >= len(self._visible_entries):
                continue
            entry = self._visible_entries[row]
            if entry.context and '[Speaker:' in entry.context:
                entry.context = re.sub(r'\[Speaker:\s*.+?\]\n?', '', entry.context)
                changed += 1

        if changed:
            self._populate_speaker_filter(self._all_entries)
            self.status_changed.emit()
        QMessageBox.information(
            self, "Clear Speaker",
            f"Cleared speaker from {changed} of {len(rows)} selected entries."
        )

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
            self.context_tree.clear()
            return

        self._selected_row = row
        entry = self._visible_entries[row]

        # Block signals while loading to avoid feedback loop
        self.trans_editor.blockSignals(True)
        self.orig_editor.setPlainText(entry.original)
        self.trans_editor.setPlainText(entry.translation)
        self.trans_editor.blockSignals(False)

        self._update_context_pane(entry)

    @staticmethod
    def _event_prefix(entry_id: str) -> str:
        """Extract event prefix from entry ID (everything before the last segment).

        "CommonEvents.json/CE169(Name)/dialog_5" → "CommonEvents.json/CE169(Name)"
        "Map001.json/Ev3(EV003)/p0/dialog_5" → "Map001.json/Ev3(EV003)/p0"
        """
        idx = entry_id.rfind("/")
        return entry_id[:idx] if idx > 0 else ""

    def _update_context_pane(self, selected_entry):
        """Show surrounding entries from the same event in the context tree."""
        self.context_tree.clear()

        prefix = self._event_prefix(selected_entry.id)
        if not prefix:
            return

        # Find all entries from the same event (search ALL entries, not just visible)
        event_entries = [e for e in self._all_entries
                         if e.id.startswith(prefix + "/")]
        if not event_entries:
            return

        # Find selected entry's position in the event
        sel_idx = -1
        for i, e in enumerate(event_entries):
            if e.id == selected_entry.id:
                sel_idx = i
                break

        # Show ~10 before + selected + ~10 after (or full event if small)
        radius = 10
        if len(event_entries) <= radius * 2 + 1:
            start, end = 0, len(event_entries)
        else:
            start = max(0, sel_idx - radius)
            end = min(len(event_entries), sel_idx + radius + 1)

        # Update group box title with event name
        ctx_display = _extract_event_context(selected_entry.id)
        parent_group = self.context_tree.parent()
        if isinstance(parent_group, QGroupBox):
            count_info = f"{len(event_entries)} entries"
            parent_group.setTitle(
                f"Event Context — {ctx_display} ({count_info})"
                if ctx_display else "Event Context")

        highlight_item = None
        for i in range(start, end):
            e = event_entries[i]
            # Extract speaker from context
            speaker = ""
            if e.context:
                for line in e.context.split("\n"):
                    if line.startswith("[Speaker:"):
                        speaker = line.strip("[]").replace("Speaker: ", "")
                        break

            orig = e.original.replace("\n", " ")
            trans = e.translation.replace("\n", " ") if e.translation else ""

            item = QTreeWidgetItem([speaker, orig, trans])
            item.setData(0, Qt.ItemDataRole.UserRole, e.id)
            # Make EN column editable
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)

            # Highlight the selected entry
            if i == sel_idx:
                font = item.font(0)
                font.setBold(True)
                for col in range(3):
                    item.setFont(col, font)
                    item.setBackground(col, QColor("#45475a"))
                    item.setForeground(col, QColor("#cdd6f4"))
                highlight_item = item
            elif e.status == "untranslated":
                red = QColor("#f38ba8")
                for col in range(3):
                    item.setForeground(col, red)

            self.context_tree.addTopLevelItem(item)

        # Scroll to keep selected entry visible
        if highlight_item:
            self.context_tree.scrollToItem(highlight_item)

    def _on_context_item_clicked(self, item: QTreeWidgetItem, column: int):
        """Navigate the main table to the clicked context entry."""
        entry_id = item.data(0, Qt.ItemDataRole.UserRole)
        if entry_id:
            self._select_entry_by_id(entry_id)

    def _on_context_item_edited(self, item: QTreeWidgetItem, column: int):
        """Sync edits from the context pane EN column back to the entry."""
        if column != 2:  # Only EN column
            return
        entry_id = item.data(0, Qt.ItemDataRole.UserRole)
        if not entry_id:
            return
        new_text = item.text(2)
        # Find the entry and update it
        for entry in self._all_entries:
            if entry.id == entry_id:
                entry.translation = new_text
                entry.status = "translated" if new_text.strip() else "untranslated"
                # If this entry is currently shown in the main editor, sync it
                if (self._selected_row >= 0
                        and self._selected_row < len(self._visible_entries)
                        and self._visible_entries[self._selected_row].id == entry_id):
                    self.trans_editor.blockSignals(True)
                    self.trans_editor.setPlainText(new_text)
                    self.trans_editor.blockSignals(False)
                # Refresh main table row if visible
                for row, ve in enumerate(self._visible_entries):
                    if ve.id == entry_id:
                        self._model.refresh_row(row)
                        break
                break

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
        # Note: refresh_row triggers dataChanged → _on_model_data_changed → status_changed
        # so we don't need a separate emit here
        self._model.refresh_row(row)

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

    # ── Find & Replace ───────────────────────────────────────────

    def show_replace_bar(self):
        """Show the Find & Replace bar and focus the find field."""
        self._replace_bar.show()
        self._find_edit.setFocus()
        self._find_edit.selectAll()

    def _hide_replace_bar(self):
        """Hide the Find & Replace bar."""
        self._replace_bar.hide()
        self._replace_status.setText("")
        self._replace_index = 0

    def _replace_all(self):
        """Replace all occurrences in all translation entries."""
        find = self._find_edit.text()
        replace = self._replace_edit.text()
        if not find:
            return

        entries_changed = 0
        total_occurrences = 0
        for entry in self._all_entries:
            if not entry.translation or find not in entry.translation:
                continue
            count = entry.translation.count(find)
            entry.translation = entry.translation.replace(find, replace)
            total_occurrences += count
            entries_changed += 1

        if entries_changed:
            self._apply_filter()
            self.status_changed.emit()
            # Update editor panel if currently selected row was affected
            if 0 <= self._selected_row < len(self._visible_entries):
                entry = self._visible_entries[self._selected_row]
                self.trans_editor.blockSignals(True)
                self.trans_editor.setPlainText(entry.translation)
                self.trans_editor.blockSignals(False)

        self._replace_status.setText(
            f"Replaced {total_occurrences} in {entries_changed} entries"
            if entries_changed else "No matches found"
        )

    def _replace_next(self):
        """Find and select the next entry containing the search text."""
        find = self._find_edit.text()
        if not find:
            return

        # Search from next position forward through all entries
        n = len(self._all_entries)
        for offset in range(1, n + 1):
            idx = (self._replace_index + offset) % n
            entry = self._all_entries[idx]
            if entry.translation and find in entry.translation:
                self._replace_index = idx
                self._select_entry_by_id(entry.id)
                self._replace_status.setText(
                    f"Match {idx + 1} of {n} entries"
                )
                return

        self._replace_status.setText("No matches found")

    def _replace_current_and_next(self):
        """Replace in the current match and advance to next."""
        find = self._find_edit.text()
        replace = self._replace_edit.text()
        if not find:
            return

        # Replace in current entry if it matches
        if self._replace_index < len(self._all_entries):
            entry = self._all_entries[self._replace_index]
            if entry.translation and find in entry.translation:
                entry.translation = entry.translation.replace(find, replace, 1)
                # Update table display
                for row, ve in enumerate(self._visible_entries):
                    if ve.id == entry.id:
                        self._model.refresh_row(row)
                        if row == self._selected_row:
                            self.trans_editor.blockSignals(True)
                            self.trans_editor.setPlainText(entry.translation)
                            self.trans_editor.blockSignals(False)
                        break
                self.status_changed.emit()

        # Advance to next match (_replace_next starts at offset=1 from current)
        self._replace_next()

    def _select_entry_by_id(self, entry_id: str):
        """Select and scroll to an entry by ID in the visible table."""
        # First try to find in current visible entries
        for row, entry in enumerate(self._visible_entries):
            if entry.id == entry_id:
                index = self._model.index(row, 0)
                self.table.setCurrentIndex(index)
                self.table.scrollTo(index)
                return

        # Not visible — temporarily clear file filter, find, then restore
        prev_entries = self._entries
        self._entries = self._all_entries
        self._apply_filter()

        for row, entry in enumerate(self._visible_entries):
            if entry.id == entry_id:
                index = self._model.index(row, 0)
                self.table.setCurrentIndex(index)
                self.table.scrollTo(index)
                return

        # Entry not found — restore previous filter
        self._entries = prev_entries
        self._apply_filter()

    def _update_stats(self):
        """Update the stats label."""
        total = len(self._visible_entries)
        translated = sum(1 for e in self._visible_entries if e.status in ("translated", "reviewed"))
        reviewed = sum(1 for e in self._visible_entries if e.status == "reviewed")
        self.stats_label.setText(
            f"Showing {total} entries  |  "
            f"Translated: {translated}  |  Reviewed: {reviewed}"
        )
