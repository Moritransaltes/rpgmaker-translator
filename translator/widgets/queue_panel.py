"""Translation queue panel — event-grouped tree view of batch progress."""

import re
import time
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem,
    QHeaderView, QLabel, QAbstractItemView, QPushButton, QComboBox,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor

from ..translation_engine import _event_key


# Status icons and colors
_STATUS = {
    "queued":      ("⏳", QColor("#9399b2")),
    "translating": ("⚙", QColor("#89b4fa")),
    "done":        ("✔", QColor("#a6e3a1")),
    "error":       ("✘", QColor("#f38ba8")),
    "skipped":     ("⏭", QColor("#6c7086")),
    "tm":          ("♻", QColor("#cba6f7")),
    "glossary":    ("\U0001f4d6", QColor("#f9e2af")),
}

DB_KEY = "__database__"

# Group bucket for sort order: CommonEvents → Maps → Troops → other → DB last
_BUCKET_CE = 0
_BUCKET_MAP = 1
_BUCKET_TROOP = 2
_BUCKET_OTHER = 3
_BUCKET_DB = 99

_NUM_RE = re.compile(r'(\d+)')


def _event_sort_key(key: str) -> tuple:
    """Sort events: CommonEvents first (so map duplicates benefit from TM),
    then Maps in numeric order (Map001, Map002, ...), then Troops, then DB."""
    if key == DB_KEY:
        return (_BUCKET_DB,)
    if key.startswith("CommonEvents"):
        m = re.search(r'CE(\d+)', key)
        return (_BUCKET_CE, int(m.group(1)) if m else 0, key)
    if key.startswith("Map"):
        nums = _NUM_RE.findall(key)
        # nums[0] = map number, nums[1] = event number, nums[2] = page (if any)
        n = [int(x) for x in nums[:3]]
        while len(n) < 3:
            n.append(0)
        return (_BUCKET_MAP, n[0], n[1], n[2], key)
    if key.startswith("Troops"):
        m = re.search(r'/T?(\d+)', key)
        return (_BUCKET_TROOP, int(m.group(1)) if m else 0, key)
    return (_BUCKET_OTHER, key)


def _friendly_event_name(key: str) -> str:
    """Turn an internal node key into a human-readable label."""
    if key == DB_KEY:
        return "Database"
    if key.startswith(DB_KEY + "/"):
        # "__database__/Actors.json" → "Actors"
        return key[len(DB_KEY) + 1:].replace(".json", "")
    return key.replace(".json", "").replace("/", " · ")


class QueuePanel(QWidget):
    """Live translation queue, grouped by event with expandable rows."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries = []
        self._entry_items: dict[str, QTreeWidgetItem] = {}
        self._event_items: dict[str, QTreeWidgetItem] = {}
        self._event_counts: dict[str, dict] = {}
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

        # Expand / collapse all
        expand_btn = QPushButton("Expand All")
        expand_btn.clicked.connect(lambda: self._tree.expandAll())
        header.addWidget(expand_btn)

        collapse_btn = QPushButton("Collapse All")
        collapse_btn.clicked.connect(lambda: self._tree.collapseAll())
        header.addWidget(collapse_btn)

        # Filter dropdown
        self._filter_combo = QComboBox()
        self._filter_combo.addItems(["All", "Queued", "Done", "Error", "TM/Glossary"])
        self._filter_combo.currentTextChanged.connect(self._apply_filter)
        header.addWidget(QLabel("Show:"))
        header.addWidget(self._filter_combo)

        clear_btn = QPushButton("Clear Log")
        clear_btn.clicked.connect(self.clear)
        header.addWidget(clear_btn)

        layout.addLayout(header)

        # Tree
        self._tree = QTreeWidget()
        self._tree.setColumnCount(5)
        self._tree.setHeaderLabels([
            "Event / Entry", "Progress", "Original", "Translation", "Source"
        ])
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self._tree.setColumnWidth(0, 280)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self._tree.setColumnWidth(1, 90)
        self._tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self._tree.setColumnWidth(4, 90)

        self._tree.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self._tree.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tree.setAlternatingRowColors(True)
        self._tree.setRootIsDecorated(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setStyleSheet("""
            QTreeWidget {
                background-color: #1e1e2e;
                alternate-background-color: #24243a;
                color: #cdd6f4;
                border: 1px solid #313244;
                font-size: 12px;
            }
            QTreeWidget::item { padding: 2px 4px; }
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
        layout.addWidget(self._tree)

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
        """Populate the queue grouped by event (with DB grouped by JSON file)."""
        self.clear()
        self._entries = list(entries)
        self._start_time = time.time()
        self._done_count = 0

        # Group: events go under their event key. DB entries go under
        # "Database / <file>" so they can be expanded by JSON file.
        event_groups: dict[str, list] = {}
        order: list[str] = []
        db_groups: dict[str, list] = {}
        db_order: list[str] = []
        for e in entries:
            ek = _event_key(e)
            if ek is None:
                if e.file not in db_groups:
                    db_groups[e.file] = []
                    db_order.append(e.file)
                db_groups[e.file].append(e)
            else:
                if ek not in event_groups:
                    event_groups[ek] = []
                    order.append(ek)
                event_groups[ek].append(e)

        # Sort events: CEs first, then Maps numerically, then Troops, etc.
        order.sort(key=_event_sort_key)
        # DB files alphabetical
        db_order.sort()

        dim = QColor("#7f849c")
        queued_color = _STATUS["queued"][1]

        def make_entry_item(e):
            orig = (e.original or "")[:120].replace("\n", " ")
            preview = (e.field or "entry")
            child = QTreeWidgetItem([f"⏳ {preview}", "", orig, "", ""])
            child.setData(0, Qt.ItemDataRole.UserRole, "queued")
            child.setData(1, Qt.ItemDataRole.UserRole, e.id)
            child.setForeground(0, queued_color)
            child.setToolTip(0, e.id)
            self._entry_items[e.id] = child
            return child

        def make_event_node(key: str, group_entries: list, label: str):
            node = QTreeWidgetItem([
                f"⏳ {label}", f"0/{len(group_entries)}", "", "", "",
            ])
            node.setForeground(1, dim)
            node.setData(0, Qt.ItemDataRole.UserRole, key)
            self._event_items[key] = node
            self._event_counts[key] = {
                "total": len(group_entries), "done": 0, "error": 0,
            }
            for e in group_entries:
                node.addChild(make_entry_item(e))
            return node

        # Top-level event nodes
        for key in order:
            self._tree.addTopLevelItem(
                make_event_node(key, event_groups[key],
                                _friendly_event_name(key))
            )

        # Database parent with one child per JSON file
        if db_groups:
            db_total = sum(len(g) for g in db_groups.values())
            db_root = QTreeWidgetItem([
                f"⏳ Database", f"0/{db_total}", "", "", "",
            ])
            db_root.setForeground(1, dim)
            db_root.setData(0, Qt.ItemDataRole.UserRole, DB_KEY)
            self._event_items[DB_KEY] = db_root
            self._event_counts[DB_KEY] = {
                "total": db_total, "done": 0, "error": 0,
            }
            for fname in db_order:
                file_entries = db_groups[fname]
                file_label = fname.replace(".json", "")
                file_key = f"{DB_KEY}/{fname}"
                file_node = make_event_node(file_key, file_entries, file_label)
                db_root.addChild(file_node)
            self._tree.addTopLevelItem(db_root)

        self._update_summary()

    def mark_entry_done(self, entry_id: str, translation: str,
                        source: str = "LLM"):
        """Mark an entry as translated."""
        item = self._entry_items.get(entry_id)
        if item is None:
            return

        # Guard against double-counting
        if item.data(0, Qt.ItemDataRole.UserRole) == "done":
            return

        self._done_count += 1
        icon, color = _STATUS["done"]

        preview = item.text(0).split(" ", 1)[-1] if " " in item.text(0) else ""
        item.setText(0, f"{icon} {preview}")
        item.setData(0, Qt.ItemDataRole.UserRole, "done")
        item.setForeground(0, color)

        trans_text = (translation or "")[:120].replace("\n", " ")
        item.setText(3, trans_text)
        item.setForeground(3, color)

        item.setText(4, source)
        if source == "TM":
            item.setForeground(4, _STATUS["tm"][1])
        elif source == "Glossary":
            item.setForeground(4, _STATUS["glossary"][1])
        else:
            item.setForeground(4, color)

        self._update_parent(item)
        self._update_summary()
        self._apply_filter(self._filter_combo.currentText())

        # Auto-scroll only if the user isn't actively browsing — keep the
        # active item in view but don't yank focus around if they expanded
        # something specific.
        self._tree.scrollToItem(item, QAbstractItemView.ScrollHint.EnsureVisible)

    def mark_entry_error(self, entry_id: str, error_msg: str):
        item = self._entry_items.get(entry_id)
        if item is None:
            return
        icon, color = _STATUS["error"]
        preview = item.text(0).split(" ", 1)[-1] if " " in item.text(0) else ""
        item.setText(0, f"{icon} {preview}")
        item.setData(0, Qt.ItemDataRole.UserRole, "error")
        item.setForeground(0, color)
        item.setText(3, f"ERROR: {error_msg[:80]}")
        item.setForeground(3, color)
        item.setText(4, "Error")
        item.setForeground(4, color)
        self._update_parent(item)

    def mark_prefill(self, entry_id: str, translation: str,
                     source: str = "TM"):
        self.mark_entry_done(entry_id, translation, source=source)

    def mark_batch_finished(self):
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
        self._tree.clear()
        self._entries = []
        self._entry_items = {}
        self._event_items = {}
        self._event_counts = {}
        self._done_count = 0
        self._start_time = 0
        self._summary_label.setText("No batch running")
        self._eta_label.setText("")
        self._speed_label.setText("")

    # ── Internal ────────────────────────────────────────────────────

    def _update_parent(self, child_item: QTreeWidgetItem):
        """Roll up child status into all ancestor event/DB nodes."""
        parent = child_item.parent()
        while parent is not None:
            key = parent.data(0, Qt.ItemDataRole.UserRole)
            if key in self._event_counts:
                self._refresh_node(parent, key)
            parent = parent.parent()

    def _refresh_node(self, node: QTreeWidgetItem, key: str):
        """Recount children of *node* and update its label/color/progress.

        Children may be leaf entries (with status in UserRole) or sub-nodes
        (Database root case, where children are DB-file nodes).
        """
        done = error = total = 0
        for i in range(node.childCount()):
            ch = node.child(i)
            ch_key = ch.data(0, Qt.ItemDataRole.UserRole)
            if ch_key in self._event_counts:
                sub = self._event_counts[ch_key]
                done += sub["done"]
                error += sub["error"]
                total += sub["total"]
            else:
                status = ch_key  # entry status stored in UserRole on col 0
                if status == "done":
                    done += 1
                elif status == "error":
                    error += 1
                total += 1
        cnt = self._event_counts[key]
        cnt["done"] = done
        cnt["error"] = error
        cnt["total"] = total
        node.setText(1, f"{done}/{total}" + (f" · {error} err" if error else ""))

        label = _friendly_event_name(key)
        if done == total and error == 0:
            icon, color = _STATUS["done"]
        elif error > 0:
            icon, color = _STATUS["error"]
        elif done > 0:
            icon, color = _STATUS["translating"]
        else:
            icon, color = _STATUS["queued"]
        node.setForeground(0, color)
        node.setText(0, f"{icon} {label}")

    def _update_summary(self):
        total = len(self._entries)
        if total == 0:
            return
        elapsed = time.time() - self._start_time if self._start_time else 0
        remaining = total - self._done_count
        n_events = len(self._event_items)
        self._summary_label.setText(
            f"Translating: {self._done_count}/{total} "
            f"({remaining} remaining, {n_events} events)"
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
        """Hide entries that don't match; hide parents with no visible children.

        Walks the tree recursively so the Database root → DB-file → entry
        hierarchy is collapsed correctly when its leaves are filtered out.
        """
        def visit(node: QTreeWidgetItem) -> bool:
            """Apply filter to *node*'s subtree. Returns True if any
            descendant is visible after filtering."""
            if node.childCount() == 0:
                return not node.isHidden()
            first_key = node.child(0).data(0, Qt.ItemDataRole.UserRole)
            if first_key in self._event_counts:
                # Children are sub-nodes — recurse
                any_visible = False
                for i in range(node.childCount()):
                    if visit(node.child(i)):
                        any_visible = True
                node.setHidden(not any_visible and filter_text != "All")
                return any_visible or filter_text == "All"
            # Children are leaf entries
            any_visible = False
            for i in range(node.childCount()):
                ch = node.child(i)
                status = ch.data(0, Qt.ItemDataRole.UserRole)
                source = ch.text(4)
                show = True
                if filter_text == "Queued":
                    show = status == "queued"
                elif filter_text == "Done":
                    show = status == "done"
                elif filter_text == "Error":
                    show = status == "error"
                elif filter_text == "TM/Glossary":
                    show = source in ("TM", "Glossary")
                ch.setHidden(not show)
                if show:
                    any_visible = True
            node.setHidden(not any_visible and filter_text != "All")
            return any_visible or filter_text == "All"

        for i in range(self._tree.topLevelItemCount()):
            visit(self._tree.topLevelItem(i))

    @staticmethod
    def _format_time(seconds: float) -> str:
        if seconds < 60:
            return f"{seconds:.0f}s"
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        if minutes < 60:
            return f"{minutes}m {secs}s"
        hours = minutes // 60
        mins = minutes % 60
        return f"{hours}h {mins}m"
