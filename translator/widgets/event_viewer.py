"""Event Viewer panel — tree-based event browser for reviewing full dialogue flows."""

import re
from collections import OrderedDict

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QTreeWidget,
    QTreeWidgetItem, QTableWidget, QTableWidgetItem, QHeaderView,
    QLabel, QPushButton, QAbstractItemView, QTextEdit, QGroupBox,
)
from PyQt6.QtCore import Qt, pyqtSignal as Signal, QEvent
from PyQt6.QtGui import QColor, QFont

from ..utils import event_prefix, extract_event_context

# Files that contain events (not database flat entries)
_EVENT_FILES = {"CommonEvents.json", "Troops.json"}
_DB_FILES = {
    "Actors.json", "Classes.json", "Items.json", "Weapons.json",
    "Armors.json", "Skills.json", "States.json", "Enemies.json",
    "System.json", "plugins.js",
}

# Fields that are not part of event dialogue flow
_SKIP_FIELDS = {"speaker_name", "displayName"}

# Status icons
_STATUS_ICONS = {
    "untranslated": "\u2610",  # ballot box
    "translated":   "\u2714",  # check
    "reviewed":     "\u2705",  # green check
    "skipped":      "\u23ed",  # skip
}

_MAP_RE = re.compile(r'^Map\d+\.json$', re.IGNORECASE)


class EventViewerPanel(QWidget):
    """Tree-based event browser for reviewing full dialogue flows."""

    status_changed = Signal()     # emitted when entries are modified
    entry_updated = Signal(str)   # emitted with entry_id after inline edit

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all_entries = []
        self._event_groups: dict[str, list] = OrderedDict()
        self._current_prefix = ""
        self._current_entries = []
        self._speaker_lookup = {}
        self._dark_mode = True
        self._tree_items: dict[str, QTreeWidgetItem] = {}  # prefix -> tree item
        self._build_ui()

    # ── UI Construction ──────────────────────────────────────────────

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: event tree
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Event", "Progress"])
        self._tree.setColumnWidth(0, 220)
        self._tree.setColumnWidth(1, 80)
        self._tree.setIndentation(16)
        self._tree.currentItemChanged.connect(self._on_tree_selection_changed)
        self._tree.setStyleSheet("""
            QTreeWidget {
                background-color: #1e1e2e;
                color: #cdd6f4;
                border: 1px solid #313244;
                font-size: 12px;
            }
            QTreeWidget::item {
                padding: 2px 4px;
            }
            QTreeWidget::item:selected {
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
        splitter.addWidget(self._tree)

        # Right: detail panel
        detail_widget = QWidget()
        detail_layout = QVBoxLayout(detail_widget)
        detail_layout.setContentsMargins(4, 4, 4, 4)

        # Header row: event name + mark reviewed button
        header = QHBoxLayout()
        self._event_label = QLabel("Select an event from the tree")
        self._event_label.setStyleSheet(
            "font-weight: bold; font-size: 13px; color: #cdd6f4;")
        header.addWidget(self._event_label, 1)

        self._review_btn = QPushButton("Mark Event Reviewed")
        self._review_btn.setEnabled(False)
        self._review_btn.clicked.connect(self._mark_event_reviewed)
        self._review_btn.setStyleSheet("""
            QPushButton {
                background-color: #313244;
                color: #a6e3a1;
                border: 1px solid #45475a;
                padding: 4px 12px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #45475a;
            }
            QPushButton:disabled {
                color: #6c7086;
            }
        """)
        header.addWidget(self._review_btn)
        detail_layout.addLayout(header)

        # Detail table
        self._detail_table = QTableWidget()
        self._detail_table.setColumnCount(4)
        self._detail_table.setHorizontalHeaderLabels(
            ["", "Speaker", "Original (JP)", "Translation (EN)"])

        # Column sizing
        h = self._detail_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self._detail_table.setColumnWidth(0, 30)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self._detail_table.setColumnWidth(1, 120)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

        # Behavior — read-only table, editing via panel below
        self._detail_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self._detail_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._detail_table.setWordWrap(True)
        self._detail_table.verticalHeader().setVisible(False)
        self._detail_table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        self._detail_table.setAlternatingRowColors(False)
        self._detail_table.currentCellChanged.connect(self._on_row_selected)
        self._detail_table.setStyleSheet("""
            QTableWidget {
                background-color: #1e1e2e;
                color: #cdd6f4;
                gridline-color: #313244;
                border: 1px solid #313244;
                font-size: 12px;
            }
            QTableWidget::item {
                padding: 3px 4px;
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

        # Vertical splitter: table (top 70%) + editor (bottom 30%)
        v_splitter = QSplitter(Qt.Orientation.Vertical)
        v_splitter.addWidget(self._detail_table)

        # Editor panel
        editor_widget = QWidget()
        editor_layout = QHBoxLayout(editor_widget)
        editor_layout.setContentsMargins(0, 2, 0, 0)

        orig_box = QGroupBox("Original (JP)")
        orig_box.setStyleSheet("""
            QGroupBox {
                color: #a6adc8; font-weight: bold; font-size: 11px;
                border: 1px solid #313244; border-radius: 4px;
                margin-top: 6px; padding-top: 14px;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; }
        """)
        orig_inner = QVBoxLayout(orig_box)
        self._orig_editor = QTextEdit()
        self._orig_editor.setReadOnly(True)
        self._orig_editor.setAcceptRichText(False)
        self._orig_editor.setStyleSheet("""
            QTextEdit {
                background-color: #181825; color: #bac2de;
                border: 1px solid #313244; font-size: 13px;
            }
        """)
        orig_inner.addWidget(self._orig_editor)
        editor_layout.addWidget(orig_box)

        trans_box = QGroupBox("Translation (EN)")
        trans_box.setStyleSheet("""
            QGroupBox {
                color: #a6adc8; font-weight: bold; font-size: 11px;
                border: 1px solid #313244; border-radius: 4px;
                margin-top: 6px; padding-top: 14px;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; }
        """)
        trans_inner = QVBoxLayout(trans_box)
        self._trans_editor = QTextEdit()
        self._trans_editor.setAcceptRichText(False)
        self._trans_editor.setStyleSheet("""
            QTextEdit {
                background-color: #1e1e2e; color: #cdd6f4;
                border: 1px solid #45475a; font-size: 13px;
            }
        """)
        self._trans_editor.textChanged.connect(self._on_editor_changed)
        trans_inner.addWidget(self._trans_editor)
        editor_layout.addWidget(trans_box)

        v_splitter.addWidget(editor_widget)
        v_splitter.setSizes([500, 200])

        detail_layout.addWidget(v_splitter)

        splitter.addWidget(detail_widget)
        splitter.setSizes([300, 900])
        layout.addWidget(splitter)

        self._selected_row = -1

        # Spacebar = mark reviewed when table has focus
        self._detail_table.installEventFilter(self)

    def eventFilter(self, obj, event):
        """Intercept spacebar on detail table to toggle reviewed status."""
        if (obj is self._detail_table
                and event.type() == QEvent.Type.KeyPress
                and event.key() == Qt.Key.Key_Space):
            self._toggle_row_reviewed()
            return True
        return super().eventFilter(obj, event)

    def _toggle_row_reviewed(self):
        """Toggle reviewed status on current row (spacebar shortcut)."""
        row = self._detail_table.currentRow()
        if row < 0 or row >= len(self._current_entries):
            return
        entry = self._current_entries[row]
        if entry.status == "reviewed":
            # Un-review: go back to translated (or untranslated if empty)
            entry.status = "translated" if entry.translation else "untranslated"
        else:
            entry.status = "reviewed"
        # Update table
        self._detail_table.blockSignals(True)
        status_item = self._detail_table.item(row, 0)
        if status_item:
            status_item.setText(_STATUS_ICONS.get(entry.status, ""))
        self._apply_row_colors(row, entry)
        self._detail_table.blockSignals(False)
        self._refresh_tree_stats()
        self.status_changed.emit()

    # ── Public API ───────────────────────────────────────────────────

    def set_entries(self, entries: list):
        """Load entries and rebuild the event tree."""
        self._all_entries = entries
        self._current_prefix = ""
        self._current_entries = []
        self._build_speaker_lookup()
        self._build_event_groups()
        self._populate_tree()
        self._detail_table.setRowCount(0)
        self._event_label.setText("Select an event from the tree")
        self._review_btn.setEnabled(False)

    def update_entry(self, entry_id: str, translation: str):
        """Update a single entry's display after batch translation."""
        prefix = event_prefix(entry_id)
        if prefix != self._current_prefix:
            return  # Not currently displayed
        for row, entry in enumerate(self._current_entries):
            if entry.id == entry_id:
                self._detail_table.blockSignals(True)
                # Status icon
                status_item = self._detail_table.item(row, 0)
                if status_item:
                    status_item.setText(_STATUS_ICONS.get(entry.status, ""))
                # Translation text
                trans_item = self._detail_table.item(row, 3)
                if trans_item:
                    trans_item.setText(translation)
                self._apply_row_colors(row, entry)
                self._detail_table.blockSignals(False)
                # Update editor panel if this row is selected
                if row == self._selected_row:
                    self._trans_editor.blockSignals(True)
                    self._trans_editor.setPlainText(translation)
                    self._trans_editor.blockSignals(False)
                break

    def refresh_current_event(self):
        """Re-render the current event detail (called after external edits)."""
        if self._current_prefix:
            # Preserve scroll and selection
            saved_row = self._selected_row
            scroll_val = self._detail_table.verticalScrollBar().value()
            self._show_event(self._current_prefix)
            if 0 <= saved_row < len(self._current_entries):
                self._detail_table.setCurrentCell(saved_row, 0)
                # Qt skips currentCellChanged if same row — update editors manually
                self._on_row_selected(saved_row, 0, -1, -1)
            self._detail_table.verticalScrollBar().setValue(scroll_val)
        self._refresh_tree_stats()

    def refresh_stats(self):
        """Refresh tree progress badges (called at batch checkpoints)."""
        self._refresh_tree_stats()

    def set_dark_mode(self, enabled: bool):
        """Toggle dark/light mode."""
        self._dark_mode = enabled
        # Re-render current event if any
        if self._current_prefix:
            self._show_event(self._current_prefix)

    # ── Internal: build data structures ──────────────────────────────

    def _build_speaker_lookup(self):
        """Build JP->EN speaker name lookup from speaker_name and actor entries."""
        self._speaker_lookup = {}
        for e in self._all_entries:
            if e.field == "speaker_name" and e.translation:
                self._speaker_lookup[e.original.strip()] = e.translation.strip()
            elif e.field == "name" and e.file == "Actors.json" and e.translation:
                self._speaker_lookup[e.original.strip()] = e.translation.strip()

    def _is_event_entry(self, entry) -> bool:
        """Return True if this entry belongs to an in-game event."""
        if entry.file in _DB_FILES:
            return False
        if entry.field in _SKIP_FIELDS:
            return False
        # Must have at least 3 ID segments: file/event/field
        if len(entry.id.split("/")) < 3:
            return False
        return True

    def _build_event_groups(self):
        """Group entries by event prefix."""
        self._event_groups = OrderedDict()
        for e in self._all_entries:
            if not self._is_event_entry(e):
                continue
            prefix = event_prefix(e.id)
            if not prefix:
                continue
            if prefix not in self._event_groups:
                self._event_groups[prefix] = []
            self._event_groups[prefix].append(e)

    # ── Internal: tree population ────────────────────────────────────

    def _populate_tree(self):
        """Build the event tree from grouped entries."""
        self._tree.clear()
        self._tree_items = {}

        # Categorize prefixes
        ce_prefixes = []
        troop_prefixes = []
        map_prefixes: dict[str, list] = {}  # filename -> [prefixes]

        for prefix in self._event_groups:
            filename = prefix.split("/")[0]
            if filename == "CommonEvents.json":
                ce_prefixes.append(prefix)
            elif filename == "Troops.json":
                troop_prefixes.append(prefix)
            elif _MAP_RE.match(filename):
                map_prefixes.setdefault(filename, []).append(prefix)

        # Common Events category
        if ce_prefixes:
            ce_root = QTreeWidgetItem(self._tree, ["Common Events", ""])
            ce_root.setFlags(
                Qt.ItemFlag.ItemIsEnabled)
            for prefix in ce_prefixes:
                entries = self._event_groups[prefix]
                display = extract_event_context(entries[0].id)
                reviewed = sum(1 for e in entries if e.status == "reviewed")
                total = len(entries)
                progress = f"{reviewed}/{total}"

                item = QTreeWidgetItem(ce_root, [display, progress])
                item.setData(0, Qt.ItemDataRole.UserRole, prefix)
                self._apply_tree_item_color(item, reviewed, total)
                self._tree_items[prefix] = item

        # Maps category
        if map_prefixes:
            maps_root = QTreeWidgetItem(self._tree, ["Maps", ""])
            maps_root.setFlags(
                Qt.ItemFlag.ItemIsEnabled)
            for filename in sorted(map_prefixes.keys()):
                prefixes = map_prefixes[filename]
                map_name = filename.replace(".json", "")

                if len(prefixes) == 1:
                    # Single event in map — show directly under Maps
                    prefix = prefixes[0]
                    entries = self._event_groups[prefix]
                    display = extract_event_context(entries[0].id)
                    reviewed = sum(1 for e in entries if e.status == "reviewed")
                    total = len(entries)

                    item = QTreeWidgetItem(
                        maps_root,
                        [f"{map_name}/{display}", f"{reviewed}/{total}"])
                    item.setData(0, Qt.ItemDataRole.UserRole, prefix)
                    self._apply_tree_item_color(item, reviewed, total)
                    self._tree_items[prefix] = item
                else:
                    # Multiple events — group under map file
                    map_node = QTreeWidgetItem(maps_root, [map_name, ""])
                    map_node.setFlags(Qt.ItemFlag.ItemIsEnabled)
                    for prefix in prefixes:
                        entries = self._event_groups[prefix]
                        display = extract_event_context(entries[0].id)
                        reviewed = sum(
                            1 for e in entries if e.status == "reviewed")
                        total = len(entries)

                        item = QTreeWidgetItem(
                            map_node, [display, f"{reviewed}/{total}"])
                        item.setData(0, Qt.ItemDataRole.UserRole, prefix)
                        self._apply_tree_item_color(item, reviewed, total)
                        self._tree_items[prefix] = item

        # Troops category
        if troop_prefixes:
            troops_root = QTreeWidgetItem(self._tree, ["Troops", ""])
            troops_root.setFlags(
                Qt.ItemFlag.ItemIsEnabled)
            for prefix in troop_prefixes:
                entries = self._event_groups[prefix]
                display = extract_event_context(entries[0].id)
                reviewed = sum(1 for e in entries if e.status == "reviewed")
                total = len(entries)

                item = QTreeWidgetItem(troops_root, [display, f"{reviewed}/{total}"])
                item.setData(0, Qt.ItemDataRole.UserRole, prefix)
                self._apply_tree_item_color(item, reviewed, total)
                self._tree_items[prefix] = item

    def _apply_tree_item_color(self, item: QTreeWidgetItem,
                                reviewed: int, total: int):
        """Color tree item based on review progress."""
        if total == 0:
            return
        if reviewed == total:
            item.setForeground(0, QColor("#a6e3a1"))  # green
            item.setForeground(1, QColor("#a6e3a1"))
        elif reviewed > 0:
            item.setForeground(0, QColor("#f9e2af"))  # yellow
            item.setForeground(1, QColor("#f9e2af"))
        else:
            item.setForeground(0, QColor("#cdd6f4"))  # default
            item.setForeground(1, QColor("#9399b2"))

    def _refresh_tree_stats(self):
        """Update progress badges on all tree items."""
        for prefix, item in self._tree_items.items():
            entries = self._event_groups.get(prefix, [])
            reviewed = sum(1 for e in entries if e.status == "reviewed")
            total = len(entries)
            item.setText(1, f"{reviewed}/{total}")
            self._apply_tree_item_color(item, reviewed, total)

    # ── Internal: event detail display ───────────────────────────────

    def _on_tree_selection_changed(self, current: QTreeWidgetItem,
                                    previous: QTreeWidgetItem):
        """Handle tree selection change (click or arrow keys)."""
        if not current:
            return
        prefix = current.data(0, Qt.ItemDataRole.UserRole)
        if prefix:
            self._show_event(prefix)

    def _show_event(self, prefix: str):
        """Populate the detail table with all entries from an event."""
        entries = self._event_groups.get(prefix, [])
        if not entries:
            return

        self._current_prefix = prefix
        self._current_entries = entries

        # Update header
        display = extract_event_context(entries[0].id)
        filename = prefix.split("/")[0].replace(".json", "")
        reviewed = sum(1 for e in entries if e.status == "reviewed")
        total = len(entries)
        self._event_label.setText(
            f"{filename} \u2014 {display}  ({reviewed}/{total} reviewed)")
        self._review_btn.setEnabled(True)

        # Populate table
        self._detail_table.blockSignals(True)
        self._detail_table.setRowCount(len(entries))

        for row, e in enumerate(entries):
            # Status icon
            icon = _STATUS_ICONS.get(e.status, "")
            status_item = QTableWidgetItem(icon)
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            status_item.setFlags(
                Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
            status_item.setData(Qt.ItemDataRole.UserRole, e.id)
            self._detail_table.setItem(row, 0, status_item)

            # Speaker
            speaker_jp = ""
            if e.context:
                for line in e.context.split("\n"):
                    if line.startswith("[Speaker:"):
                        speaker_jp = line.strip("[]").replace("Speaker: ", "")
                        break
            speaker_en = self._speaker_lookup.get(
                speaker_jp, "") if speaker_jp else ""
            speaker_display = speaker_en or speaker_jp

            spk_item = QTableWidgetItem(speaker_display)
            spk_item.setFlags(
                Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
            if speaker_jp and speaker_en and speaker_en != speaker_jp:
                spk_item.setToolTip(f"JP: {speaker_jp}")
            self._detail_table.setItem(row, 1, spk_item)

            # Original (read-only)
            orig_item = QTableWidgetItem(e.original)
            orig_item.setFlags(
                Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
            self._detail_table.setItem(row, 2, orig_item)

            # Translation (read-only in table, edit via panel below)
            trans_item = QTableWidgetItem(e.translation or "")
            trans_item.setFlags(
                Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
            self._detail_table.setItem(row, 3, trans_item)

            self._apply_row_colors(row, e)

        self._detail_table.blockSignals(False)

        # Auto-select first row so editors populate
        if entries:
            self._detail_table.setCurrentCell(0, 0)

    def _apply_row_colors(self, row: int, entry):
        """Apply Catppuccin colors to a detail table row."""
        if self._dark_mode:
            base_bg = QColor("#1e1e2e")
            alt_bg = QColor("#181825")
            text_fg = QColor("#bac2de")
            untrans_fg = QColor("#f38ba8")
            reviewed_fg = QColor("#a6e3a1")
            speaker_fg = QColor("#89b4fa")
        else:
            base_bg = QColor("#ffffff")
            alt_bg = QColor("#f5f5f5")
            text_fg = QColor("#333333")
            untrans_fg = QColor("#cc3333")
            reviewed_fg = QColor("#228833")
            speaker_fg = QColor("#0066cc")

        bg = base_bg if row % 2 == 0 else alt_bg

        if entry.status == "untranslated":
            fg = untrans_fg
        elif entry.status == "reviewed":
            fg = reviewed_fg
        else:
            fg = text_fg

        for col in range(4):
            item = self._detail_table.item(row, col)
            if item:
                item.setBackground(bg)
                item.setForeground(fg)

        # Speaker accent color
        spk_item = self._detail_table.item(row, 1)
        if spk_item and spk_item.text():
            spk_item.setForeground(speaker_fg)

        # Status icon color
        status_item = self._detail_table.item(row, 0)
        if status_item:
            if entry.status == "reviewed":
                status_item.setForeground(reviewed_fg)
            elif entry.status == "translated":
                status_item.setForeground(QColor("#a6e3a1"))
            elif entry.status == "untranslated":
                status_item.setForeground(untrans_fg)

    # ── Internal: editing & review ───────────────────────────────────

    def _on_row_selected(self, row: int, col: int, prev_row: int, prev_col: int):
        """Update editor panel when a row is selected."""
        if row < 0 or row >= len(self._current_entries):
            self._trans_editor.blockSignals(True)
            self._orig_editor.clear()
            self._trans_editor.clear()
            self._trans_editor.blockSignals(False)
            self._selected_row = -1
            return

        self._selected_row = row
        entry = self._current_entries[row]
        self._orig_editor.setPlainText(entry.original)
        self._trans_editor.blockSignals(True)
        self._trans_editor.setPlainText(entry.translation or "")
        self._trans_editor.blockSignals(False)

    def _on_editor_changed(self):
        """Sync translation edits from editor panel back to entry + table."""
        row = self._selected_row
        if row < 0 or row >= len(self._current_entries):
            return

        entry = self._current_entries[row]
        new_text = self._trans_editor.toPlainText()
        entry.translation = new_text
        entry.status = "translated" if new_text.strip() else "untranslated"

        # Update table row
        self._detail_table.blockSignals(True)
        status_item = self._detail_table.item(row, 0)
        if status_item:
            status_item.setText(_STATUS_ICONS.get(entry.status, ""))
        trans_item = self._detail_table.item(row, 3)
        if trans_item:
            trans_item.setText(new_text)
        self._apply_row_colors(row, entry)
        self._detail_table.blockSignals(False)

        self.status_changed.emit()

    def _mark_event_reviewed(self):
        """Mark all entries in the current event as reviewed."""
        if not self._current_entries:
            return

        self._detail_table.blockSignals(True)
        for row, entry in enumerate(self._current_entries):
            if entry.status in ("translated", "untranslated"):
                entry.status = "reviewed"
            # Update icon
            status_item = self._detail_table.item(row, 0)
            if status_item:
                status_item.setText(_STATUS_ICONS.get(entry.status, ""))
            self._apply_row_colors(row, entry)
        self._detail_table.blockSignals(False)

        # Update header
        total = len(self._current_entries)
        reviewed = sum(1 for e in self._current_entries
                       if e.status == "reviewed")
        display = extract_event_context(self._current_entries[0].id)
        filename = self._current_prefix.split("/")[0].replace(".json", "")
        self._event_label.setText(
            f"{filename} \u2014 {display}  ({reviewed}/{total} reviewed)")

        # Update tree badge
        self._refresh_tree_stats()

        self.status_changed.emit()
