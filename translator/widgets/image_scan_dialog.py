"""Dialog for selecting which img/ subdirectories to scan for image translation."""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QCheckBox, QScrollArea, QWidget,
)
from PyQt6.QtCore import Qt

from ..image_translator import ImageTranslator


class ImageScanDialog(QDialog):
    """Checkbox dialog for selecting img/ subdirectories to translate."""

    _DEFAULT_CHECKED = ImageTranslator.PRIORITY_DIRS

    def __init__(self, subdirs: list[tuple[str, int]], parent=None):
        """
        Args:
            subdirs: List of (subdir_name, image_count) tuples.
        """
        super().__init__(parent)
        self.setWindowTitle("Select Image Folders")
        self.setMinimumWidth(350)

        self._checks: list[tuple[QCheckBox, str]] = []

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Select which img/ subdirectories to scan for Japanese text.\n"
            "Checked folders will be OCR'd, translated, and saved to img_translated/."
        ))

        # Scrollable checkbox area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)

        for name, count in subdirs:
            cb = QCheckBox(f"{name}  ({count} images)")
            cb.setChecked(name.lower() in self._DEFAULT_CHECKED)
            inner_layout.addWidget(cb)
            self._checks.append((cb, name))

        inner_layout.addStretch()
        scroll.setWidget(inner)
        layout.addWidget(scroll)

        # Select All / None buttons
        sel_row = QHBoxLayout()
        all_btn = QPushButton("Select All")
        all_btn.clicked.connect(lambda: self._set_all(True))
        sel_row.addWidget(all_btn)

        none_btn = QPushButton("Select None")
        none_btn.clicked.connect(lambda: self._set_all(False))
        sel_row.addWidget(none_btn)
        sel_row.addStretch()
        layout.addLayout(sel_row)

        # OK / Cancel
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def _set_all(self, checked: bool):
        for cb, _ in self._checks:
            cb.setChecked(checked)

    def selected_subdirs(self) -> list[str]:
        """Return list of selected subdirectory names."""
        return [name for cb, name in self._checks if cb.isChecked()]
