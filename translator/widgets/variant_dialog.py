"""Dialog for displaying multiple translation variants to choose from."""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTextEdit, QGroupBox, QRadioButton, QButtonGroup,
)
from PyQt6.QtCore import Qt


class VariantDialog(QDialog):
    """Shows multiple translation variants and lets the user pick one."""

    def __init__(self, original: str, variants: list, parent=None):
        """
        Args:
            original: The original Japanese text.
            variants: List of translation strings (typically 3).
        """
        super().__init__(parent)
        self.variants = variants
        self._selected_idx = 0
        self.setWindowTitle("Choose Translation Variant")
        self.setMinimumSize(700, 500)
        self._build_ui(original)

    def _build_ui(self, original: str):
        layout = QVBoxLayout(self)

        # Original text
        orig_group = QGroupBox("Original (JP)")
        orig_layout = QVBoxLayout(orig_group)
        orig_edit = QTextEdit()
        orig_edit.setPlainText(original)
        orig_edit.setReadOnly(True)
        orig_edit.setMaximumHeight(100)
        orig_layout.addWidget(orig_edit)
        layout.addWidget(orig_group)

        # Variant radio buttons + text
        self._button_group = QButtonGroup(self)
        for i, variant in enumerate(self.variants):
            group = QGroupBox()
            group_layout = QHBoxLayout(group)

            radio = QRadioButton(f"Variant {i + 1}")
            if i == 0:
                radio.setChecked(True)
            self._button_group.addButton(radio, i)
            group_layout.addWidget(radio, 0)

            text = QTextEdit()
            text.setPlainText(variant)
            text.setReadOnly(True)
            text.setMinimumHeight(60)
            group_layout.addWidget(text, 1)

            layout.addWidget(group)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        apply_btn = QPushButton("Apply Selected")
        apply_btn.clicked.connect(self.accept)
        btn_row.addWidget(apply_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        layout.addLayout(btn_row)

    def get_selected(self) -> str:
        """Return the chosen translation variant."""
        idx = self._button_group.checkedId()
        if 0 <= idx < len(self.variants):
            return self.variants[idx]
        return self.variants[0] if self.variants else ""
