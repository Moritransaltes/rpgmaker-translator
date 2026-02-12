"""Dialog for reviewing and selecting glossary terms harvested from a translated game."""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QHeaderView, QAbstractItemView, QLineEdit,
)
from PyQt6.QtCore import Qt


class GlossaryScanDialog(QDialog):
    """Shows JP→EN pairs from a translated game for user to pick glossary entries."""

    def __init__(self, pairs: list[tuple[str, str]], parent=None):
        """
        Args:
            pairs: List of (japanese, english) candidate terms, pre-filtered.
        """
        super().__init__(parent)
        self._pairs = pairs
        self.setWindowTitle("Scan Game for Glossary")
        self.setMinimumSize(750, 500)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(
            f"Found {len(self._pairs)} glossary candidates from the translated game.\n"
            "Check the terms you want to add to your general glossary."
        ))

        # Filter bar
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter:"))
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("Type to filter...")
        self._filter_edit.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self._filter_edit)
        layout.addLayout(filter_row)

        # Table
        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["", "Japanese", "English"])
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 40)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

        self.table.setRowCount(len(self._pairs))
        for row, (jp, en) in enumerate(self._pairs):
            # Checkbox
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable
                         | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(row, 0, chk)

            jp_item = QTableWidgetItem(jp)
            jp_item.setFlags(Qt.ItemFlag.ItemIsEnabled
                             | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, 1, jp_item)

            en_item = QTableWidgetItem(en)
            en_item.setFlags(Qt.ItemFlag.ItemIsEnabled
                             | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, 2, en_item)

        self.table.sortItems(1)  # Sort by JP text
        layout.addWidget(self.table)

        # Buttons row
        btn_row = QHBoxLayout()

        self._count_label = QLabel()
        self._update_count()
        btn_row.addWidget(self._count_label)
        btn_row.addStretch()

        select_all_btn = QPushButton("Select All")
        select_all_btn.clicked.connect(lambda: self._set_all(True))
        btn_row.addWidget(select_all_btn)

        select_none_btn = QPushButton("Select None")
        select_none_btn.clicked.connect(lambda: self._set_all(False))
        btn_row.addWidget(select_none_btn)

        ok_btn = QPushButton("Add Selected")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        layout.addLayout(btn_row)

        # Track check changes for count
        self.table.itemChanged.connect(self._update_count)

    def _set_all(self, checked: bool):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self.table.rowCount()):
            if not self.table.isRowHidden(row):
                self.table.item(row, 0).setCheckState(state)

    def _apply_filter(self, text: str):
        text = text.strip().lower()
        for row in range(self.table.rowCount()):
            if not text:
                self.table.setRowHidden(row, False)
                continue
            jp = self.table.item(row, 1).text().lower()
            en = self.table.item(row, 2).text().lower()
            self.table.setRowHidden(row, text not in jp and text not in en)

    def _update_count(self, _item=None):
        checked = sum(
            1 for row in range(self.table.rowCount())
            if self.table.item(row, 0).checkState() == Qt.CheckState.Checked
        )
        self._count_label.setText(
            f"{checked} / {self.table.rowCount()} selected")

    def selected_pairs(self) -> list[tuple[str, str]]:
        """Return the (jp, en) pairs that are checked."""
        result = []
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).checkState() == Qt.CheckState.Checked:
                jp = self.table.item(row, 1).text()
                en = self.table.item(row, 2).text()
                result.append((jp, en))
        return result
