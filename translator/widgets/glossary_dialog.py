"""Standalone glossary editor dialog with General and Project tabs."""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton,
    QLabel, QMessageBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QTabWidget, QWidget, QMenu,
)

from ..default_glossary import CATEGORIES as DEFAULT_GLOSSARY_CATEGORIES


class GlossaryDialog(QDialog):
    """Two-tab glossary editor: General (cross-project) + Project (per-project)."""

    def __init__(self, parent=None, general_glossary=None, project_glossary=None):
        super().__init__(parent)
        self._general_init = general_glossary or {}
        self._project_init = project_glossary or {}
        self.general_glossary = {}   # result after save
        self.project_glossary = {}   # result after save
        self.setWindowTitle("Edit Glossary")
        self.setMinimumSize(600, 500)
        self._build_ui()
        self._load_tables()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        tabs = QTabWidget()
        layout.addWidget(tabs)

        # ── Tab 1: General Glossary ──────────────────────────────────
        general_tab = QWidget()
        general_layout = QVBoxLayout(general_tab)

        general_layout.addWidget(QLabel(
            "General glossary terms apply to ALL projects and persist across sessions.\n"
            "Use this for common eroge/RPG terms, honorifics, and recurring vocabulary."
        ))

        self.gen_search = QLineEdit()
        self.gen_search.setPlaceholderText("Search glossary (JP or EN)...")
        self.gen_search.textChanged.connect(self._filter_general)
        general_layout.addWidget(self.gen_search)

        self.general_table = QTableWidget()
        self.general_table.setColumnCount(2)
        self.general_table.setHorizontalHeaderLabels(["Japanese Term", "English Translation"])
        gen_header = self.general_table.horizontalHeader()
        gen_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        gen_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.general_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        general_layout.addWidget(self.general_table)

        gen_btn_row = QHBoxLayout()
        gen_add_btn = QPushButton("Add Row")
        gen_add_btn.clicked.connect(self._add_general_row)
        gen_btn_row.addWidget(gen_add_btn)

        gen_remove_btn = QPushButton("Remove Selected")
        gen_remove_btn.clicked.connect(self._remove_general_rows)
        gen_btn_row.addWidget(gen_remove_btn)

        gen_clear_btn = QPushButton("Clear All")
        gen_clear_btn.clicked.connect(self._clear_general)
        gen_btn_row.addWidget(gen_clear_btn)

        gen_btn_row.addStretch()

        defaults_btn = QPushButton("Load Defaults \u25bc")
        defaults_menu = QMenu(self)
        defaults_menu.addAction("All Categories", self._load_all_defaults)
        defaults_menu.addSeparator()
        for cat_name in DEFAULT_GLOSSARY_CATEGORIES:
            defaults_menu.addAction(cat_name, lambda c=cat_name: self._load_default_category(c))
        defaults_btn.setMenu(defaults_menu)
        gen_btn_row.addWidget(defaults_btn)

        general_layout.addLayout(gen_btn_row)
        tabs.addTab(general_tab, "General Glossary")

        # ── Tab 2: Project Glossary ──────────────────────────────────
        project_tab = QWidget()
        project_layout = QVBoxLayout(project_tab)

        project_layout.addWidget(QLabel(
            "Project-specific term translations (character names, locations, items).\n"
            "These are saved with the project state. Overrides general glossary if both define a term."
        ))

        self.proj_search = QLineEdit()
        self.proj_search.setPlaceholderText("Search glossary (JP or EN)...")
        self.proj_search.textChanged.connect(self._filter_project)
        project_layout.addWidget(self.proj_search)

        self.project_table = QTableWidget()
        self.project_table.setColumnCount(2)
        self.project_table.setHorizontalHeaderLabels(["Japanese Term", "English Translation"])
        proj_header = self.project_table.horizontalHeader()
        proj_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        proj_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.project_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        project_layout.addWidget(self.project_table)

        proj_btn_row = QHBoxLayout()
        proj_add_btn = QPushButton("Add Row")
        proj_add_btn.clicked.connect(self._add_project_row)
        proj_btn_row.addWidget(proj_add_btn)

        proj_remove_btn = QPushButton("Remove Selected")
        proj_remove_btn.clicked.connect(self._remove_project_rows)
        proj_btn_row.addWidget(proj_remove_btn)

        proj_clear_btn = QPushButton("Clear All")
        proj_clear_btn.clicked.connect(self._clear_project)
        proj_btn_row.addWidget(proj_clear_btn)

        proj_btn_row.addStretch()
        project_layout.addLayout(proj_btn_row)
        tabs.addTab(project_tab, "Project Glossary")

        # ── Bottom buttons ───────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        layout.addLayout(btn_row)

    # ── Table loading ────────────────────────────────────────────────

    def _load_tables(self):
        """Populate both glossary tables from init data."""
        self._load_table(self.general_table, self._general_init)
        self._load_table(self.project_table, self._project_init)

    @staticmethod
    def _load_table(table: QTableWidget, data: dict):
        """Bulk-load a dict into a QTableWidget."""
        items = list(data.items())
        table.setUpdatesEnabled(False)
        table.setRowCount(len(items) or 1)
        for row, (jp, en) in enumerate(items):
            table.setItem(row, 0, QTableWidgetItem(jp))
            table.setItem(row, 1, QTableWidgetItem(en))
        if not items:
            table.setItem(0, 0, QTableWidgetItem(""))
            table.setItem(0, 1, QTableWidgetItem(""))
        table.setUpdatesEnabled(True)

    # ── General glossary operations ──────────────────────────────────

    def _add_general_row(self):
        row = self.general_table.rowCount()
        self.general_table.insertRow(row)
        self.general_table.setItem(row, 0, QTableWidgetItem(""))
        self.general_table.setItem(row, 1, QTableWidgetItem(""))

    def _remove_general_rows(self):
        rows = sorted(set(idx.row() for idx in self.general_table.selectedIndexes()), reverse=True)
        for row in rows:
            self.general_table.removeRow(row)

    def _clear_general(self):
        self.general_table.setRowCount(0)
        self._add_general_row()

    def _filter_general(self, text: str):
        q = text.lower()
        for row in range(self.general_table.rowCount()):
            jp = (self.general_table.item(row, 0) or QTableWidgetItem("")).text().lower()
            en = (self.general_table.item(row, 1) or QTableWidgetItem("")).text().lower()
            self.general_table.setRowHidden(row, bool(q) and q not in jp and q not in en)

    # ── Project glossary operations ──────────────────────────────────

    def _add_project_row(self):
        row = self.project_table.rowCount()
        self.project_table.insertRow(row)
        self.project_table.setItem(row, 0, QTableWidgetItem(""))
        self.project_table.setItem(row, 1, QTableWidgetItem(""))

    def _remove_project_rows(self):
        rows = sorted(set(idx.row() for idx in self.project_table.selectedIndexes()), reverse=True)
        for row in rows:
            self.project_table.removeRow(row)

    def _clear_project(self):
        self.project_table.setRowCount(0)
        self._add_project_row()

    def _filter_project(self, text: str):
        q = text.lower()
        for row in range(self.project_table.rowCount()):
            jp = (self.project_table.item(row, 0) or QTableWidgetItem("")).text().lower()
            en = (self.project_table.item(row, 1) or QTableWidgetItem("")).text().lower()
            self.project_table.setRowHidden(row, bool(q) and q not in jp and q not in en)

    # ── Default glossary loading ─────────────────────────────────────

    def _load_default_category(self, category: str):
        entries = DEFAULT_GLOSSARY_CATEGORIES.get(category, {})
        self._merge_into_general(entries)

    def _load_all_defaults(self):
        from ..default_glossary import get_all_defaults
        self._merge_into_general(get_all_defaults())

    def _merge_into_general(self, entries: dict):
        existing = self._get_table_dict(self.general_table)
        added = 0
        for jp, en in entries.items():
            if jp in existing:
                continue
            row = self.general_table.rowCount()
            if row > 0:
                last_jp = self.general_table.item(row - 1, 0)
                last_en = self.general_table.item(row - 1, 1)
                if last_jp and not last_jp.text().strip() and last_en and not last_en.text().strip():
                    row -= 1
                    self.general_table.setItem(row, 0, QTableWidgetItem(jp))
                    self.general_table.setItem(row, 1, QTableWidgetItem(en))
                    added += 1
                    continue
            self.general_table.insertRow(row)
            self.general_table.setItem(row, 0, QTableWidgetItem(jp))
            self.general_table.setItem(row, 1, QTableWidgetItem(en))
            added += 1
        QMessageBox.information(
            self, "Defaults Loaded",
            f"Added {added} new entries ({len(entries) - added} already existed)."
        )

    # ── Read tables back to dicts ────────────────────────────────────

    @staticmethod
    def _get_table_dict(table: QTableWidget) -> dict:
        glossary = {}
        for row in range(table.rowCount()):
            jp_item = table.item(row, 0)
            en_item = table.item(row, 1)
            jp = jp_item.text().strip() if jp_item else ""
            en = en_item.text().strip() if en_item else ""
            if jp and en:
                glossary[jp] = en
        return glossary

    # ── Save / Cancel ────────────────────────────────────────────────

    def _save(self):
        self.general_glossary = self._get_table_dict(self.general_table)
        self.project_glossary = self._get_table_dict(self.project_table)
        self.accept()
