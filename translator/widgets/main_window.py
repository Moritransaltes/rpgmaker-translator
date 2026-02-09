"""Main application window — ties together all widgets."""

import os
import re
import time

from PyQt6.QtWidgets import (
    QMainWindow, QSplitter, QToolBar, QStatusBar, QProgressBar,
    QFileDialog, QMessageBox, QLabel, QWidget, QVBoxLayout, QApplication,
    QProgressDialog, QMenu, QInputDialog,
)
from PyQt6.QtCore import Qt, QSize, QTimer
from PyQt6.QtGui import QAction, QPalette, QColor

DARK_STYLESHEET = """
QMainWindow, QDialog, QWidget {
    background-color: #1e1e2e;
    color: #cdd6f4;
}
QMenuBar, QToolBar {
    background-color: #181825;
    color: #cdd6f4;
    border-bottom: 1px solid #313244;
}
QMenuBar::item:selected, QToolBar QToolButton:hover {
    background-color: #313244;
}
QMenu {
    background-color: #1e1e2e;
    color: #cdd6f4;
    border: 1px solid #313244;
}
QMenu::item:selected {
    background-color: #45475a;
}
QMenu::separator {
    height: 1px;
    background-color: #313244;
    margin: 4px 8px;
}
QTreeWidget, QTableWidget, QPlainTextEdit, QLineEdit, QComboBox {
    background-color: #181825;
    color: #cdd6f4;
    border: 1px solid #313244;
    selection-background-color: #45475a;
}
QTableWidget::item {
    padding: 4px;
}
QHeaderView::section {
    background-color: #1e1e2e;
    color: #cdd6f4;
    border: 1px solid #313244;
    padding: 4px;
}
QPushButton {
    background-color: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    padding: 5px 15px;
    border-radius: 3px;
}
QPushButton:hover {
    background-color: #45475a;
}
QPushButton:pressed {
    background-color: #585b70;
}
QProgressBar {
    border: 1px solid #313244;
    background-color: #181825;
    text-align: center;
    color: #cdd6f4;
}
QProgressBar::chunk {
    background-color: #89b4fa;
}
QStatusBar {
    background-color: #181825;
    color: #a6adc8;
}
QGroupBox {
    border: 1px solid #313244;
    border-radius: 4px;
    margin-top: 8px;
    padding-top: 16px;
    color: #cdd6f4;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
}
QTabWidget::pane {
    border: 1px solid #313244;
}
QTabBar::tab {
    background-color: #181825;
    color: #a6adc8;
    padding: 6px 16px;
    border: 1px solid #313244;
}
QTabBar::tab:selected {
    background-color: #1e1e2e;
    color: #cdd6f4;
}
QSplitter::handle {
    background-color: #313244;
}
QLabel {
    color: #cdd6f4;
}
"""

from ..ollama_client import OllamaClient
from ..rpgmaker_mv import RPGMakerMVParser
from ..project_model import TranslationProject
from ..translation_engine import TranslationEngine
from ..text_processor import PluginAnalyzer, TextProcessor
from .file_tree import FileTreeWidget
from .translation_table import TranslationTable
from .settings_dialog import SettingsDialog
from .actor_gender_dialog import ActorGenderDialog
from .variant_dialog import VariantDialog


class MainWindow(QMainWindow):
    """Main application window."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("RPG Maker Translator — Local LLM")
        self.setMinimumSize(1200, 700)

        # Core objects
        self.client = OllamaClient()
        self.parser = RPGMakerMVParser()
        self.project = TranslationProject()
        self.engine = TranslationEngine(self.client)
        self.plugin_analyzer = PluginAnalyzer()
        self.text_processor = TextProcessor(self.plugin_analyzer)
        self._dark_mode = True
        self._batch_start_time = 0
        self._batch_done_count = 0
        self._last_save_path = ""

        self._build_ui()
        self._build_menubar()
        self._build_toolbar()
        self._build_statusbar()
        self._connect_signals()

        # Apply dark mode by default
        self._apply_dark_mode()

        # Auto-save timer (every 2 minutes)
        self._autosave_timer = QTimer(self)
        self._autosave_timer.timeout.connect(self._autosave)
        self._autosave_timer.start(120_000)

    # ── UI Setup ───────────────────────────────────────────────────

    def _build_ui(self):
        """Build the main layout with splitter."""
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: file tree
        self.file_tree = FileTreeWidget()
        splitter.addWidget(self.file_tree)

        # Right: translation table
        self.trans_table = TranslationTable()
        splitter.addWidget(self.trans_table)

        splitter.setSizes([250, 950])
        self.setCentralWidget(splitter)

    def _build_menubar(self):
        """Build the menu bar with organized menus."""
        menubar = self.menuBar()

        # ── Project menu ──────────────────────────────────────────
        project_menu = menubar.addMenu("Project")

        self.open_action = QAction("Open Project...", self)
        self.open_action.setShortcut("Ctrl+O")
        self.open_action.triggered.connect(self._open_project)
        project_menu.addAction(self.open_action)

        self.save_action = QAction("Save State", self)
        self.save_action.setShortcut("Ctrl+S")
        self.save_action.triggered.connect(self._save_state)
        self.save_action.setEnabled(False)
        project_menu.addAction(self.save_action)

        self.load_action = QAction("Load State...", self)
        self.load_action.setShortcut("Ctrl+L")
        self.load_action.triggered.connect(self._load_state)
        project_menu.addAction(self.load_action)

        project_menu.addSeparator()

        self.rename_action = QAction("Rename Folder...", self)
        self.rename_action.triggered.connect(self._rename_folder)
        self.rename_action.setEnabled(False)
        project_menu.addAction(self.rename_action)

        # ── Translate menu ────────────────────────────────────────
        translate_menu = menubar.addMenu("Translate")

        self.batch_action = QAction("Batch Translate", self)
        self.batch_action.setShortcut("Ctrl+T")
        self.batch_action.triggered.connect(self._batch_translate)
        self.batch_action.setEnabled(False)
        translate_menu.addAction(self.batch_action)

        self.stop_action = QAction("Stop", self)
        self.stop_action.triggered.connect(self._stop_translation)
        self.stop_action.setEnabled(False)
        translate_menu.addAction(self.stop_action)

        translate_menu.addSeparator()

        self.wordwrap_action = QAction("Apply Word Wrap", self)
        self.wordwrap_action.triggered.connect(self._apply_wordwrap)
        self.wordwrap_action.setEnabled(False)
        translate_menu.addAction(self.wordwrap_action)

        # ── Game menu ─────────────────────────────────────────────
        game_menu = menubar.addMenu("Game")

        self.export_action = QAction("Export to Game", self)
        self.export_action.setShortcut("Ctrl+E")
        self.export_action.triggered.connect(self._export_to_game)
        self.export_action.setEnabled(False)
        game_menu.addAction(self.export_action)

        self.restore_action = QAction("Restore Originals", self)
        self.restore_action.triggered.connect(self._restore_originals)
        self.restore_action.setEnabled(False)
        game_menu.addAction(self.restore_action)

        game_menu.addSeparator()

        self.txt_export_action = QAction("Export TXT...", self)
        self.txt_export_action.triggered.connect(self._export_txt)
        self.txt_export_action.setEnabled(False)
        game_menu.addAction(self.txt_export_action)

        # ── Settings (top-level action) ───────────────────────────
        self.settings_action = QAction("Settings", self)
        self.settings_action.triggered.connect(self._open_settings)
        menubar.addAction(self.settings_action)

    def _build_toolbar(self):
        """Build a slim toolbar with quick-access translation controls."""
        toolbar = QToolBar("Quick Actions")
        toolbar.setIconSize(QSize(20, 20))
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        # Reuse actions created in _build_menubar
        toolbar.addAction(self.batch_action)
        toolbar.addAction(self.stop_action)

    def _build_statusbar(self):
        """Build the bottom status bar with progress."""
        self.statusbar = QStatusBar()
        self.setStatusBar(self.statusbar)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedWidth(300)
        self.progress_bar.setVisible(False)
        self.statusbar.addPermanentWidget(self.progress_bar)

        self.progress_label = QLabel("")
        self.statusbar.addWidget(self.progress_label)

    def _connect_signals(self):
        """Wire up signals between components."""
        # File tree
        self.file_tree.file_selected.connect(self._filter_by_file)
        self.file_tree.all_selected.connect(self._show_all_entries)

        # Translation table
        self.trans_table.translate_requested.connect(self._translate_selected)
        self.trans_table.retranslate_correction.connect(self._retranslate_with_correction)
        self.trans_table.variant_requested.connect(self._show_variants)
        self.trans_table.status_changed.connect(self._on_status_changed)

        # Engine
        self.engine.progress.connect(self._on_progress)
        self.engine.entry_done.connect(self._on_entry_done)
        self.engine.error.connect(self._on_error)
        self.engine.checkpoint.connect(self._on_checkpoint)
        self.engine.finished.connect(self._on_batch_finished)

    # ── Actions ────────────────────────────────────────────────────

    def _open_project(self):
        """Open an RPG Maker MV/MZ project folder."""
        path = QFileDialog.getExistingDirectory(
            self, "Select RPG Maker MV/MZ Project Folder"
        )
        if not path:
            return

        try:
            entries = self.parser.load_project(path)
        except FileNotFoundError as e:
            QMessageBox.warning(self, "Error", str(e))
            return

        self.project = TranslationProject(project_path=path, entries=entries)
        self.file_tree.load_project(self.project)
        self.trans_table.set_entries(entries)

        # Load actors for gender assignment
        actors_raw = self.parser.load_actors_raw(path)

        # Pre-translate game title + actor info so the user can read them
        translated_title = ""
        actor_translations = {}
        raw_title = self.parser.get_game_title(path)
        has_jp_title = any(e.id == "System.json/gameTitle" for e in entries)
        if actors_raw or has_jp_title:
            actor_translations, translated_title = self._pre_translate_info(
                entries, actors_raw
            )
        # If game title is already English (not in entries), use it directly
        if not translated_title and raw_title and not has_jp_title:
            translated_title = raw_title

        # Show gender assignment dialog with translated names
        if actors_raw:
            dlg = ActorGenderDialog(actors_raw, self, translations=actor_translations)
            if dlg.exec():
                genders = dlg.get_genders()
            else:
                # User skipped — use auto-detected genders
                genders = {a["id"]: a["auto_gender"] for a in actors_raw
                           if a["auto_gender"] != "unknown"}
            actor_ctx = self.parser.build_actor_context(actors_raw, genders)
            self.client.actor_context = actor_ctx
            self.project.actor_genders = genders
        else:
            self.client.actor_context = ""

        # Offer to rename folder to English title
        path = self._rename_project_folder(path, translated_title)
        self.project.project_path = path

        # Analyze plugins for word wrap settings
        self.plugin_analyzer.analyze_project(path)

        self.save_action.setEnabled(True)
        self.batch_action.setEnabled(True)
        self.export_action.setEnabled(True)
        self.restore_action.setEnabled(True)
        self.rename_action.setEnabled(True)
        self.txt_export_action.setEnabled(True)
        self.wordwrap_action.setEnabled(True)

        plugin_info = ""
        if self.plugin_analyzer.detected_plugins:
            plugin_info = f" | Plugins: {', '.join(self.plugin_analyzer.detected_plugins)}"
        self.statusbar.showMessage(
            f"Loaded {len(entries)} entries | "
            f"~{self.plugin_analyzer.chars_per_line} chars/line{plugin_info}", 8000
        )

        # Window title
        folder = os.path.basename(path)
        self.setWindowTitle(f"RPG Maker Translator \u2014 {folder}")

    def _pre_translate_info(self, entries, actors_raw):
        """Translate game title + actor names/profiles before the gender dialog.

        Returns:
            (actor_translations, translated_title) where actor_translations is
            {actor_id: {"name": ..., "nickname": ..., "profile": ...}}
        """
        # Build list of items to translate
        items = []  # (label, text) for progress display
        title_entry = None
        for e in entries:
            if e.id == "System.json/gameTitle":
                title_entry = e
                items.append(("Game title", e.original))
                break

        for actor in actors_raw:
            if actor.get("name"):
                items.append((f"Actor {actor['id']} name", actor["name"]))
            if actor.get("nickname"):
                items.append((f"Actor {actor['id']} nickname", actor["nickname"]))
            if actor.get("profile"):
                items.append((f"Actor {actor['id']} profile", actor["profile"]))

        if not items:
            return {}, ""

        # Check Ollama availability first
        if not self.client.is_available():
            return {}, ""

        progress = QProgressDialog(
            "Translating character info...", "Skip", 0, len(items), self
        )
        progress.setWindowTitle("Pre-translating")
        progress.setMinimumDuration(0)
        progress.setValue(0)

        translated_title = ""
        actor_translations = {}  # {actor_id: {"name":..., "nickname":..., "profile":...}}
        idx = 0

        # Translate game title
        if title_entry:
            progress.setLabelText(f"Translating game title...")
            QApplication.processEvents()
            if progress.wasCanceled():
                return actor_translations, translated_title
            result = self.client.translate_name(title_entry.original, hint="game title")
            if result and result != title_entry.original:
                translated_title = result
                title_entry.translation = result
                title_entry.status = "translated"
            idx += 1
            progress.setValue(idx)

        # Translate actor fields
        for actor in actors_raw:
            aid = actor["id"]
            if aid not in actor_translations:
                actor_translations[aid] = {}

            for field in ("name", "nickname", "profile"):
                text = actor.get(field, "")
                if not text:
                    continue
                progress.setLabelText(f"Translating Actor {aid} {field}...")
                QApplication.processEvents()
                if progress.wasCanceled():
                    return actor_translations, translated_title
                field_hints = {
                    "name": "character's personal name",
                    "nickname": "character's nickname or title",
                    "profile": "character's biography",
                }
                result = self.client.translate_name(text, hint=field_hints[field])
                if result and result != text:
                    actor_translations[aid][field] = result
                idx += 1
                progress.setValue(idx)

        progress.close()
        return actor_translations, translated_title

    def _rename_project_folder(self, path: str, translated_title: str) -> str:
        """Offer to rename the project folder to 'English Title - WIP'.

        Returns the (possibly new) project path.
        """
        if not translated_title:
            return path

        # Sanitize for filesystem — remove characters illegal on Windows
        safe_name = re.sub(r'[\\/:*?"<>|]', '', translated_title).strip()
        # Collapse multiple spaces
        safe_name = re.sub(r'\s+', ' ', safe_name)
        if not safe_name:
            return path

        new_name = f"{safe_name} - WIP"
        parent = os.path.dirname(path)
        new_path = os.path.join(parent, new_name)

        if os.path.normpath(new_path) == os.path.normpath(path):
            return path  # Already named correctly

        if os.path.exists(new_path):
            # Target already exists — use it without renaming
            return path

        reply = QMessageBox.question(
            self, "Rename Project Folder",
            f"Rename folder to English title?\n\n"
            f"From: {os.path.basename(path)}\n"
            f"To: {new_name}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return path

        try:
            os.rename(path, new_path)
            # Update autosave path to point inside the new folder
            if self._last_save_path:
                self._last_save_path = os.path.join(
                    new_path, "_translation_autosave.json"
                )
            return new_path
        except OSError as e:
            QMessageBox.warning(
                self, "Rename Failed",
                f"Could not rename folder:\n{e}\n\n"
                "Continuing with original folder name."
            )
            return path

    def _rename_folder(self):
        """Translate the folder name and rename the project folder."""
        if not self.project.project_path or not os.path.isdir(self.project.project_path):
            QMessageBox.warning(self, "No Project", "Open a project first.")
            return

        folder_name = os.path.basename(self.project.project_path)

        # Translate the folder name via Ollama
        translated = folder_name
        if self.client.is_available():
            self.statusbar.showMessage("Translating folder name...")
            QApplication.processEvents()
            result = self.client.translate_name(folder_name, hint="game title")
            if result and result != folder_name:
                translated = result
            self.statusbar.clearMessage()

        suggested = f"{translated} - WIP"
        suggested = re.sub(r'[\\/:*?"<>|]', '', suggested).strip()
        suggested = re.sub(r'\s+', ' ', suggested)

        new_name, ok = QInputDialog.getText(
            self, "Rename Folder",
            f"Current: {folder_name}\n"
            f"Translated: {translated}\n\n"
            f"New folder name:",
            text=suggested,
        )
        if not ok or not new_name.strip():
            return

        new_name = new_name.strip()
        new_name = re.sub(r'[\\/:*?"<>|]', '', new_name).strip()
        new_name = re.sub(r'\s+', ' ', new_name)
        if not new_name:
            QMessageBox.warning(self, "Invalid Name",
                                "The folder name contains only invalid characters.")
            return

        parent = os.path.dirname(self.project.project_path)
        new_path = os.path.join(parent, new_name)

        if os.path.normpath(new_path) == os.path.normpath(self.project.project_path):
            return

        if os.path.exists(new_path):
            QMessageBox.warning(self, "Already Exists",
                                f"A folder named '{new_name}' already exists.")
            return

        try:
            os.rename(self.project.project_path, new_path)
            self.project.project_path = new_path
            # Update autosave path to point inside the new folder
            if self._last_save_path:
                self._last_save_path = os.path.join(
                    new_path, "_translation_autosave.json"
                )
            self.setWindowTitle(f"RPG Maker Translator \u2014 {new_name}")
            self.statusbar.showMessage(f"Renamed folder to: {new_name}", 5000)
        except OSError as e:
            QMessageBox.warning(self, "Rename Failed",
                                f"Could not rename folder:\n{e}")

    def _save_state(self):
        """Save current translation state to a JSON file."""
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Translation State", "", "JSON Files (*.json)"
        )
        if path:
            self.project.save_state(path)
            self._last_save_path = path
            self.statusbar.showMessage(f"State saved to {path}", 3000)

    def _load_state(self):
        """Load a previously saved translation state."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Translation State", "", "JSON Files (*.json)"
        )
        if not path:
            return

        try:
            self.project = TranslationProject.load_state(path)
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to load state:\n{e}")
            return

        self.file_tree.load_project(self.project)
        self.trans_table.set_entries(self.project.entries)

        # Restore glossary
        self.client.glossary = self.project.glossary

        # Restore actor context from saved genders
        if self.project.actor_genders and self.project.project_path:
            actors_raw = self.parser.load_actors_raw(self.project.project_path)
            if actors_raw:
                self.client.actor_context = self.parser.build_actor_context(
                    actors_raw, self.project.actor_genders
                )

        self.save_action.setEnabled(True)
        self.batch_action.setEnabled(True)
        self.export_action.setEnabled(bool(self.project.project_path))
        self.restore_action.setEnabled(bool(self.project.project_path))
        self.rename_action.setEnabled(bool(self.project.project_path))
        self.txt_export_action.setEnabled(True)
        self.wordwrap_action.setEnabled(True)

        self.statusbar.showMessage(
            f"Loaded state: {self.project.total} entries "
            f"({self.project.translated_count} translated)", 5000
        )
        name = os.path.basename(self.project.project_path) if self.project.project_path else "Restored"
        self.setWindowTitle(f"RPG Maker Translator — {name}")

    def _stop_translation(self):
        """Cancel the running batch translation."""
        self.engine.cancel()
        self.stop_action.setEnabled(False)

    def _export_to_game(self):
        """Write translations back to the game's JSON files."""
        if not self.project.project_path:
            QMessageBox.warning(self, "Error", "No project path set. Open a project first.")
            return

        translated = [e for e in self.project.entries if e.status in ("translated", "reviewed")]
        if not translated:
            QMessageBox.information(self, "Nothing to Export", "No translated entries to export.")
            return

        reply = QMessageBox.question(
            self, "Confirm Export",
            f"This will overwrite {len(set(e.file for e in translated))} file(s) "
            f"in:\n{self.project.project_path}\n\n"
            f"Original files will be backed up to data_original/ (first export only).\n\n"
            f"Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            self.parser.save_project(self.project.project_path, self.project.entries)
            QMessageBox.information(
                self, "Export Complete",
                f"Exported {len(translated)} translations to game files.\n"
                f"Original Japanese files backed up in data_original/."
            )
        except Exception as e:
            QMessageBox.critical(self, "Export Failed", str(e))

    def _restore_originals(self):
        """Restore the original Japanese game files from backup."""
        if not self.project or not self.project.project_path:
            return

        data_dir = self.parser._find_data_dir(self.project.project_path)
        if not data_dir:
            QMessageBox.warning(self, "Error", "Could not find data directory.")
            return

        backup_dir = data_dir + "_original"
        if not os.path.isdir(backup_dir):
            QMessageBox.information(
                self, "No Backup Found",
                "No data_original/ backup exists. Export to game first to create one."
            )
            return

        reply = QMessageBox.question(
            self, "Restore Originals",
            "This will overwrite the current game files with the original "
            "Japanese versions from data_original/.\n\n"
            "Your translation state is NOT affected — only the game files.\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            import shutil
            # Remove current data dir and replace with backup
            shutil.rmtree(data_dir)
            shutil.copytree(backup_dir, data_dir)
            QMessageBox.information(
                self, "Restore Complete",
                "Original Japanese files have been restored.\n"
                "The backup in data_original/ is still available."
            )
        except Exception as e:
            QMessageBox.critical(self, "Restore Failed", str(e))

    def _open_settings(self):
        """Open the settings dialog."""
        dlg = SettingsDialog(self.client, self, parser=self.parser, dark_mode=self._dark_mode)
        if dlg.exec():
            # Sync glossary to project model
            self.project.glossary = self.client.glossary
            # Apply dark mode if changed
            if dlg.dark_mode != self._dark_mode:
                self._dark_mode = dlg.dark_mode
                self._apply_dark_mode()
                self.trans_table.set_dark_mode(self._dark_mode)

    # ── Filtering ──────────────────────────────────────────────────

    def _filter_by_file(self, filename: str):
        """Show only entries from a specific file."""
        entries = self.project.get_entries_for_file(filename)
        self.trans_table.set_entries(entries)

    def _show_all_entries(self):
        """Show all entries."""
        self.trans_table.set_entries(self.project.entries)

    # ── Engine signal handlers ─────────────────────────────────────

    def _on_error(self, entry_id: str, error_msg: str):
        """Handle translation error for a single entry."""
        self.statusbar.showMessage(f"Error translating {entry_id}: {error_msg}", 5000)

    def _on_batch_finished(self):
        """Handle batch translation completing."""
        self.batch_action.setEnabled(True)
        self.stop_action.setEnabled(False)
        self.progress_bar.setVisible(False)
        self.progress_label.setText("")
        self.file_tree.refresh_stats(self.project)
        self.statusbar.showMessage(
            f"Batch complete — {self.project.translated_count}/{self.project.total} translated",
            5000,
        )

    def _on_checkpoint(self):
        """Auto-save during batch translation (every 25 entries)."""
        self._autosave()

    def _on_status_changed(self):
        """Handle status change from manual edits."""
        self.file_tree.refresh_stats(self.project)

    # ── Dark mode ──────────────────────────────────────────────────

    def _apply_dark_mode(self):
        """Apply or remove dark stylesheet."""
        app = QApplication.instance()
        if self._dark_mode:
            app.setStyleSheet(DARK_STYLESHEET)
        else:
            app.setStyleSheet("")

    # ── Auto-save ──────────────────────────────────────────────────

    def _autosave(self):
        """Auto-save project state if there are entries and a save path exists."""
        if not self.project.entries:
            return
        if not self._last_save_path:
            # Auto-save next to project if possible
            if self.project.project_path:
                self._last_save_path = os.path.join(
                    self.project.project_path, "_translation_autosave.json"
                )
            else:
                return
        try:
            self.project.save_state(self._last_save_path)
            self.statusbar.showMessage("Auto-saved", 2000)
        except Exception:
            pass  # Silent fail on autosave

    # ── Progress ETA ───────────────────────────────────────────────

    def _on_progress(self, current: int, total: int, text: str):
        """Update progress bar with ETA during batch translation."""
        self.progress_bar.setValue(current)
        self._batch_done_count = current

        # Calculate ETA
        eta_str = ""
        elapsed = time.time() - self._batch_start_time
        if current > 0 and elapsed > 0:
            rate = elapsed / current  # seconds per entry
            remaining = (total - current) * rate
            if remaining > 3600:
                eta_str = f" | ETA: {remaining/3600:.1f}h"
            elif remaining > 60:
                eta_str = f" | ETA: {remaining/60:.0f}m"
            else:
                eta_str = f" | ETA: {remaining:.0f}s"

        self.progress_label.setText(f"Translating {current}/{total}{eta_str}: {text}")

    # ── Translation memory ─────────────────────────────────────────

    def _batch_translate(self):
        """Start batch translating all untranslated entries."""
        if not self.client.is_available():
            QMessageBox.warning(
                self, "Ollama Not Available",
                "Cannot connect to Ollama. Make sure it's running:\n  ollama serve"
            )
            return

        # Translation memory: auto-fill exact duplicates from already-translated entries
        translated_map = {}
        for e in self.project.entries:
            if e.status in ("translated", "reviewed") and e.translation:
                translated_map[e.original] = e.translation

        tm_count = 0
        for e in self.project.entries:
            if e.status == "untranslated" and e.original in translated_map:
                e.translation = translated_map[e.original]
                e.status = "translated"
                self.trans_table.update_entry(e.id, e.translation)
                tm_count += 1

        if tm_count:
            self.file_tree.refresh_stats(self.project)
            self.statusbar.showMessage(
                f"Translation memory: filled {tm_count} duplicate(s)", 3000
            )

        untranslated = [e for e in self.project.entries if e.status == "untranslated"]
        if not untranslated:
            QMessageBox.information(self, "Done", "All entries are already translated!")
            return

        self.batch_action.setEnabled(False)
        self.stop_action.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(len(untranslated))
        self.progress_bar.setValue(0)
        self._batch_start_time = time.time()
        self._batch_done_count = 0

        self.engine.translate_batch(self.project.entries)

    # ── Word Wrap ──────────────────────────────────────────────────

    def _apply_wordwrap(self):
        """Apply word wrapping to all translated entries based on plugin analysis."""
        if not self.project.entries:
            return

        summary = self.plugin_analyzer.get_summary()
        reply = QMessageBox.question(
            self, "Apply Word Wrap",
            f"Detected settings:\n\n{summary}\n\n"
            f"Apply word wrapping to all translated entries?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        count = self.text_processor.process_all(self.project.entries)
        # Refresh table
        self.trans_table.set_entries(
            self.trans_table._entries  # refresh current view
        )
        QMessageBox.information(
            self, "Word Wrap Applied",
            f"Modified {count} entries to fit ~{self.plugin_analyzer.chars_per_line} chars/line."
        )

    # ── Export TXT ─────────────────────────────────────────────────

    def _export_txt(self):
        """Export translations to a human-readable TXT patch file."""
        if not self.project.entries:
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Translation Patch", "", "Text Files (*.txt)"
        )
        if not path:
            return

        translated = [e for e in self.project.entries if e.status in ("translated", "reviewed")]
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# RPG Maker Translation Patch\n")
            f.write(f"# Project: {self.project.project_path}\n")
            f.write(f"# Entries: {len(translated)}\n")
            f.write(f"# Generated by RPG Maker Translator (Local LLM)\n\n")

            current_file = ""
            for entry in translated:
                if entry.file != current_file:
                    current_file = entry.file
                    f.write(f"\n{'='*60}\n")
                    f.write(f"# File: {current_file}\n")
                    f.write(f"{'='*60}\n\n")

                f.write(f"[{entry.id}]\n")
                f.write(f"  JP: {entry.original}\n")
                f.write(f"  EN: {entry.translation}\n\n")

        QMessageBox.information(
            self, "Export Complete",
            f"Exported {len(translated)} translations to:\n{path}"
        )

    # ── Re-translate with diff ─────────────────────────────────────

    def _translate_selected(self, entry_ids: list):
        """Translate specific selected entries (allows re-translation)."""
        if not self.client.is_available():
            QMessageBox.warning(
                self, "Ollama Not Available",
                "Cannot connect to Ollama. Make sure it's running:\n  ollama serve"
            )
            return

        entries = [self.project.get_entry_by_id(eid) for eid in entry_ids]
        entries = [e for e in entries if e is not None]
        if not entries:
            return

        # Store old translations for diff display
        self._old_translations = {e.id: e.translation for e in entries if e.translation}

        # Force re-translate by temporarily marking as untranslated
        for e in entries:
            if e.status in ("translated", "reviewed"):
                e.status = "untranslated"

        self.batch_action.setEnabled(False)
        self.stop_action.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(len(entries))
        self.progress_bar.setValue(0)
        self._batch_start_time = time.time()
        self._batch_done_count = 0

        self.engine.translate_batch(entries)

    def _on_entry_done(self, entry_id: str, translation: str):
        """Handle a single entry translation completing, with diff info."""
        entry = self.project.get_entry_by_id(entry_id)
        if entry:
            # Check for diff with previous translation
            old = getattr(self, '_old_translations', {}).get(entry_id, "")
            if old and old != translation:
                self.statusbar.showMessage(
                    f"Re-translated: was \"{old[:40]}...\" -> now \"{translation[:40]}...\"",
                    5000,
                )
            entry.translation = translation
            entry.status = "translated"
        self.trans_table.update_entry(entry_id, translation)
        self.file_tree.refresh_stats(self.project)

    # ── Retranslate single entry with correction ──────────────────

    def _retranslate_with_correction(self, entry_id: str, correction: str):
        """Retranslate a single entry with user's correction hint."""
        if not self.client.is_available():
            QMessageBox.warning(
                self, "Ollama Not Available",
                "Cannot connect to Ollama. Make sure it's running:\n  ollama serve"
            )
            return

        entry = self.project.get_entry_by_id(entry_id)
        if not entry:
            return

        old_translation = entry.translation
        self.statusbar.showMessage(f"Retranslating with correction: {correction}...")

        # Run in a background thread to avoid freezing the UI
        from PyQt6.QtCore import QThread, QObject, pyqtSignal as Signal

        class _RetranslateWorker(QObject):
            done = Signal(str)
            failed = Signal(str)

            def __init__(self, client, text, context, correction, old_trans, field):
                super().__init__()
                self.client = client
                self.text = text
                self.context = context
                self.correction = correction
                self.old_trans = old_trans
                self.field = field

            def run(self):
                try:
                    result = self.client.translate(
                        text=self.text,
                        context=self.context,
                        correction=self.correction,
                        old_translation=self.old_trans,
                        field=self.field,
                    )
                    self.done.emit(result)
                except Exception as e:
                    self.failed.emit(str(e))

        thread = QThread(self)
        worker = _RetranslateWorker(
            self.client, entry.original, entry.context, correction, old_translation,
            entry.field,
        )
        worker.moveToThread(thread)

        def on_done(new_translation):
            entry.translation = new_translation
            entry.status = "translated"
            self.trans_table.update_entry(entry_id, new_translation)
            self.file_tree.refresh_stats(self.project)
            if old_translation and old_translation != new_translation:
                self.statusbar.showMessage(
                    f"Corrected: \"{old_translation[:40]}\" -> \"{new_translation[:40]}\"", 8000
                )
            else:
                self.statusbar.showMessage("Retranslation complete", 3000)
            thread.quit()

        def on_failed(err):
            self.statusbar.showMessage(f"Retranslation failed: {err}", 5000)
            thread.quit()

        def on_thread_finished():
            thread.deleteLater()

        worker.done.connect(on_done)
        worker.failed.connect(on_failed)
        thread.started.connect(worker.run)
        thread.finished.connect(on_thread_finished)
        thread.start()

        # Keep references alive until thread completes
        self._correction_thread = thread
        self._correction_worker = worker

    # ── Translation variants ──────────────────────────────────────

    def _show_variants(self, entry_id: str):
        """Generate 3 translation variants and let the user pick one."""
        if not self.client.is_available():
            QMessageBox.warning(
                self, "Ollama Not Available",
                "Cannot connect to Ollama. Make sure it's running:\n  ollama serve"
            )
            return

        entry = self.project.get_entry_by_id(entry_id)
        if not entry:
            return

        self.statusbar.showMessage("Generating 3 translation variants...")

        from PyQt6.QtCore import QThread, QObject, pyqtSignal as Signal

        class _VariantWorker(QObject):
            done = Signal(list)
            failed = Signal(str)

            def __init__(self, client, text, context, field):
                super().__init__()
                self.client = client
                self.text = text
                self.context = context
                self.field = field

            def run(self):
                try:
                    variants = self.client.translate_variants(
                        text=self.text,
                        context=self.context,
                        field=self.field,
                        count=3,
                    )
                    self.done.emit(variants)
                except Exception as e:
                    self.failed.emit(str(e))

        thread = QThread(self)
        worker = _VariantWorker(
            self.client, entry.original, entry.context, entry.field,
        )
        worker.moveToThread(thread)

        def on_done(variants):
            thread.quit()
            self.statusbar.showMessage(
                f"Generated {len(variants)} variant(s)", 3000
            )
            if not variants:
                QMessageBox.warning(self, "No Variants", "Failed to generate any variants.")
                return
            dlg = VariantDialog(entry.original, variants, self)
            if dlg.exec():
                chosen = dlg.get_selected()
                entry.translation = chosen
                entry.status = "translated"
                self.trans_table.update_entry(entry_id, chosen)
                self.file_tree.refresh_stats(self.project)

        def on_failed(err):
            self.statusbar.showMessage(f"Variant generation failed: {err}", 5000)
            thread.quit()

        def on_thread_finished():
            thread.deleteLater()

        worker.done.connect(on_done)
        worker.failed.connect(on_failed)
        thread.started.connect(worker.run)
        thread.finished.connect(on_thread_finished)
        thread.start()

        self._variant_thread = thread
        self._variant_worker = worker
