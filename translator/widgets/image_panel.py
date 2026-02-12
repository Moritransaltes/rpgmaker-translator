"""Image Translation tab — browse, OCR, translate, and preview game images."""

import os
import shutil
import tempfile
from dataclasses import dataclass, field

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QLabel,
    QPushButton, QListWidget, QListWidgetItem, QGroupBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QComboBox, QMessageBox, QApplication,
)
from PyQt6.QtCore import Qt, QThread, QObject, pyqtSignal, QSize
from PyQt6.QtGui import QPixmap, QColor

from ..image_translator import (
    ImageTranslator, TextRegion,
    ALL_IMAGE_EXTS, ENCRYPTED_EXTS,
    read_encryption_key, decrypt_rpgmvp, encrypt_to_rpgmvp,
)


def _png_output_name(filename: str) -> str:
    """Ensure output filename always has .png extension."""
    root, ext = os.path.splitext(filename)
    if ext.lower() not in (".png",):
        return root + ".png"
    return filename


# ── Data ─────────────────────────────────────────────────────────

@dataclass
class ImageEntry:
    """State for one image in the panel."""
    path: str
    subdir: str
    filename: str
    regions: list[TextRegion] = field(default_factory=list)
    status: str = "pending"   # pending | translated | skipped | no_text | error
    output_path: str = ""
    error: str = ""


# Status display
_STATUS_LABELS = {
    "pending": "Pending",
    "translated": "Translated",
    "skipped": "Skipped",
    "no_text": "No JP text",
    "error": "Error",
}

_STATUS_COLORS_DARK = {
    "pending":    QColor(60, 60, 70),
    "translated": QColor(30, 70, 40),
    "skipped":    QColor(55, 55, 55),
    "no_text":    QColor(50, 50, 60),
    "error":      QColor(80, 40, 40),
}


# ── Worker ───────────────────────────────────────────────────────

class _ImageWorker(QObject):
    """Background worker for OCR + translate + render."""
    image_done = pyqtSignal(int)            # row index
    image_error = pyqtSignal(int, str)      # row index, error message
    all_done = pyqtSignal()

    def __init__(self, translator: ImageTranslator, entries: list[ImageEntry],
                 indices: list[int], out_base: str):
        super().__init__()
        self.translator = translator
        self.entries = entries
        self.indices = indices
        self.out_base = out_base
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        for idx in self.indices:
            if self._cancelled:
                break
            entry = self.entries[idx]
            try:
                regions = self.translator.ocr_image(entry.path)
                if not regions:
                    entry.status = "no_text"
                    entry.regions = []
                    self.image_done.emit(idx)
                    continue

                regions = self.translator.translate_regions(regions)
                entry.regions = regions

                # Render to output (always .png)
                rel = os.path.join(entry.subdir, _png_output_name(entry.filename))
                out_path = os.path.join(self.out_base, rel)
                self.translator.render_translated(entry.path, regions, out_path)

                entry.output_path = out_path
                entry.status = "translated"
                self.image_done.emit(idx)

            except Exception as e:
                entry.status = "error"
                entry.error = str(e)
                self.image_error.emit(idx, str(e))

        self.all_done.emit()


# ── Image Panel ──────────────────────────────────────────────────

class ImagePanel(QWidget):
    """Image Translation tab — folder list, image table, preview, region editor."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._project_path = ""
        self._client = None
        self._translator = None
        self._entries: list[ImageEntry] = []
        self._selected_idx = -1
        self._img_dir = ""
        self._encryption_key = ""
        self._out_base = ""
        self._worker = None
        self._thread = None
        # Store full-size pixmaps for rescaling on resize
        self._orig_pixmap = None
        self._trans_pixmap = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Main horizontal splitter: folder list | content
        hsplit = QSplitter(Qt.Orientation.Horizontal)

        # ── Left: folder list ─────────────────────────────────────
        self.folder_list = QListWidget()
        self.folder_list.setMaximumWidth(200)
        self.folder_list.currentItemChanged.connect(self._on_folder_clicked)
        hsplit.addWidget(self.folder_list)

        # ── Right: content area ───────────────────────────────────
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        # Toolbar
        toolbar = QHBoxLayout()

        self.translate_all_btn = QPushButton("Translate All")
        self.translate_all_btn.setToolTip("OCR + translate all pending images in this folder")
        self.translate_all_btn.clicked.connect(self._translate_all)
        self.translate_all_btn.setEnabled(False)
        toolbar.addWidget(self.translate_all_btn)

        self.export_btn = QPushButton("Export All")
        self.export_btn.setToolTip("Re-encrypt and copy translated images into the game's img/ folder")
        self.export_btn.clicked.connect(self._export_all)
        self.export_btn.setEnabled(False)
        toolbar.addWidget(self.export_btn)

        self.textmap_btn = QPushButton("Export Text Map")
        self.textmap_btn.setToolTip(
            "Save a text file listing all OCR'd regions + translations.\n"
            "Useful as a reference for manual Photoshop editing."
        )
        self.textmap_btn.clicked.connect(self._export_text_map)
        self.textmap_btn.setEnabled(False)
        toolbar.addWidget(self.textmap_btn)

        toolbar.addWidget(QLabel("Filter:"))
        self.status_filter = QComboBox()
        self.status_filter.addItems(["All", "Pending", "Translated", "Skipped", "No JP text", "Error"])
        self.status_filter.currentTextChanged.connect(self._refresh_table)
        toolbar.addWidget(self.status_filter)

        toolbar.addStretch()
        self.stats_label = QLabel("No project loaded")
        toolbar.addWidget(self.stats_label)

        right_layout.addLayout(toolbar)

        # Vertical splitter: preview | table | region editor
        vsplit = QSplitter(Qt.Orientation.Vertical)

        # ── Preview panel ─────────────────────────────────────────
        preview_widget = QWidget()
        preview_layout = QHBoxLayout(preview_widget)
        preview_layout.setContentsMargins(0, 0, 0, 0)

        orig_group = QGroupBox("Original")
        orig_box = QVBoxLayout(orig_group)
        self.orig_label = QLabel("Select an image to preview")
        self.orig_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.orig_label.setMinimumHeight(150)
        self.orig_label.setStyleSheet("background-color: #181825; border: 1px solid #313244;")
        orig_box.addWidget(self.orig_label)
        preview_layout.addWidget(orig_group)

        trans_group = QGroupBox("Translated")
        trans_box = QVBoxLayout(trans_group)
        self.trans_label = QLabel("Not yet translated")
        self.trans_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.trans_label.setMinimumHeight(150)
        self.trans_label.setStyleSheet("background-color: #181825; border: 1px solid #313244;")
        trans_box.addWidget(self.trans_label)
        preview_layout.addWidget(trans_group)

        vsplit.addWidget(preview_widget)

        # ── Image table ───────────────────────────────────────────
        self.image_table = QTableWidget()
        self.image_table.setColumnCount(4)
        self.image_table.setHorizontalHeaderLabels(["St", "Filename", "Regions", "Status"])
        header = self.image_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.image_table.setColumnWidth(0, 30)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.image_table.setColumnWidth(2, 60)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.image_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.image_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.image_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.image_table.currentCellChanged.connect(self._on_image_selected)
        vsplit.addWidget(self.image_table)

        # ── Region editor ─────────────────────────────────────────
        editor_widget = QWidget()
        editor_layout = QVBoxLayout(editor_widget)
        editor_layout.setContentsMargins(0, 0, 0, 0)

        editor_layout.addWidget(QLabel("Text Regions (edit EN translations, then Apply):"))

        self.region_table = QTableWidget()
        self.region_table.setColumnCount(2)
        self.region_table.setHorizontalHeaderLabels(["JP Text (read-only)", "EN Translation (editable)"])
        rh = self.region_table.horizontalHeader()
        rh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        rh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        editor_layout.addWidget(self.region_table)

        btn_row = QHBoxLayout()
        self.apply_btn = QPushButton("Apply Changes")
        self.apply_btn.setToolTip("Re-render image with edited translations")
        self.apply_btn.clicked.connect(self._apply_edits)
        self.apply_btn.setEnabled(False)
        btn_row.addWidget(self.apply_btn)

        self.translate_btn = QPushButton("Translate Selected")
        self.translate_btn.setToolTip("OCR + translate selected images")
        self.translate_btn.clicked.connect(self._translate_selected)
        self.translate_btn.setEnabled(False)
        btn_row.addWidget(self.translate_btn)

        self.retranslate_btn = QPushButton("Retranslate")
        self.retranslate_btn.setToolTip("Re-run OCR + translate on selected image (resets regions)")
        self.retranslate_btn.clicked.connect(self._retranslate_selected)
        self.retranslate_btn.setEnabled(False)
        btn_row.addWidget(self.retranslate_btn)

        self.skip_btn = QPushButton("Skip Selected")
        self.skip_btn.setToolTip("Mark selected images as skipped")
        self.skip_btn.clicked.connect(self._skip_selected)
        self.skip_btn.setEnabled(False)
        btn_row.addWidget(self.skip_btn)

        btn_row.addStretch()
        editor_layout.addLayout(btn_row)

        vsplit.addWidget(editor_widget)

        # Default split: 35% preview, 40% table, 25% editor
        vsplit.setStretchFactor(0, 35)
        vsplit.setStretchFactor(1, 40)
        vsplit.setStretchFactor(2, 25)

        right_layout.addWidget(vsplit)
        hsplit.addWidget(right)
        hsplit.setSizes([180, 1020])

        layout.addWidget(hsplit)

    # ── Project setup ─────────────────────────────────────────────

    def set_project(self, project_path: str, client):
        """Initialize with a project — discover img/ folders."""
        self._project_path = project_path
        self._client = client
        self._img_dir = ImageTranslator.find_img_dir(project_path) or ""
        self._encryption_key = read_encryption_key(project_path)

        if not self._img_dir:
            self.stats_label.setText("No img/ folder found")
            return

        self._out_base = os.path.join(os.path.dirname(self._img_dir), "img_translated")

        # Build translator
        vision_model = getattr(client, "vision_model", "") or ""
        if vision_model:
            self._translator = ImageTranslator(
                ollama_url=client.base_url,
                vision_model=vision_model,
                text_client=client,
                encryption_key=self._encryption_key,
            )

        # Populate folder list
        self.folder_list.clear()
        subdirs = ImageTranslator.list_subdirs(project_path)
        for name, count in subdirs:
            item = QListWidgetItem(f"{name}  ({count})")
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.folder_list.addItem(item)

        self.stats_label.setText(f"{len(subdirs)} folders found")
        self.translate_all_btn.setEnabled(bool(subdirs) and bool(vision_model))
        self.translate_btn.setEnabled(bool(vision_model))
        self.retranslate_btn.setEnabled(bool(vision_model))
        self.skip_btn.setEnabled(True)

    # ── Folder selection ──────────────────────────────────────────

    def _on_folder_clicked(self, current, previous):
        """Load images from the selected folder."""
        if not current:
            return
        subdir = current.data(Qt.ItemDataRole.UserRole)
        if not subdir or not self._img_dir:
            return

        folder = os.path.join(self._img_dir, subdir)
        if not os.path.isdir(folder):
            return

        self._entries = []
        for fname in sorted(os.listdir(folder)):
            if fname.lower().endswith(ALL_IMAGE_EXTS):
                path = os.path.join(folder, fname)
                # Check if already translated (output always .png)
                out_path = os.path.join(self._out_base, subdir, _png_output_name(fname))
                entry = ImageEntry(
                    path=path, subdir=subdir, filename=fname,
                    output_path=out_path if os.path.isfile(out_path) else "",
                    status="translated" if os.path.isfile(out_path) else "pending",
                )
                self._entries.append(entry)

        self._refresh_table()
        self._clear_preview()
        self.export_btn.setEnabled(bool(self._entries))
        self.textmap_btn.setEnabled(bool(self._entries))

    def _refresh_table(self):
        """Rebuild image table with current filter."""
        status_map = {
            "All": None, "Pending": "pending", "Translated": "translated",
            "Skipped": "skipped", "No JP text": "no_text", "Error": "error",
        }
        filt = status_map.get(self.status_filter.currentText())

        self.image_table.setRowCount(0)
        for i, entry in enumerate(self._entries):
            if filt and entry.status != filt:
                continue
            row = self.image_table.rowCount()
            self.image_table.insertRow(row)

            # Status dot
            dot = self._status_dot(entry.status)
            dot_item = QTableWidgetItem(dot)
            dot_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            dot_item.setData(Qt.ItemDataRole.UserRole, i)  # store real index
            color = _STATUS_COLORS_DARK.get(entry.status)
            if color:
                dot_item.setBackground(color)
            self.image_table.setItem(row, 0, dot_item)

            # Filename
            fn_item = QTableWidgetItem(entry.filename)
            fn_item.setData(Qt.ItemDataRole.UserRole, i)
            if color:
                fn_item.setBackground(color)
            self.image_table.setItem(row, 1, fn_item)

            # Region count
            rc = QTableWidgetItem(str(len(entry.regions)) if entry.regions else "")
            rc.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if color:
                rc.setBackground(color)
            self.image_table.setItem(row, 2, rc)

            # Status text
            st = QTableWidgetItem(_STATUS_LABELS.get(entry.status, entry.status))
            if color:
                st.setBackground(color)
            self.image_table.setItem(row, 3, st)

        # Update stats
        total = len(self._entries)
        translated = sum(1 for e in self._entries if e.status == "translated")
        pending = sum(1 for e in self._entries if e.status == "pending")
        self.stats_label.setText(
            f"{total} images | {translated} translated | {pending} pending"
        )

    @staticmethod
    def _status_dot(status: str) -> str:
        return {"pending": "\u25cb", "translated": "\u25cf", "skipped": "\u2013",
                "no_text": "\u25cb", "error": "\u2716"}.get(status, "?")

    # ── Image selection / preview ─────────────────────────────────

    def _load_pixmap(self, path: str) -> QPixmap:
        """Load a QPixmap, decrypting .rpgmvp/.png_ files if needed."""
        if path.lower().endswith(ENCRYPTED_EXTS) and self._encryption_key:
            try:
                raw_bytes = decrypt_rpgmvp(path, self._encryption_key)
                pm = QPixmap()
                pm.loadFromData(raw_bytes)
                return pm
            except Exception:
                return QPixmap()
        return QPixmap(path)

    def _on_image_selected(self, row, col, prev_row, prev_col):
        """Show preview and populate region editor for selected image."""
        if row < 0:
            return
        item = self.image_table.item(row, 0)
        if not item:
            return
        idx = item.data(Qt.ItemDataRole.UserRole)
        if idx is None or idx < 0 or idx >= len(self._entries):
            return
        self._selected_idx = idx
        entry = self._entries[idx]

        # Load original preview (handles encrypted files)
        self._orig_pixmap = self._load_pixmap(entry.path)
        self._scale_preview(self.orig_label, self._orig_pixmap)

        # Load translated preview if available
        if entry.output_path and os.path.isfile(entry.output_path):
            self._trans_pixmap = QPixmap(entry.output_path)
            if self._trans_pixmap.isNull():
                # File exists but failed to load — mark as needing re-render
                self._trans_pixmap = None
                self.trans_label.setPixmap(QPixmap())
                self.trans_label.setText("Render failed — try Retranslate")
            else:
                self._scale_preview(self.trans_label, self._trans_pixmap)
        else:
            self._trans_pixmap = None
            self.trans_label.setPixmap(QPixmap())
            self.trans_label.setText("Not yet translated")

        # Populate region editor
        self._populate_regions(entry)
        self.apply_btn.setEnabled(bool(entry.regions))

    def _scale_preview(self, label: QLabel, pixmap: QPixmap):
        """Scale pixmap to fit label while keeping aspect ratio."""
        if pixmap and not pixmap.isNull():
            scaled = pixmap.scaled(
                label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            label.setPixmap(scaled)
            label.setText("")

    def _clear_preview(self):
        """Reset preview labels."""
        self._orig_pixmap = None
        self._trans_pixmap = None
        self.orig_label.setPixmap(QPixmap())
        self.orig_label.setText("Select an image to preview")
        self.trans_label.setPixmap(QPixmap())
        self.trans_label.setText("Not yet translated")
        self.region_table.setRowCount(0)
        self._selected_idx = -1
        self.apply_btn.setEnabled(False)

    def resizeEvent(self, event):
        """Re-scale previews when the panel is resized."""
        super().resizeEvent(event)
        if self._orig_pixmap and not self._orig_pixmap.isNull():
            self._scale_preview(self.orig_label, self._orig_pixmap)
        if self._trans_pixmap and not self._trans_pixmap.isNull():
            self._scale_preview(self.trans_label, self._trans_pixmap)

    # ── Region editor ─────────────────────────────────────────────

    def _populate_regions(self, entry: ImageEntry):
        """Fill region table with JP text and EN translations."""
        self.region_table.setRowCount(0)
        for region in entry.regions:
            row = self.region_table.rowCount()
            self.region_table.insertRow(row)

            jp_item = QTableWidgetItem(region.text)
            jp_item.setFlags(jp_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.region_table.setItem(row, 0, jp_item)

            en_item = QTableWidgetItem(region.translation)
            self.region_table.setItem(row, 1, en_item)

    def _read_regions_from_editor(self) -> list[TextRegion]:
        """Read back edited translations from the region table."""
        if self._selected_idx < 0:
            return []
        entry = self._entries[self._selected_idx]
        regions = []
        for i, region in enumerate(entry.regions):
            en_item = self.region_table.item(i, 1)
            en_text = en_item.text() if en_item else region.translation
            regions.append(TextRegion(
                text=region.text,
                bbox=region.bbox,
                translation=en_text,
            ))
        return regions

    def _apply_edits(self):
        """Re-render image with edited translations from region editor."""
        if self._selected_idx < 0 or not self._translator:
            return
        entry = self._entries[self._selected_idx]
        regions = self._read_regions_from_editor()
        if not regions:
            return

        # Update entry regions
        entry.regions = regions

        # Render (always .png output)
        rel = os.path.join(entry.subdir, _png_output_name(entry.filename))
        out_path = os.path.join(self._out_base, rel)
        try:
            self._translator.render_translated(entry.path, regions, out_path)
            entry.output_path = out_path
            entry.status = "translated"

            # Update preview
            self._trans_pixmap = QPixmap(out_path)
            self._scale_preview(self.trans_label, self._trans_pixmap)

            # Refresh table row
            self._refresh_table()

        except Exception as e:
            QMessageBox.warning(self, "Render Error", f"Failed to render image: {e}")

    # ── Translation actions ───────────────────────────────────────

    def _get_selected_indices(self) -> list[int]:
        """Get real entry indices from selected table rows."""
        indices = []
        for item in self.image_table.selectedItems():
            if item.column() == 0:
                idx = item.data(Qt.ItemDataRole.UserRole)
                if idx is not None:
                    indices.append(idx)
        return indices

    def _translate_selected(self):
        """Translate selected images in background thread."""
        indices = self._get_selected_indices()
        if not indices:
            # If nothing selected, translate the currently viewed image
            if self._selected_idx >= 0:
                indices = [self._selected_idx]
        if not indices or not self._translator:
            return
        # Only translate pending/error/no_text ones (no_text = OCR may have missed it)
        indices = [i for i in indices if self._entries[i].status in ("pending", "error", "no_text")]
        if not indices:
            return
        self._start_worker(indices)

    def _retranslate_selected(self):
        """Force re-OCR + translate on selected images (ignores current status)."""
        indices = self._get_selected_indices()
        if not indices:
            if self._selected_idx >= 0:
                indices = [self._selected_idx]
        if not indices or not self._translator:
            return
        # Reset status so the worker processes them
        for i in indices:
            self._entries[i].status = "pending"
            self._entries[i].regions = []
            self._entries[i].output_path = ""
        self._refresh_table()
        self._start_worker(indices)

    def _translate_all(self):
        """Translate all pending/failed images in current folder."""
        if not self._translator:
            return
        # Include "no_text" — OCR may have missed text on first attempt
        indices = [i for i, e in enumerate(self._entries)
                   if e.status in ("pending", "no_text", "error")]
        if not indices:
            QMessageBox.information(self, "Nothing to do", "No pending images to translate.")
            return
        self._start_worker(indices)

    def _start_worker(self, indices: list[int]):
        """Start background worker for OCR + translate + render."""
        if self._thread is not None:
            QMessageBox.warning(self, "Busy", "Translation is already running.")
            return

        self.translate_all_btn.setEnabled(False)
        self.translate_btn.setEnabled(False)

        self._thread = QThread(self)
        self._worker = _ImageWorker(
            self._translator, self._entries, indices, self._out_base
        )
        self._worker.moveToThread(self._thread)

        self._worker.image_done.connect(self._on_image_done)
        self._worker.image_error.connect(self._on_image_error)
        self._worker.all_done.connect(self._on_all_done)
        self._thread.started.connect(self._worker.run)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.start()

    def _on_image_done(self, idx: int):
        """Update UI after one image is processed."""
        self._refresh_table()
        # If this is the currently selected image, update preview + regions
        if idx == self._selected_idx:
            entry = self._entries[idx]
            if entry.output_path and os.path.isfile(entry.output_path):
                self._trans_pixmap = QPixmap(entry.output_path)
                self._scale_preview(self.trans_label, self._trans_pixmap)
            self._populate_regions(entry)
            self.apply_btn.setEnabled(bool(entry.regions))

    def _on_image_error(self, idx: int, msg: str):
        """Update UI after an image error."""
        self._refresh_table()

    def _on_all_done(self):
        """Re-enable buttons after batch completes."""
        self.translate_all_btn.setEnabled(True)
        self.translate_btn.setEnabled(True)
        if self._thread:
            self._thread.quit()
            self._thread.wait(5000)
        self._refresh_table()

    def _on_thread_finished(self):
        """Clean up thread/worker references after thread exits."""
        self._thread = None
        self._worker = None

    def _skip_selected(self):
        """Mark selected images as skipped."""
        indices = self._get_selected_indices()
        if not indices:
            if self._selected_idx >= 0:
                indices = [self._selected_idx]
        for idx in indices:
            self._entries[idx].status = "skipped"
        self._refresh_table()

    # ── Export ─────────────────────────────────────────────────────

    def _export_all(self):
        """Export translated images into the game's img/ folder.

        For encrypted games (.rpgmvp), re-encrypts the PNG back to .rpgmvp
        and replaces the original file. For plain PNG games, copies directly.
        Creates backups on first export (img_original/).
        """
        if not self._entries or not self._img_dir:
            return
        translated = [e for e in self._entries if e.status == "translated" and e.output_path]
        if not translated:
            QMessageBox.information(self, "Nothing to export", "No translated images to export.")
            return

        # Create backup directory on first export
        img_parent = os.path.dirname(self._img_dir)
        backup_dir = os.path.join(img_parent, "img_original")
        first_export = not os.path.isdir(backup_dir)

        exported = 0
        errors = []
        for entry in translated:
            try:
                # Original file in game's img/ folder
                orig_file = entry.path  # e.g. .../img/system/Command_0.rpgmvp
                subdir_path = os.path.join(self._img_dir, entry.subdir)

                # Backup original on first export
                if first_export:
                    bak_subdir = os.path.join(backup_dir, entry.subdir)
                    os.makedirs(bak_subdir, exist_ok=True)
                    bak_file = os.path.join(bak_subdir, entry.filename)
                    if not os.path.isfile(bak_file):
                        shutil.copy2(orig_file, bak_file)

                # Export: re-encrypt if original was .rpgmvp, else copy PNG
                if entry.filename.lower().endswith(ENCRYPTED_EXTS) and self._encryption_key:
                    # Re-encrypt translated PNG → .rpgmvp in game folder
                    dest = os.path.join(subdir_path, entry.filename)
                    encrypt_to_rpgmvp(entry.output_path, dest, self._encryption_key)
                else:
                    # Plain image: copy PNG (matching original filename)
                    dest = os.path.join(subdir_path, _png_output_name(entry.filename))
                    shutil.copy2(entry.output_path, dest)

                exported += 1
            except Exception as e:
                errors.append(f"{entry.filename}: {e}")

        msg = f"Exported {exported} images to game folder."
        if first_export:
            msg += f"\nOriginals backed up to: img_original/"
        if errors:
            msg += f"\n\n{len(errors)} errors:\n" + "\n".join(errors[:5])
        QMessageBox.information(self, "Export Complete", msg)

    def _export_text_map(self):
        """Export a text file mapping every image's OCR regions + translations.

        Useful as a Photoshop reference — lists each image, the bounding box
        coordinates, the original Japanese text, and the English translation.
        """
        entries_with_regions = [
            e for e in self._entries if e.regions
        ]
        if not entries_with_regions:
            QMessageBox.information(
                self, "Nothing to export",
                "No OCR results yet. Translate some images first.",
            )
            return

        # Write to img_translated/<subdir>/_text_map.txt
        subdir = entries_with_regions[0].subdir
        out_dir = os.path.join(self._out_base, subdir)
        os.makedirs(out_dir, exist_ok=True)
        map_path = os.path.join(out_dir, "_text_map.txt")

        lines = []
        lines.append(f"Image Translation Text Map — {subdir}/")
        lines.append(f"{'=' * 60}")
        lines.append("")

        for entry in entries_with_regions:
            lines.append(f"File: {entry.filename}")
            lines.append(f"Status: {entry.status}")
            if not entry.regions:
                lines.append("  (no regions detected)")
            for i, region in enumerate(entry.regions):
                x1, y1, x2, y2 = region.bbox
                lines.append(f"  Region {i + 1}: bbox=({x1}, {y1}, {x2}, {y2})")
                lines.append(f"    JP: {region.text}")
                lines.append(f"    EN: {region.translation}")
            lines.append("")

        with open(map_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        QMessageBox.information(
            self, "Text Map Exported",
            f"Saved {len(entries_with_regions)} images to:\n{map_path}\n\n"
            "Use this file as a reference for manual image editing.",
        )

    # ── Cleanup ───────────────────────────────────────────────────

    def stop_worker(self):
        """Cancel running worker. Call on app close."""
        if self._worker:
            self._worker.cancel()
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(3000)
