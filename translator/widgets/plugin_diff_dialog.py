"""Dialog for reviewing plugin.js parameter diffs (hand-translated edits)."""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)


class PluginDiffDialog(QDialog):
    """Shows a table of plugin parameter diffs with per-row checkboxes.

    Parameters
    ----------
    diffs : list of (entry_id, plugin_name, param_label, original, translated)
    parent : QWidget or None
    """

    def __init__(self, diffs: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Scan Plugin Edits")
        self.resize(900, 500)
        self._diffs = diffs
        self._checks: list[QCheckBox] = []
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Table
        self._table = table = QTableWidget(len(self._diffs), 4)
        table.setHorizontalHeaderLabels(["Plugin", "Parameter", "Original (JP)", "Translated (EN)"])
        table.verticalHeader().setVisible(False)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

        for row, (eid, pname, plabel, orig, trans) in enumerate(self._diffs):
            # Checkbox in first column via checkable item
            cb = QTableWidgetItem()
            cb.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            cb.setCheckState(Qt.CheckState.Checked)
            cb.setText(pname)
            table.setItem(row, 0, cb)

            table.setItem(row, 1, self._ro_item(plabel))
            table.setItem(row, 2, self._ro_item(orig))
            table.setItem(row, 3, self._ro_item(trans))

        layout.addWidget(table)

        # Buttons
        btn_row = QHBoxLayout()
        sel_all = QPushButton("Select All")
        sel_all.clicked.connect(self._select_all)
        desel = QPushButton("Deselect All")
        desel.clicked.connect(self._deselect_all)
        btn_row.addWidget(sel_all)
        btn_row.addWidget(desel)
        btn_row.addStretch()

        apply_btn = QPushButton("Apply Selected")
        apply_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(apply_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    @staticmethod
    def _ro_item(text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        return item

    def _select_all(self):
        for row in range(self._table.rowCount()):
            self._table.item(row, 0).setCheckState(Qt.CheckState.Checked)

    def _deselect_all(self):
        for row in range(self._table.rowCount()):
            self._table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)

    def accepted_diffs(self) -> list:
        """Return list of (entry_id, original, translation) for checked rows."""
        result = []
        for row in range(self._table.rowCount()):
            if self._table.item(row, 0).checkState() == Qt.CheckState.Checked:
                eid, _pname, _plabel, orig, trans = self._diffs[row]
                result.append((eid, orig, trans))
        return result
