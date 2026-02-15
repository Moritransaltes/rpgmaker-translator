"""Main application window — ties together all widgets."""

import json
import os
import re
import shutil
import subprocess
import time
from collections import Counter

from PyQt6.QtWidgets import (
    QMainWindow, QSplitter, QToolBar, QStatusBar, QProgressBar,
    QFileDialog, QMessageBox, QLabel, QWidget, QVBoxLayout, QApplication,
    QProgressDialog, QMenu, QInputDialog, QDialog, QTabWidget,
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
QMessageBox {
    background-color: #1e1e2e;
}
QMessageBox QLabel {
    color: #cdd6f4;
    min-width: 320px;
}
QMessageBox QPushButton {
    background-color: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    padding: 6px 24px;
    min-width: 80px;
    border-radius: 3px;
}
QMessageBox QPushButton:hover {
    background-color: #45475a;
}
QMessageBox QPushButton:default {
    border: 1px solid #89b4fa;
    color: #89b4fa;
}
QInputDialog {
    background-color: #1e1e2e;
    color: #cdd6f4;
}
QTextEdit {
    background-color: #181825;
    color: #cdd6f4;
    border: 1px solid #313244;
    selection-background-color: #45475a;
}
QCheckBox {
    color: #cdd6f4;
    spacing: 6px;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid #45475a;
    border-radius: 3px;
    background-color: #181825;
}
QCheckBox::indicator:checked {
    background-color: #89b4fa;
    border-color: #89b4fa;
}
QDialogButtonBox QPushButton {
    padding: 6px 24px;
    min-width: 80px;
}
"""

from ..ai_client import AIClient
from ..rpgmaker_mv import RPGMakerMVParser
from ..project_model import TranslationProject
from ..translation_engine import TranslationEngine
from ..text_processor import PluginAnalyzer, TextProcessor
from .file_tree import FileTreeWidget
from .translation_table import TranslationTable
from .settings_dialog import SettingsDialog
from .glossary_dialog import GlossaryDialog
from .actor_gender_dialog import ActorGenderDialog
from .variant_dialog import VariantDialog
from .image_panel import ImagePanel
from .gpu_monitor import GPUMonitorPanel
from .queue_panel import QueuePanel
from .model_suggestion_dialog import ModelSuggestionDialog


class MainWindow(QMainWindow):
    """Main application window."""

    # Files considered "DB" for stage-1 batch (translate names first, QA, then dialogue)
    _DB_FILES = {
        "Actors.json", "Classes.json", "Items.json", "Weapons.json",
        "Armors.json", "Skills.json", "States.json", "Enemies.json",
        "System.json",
    }

    # DB fields whose translated values should be auto-added to glossary
    # so the LLM uses consistent names when they appear in dialogue.
    _AUTO_GLOSSARY_FIELDS = {
        "Actors.json": ("name", "nickname"),
        "Classes.json": ("name",),
        "Items.json": ("name",),
        "Weapons.json": ("name",),
        "Armors.json": ("name",),
        "Skills.json": ("name",),
        "Enemies.json": ("name",),
        "States.json": ("name",),
    }
    # Map displayNames are keyed by Map###.json — matched dynamically
    _AUTO_GLOSSARY_MAP_FIELD = "displayName"
    # Fields where each word should be capitalized (names, titles, places)
    _CAPITALIZE_FIELDS = {"name", "nickname", "displayName", "speaker_name"}
    # Words to leave lowercase in title case (prepositions, articles, conjunctions)
    _TITLE_SMALL_WORDS = {
        "a", "an", "the", "of", "in", "on", "at", "to", "for", "and",
        "or", "but", "nor", "by", "with", "from", "as", "is", "vs",
    }

    # Settings file lives next to main.py
    _SETTINGS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "_settings.json")

    def __init__(self):
        super().__init__()
        self.setWindowTitle("RPG Maker Translator — Local LLM")
        self.setMinimumSize(1200, 700)

        # Core objects
        self.client = AIClient()
        self.parser = RPGMakerMVParser()
        self.project = TranslationProject()
        self.engine = TranslationEngine(self.client)
        self.plugin_analyzer = PluginAnalyzer()
        self.text_processor = TextProcessor(self.plugin_analyzer)
        self._dark_mode = True
        self._export_review_file = False
        self._actors_ready = False  # True after actor gender dialog has been shown/skipped
        self._batch_start_time = 0
        self._batch_done_count = 0
        self._tm_checkpoint_count = 0
        self._batch_all_chained = False
        self._last_save_path = ""
        self._general_glossary = {}  # persists across all projects
        self.client.vision_model = ""  # vision model for image OCR

        # Restore persistent settings before building UI
        self._load_settings()

        self._build_ui()
        self._build_menubar()
        self._build_toolbar()
        self._build_statusbar()
        self._connect_signals()

        # Apply dark mode by default
        self._apply_dark_mode()

        # Auto-start Ollama with saved worker count if not already running
        if not self.client.is_available():
            self.client.restart_server(self.engine.num_workers)

        # Clear stale models from VRAM on startup (keep_alive=-1 persists across sessions)
        if not self.client.is_cloud:
            self.client.unload_models()

        # Auto-save timer (every 2 minutes)
        self._autosave_timer = QTimer(self)
        self._autosave_timer.timeout.connect(self._autosave)
        self._autosave_timer.start(120_000)

        # First-launch model suggestion (deferred to after window shows)
        if not os.path.exists(self._SETTINGS_FILE) and not self.client.is_cloud:
            QTimer.singleShot(500, self._show_model_suggestion)

    # ── UI Setup ───────────────────────────────────────────────────

    def _build_ui(self):
        """Build the main layout with tabs: Text Translation | Image Translation."""
        self.tabs = QTabWidget()

        # Tab 1: Text Translation (existing layout)
        text_tab = QSplitter(Qt.Orientation.Horizontal)

        # Left column: file tree + GPU monitor
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        self.file_tree = FileTreeWidget()
        left_layout.addWidget(self.file_tree, 1)
        self.gpu_monitor = GPUMonitorPanel()
        left_layout.addWidget(self.gpu_monitor)

        text_tab.addWidget(left_panel)
        self.trans_table = TranslationTable()
        text_tab.addWidget(self.trans_table)
        text_tab.setSizes([250, 950])
        self.tabs.addTab(text_tab, "Text Translation")

        # Tab 2: Image Translation
        self.image_panel = ImagePanel()
        self.tabs.addTab(self.image_panel, "Image Translation (Experimental)")

        # Tab 3: Translation Queue
        self.queue_panel = QueuePanel()
        self.tabs.addTab(self.queue_panel, "Translation Queue")

        self.setCentralWidget(self.tabs)

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

        self.save_as_action = QAction("Save State As...", self)
        self.save_as_action.setShortcut("Ctrl+Shift+S")
        self.save_as_action.triggered.connect(self._save_state_as)
        self.save_as_action.setEnabled(False)
        project_menu.addAction(self.save_as_action)

        self.load_action = QAction("Load State...", self)
        self.load_action.setShortcut("Ctrl+L")
        self.load_action.triggered.connect(self._load_state)
        project_menu.addAction(self.load_action)

        project_menu.addSeparator()

        self.close_action = QAction("Close Project", self)
        self.close_action.setShortcut("Ctrl+W")
        self.close_action.triggered.connect(self._close_project)
        self.close_action.setEnabled(False)
        project_menu.addAction(self.close_action)

        project_menu.addSeparator()

        self.rename_action = QAction("Rename Folder...", self)
        self.rename_action.triggered.connect(self._rename_folder)
        self.rename_action.setEnabled(False)
        project_menu.addAction(self.rename_action)

        # Import submenu
        import_menu = project_menu.addMenu("Import")

        self.import_action = QAction("From Save State...", self)
        self.import_action.setToolTip(
            "Import translations from an older version's save state"
        )
        self.import_action.triggered.connect(self._import_translations)
        self.import_action.setEnabled(False)
        import_menu.addAction(self.import_action)

        self.import_folder_action = QAction("From Game Folder...", self)
        self.import_folder_action.setToolTip(
            "Import translations from an already-translated game folder"
        )
        self.import_folder_action.triggered.connect(self._import_from_game_folder)
        self.import_folder_action.setEnabled(False)
        import_menu.addAction(self.import_folder_action)

        self.scan_plugin_edits_action = QAction("Plugin Parameters...", self)
        self.scan_plugin_edits_action.setToolTip(
            "Compare two plugins.js files and import translated parameters"
        )
        self.scan_plugin_edits_action.triggered.connect(
            self._scan_plugin_edits)
        self.scan_plugin_edits_action.setEnabled(False)
        import_menu.addAction(self.scan_plugin_edits_action)

        # ── Translate menu ────────────────────────────────────────
        translate_menu = menubar.addMenu("Translate")

        self.batch_db_action = QAction("Batch DB (Names && Terms)", self)
        self.batch_db_action.setShortcut("Ctrl+D")
        self.batch_db_action.setToolTip(
            "Stage 1: Translate database names, descriptions, and system terms. "
            "QA these before translating dialogue."
        )
        self.batch_db_action.triggered.connect(self._batch_translate_db)
        self.batch_db_action.setEnabled(False)
        translate_menu.addAction(self.batch_db_action)

        self.batch_dialogue_action = QAction("Batch Dialogue", self)
        self.batch_dialogue_action.setShortcut("Ctrl+T")
        self.batch_dialogue_action.setToolTip(
            "Stage 2: Translate dialogue, events, and plugin text. "
            "Translated DB names are used as glossary terms."
        )
        self.batch_dialogue_action.triggered.connect(self._batch_translate_dialogue)
        self.batch_dialogue_action.setEnabled(False)
        translate_menu.addAction(self.batch_dialogue_action)

        self.batch_action = QAction("Batch All", self)
        self.batch_action.setShortcut("Ctrl+Shift+T")
        self.batch_action.setToolTip("Translate everything at once (DB + dialogue)")
        self.batch_action.triggered.connect(self._batch_translate)
        self.batch_action.setEnabled(False)
        translate_menu.addAction(self.batch_action)

        self.batch_actor_action = QAction("Batch by Actor", self)
        self.batch_actor_action.setShortcut("Ctrl+Shift+A")
        self.batch_actor_action.setToolTip(
            "Translate dialogue grouped by speaker — female speakers first, "
            "then male, then ungendered. Gives the LLM strong gender context."
        )
        self.batch_actor_action.triggered.connect(self._batch_translate_by_actor)
        self.batch_actor_action.setEnabled(False)
        translate_menu.addAction(self.batch_actor_action)

        translate_menu.addSeparator()

        self.stop_action = QAction("Stop", self)
        self.stop_action.triggered.connect(self._stop_translation)
        self.stop_action.setEnabled(False)
        translate_menu.addAction(self.stop_action)

        translate_menu.addSeparator()

        self.wordwrap_action = QAction("Wrap Text to Lines", self)
        self.wordwrap_action.setToolTip(
            "Redistribute translated text across lines to fit message window width"
        )
        self.wordwrap_action.triggered.connect(self._apply_wordwrap)
        self.wordwrap_action.setEnabled(False)
        translate_menu.addAction(self.wordwrap_action)

        self.find_replace_action = QAction("Find && Replace...", self)
        self.find_replace_action.setShortcut("Ctrl+H")
        self.find_replace_action.setToolTip("Find and replace text in translations")
        self.find_replace_action.triggered.connect(self.trans_table.show_replace_bar)
        self.find_replace_action.setEnabled(False)
        translate_menu.addAction(self.find_replace_action)

        self.cleanup_action = QAction("Clean Up Translations", self)
        self.cleanup_action.setToolTip(
            "Strip redundant dialogue quotes and fix contraction spacing (I 've → I've)"
        )
        self.cleanup_action.triggered.connect(self._cleanup_translations)
        self.cleanup_action.setEnabled(False)
        translate_menu.addAction(self.cleanup_action)

        translate_menu.addSeparator()

        # Post-Process submenu (experimental)
        postprocess_menu = translate_menu.addMenu("Post-Process (Experimental)")

        self.polish_action = QAction("Polish Grammar", self)
        self.polish_action.setToolTip(
            "Run all translations through the LLM for grammar and fluency cleanup"
        )
        self.polish_action.triggered.connect(self._polish_translations)
        self.polish_action.setEnabled(False)
        postprocess_menu.addAction(self.polish_action)

        self.consistency_action = QAction("Consistency Pass", self)
        self.consistency_action.setShortcut("Ctrl+Shift+C")
        self.consistency_action.setToolTip(
            "Fix name spelling variants, capitalization, and term inconsistencies"
        )
        self.consistency_action.triggered.connect(self._consistency_pass)
        self.consistency_action.setEnabled(False)
        postprocess_menu.addAction(self.consistency_action)

        translate_menu.addSeparator()

        self.translate_images_action = QAction(
            "Translate Images (Experimental)...", self)
        self.translate_images_action.setShortcut("Ctrl+I")
        self.translate_images_action.setToolTip(
            "OCR Japanese text from game images, translate, and render English overlays"
        )
        self.translate_images_action.triggered.connect(self._translate_images)
        self.translate_images_action.setEnabled(False)
        translate_menu.addAction(self.translate_images_action)

        # ── Glossary menu ─────────────────────────────────────────
        glossary_menu = menubar.addMenu("Glossary")

        self.edit_glossary_action = QAction("Edit Glossary...", self)
        self.edit_glossary_action.triggered.connect(self._open_glossary)
        glossary_menu.addAction(self.edit_glossary_action)

        glossary_menu.addSeparator()

        self.load_vocab_action = QAction("Import Vocab File...", self)
        self.load_vocab_action.setToolTip(
            "Import a DazedMTL-style vocab.txt into project glossary"
        )
        self.load_vocab_action.triggered.connect(self._load_vocab_file)
        self.load_vocab_action.setEnabled(False)
        glossary_menu.addAction(self.load_vocab_action)

        self.export_vocab_action = QAction("Export Vocab File...", self)
        self.export_vocab_action.setToolTip(
            "Export glossary as a DazedMTL-compatible vocab.txt"
        )
        self.export_vocab_action.triggered.connect(self._export_vocab_file)
        self.export_vocab_action.setEnabled(False)
        glossary_menu.addAction(self.export_vocab_action)

        glossary_menu.addSeparator()

        self.scan_glossary_action = QAction(
            "Scan Translated Game...", self)
        self.scan_glossary_action.setToolTip(
            "Open a translated game folder and harvest JP\u2192EN pairs "
            "to add to your general glossary"
        )
        self.scan_glossary_action.triggered.connect(
            self._scan_game_for_glossary)
        self.scan_glossary_action.setEnabled(False)
        glossary_menu.addAction(self.scan_glossary_action)

        self.scan_project_glossary_action = QAction(
            "Build from Translations", self)
        self.scan_project_glossary_action.setToolTip(
            "Scan this project's translations for terms to add "
            "to your general glossary"
        )
        self.scan_project_glossary_action.triggered.connect(
            self._scan_project_for_glossary)
        self.scan_project_glossary_action.setEnabled(False)
        glossary_menu.addAction(self.scan_project_glossary_action)

        glossary_menu.addSeparator()

        self.apply_glossary_action = QAction("Apply Glossary to All", self)
        self.apply_glossary_action.setToolTip(
            "Find translated entries where glossary terms are inconsistent "
            "and offer to fix them"
        )
        self.apply_glossary_action.triggered.connect(self._apply_glossary)
        self.apply_glossary_action.setEnabled(False)
        glossary_menu.addAction(self.apply_glossary_action)

        # ── Game menu ─────────────────────────────────────────────
        game_menu = menubar.addMenu("Game")

        self.export_action = QAction("Apply Translation to Game", self)
        self.export_action.setShortcut("Ctrl+E")
        self.export_action.setToolTip(
            "Write translated text into the game's data files"
        )
        self.export_action.triggered.connect(self._export_to_game)
        self.export_action.setEnabled(False)
        game_menu.addAction(self.export_action)

        self.restore_action = QAction("Restore Original Game Files", self)
        self.restore_action.setToolTip(
            "Restore backed-up Japanese originals to the game's data folder"
        )
        self.restore_action.triggered.connect(self._restore_originals)
        self.restore_action.setEnabled(False)
        game_menu.addAction(self.restore_action)

        self.open_rpgmaker_action = QAction("Open in RPG Maker", self)
        self.open_rpgmaker_action.setShortcut("Ctrl+R")
        self.open_rpgmaker_action.setToolTip(
            "Create a workspace project and open the game in RPG Maker for visual QA"
        )
        self.open_rpgmaker_action.triggered.connect(self._open_in_rpgmaker)
        self.open_rpgmaker_action.setEnabled(False)
        game_menu.addAction(self.open_rpgmaker_action)

        game_menu.addSeparator()

        self.txt_export_action = QAction("Export Raw Text...", self)
        self.txt_export_action.setToolTip(
            "Export all original and translated text to a plain text file"
        )
        self.txt_export_action.triggered.connect(self._export_txt)
        self.txt_export_action.setEnabled(False)
        game_menu.addAction(self.txt_export_action)

        self.create_patch_action = QAction("Share Translation Data...", self)
        self.create_patch_action.setToolTip(
            "Export translation mappings as a zip for other translators "
            "(no game data, copyright-safe)"
        )
        self.create_patch_action.triggered.connect(self._create_patch)
        self.create_patch_action.setEnabled(False)
        game_menu.addAction(self.create_patch_action)

        self.export_zip_action = QAction("Create Install Package...", self)
        self.export_zip_action.setToolTip(
            "Export translated game files + install.bat as a zip \u2014 "
            "end users just extract and run"
        )
        self.export_zip_action.triggered.connect(self._export_patch_zip)
        self.export_zip_action.setEnabled(False)
        game_menu.addAction(self.export_zip_action)

        self.export_folder_zip_action = QAction(
            "Create Patch from Game Folder...", self
        )
        self.export_folder_zip_action.setToolTip(
            "Package a game folder's current data/ files into a zip \u2014 "
            "works without a project, includes all existing translations"
        )
        self.export_folder_zip_action.triggered.connect(
            self._export_game_as_patch
        )
        game_menu.addAction(self.export_folder_zip_action)

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
        toolbar.addAction(self.batch_db_action)
        toolbar.addAction(self.batch_dialogue_action)
        toolbar.addAction(self.batch_action)
        toolbar.addAction(self.stop_action)
        toolbar.addSeparator()
        toolbar.addAction(self.export_action)

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
        self.trans_table.polish_requested.connect(self._polish_selected)
        self.trans_table.status_changed.connect(self._on_status_changed)
        self.trans_table.glossary_add.connect(self._on_glossary_add)

        # Engine
        self.engine.progress.connect(self._on_progress)
        self.engine.entry_done.connect(self._on_entry_done)
        self.engine.error.connect(self._on_error)
        self.engine.checkpoint.connect(self._on_checkpoint)
        self.engine.finished.connect(self._on_batch_finished)
        self.engine.calibrating.connect(self._on_calibrating)
        self.engine.calibration_done.connect(self._on_calibration_done)

    # ── Actions ────────────────────────────────────────────────────

    def _open_project(self):
        """Open an RPG Maker MV/MZ project folder."""
        path = QFileDialog.getExistingDirectory(
            self, "Select RPG Maker MV/MZ Project Folder"
        )
        if not path:
            return

        # Check for existing save state in project folder
        default_save = os.path.join(path, "_translation_state.json")
        autosave = os.path.join(path, "_translation_autosave.json")
        save_path = self._pick_newest_save(default_save, autosave)

        if save_path:
            reply = QMessageBox.question(
                self, "Resume Previous Session?",
                f"Found saved translation state:\n{os.path.basename(save_path)}\n\n"
                "Load it to resume where you left off?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                if self._restore_from_state(save_path):
                    self._enable_project_actions()
                    self.plugin_analyzer.analyze_project(path)
                    if getattr(self.client, "vision_model", ""):
                        self.image_panel.set_project(path, self.client)
                    # Count plugin entries for status message
                    plugin_count = sum(
                        1 for e in self.project.entries if e.file == "plugins.js"
                    )
                    plugin_info = (f" | +{plugin_count} plugin entries"
                                   if plugin_count else "")
                    self.statusbar.showMessage(
                        f"Resumed: {self.project.total} entries "
                        f"({self.project.translated_count} translated)"
                        f"{plugin_info}", 8000
                    )
                    folder = os.path.basename(path)
                    self.setWindowTitle(f"RPG Maker Translator \u2014 {folder}")
                    # Preload model into VRAM
                    if not self.client.is_cloud:
                        self._preload_model()
                    return
                # Fall through to fresh project on load failure

        # Fresh project — parse game files from scratch
        try:
            entries = self.parser.load_project(path)
        except FileNotFoundError as e:
            QMessageBox.warning(self, "Error", str(e))
            return

        self.project = TranslationProject(project_path=path, entries=entries)
        self.file_tree.load_project(self.project)
        self.trans_table.set_entries(entries)

        # Defer actor gender dialog + pre-translate to first batch start
        self._actors_ready = False

        # Check for vocab.txt first — if found and accepted, skip default glossary
        vocab_loaded = self._check_vocab_file(path)

        # Offer default glossary only if no vocab.txt was loaded
        if not vocab_loaded and not self._general_glossary:
            from ..default_glossary import get_all_defaults
            reply = QMessageBox.question(
                self, "Load Default Glossary?",
                "Would you like to load common term translations?\n\n"
                "This adds ~100 preset Japanese\u2192English mappings for body parts,\n"
                "RPG terms, expressions, etc. so the LLM translates them consistently.\n\n"
                "These go into the General Glossary (shared across all projects).\n"
                "You can edit them in Settings > General Glossary.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._general_glossary.update(get_all_defaults())
                self._save_settings()

        # Rebuild merged glossary (general + project auto-glossary entries)
        self._rebuild_glossary()

        # Analyze plugins for word wrap settings
        self.plugin_analyzer.analyze_project(path)

        self._enable_project_actions()

        # Initialize image panel if vision model is set
        if getattr(self.client, "vision_model", ""):
            self.image_panel.set_project(path, self.client)

        plugin_info = ""
        if self.plugin_analyzer.detected_plugins:
            plugin_info = f" | Plugins: {', '.join(self.plugin_analyzer.detected_plugins)}"
        self.statusbar.showMessage(
            f"Loaded {len(entries)} entries | "
            f"~{self.plugin_analyzer.chars_per_line} chars/line{plugin_info}", 8000
        )

        # Info about plugin entries
        plugin_count = sum(1 for e in entries if e.file == "plugins.js")
        if plugin_count > 0:
            QMessageBox.information(
                self, "Plugin Parameters",
                f"Found {plugin_count} translatable strings in plugins.js.\n\n"
                "Only values containing Japanese display text were extracted.\n"
                "Asset filenames and internal identifiers are skipped.\n\n"
                "Review the entries in the Plugins section of the file tree.\n"
                "Skip any entries that look like command triggers or tags\n"
                "rather than player-visible text.",
            )

        # Window title
        folder = os.path.basename(path)
        self.setWindowTitle(f"RPG Maker Translator \u2014 {folder}")

        # Preload model into VRAM so it's ready for translation
        if not self.client.is_cloud:
            self._preload_model()

    @staticmethod
    def _pick_newest_save(*paths: str) -> str | None:
        """Return the most recently modified path that exists, or None."""
        candidates = [(p, os.path.getmtime(p)) for p in paths if os.path.isfile(p)]
        if not candidates:
            return None
        return max(candidates, key=lambda x: x[1])[0]

    def _close_project(self):
        """Close the current project and reset to empty state."""
        if not self.project.entries:
            return

        reply = QMessageBox.question(
            self, "Close Project",
            "Close the current project?\n\n"
            "Make sure you have saved your state first.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Reset project
        self.project = TranslationProject()
        self.file_tree.load_project(self.project)
        self.trans_table.set_entries([])
        self._actors_ready = False
        self._last_save_path = ""

        # Clear client actor context
        self.client.actor_genders = {}
        self.client.actor_names = {}
        self.client.actor_context = ""

        # Disable project-dependent actions
        self.close_action.setEnabled(False)
        self.save_action.setEnabled(False)
        self.save_as_action.setEnabled(False)
        self.rename_action.setEnabled(False)
        self.import_action.setEnabled(False)
        self.import_folder_action.setEnabled(False)
        self.scan_plugin_edits_action.setEnabled(False)
        self.batch_db_action.setEnabled(False)
        self.batch_dialogue_action.setEnabled(False)
        self.batch_action.setEnabled(False)
        self.batch_actor_action.setEnabled(False)
        self.wordwrap_action.setEnabled(False)
        self.find_replace_action.setEnabled(False)
        self.cleanup_action.setEnabled(False)
        self.polish_action.setEnabled(False)
        self.consistency_action.setEnabled(False)
        self.translate_images_action.setEnabled(False)
        self.load_vocab_action.setEnabled(False)
        self.export_vocab_action.setEnabled(False)
        self.scan_glossary_action.setEnabled(False)
        self.scan_project_glossary_action.setEnabled(False)
        self.apply_glossary_action.setEnabled(False)
        self.export_action.setEnabled(False)
        self.restore_action.setEnabled(False)
        self.open_rpgmaker_action.setEnabled(False)
        self.txt_export_action.setEnabled(False)
        self.create_patch_action.setEnabled(False)
        self.export_zip_action.setEnabled(False)

        self.setWindowTitle("RPG Maker Translator")
        self.statusbar.showMessage("Project closed.", 5000)

    def _pre_translate_info(self, entries, actors_raw):
        """Translate game title + actor names/profiles before the gender dialog.

        Uses batch mode when batch_size > 1 (DazedMTL mode / cloud APIs)
        to translate all names in 1-2 API calls instead of one per field.

        Returns:
            (actor_translations, translated_title) where actor_translations is
            {actor_id: {"name": ..., "nickname": ..., "profile": ...}}
        """
        # Check availability first
        if not self.client.is_available():
            return {}, ""

        entry_by_id = {e.id: e for e in entries}
        batch_size = self.engine.batch_size if self.engine else 1

        # Find game title entry
        title_entry = None
        for e in entries:
            if e.id == "System.json/gameTitle":
                title_entry = e
                break

        # Collect all items that need translation (skip already-translated)
        translated_title = ""
        actor_translations = {}
        to_translate = []  # (key, text, hint) for batch

        if title_entry:
            if title_entry.status in ("translated", "reviewed"):
                translated_title = title_entry.translation
            else:
                to_translate.append(("gameTitle", title_entry.original, "game title"))

        field_hints = {
            "name": "character's personal name",
            "nickname": "character's nickname or title",
            "profile": "character's biography",
        }
        for actor in actors_raw:
            aid = actor["id"]
            if aid not in actor_translations:
                actor_translations[aid] = {}
            for field in ("name", "nickname", "profile"):
                text = actor.get(field, "")
                if not text:
                    continue
                entry_id = f"Actors.json/{aid}/{field}"
                entry = entry_by_id.get(entry_id)
                if entry and entry.status in ("translated", "reviewed") and entry.translation:
                    actor_translations[aid][field] = entry.translation
                    continue
                to_translate.append((f"actor_{aid}_{field}", text, field_hints[field]))

        if not to_translate:
            return actor_translations, translated_title

        progress = QProgressDialog(
            "Translating character info...", "Skip", 0, len(to_translate), self
        )
        progress.setWindowTitle("Pre-translating")
        progress.setMinimumDuration(0)
        progress.setValue(0)

        # ── Batch mode: send all names in chunks ──
        if batch_size > 1:
            progress.setLabelText(
                f"Batch translating {len(to_translate)} names..."
            )
            QApplication.processEvents()

            results = {}
            for i in range(0, len(to_translate), batch_size):
                if progress.wasCanceled():
                    break
                chunk = to_translate[i:i + batch_size]
                progress.setLabelText(
                    f"Translating names {i + 1}-{min(i + len(chunk), len(to_translate))} "
                    f"of {len(to_translate)}..."
                )
                QApplication.processEvents()
                batch_results = self.client.translate_names_batch(chunk)
                results.update(batch_results)
                progress.setValue(min(i + len(chunk), len(to_translate)))
                QApplication.processEvents()

            # Apply batch results
            for key, text, _hint in to_translate:
                translated = results.get(key, "")
                if not translated:
                    continue
                if key == "gameTitle":
                    translated_title = translated
                    if title_entry and title_entry.status == "untranslated":
                        title_entry.translation = translated
                        title_entry.status = "translated"
                elif key.startswith("actor_"):
                    _, aid_str, field = key.split("_", 2)
                    aid = int(aid_str)
                    if aid not in actor_translations:
                        actor_translations[aid] = {}
                    actor_translations[aid][field] = translated
                    entry_id = f"Actors.json/{aid}/{field}"
                    entry = entry_by_id.get(entry_id)
                    if entry and entry.status == "untranslated":
                        entry.translation = translated
                        entry.status = "translated"

        # ── Single mode: one API call per name ──
        else:
            for idx, (key, text, hint) in enumerate(to_translate):
                progress.setLabelText(f"Translating {hint}...")
                QApplication.processEvents()
                if progress.wasCanceled():
                    break

                result = self.client.translate_name(text, hint=hint)
                if result and result != text:
                    if key == "gameTitle":
                        translated_title = result
                        if title_entry and title_entry.status == "untranslated":
                            title_entry.translation = result
                            title_entry.status = "translated"
                    elif key.startswith("actor_"):
                        _, aid_str, field = key.split("_", 2)
                        aid = int(aid_str)
                        if aid not in actor_translations:
                            actor_translations[aid] = {}
                        actor_translations[aid][field] = result
                        entry_id = f"Actors.json/{aid}/{field}"
                        entry = entry_by_id.get(entry_id)
                        if entry and entry.status == "untranslated":
                            entry.translation = result
                            entry.status = "translated"
                progress.setValue(idx + 1)

        progress.close()
        return actor_translations, translated_title

    def _ensure_actors_ready(self) -> bool:
        """Run actor pre-translate + gender dialog once before first batch.

        Returns True if ready to proceed, False if user cancelled or
        no project is open.
        """
        if self._actors_ready:
            return True

        path = self.project.project_path
        if not path:
            return False

        entries = self.project.entries
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
        if not translated_title and raw_title and not has_jp_title:
            translated_title = raw_title

        # Auto-glossary: add translated actor names to project glossary
        for aid, tl in actor_translations.items():
            for field_name in ("name", "nickname"):
                en = tl.get(field_name, "")
                if not en:
                    continue
                actor = next((a for a in actors_raw if a["id"] == aid), None)
                if not actor:
                    continue
                jp = actor.get(field_name, "")
                if jp and en != jp and jp not in self.project.glossary:
                    self.project.glossary[jp] = en

        # Apply vocab.txt gender overrides (if loaded)
        if hasattr(self, "_vocab_genders") and self._vocab_genders:
            for actor in actors_raw:
                jp_name = actor.get("name", "")
                if jp_name in self._vocab_genders:
                    actor["auto_gender"] = self._vocab_genders[jp_name]

        # Show gender assignment dialog with translated names
        if actors_raw:
            dlg = ActorGenderDialog(actors_raw, self, translations=actor_translations)
            if dlg.exec():
                genders = dlg.get_genders()
            else:
                genders = {a["id"]: a["auto_gender"] for a in actors_raw
                           if a["auto_gender"] != "unknown"}
            actor_ctx = self.parser.build_actor_context(actors_raw, genders)
            self.client.actor_context = actor_ctx
            self.client.actor_genders = genders
            self.client.actor_names = {a["id"]: a["name"] for a in actors_raw}
            self.project.actor_genders = genders
        else:
            self.client.actor_context = ""
            self.client.actor_genders = {}
            self.client.actor_names = {}

        # Rebuild glossary with any new actor name entries
        self._rebuild_glossary()

        # Offer to rename folder to English title (only on first run)
        if translated_title:
            new_path = self._rename_project_folder(path, translated_title)
            self.project.project_path = new_path

        self._actors_ready = True
        return True

    def _backfill_db_glossary(self) -> int:
        """Add DB name glossary entries from already-translated entries.

        Scans translated name fields from all database files (Actors, Items,
        Weapons, Armors, Skills, Enemies, States, Classes) and adds missing
        glossary mappings so the LLM uses consistent terms in dialogue.

        Called on load_state to handle projects saved before auto-glossary
        covered all DB types (or before this feature existed at all).
        Writes directly to project.glossary (doesn't require client.glossary).

        Returns the number of entries added.
        """
        if not self.project:
            return 0
        before = len(self.project.glossary)
        for entry in self.project.entries:
            fields = self._AUTO_GLOSSARY_FIELDS.get(entry.file)
            is_map_name = (
                entry.file.startswith("Map")
                and entry.file.endswith(".json")
                and entry.field == self._AUTO_GLOSSARY_MAP_FIELD
            )
            if not is_map_name and (not fields or entry.field not in fields):
                continue
            jp = entry.original
            en = entry.translation
            if jp and en and jp != en and jp not in self.project.glossary:
                self.project.glossary[jp] = en
        return len(self.project.glossary) - before

    def _title_case(self, text: str) -> str:
        """Title-case text, keeping prepositions/articles lowercase.

        First word is always capitalized.  Uses _TITLE_SMALL_WORDS set.
        """
        words = text.split(" ")
        result = []
        for i, w in enumerate(words):
            if not w:
                result.append(w)
            elif i == 0 or w.lower() not in self._TITLE_SMALL_WORDS:
                result.append(w[0].upper() + w[1:])
            else:
                result.append(w.lower())
        return " ".join(result)

    def _maybe_add_to_glossary(self, entry):
        """Auto-add translated DB name fields to glossary for LLM consistency."""
        fields = self._AUTO_GLOSSARY_FIELDS.get(entry.file)
        is_map_name = (
            entry.file.startswith("Map")
            and entry.file.endswith(".json")
            and entry.field == self._AUTO_GLOSSARY_MAP_FIELD
        )
        is_speaker = entry.field == "speaker_name"
        if not is_map_name and not is_speaker and (not fields or entry.field not in fields):
            return
        jp = entry.original
        en = entry.translation
        if jp and en and jp != en and jp not in self.client.glossary:
            self.client.glossary[jp] = en
            self.project.glossary[jp] = en

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
            # Update autosave path only if it's inside the old folder
            if self._last_save_path and os.path.dirname(self._last_save_path) == path:
                self._last_save_path = os.path.join(
                    new_path, os.path.basename(self._last_save_path)
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
            old_path = self.project.project_path
            os.rename(old_path, new_path)
            self.project.project_path = new_path
            # Update autosave path only if it's inside the old folder
            if self._last_save_path and os.path.dirname(self._last_save_path) == old_path:
                self._last_save_path = os.path.join(
                    new_path, os.path.basename(self._last_save_path)
                )
            self.setWindowTitle(f"RPG Maker Translator \u2014 {new_name}")
            self.statusbar.showMessage(f"Renamed folder to: {new_name}", 5000)
        except OSError as e:
            QMessageBox.warning(self, "Rename Failed",
                                f"Could not rename folder:\n{e}")

    def _save_state(self):
        """Save state to default project path (no dialog)."""
        if not self.project.entries:
            return
        if not self._last_save_path:
            if self.project.project_path:
                self._last_save_path = os.path.join(
                    self.project.project_path, "_translation_state.json"
                )
            else:
                self._save_state_as()
                return
        self.project.save_state(self._last_save_path)
        self.statusbar.showMessage(
            f"Saved to {os.path.basename(self._last_save_path)}", 3000)

    def _save_state_as(self):
        """Save state with file dialog for custom location."""
        default_dir = self.project.project_path or ""
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Translation State", default_dir, "JSON Files (*.json)"
        )
        if path:
            self.project.save_state(path)
            self._last_save_path = path
            self.statusbar.showMessage(f"State saved to {path}", 3000)

    def _load_state(self):
        """Load a previously saved translation state via file dialog."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Translation State", "", "JSON Files (*.json)"
        )
        if not path:
            return
        if not self._restore_from_state(path):
            return
        self._enable_project_actions()

        # Initialize image panel if vision model is set
        if self.project.project_path and getattr(self.client, "vision_model", ""):
            self.image_panel.set_project(self.project.project_path, self.client)

        self.statusbar.showMessage(
            f"Loaded state: {self.project.total} entries "
            f"({self.project.translated_count} translated)", 5000
        )
        name = os.path.basename(self.project.project_path) if self.project.project_path else "Restored"
        self.setWindowTitle(f"RPG Maker Translator \u2014 {name}")

    def _restore_from_state(self, path: str) -> bool:
        """Load a save file and restore full project state.

        Returns True on success, False on error (shows warning dialog).
        """
        try:
            self.project = TranslationProject.load_state(path)
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to load state:\n{e}")
            return False

        # If saved project_path is stale (folder renamed/moved), update it
        # to the directory containing the save file (works for saves inside
        # the project folder like _translation_state.json).
        if self.project.project_path and not os.path.isdir(self.project.project_path):
            save_dir = os.path.dirname(os.path.abspath(path))
            if self.parser._find_data_dir(save_dir):
                self.project.project_path = save_dir

        # Merge plugin entries that didn't exist when the state was saved
        # (e.g. saved before plugin extraction was enabled).
        if self.project.project_path:
            self._merge_new_plugin_entries()

        self.file_tree.load_project(self.project)
        self.trans_table.set_entries(self.project.entries)

        # Check for vocab.txt in project folder
        if self.project.project_path:
            self._check_vocab_file(self.project.project_path)

        self._rebuild_glossary()

        # Restore actor context from saved genders (skip dialog on next batch)
        if self.project.actor_genders and self.project.project_path:
            actors_raw = self.parser.load_actors_raw(self.project.project_path)
            if actors_raw:
                self.client.actor_context = self.parser.build_actor_context(
                    actors_raw, self.project.actor_genders
                )
                self.client.actor_genders = self.project.actor_genders
                self.client.actor_names = {a["id"]: a["name"] for a in actors_raw}
            self._actors_ready = True
        else:
            self._actors_ready = False

        self._backfill_db_glossary()
        self._last_save_path = path
        return True

    def _merge_new_plugin_entries(self) -> int:
        """Extract plugin + System.json entries and merge any missing from project.

        Handles saves created before plugin extraction or new System.json
        term fields (params, basic) were added — new entries are appended
        without duplicating existing ones.

        Returns the number of entries added.
        """
        existing_ids = {e.id for e in self.project.entries}
        new_entries = self.parser._parse_plugins(self.project.project_path)
        # Also re-parse System.json for newly supported term fields
        data_dir = self.parser._find_data_dir(self.project.project_path)
        if data_dir:
            new_entries.extend(self.parser._parse_system(data_dir))
        added = [e for e in new_entries if e.id not in existing_ids]
        if added:
            self.project.entries.extend(added)
        return len(added)

    def _enable_project_actions(self):
        """Enable all project-dependent menu actions."""
        has_path = bool(self.project.project_path)
        # Project
        self.close_action.setEnabled(True)
        self.save_action.setEnabled(True)
        self.save_as_action.setEnabled(True)
        self.rename_action.setEnabled(has_path)
        self.import_action.setEnabled(True)
        self.import_folder_action.setEnabled(True)
        self.scan_plugin_edits_action.setEnabled(True)
        # Translate
        self.batch_db_action.setEnabled(True)
        self.batch_dialogue_action.setEnabled(True)
        self.batch_action.setEnabled(True)
        self.batch_actor_action.setEnabled(True)
        self.wordwrap_action.setEnabled(True)
        self.find_replace_action.setEnabled(True)
        self.cleanup_action.setEnabled(True)
        self.polish_action.setEnabled(True)
        self.consistency_action.setEnabled(True)
        self.translate_images_action.setEnabled(True)
        # Glossary
        self.load_vocab_action.setEnabled(True)
        self.export_vocab_action.setEnabled(True)
        self.scan_glossary_action.setEnabled(True)
        self.scan_project_glossary_action.setEnabled(True)
        self.apply_glossary_action.setEnabled(True)
        # Game
        self.export_action.setEnabled(has_path)
        self.restore_action.setEnabled(has_path)
        self.open_rpgmaker_action.setEnabled(has_path)
        self.txt_export_action.setEnabled(True)
        self.create_patch_action.setEnabled(True)
        self.export_zip_action.setEnabled(True)

    def _preload_model(self):
        """Unload stale models and load the active one into VRAM.

        Clears any previously loaded models first to free VRAM,
        then sends a blank request with keep_alive=-1 so the model
        stays resident and ready for instant inference.
        """
        # Clear other models from VRAM first
        unloaded = self.client.unload_models()
        if unloaded:
            self.statusbar.showMessage(
                f"Cleared {unloaded} model(s) from VRAM, loading {self.client.model}...", 3000
            )
        else:
            self.statusbar.showMessage(
                f"Loading {self.client.model} into VRAM...", 3000
            )
        QApplication.processEvents()
        ok = self.client.preload_model()
        if ok:
            self.statusbar.showMessage(
                f"{self.client.model} loaded — ready to translate", 5000
            )
        else:
            self.statusbar.showMessage(
                f"Could not preload {self.client.model} — will load on first translate", 5000
            )

    def _import_translations(self):
        """Import translations from an older version's save state."""
        if not self.project:
            return

        path, _ = QFileDialog.getOpenFileName(
            self, "Select Old Translation State", "", "JSON Files (*.json)"
        )
        if not path:
            return

        try:
            old_project = TranslationProject.load_state(path)
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to load old state:\n{e}")
            return

        old_translated = sum(
            1 for e in old_project.entries
            if e.status in ("translated", "reviewed")
        )
        current_untranslated = self.project.untranslated_count

        reply = QMessageBox.question(
            self, "Import Translations",
            f"Old project: {len(old_project.entries)} entries "
            f"({old_translated} translated)\n"
            f"Current project: {self.project.total} entries "
            f"({current_untranslated} untranslated)\n\n"
            f"Import matching translations into untranslated entries?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        stats = self.project.import_translations(old_project)

        # Also import glossary entries that don't conflict
        imported_glossary = 0
        for jp, en in old_project.glossary.items():
            if jp not in self.project.glossary:
                self.project.glossary[jp] = en
                imported_glossary += 1
        self._rebuild_glossary()

        # Refresh UI
        self.trans_table.set_entries(self.project.entries)
        self.file_tree.load_project(self.project)

        total_imported = stats["by_id"] + stats["by_text"]
        QMessageBox.information(
            self, "Import Complete",
            f"Imported {total_imported} translations:\n"
            f"  \u2022 {stats['by_id']} matched by exact position\n"
            f"  \u2022 {stats['by_text']} matched by identical text\n"
            f"  \u2022 {stats['new']} new entries (need translation)\n"
            f"  \u2022 {stats['skipped']} already translated (kept)\n"
            + (f"  \u2022 {imported_glossary} glossary entries imported\n"
               if imported_glossary else "")
        )

    def _import_from_game_folder(self):
        """Import translations from an already-translated game folder."""
        if not self.project:
            return

        folder = QFileDialog.getExistingDirectory(
            self, "Select Game Folder to Import From"
        )
        if not folder:
            return

        from ..rpgmaker_mv import RPGMakerMVParser, _has_japanese

        parser = RPGMakerMVParser()
        try:
            donor_entries = parser.load_project_raw(folder)
        except FileNotFoundError as e:
            QMessageBox.warning(self, "Error", str(e))
            return
        except Exception as e:
            QMessageBox.warning(
                self, "Error", f"Failed to parse game folder:\n{e}"
            )
            return

        if not donor_entries:
            QMessageBox.warning(
                self, "No Entries",
                "No text entries found in that game folder."
            )
            return

        # Detect if columns would be swapped: donor has JP text but
        # project originals are non-JP (user opened the translated game
        # and is importing the JP original).
        swap = False
        donor_by_id = {e.id: e.original for e in donor_entries}
        sample_donor_jp = 0
        sample_proj_jp = 0
        sample_count = 0
        for entry in self.project.entries:
            if entry.status != "untranslated":
                continue
            dt = donor_by_id.get(entry.id)
            if dt is None or dt == entry.original:
                continue
            if _has_japanese(dt):
                sample_donor_jp += 1
            if _has_japanese(entry.original):
                sample_proj_jp += 1
            sample_count += 1
            if sample_count >= 50:
                break

        if sample_count > 0 and sample_donor_jp > sample_proj_jp:
            # Donor looks more Japanese than project — likely reversed
            reply = QMessageBox.question(
                self, "Import — Column Order",
                "The selected folder appears to contain the Japanese "
                "original, while your project contains the translated "
                "text.\n\n"
                "Swap columns so the Japanese text becomes the Original "
                "and your current text becomes the Translation?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            swap = (reply == QMessageBox.StandardButton.Yes)

        current_untranslated = self.project.untranslated_count
        reply = QMessageBox.question(
            self, "Import from Game Folder",
            f"Donor game: {len(donor_entries)} text entries\n"
            f"Current project: {self.project.total} entries "
            f"({current_untranslated} untranslated)\n\n"
            "This will match entries by file position and import\n"
            "translations where the text differs.\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Build structural translation map for cross-version matching
        text_map = {}
        try:
            text_map = parser.build_cross_version_map(
                folder, self.project.project_path)
        except Exception:
            pass  # Fall back to ID-only matching

        stats = self.project.import_from_game_folder(
            donor_entries, swap=swap, text_map=text_map)

        # Refresh UI
        self.trans_table.set_entries(self.project.entries)
        self.file_tree.load_project(self.project)

        total_imported = stats['by_text'] + stats['imported']
        QMessageBox.information(
            self, "Import Complete",
            f"Imported {total_imported} translations:\n"
            f"  \u2022 {stats['by_text']} matched by structure (cross-version safe)\n"
            f"  \u2022 {stats['imported']} matched by ID (database entries)\n"
            f"  \u2022 {stats['identical']} identical (not translated in donor)\n"
            f"  \u2022 {stats['new']} new entries (need translation)\n"
            f"  \u2022 {stats['skipped']} already translated (kept)\n"
        )

    # ── Scan plugin edits ────────────────────────────────────────

    def _scan_plugin_edits(self):
        """Compare a selected original plugins.js vs the project's plugins.js."""
        if not self.project or not self.project.project_path:
            return

        from ..rpgmaker_mv import RPGMakerMVParser
        from .plugin_diff_dialog import PluginDiffDialog
        from ..project_model import TranslationEntry

        parser = RPGMakerMVParser()

        # Try auto-detect first (plugins_original.js as JP backup)
        diffs = parser.diff_plugins(self.project.project_path)

        if not diffs:
            # No backup found or no diffs — ask user to pick the other file
            default_dir = self.project.project_path
            for sub in ("js", os.path.join("www", "js")):
                candidate = os.path.join(self.project.project_path,
                                         sub, "plugins.js")
                if os.path.isfile(candidate):
                    default_dir = os.path.dirname(candidate)
                    break

            other_path, _ = QFileDialog.getOpenFileName(
                self, "Select plugins.js to compare against",
                default_dir,
                "JavaScript Files (*.js);;All Files (*)",
            )
            if not other_path:
                return

            diffs = parser.diff_plugins(self.project.project_path,
                                        other_path=other_path)

        if not diffs:
            QMessageBox.information(
                self, "Scan Plugin Edits",
                "No parameter differences found between\n"
                f"the selected file and the project's plugins.js."
            )
            return

        dlg = PluginDiffDialog(diffs, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        accepted = dlg.accepted_diffs()
        if not accepted:
            return

        # Build set of existing entry IDs to avoid duplicates
        existing_ids = {e.id for e in self.project.entries}

        added = 0
        skipped = 0
        for entry_id, original, translation in accepted:
            if entry_id in existing_ids:
                skipped += 1
                continue
            entry = TranslationEntry(
                id=entry_id,
                file="plugins.js",
                field="plugin_param",
                original=original,
                translation=translation,
                status="translated",
            )
            self.project.entries.append(entry)
            existing_ids.add(entry_id)
            added += 1

        if added:
            # Invalidate cached index so tree view sees new file
            self.project._build_index()
            self.trans_table.set_entries(self.project.entries)
            self.file_tree.load_project(self.project)

        msg = f"Imported {added} plugin translations."
        if skipped:
            msg += f"\n{skipped} entries skipped (already in project)."
        QMessageBox.information(self, "Scan Plugin Edits", msg)

    # ── Glossary scan from translated game ─────────────────────────

    # Fields worth harvesting as glossary terms (short names / labels)
    _GLOSSARY_SCAN_FIELDS = {
        "Actors.json": ("name", "nickname"),
        "Classes.json": ("name",),
        "Items.json": ("name",),
        "Weapons.json": ("name",),
        "Armors.json": ("name",),
        "Skills.json": ("name",),
        "Enemies.json": ("name",),
        "States.json": ("name",),
        "System.json": ("terms",),
    }

    # ── Vocab.txt support (DazedMTL format) ──────────────────────

    _VOCAB_FILENAMES = ("vocab.txt", "Vocab.txt", "VOCAB.txt")

    @staticmethod
    def _parse_vocab_file(filepath: str) -> tuple[dict, dict]:
        """Parse a DazedMTL-style vocab.txt.

        Format: ``JP (EN)`` or ``JP (EN) - Gender``

        Returns:
            (glossary_dict, gender_dict)
            glossary_dict: {jp_text: en_text}
            gender_dict:   {jp_name: "female"|"male"|"unknown"}
        """
        import re
        pattern = re.compile(
            r'^(.+?)\s*\((.+?)\)(?:\s*-\s*(Female|Male))?\s*$',
            re.IGNORECASE,
        )
        glossary = {}
        genders = {}
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("```"):
                    continue
                # Skip description / header lines
                if line.startswith("Here are") or line.startswith("\\N["):
                    continue
                m = pattern.match(line)
                if not m:
                    continue
                jp = m.group(1).strip()
                en = m.group(2).strip()
                gender = m.group(3)
                if jp and en:
                    glossary[jp] = en
                    if gender:
                        genders[jp] = gender.lower()
        return glossary, genders

    def _check_vocab_file(self, project_path: str) -> bool:
        """Auto-detect vocab.txt in project folder and offer to import.

        Returns True if vocab was found and the user accepted (so caller
        can skip the default glossary prompt).
        """
        vocab_path = None
        for name in self._VOCAB_FILENAMES:
            candidate = os.path.join(project_path, name)
            if os.path.isfile(candidate):
                vocab_path = candidate
                break
        if not vocab_path:
            return False

        try:
            glossary, genders = self._parse_vocab_file(vocab_path)
        except (OSError, UnicodeDecodeError):
            return False

        if not glossary:
            return False

        reply = QMessageBox.question(
            self, "Vocab File Detected",
            f"Found {os.path.basename(vocab_path)} with {len(glossary)} terms"
            + (f" and {len(genders)} character genders" if genders else "")
            + ".\n\nLoad into project glossary instead of default glossary?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return False

        # Replace project glossary: vocab first, then backfill project terms
        self.project.glossary.clear()
        for jp, en in glossary.items():
            self.project.glossary[jp] = en
        # Re-add auto-glossary from already-translated DB entries on top
        backfilled = self._backfill_db_glossary()

        # Store gender info for actor detection
        if genders:
            if not hasattr(self, "_vocab_genders"):
                self._vocab_genders = {}
            for jp_name, gender in genders.items():
                en_name = glossary.get(jp_name, jp_name)
                self._vocab_genders[jp_name] = gender
                self._vocab_genders[en_name] = gender

        self.statusbar.showMessage(
            f"Loaded {len(glossary)} vocab terms"
            + (f" + {backfilled} project terms" if backfilled else "")
            + (f" + {len(genders)} genders" if genders else ""),
            5000,
        )
        return True

    def _load_vocab_file(self):
        """Manually load a vocab.txt file."""
        if not self.project:
            return

        filepath, _ = QFileDialog.getOpenFileName(
            self, "Select Vocab File", "",
            "Text Files (*.txt);;All Files (*)"
        )
        if not filepath:
            return

        try:
            glossary, genders = self._parse_vocab_file(filepath)
        except (OSError, UnicodeDecodeError) as e:
            QMessageBox.warning(self, "Error", f"Failed to read file:\n{e}")
            return

        if not glossary:
            QMessageBox.information(
                self, "No Terms Found",
                "No glossary terms found in that file.\n"
                "Expected format: Japanese (English) or Japanese (English) - Gender"
            )
            return

        added = 0
        for jp, en in glossary.items():
            if jp not in self.project.glossary:
                self.project.glossary[jp] = en
                added += 1

        if genders:
            if not hasattr(self, "_vocab_genders"):
                self._vocab_genders = {}
            for jp_name, gender in genders.items():
                en_name = glossary.get(jp_name, jp_name)
                self._vocab_genders[jp_name] = gender
                self._vocab_genders[en_name] = gender

        self._rebuild_glossary()

        QMessageBox.information(
            self, "Vocab Loaded",
            f"Added {added} terms to project glossary"
            + (f" + {len(genders)} character genders" if genders else "")
            + f"\n({len(glossary) - added} already existed)"
        )

    def _export_vocab_file(self):
        """Export glossary as a DazedMTL-compatible vocab.txt."""
        if not self.project:
            return

        # Merge general + project glossary (project overrides general)
        merged = {}
        if hasattr(self, "_general_glossary") and self._general_glossary:
            merged.update(self._general_glossary)
        if self.project.glossary:
            merged.update(self.project.glossary)

        if not merged:
            QMessageBox.information(
                self, "Export Vocab",
                "No glossary terms to export."
            )
            return

        # Build gender lookup from actor_genders + actor entries
        genders = {}
        if self.project.actor_genders:
            # actor_genders: {actor_id: "female"/"male"/"unknown"}
            for entry in self.project.entries:
                if entry.file == "Actors.json" and entry.field == "name":
                    # Extract actor ID from entry.id
                    # Format: Actors.json/n/name
                    import re as _re
                    m = _re.search(r'/(\d+)/', entry.id)
                    if m:
                        actor_id = int(m.group(1))
                        gender = self.project.actor_genders.get(actor_id)
                        if gender and gender != "unknown":
                            jp_name = entry.original
                            en_name = entry.translation or jp_name
                            genders[jp_name] = gender.capitalize()
                            genders[en_name] = gender.capitalize()

        # Default path
        default_dir = self.project.project_path or ""
        default_path = os.path.join(default_dir, "vocab.txt") if default_dir else "vocab.txt"

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Vocab File", default_path,
            "Text Files (*.txt);;All Files (*)",
        )
        if not path:
            return

        lines = []
        for jp, en in sorted(merged.items()):
            gender = genders.get(jp, "")
            if gender:
                lines.append(f"{jp} ({en}) - {gender}")
            else:
                lines.append(f"{jp} ({en})")

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        QMessageBox.information(
            self, "Export Vocab",
            f"Exported {len(lines)} terms to:\n{path}"
        )

    def _scan_game_for_glossary(self):
        """Scan a translated game folder and harvest JP→EN pairs for glossary."""
        if not self.project:
            return

        folder = QFileDialog.getExistingDirectory(
            self, "Select Translated Game Folder"
        )
        if not folder:
            return

        from ..rpgmaker_mv import RPGMakerMVParser, _has_japanese

        parser = RPGMakerMVParser()
        try:
            donor_entries = parser.load_project_raw(folder)
        except FileNotFoundError as e:
            QMessageBox.warning(self, "Error", str(e))
            return
        except Exception as e:
            QMessageBox.warning(
                self, "Error", f"Failed to parse game folder:\n{e}")
            return

        if not donor_entries:
            QMessageBox.warning(
                self, "No Entries",
                "No text entries found in that game folder."
            )
            return

        # Build lookup from current project: entry_id → JP original
        jp_by_id = {e.id: e.original for e in self.project.entries}

        # Match donor entries against project entries to find JP→EN pairs
        candidates = []
        seen = set()
        for donor in donor_entries:
            jp_text = jp_by_id.get(donor.id)
            if not jp_text:
                continue
            en_text = donor.original  # "original" in raw parse = the EN text
            if not en_text or not jp_text:
                continue
            jp_text = jp_text.strip()
            en_text = en_text.strip()
            if not jp_text or not en_text:
                continue
            # Skip if identical (wasn't translated)
            if jp_text == en_text:
                continue
            # JP must contain Japanese, EN must not
            if not _has_japanese(jp_text) or _has_japanese(en_text):
                continue
            # DB name fields and map names are always glossary-worthy
            is_db_field = False
            fields = self._GLOSSARY_SCAN_FIELDS.get(donor.file)
            if fields and donor.field in fields:
                is_db_field = True
            is_map_name = (
                donor.file.startswith("Map") and donor.file.endswith(".json")
                and donor.field == "displayName"
            )
            if not is_db_field and not is_map_name:
                continue
            # Skip if already in general glossary
            if jp_text in self._general_glossary:
                continue
            # Deduplicate
            if jp_text in seen:
                continue
            seen.add(jp_text)
            candidates.append((jp_text, en_text))

        if not candidates:
            QMessageBox.information(
                self, "No New Terms",
                "No new glossary candidates found.\n"
                "All matching terms are already in your general glossary."
            )
            return

        from .glossary_scan_dialog import GlossaryScanDialog

        dlg = GlossaryScanDialog(candidates, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        selected = dlg.selected_pairs()
        if not selected:
            return

        # Add to general glossary
        for jp, en in selected:
            self._general_glossary[jp] = en

        self._rebuild_glossary()
        self._save_settings()

        QMessageBox.information(
            self, "Glossary Updated",
            f"Added {len(selected)} terms to your general glossary."
        )

    def _scan_project_for_glossary(self):
        """Scan current project's translations for glossary candidates."""
        if not self.project:
            return

        from ..rpgmaker_mv import _has_japanese

        candidates = []
        seen = set()
        for e in self.project.entries:
            if e.status not in ("translated", "reviewed"):
                continue
            jp = e.original.strip()
            en = e.translation.strip() if e.translation else ""
            if not jp or not en or jp == en:
                continue
            if not _has_japanese(jp) or _has_japanese(en):
                continue
            # DB name fields and map names are always glossary-worthy
            is_db_field = False
            fields = self._GLOSSARY_SCAN_FIELDS.get(e.file)
            if fields and e.field in fields:
                is_db_field = True
            is_map_name = (
                e.file.startswith("Map") and e.file.endswith(".json")
                and e.field == "displayName"
            )
            if not is_db_field and not is_map_name:
                continue
            if jp in self._general_glossary:
                continue
            if jp in seen:
                continue
            seen.add(jp)
            candidates.append((jp, en))

        if not candidates:
            QMessageBox.information(
                self, "No New Terms",
                "No new glossary candidates found.\n"
                "All qualifying terms are already in your general glossary."
            )
            return

        from .glossary_scan_dialog import GlossaryScanDialog

        dlg = GlossaryScanDialog(candidates, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        selected = dlg.selected_pairs()
        if not selected:
            return

        for jp, en in selected:
            self._general_glossary[jp] = en

        self._rebuild_glossary()
        self._save_settings()

        QMessageBox.information(
            self, "Glossary Updated",
            f"Added {len(selected)} terms to your general glossary."
        )

    def _stop_translation(self):
        """Cancel the running batch translation."""
        self._batch_all_chained = False  # Don't auto-chain to dialogue
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

        # Confirmation dialog with checkbox
        from PyQt6.QtWidgets import QCheckBox, QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle("Apply Translation to Game")
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(
            f"This will overwrite {len(set(e.file for e in translated))} "
            f"file(s) in:\n{self.project.project_path}\n\n"
            f"Original files will be backed up to data_original/ "
            f"(first export only)."
        ))
        checkbox = QCheckBox("I understand this will modify my game files")
        layout.addWidget(checkbox)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setEnabled(False)
        checkbox.toggled.connect(ok_btn.setEnabled)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        try:
            self.parser.save_project(self.project.project_path, self.project.entries)
            # Inject word wrap plugin if requested or if <WordWrap> tags are present
            plugin_msg = ""
            needs_inject = self.plugin_analyzer.should_inject_plugin()
            if not needs_inject and not self.plugin_analyzer.has_wordwrap_plugin:
                # Auto-detect: check if any translation has <WordWrap> tags
                needs_inject = any(
                    e.translation and "<WordWrap>" in e.translation
                    for e in translated
                )
            if needs_inject:
                if self.parser.inject_wordwrap_plugin(self.project.project_path):
                    plugin_msg = "\nWord wrap plugin (TranslatorWordWrap.js) injected."
                else:
                    plugin_msg = ("\nWARNING: Could not inject word wrap plugin — "
                                  "js/plugins.js not found. <WordWrap> tags will "
                                  "show as raw text. Use Strip Word Wrap Tags to remove.")
            QMessageBox.information(
                self, "Export Complete",
                f"Exported {len(translated)} translations to game files.\n"
                f"Original Japanese files backed up in data_original/."
                + plugin_msg
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
            # Atomic swap: rename current → temp, copy backup → data, delete temp
            temp_dir = data_dir + "_restoring"
            # Clean up leftover temp dir from a previously interrupted restore
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
            os.rename(data_dir, temp_dir)
            try:
                shutil.copytree(backup_dir, data_dir)
            except Exception:
                # Copy failed — restore the original data dir
                os.rename(temp_dir, data_dir)
                raise
            shutil.rmtree(temp_dir)

            # Also restore plugins.js if backup exists
            plugins_restored = False
            plugins_path = self.parser._find_plugins_file(self.project.project_path)
            if plugins_path:
                backup_path = os.path.join(
                    os.path.dirname(plugins_path),
                    os.path.basename(plugins_path).replace("plugins.", "plugins_original.")
                )
                if os.path.isfile(backup_path):
                    shutil.copy2(backup_path, plugins_path)
                    plugins_restored = True

            # Clean up injected word wrap plugin
            self.parser.remove_wordwrap_plugin(self.project.project_path)
            self.plugin_analyzer.inject_wordwrap = False

            msg = "Original Japanese files have been restored.\n"
            if plugins_restored:
                msg += "plugins.js has also been restored from backup.\n"
            msg += "The backups are still available."
            QMessageBox.information(self, "Restore Complete", msg)
        except Exception as e:
            QMessageBox.critical(self, "Restore Failed", str(e))

    # ── Open in RPG Maker ─────────────────────────────────────────

    def _open_in_rpgmaker(self):
        """Create a workspace project with directory junctions and open in RPG Maker."""
        if not self.project or not self.project.project_path:
            QMessageBox.warning(self, "Error", "No project loaded.")
            return

        project_path = self.project.project_path

        # Find content root (where data/ and js/ live)
        content_root = self.parser.find_content_root(project_path)
        if not content_root:
            QMessageBox.warning(
                self, "Error",
                "Could not find game data directory.\n"
                "Make sure the project has a data/ folder."
            )
            return

        # Detect MV vs MZ
        engine = self.parser.detect_engine(project_path)
        if not engine:
            QMessageBox.warning(
                self, "Error",
                "Could not detect RPG Maker version.\n"
                "Expected rpg_core.js (MV) or rmmz_core.js (MZ) in the js/ folder."
            )
            return

        # Create workspace folder
        workspace = os.path.join(project_path, "_rpgmaker_workspace")
        os.makedirs(workspace, exist_ok=True)

        # Create directory junctions for large asset folders
        junction_dirs = ["data", "Data", "img", "audio", "js", "fonts", "icon", "movies"]
        for dirname in junction_dirs:
            source = os.path.join(content_root, dirname)
            if not os.path.isdir(source):
                continue
            link = os.path.join(workspace, dirname)
            if os.path.exists(link):
                continue  # Junction already exists
            try:
                # mklink /J creates a directory junction (no admin needed)
                result = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", link, source],
                    capture_output=True, text=True, check=True,
                )
            except subprocess.CalledProcessError as e:
                QMessageBox.critical(
                    self, "Junction Error",
                    f"Failed to create directory junction for {dirname}:\n{e.stderr}"
                )
                return

        # Copy small files
        for filename in ("index.html", "package.json"):
            src = os.path.join(content_root, filename)
            dst = os.path.join(workspace, filename)
            if os.path.isfile(src) and not os.path.isfile(dst):
                shutil.copy2(src, dst)

        # Create MZ-specific empty dirs if needed
        if engine == "mz":
            for dirname in ("css", "effects"):
                d = os.path.join(workspace, dirname)
                if not os.path.isdir(d):
                    os.makedirs(d, exist_ok=True)

        # Create the marker file
        if engine == "mv":
            marker = os.path.join(workspace, "Game.rpgproject")
            marker_content = "RPGMV 1.6.3"
        else:
            marker = os.path.join(workspace, "game.rmmzproject")
            marker_content = "RPGMZ 1.10.0"

        if not os.path.isfile(marker):
            with open(marker, "w", encoding="utf-8") as f:
                f.write(marker_content)

        # Open in RPG Maker
        engine_label = "RPG Maker MV" if engine == "mv" else "RPG Maker MZ"
        try:
            os.startfile(marker)
            self.statusbar.showMessage(
                f"Opening in {engine_label}... Workspace: _rpgmaker_workspace/", 10000
            )
        except OSError:
            QMessageBox.information(
                self, "Open Manually",
                f"No application is associated with .{'rpgproject' if engine == 'mv' else 'rmmzproject'} files.\n\n"
                f"Please open this file manually in {engine_label}:\n\n"
                f"{marker}"
            )

    def _open_settings(self):
        """Open the settings dialog."""
        dlg = SettingsDialog(
            self.client, self, parser=self.parser, dark_mode=self._dark_mode,
            plugin_analyzer=self.plugin_analyzer, engine=self.engine,
            export_review_file=self._export_review_file,
        )
        if dlg.exec():
            # Apply dark mode if changed
            if dlg.dark_mode != self._dark_mode:
                self._dark_mode = dlg.dark_mode
                self._apply_dark_mode()
                self.trans_table.set_dark_mode(self._dark_mode)
            self._export_review_file = dlg.export_review_file
            self._save_settings()
            # Preload model into VRAM if model changed (avoids cold-start delay)
            if not self.client.is_cloud:
                self._preload_model()

    def _open_glossary(self):
        """Open the standalone glossary editor."""
        dlg = GlossaryDialog(self, self._general_glossary, self.project.glossary)
        if dlg.exec():
            self._general_glossary = dlg.general_glossary
            self.project.glossary = dlg.project_glossary
            self._rebuild_glossary()
            self._save_settings()

    # ── Filtering ──────────────────────────────────────────────────

    def _filter_by_file(self, filename: str):
        """Show only entries from a specific file."""
        # Script Strings virtual category
        if filename == "__SCRIPT_ALL__":
            entries = [e for e in self.project.entries
                       if e.field == "script_variable"]
        elif filename.startswith("__SCRIPT__"):
            real_file = filename[len("__SCRIPT__"):]
            entries = [e for e in self.project.get_entries_for_file(real_file)
                       if e.field == "script_variable"]
        else:
            entries = self.project.get_entries_for_file(filename)
        self.trans_table.filter_by_file(entries)

    def _show_all_entries(self):
        """Show all entries."""
        self.trans_table.clear_file_filter()

    # ── Engine signal handlers ─────────────────────────────────────

    def _on_error(self, entry_id: str, error_msg: str):
        """Handle translation error for a single entry."""
        self.statusbar.showMessage(f"Error translating {entry_id}: {error_msg}", 5000)
        self.queue_panel.mark_entry_error(entry_id, error_msg)

    def _on_batch_finished(self):
        """Handle batch translation/polish completing."""
        # Final pass: restore any control codes the LLM dropped
        codes_fixed = self._restore_missing_codes()

        mode = getattr(self, "_current_batch_mode", "all")
        chained = getattr(self, "_batch_all_chained", False)

        # Batch All: DB phase done → rebuild glossary → start dialogue phase
        if chained and mode == "db":
            self._batch_all_chained = False
            self._backfill_db_glossary()
            self._rebuild_glossary()
            self._autosave()
            self.file_tree.refresh_stats(self.project)

            # Check if there are dialogue entries left
            untranslated_dialogue = [
                e for e in self.project.entries
                if e.status == "untranslated" and e.file not in self._DB_FILES
            ]
            if untranslated_dialogue:
                db_done = sum(
                    1 for e in self.project.entries
                    if e.file in self._DB_FILES
                    and e.status in ("translated", "reviewed")
                )
                glossary_size = len(self.client.glossary)
                self.statusbar.showMessage(
                    f"DB phase done ({db_done} entries). "
                    f"Glossary: {glossary_size} terms. "
                    f"Starting dialogue phase ({len(untranslated_dialogue)} entries)...",
                    5000,
                )
                # Small delay so the user sees the status message
                from PyQt6.QtCore import QTimer
                QTimer.singleShot(500, lambda: self._start_batch(mode="dialogue"))
                return
            # else: no dialogue left, fall through to normal finish

        self.queue_panel.mark_batch_finished()
        self.batch_db_action.setEnabled(True)
        self.batch_dialogue_action.setEnabled(True)
        self.batch_action.setEnabled(True)
        self.batch_actor_action.setEnabled(True)
        self.polish_action.setEnabled(True)
        self.stop_action.setEnabled(False)
        self.progress_bar.setVisible(False)
        self.progress_label.setText("")
        self.file_tree.refresh_stats(self.project)
        msg = f"Batch complete — {self.project.translated_count}/{self.project.total} translated"
        if codes_fixed:
            msg += f" ({codes_fixed} control codes restored)"
        # Show cost for cloud providers
        cost_str = self.client.format_session_cost()
        if cost_str:
            msg += f" | {cost_str}"
        self.statusbar.showMessage(msg, 15000)
        # After DB batch, warn about name collisions (different JP → same EN)
        if mode in ("db", "all", "dialogue"):
            self._warn_name_collisions()
        # Auto-export review file if enabled
        if self._export_review_file:
            self._auto_export_review()

    def _warn_name_collisions(self):
        """Detect different JP names that translated to the same EN text.

        Shows a warning dialog listing collisions so the user can fix them
        before proceeding to dialogue translation.
        """
        # Build reverse map: EN translation → list of (JP original, file, field)
        en_to_sources: dict[str, list[tuple[str, str, str]]] = {}
        for entry in self.project.entries:
            if entry.status not in ("translated", "reviewed"):
                continue
            fields = self._AUTO_GLOSSARY_FIELDS.get(entry.file)
            is_name = (fields and entry.field in fields) or (
                entry.file.startswith("Map") and entry.field == self._AUTO_GLOSSARY_MAP_FIELD
            )
            if not is_name or not entry.translation:
                continue
            en = entry.translation.strip()
            if not en:
                continue
            en_to_sources.setdefault(en, []).append(
                (entry.original, entry.file, entry.field)
            )

        # Find collisions: same EN text from different JP originals
        collisions = []
        for en, sources in en_to_sources.items():
            unique_jp = set(src[0] for src in sources)
            if len(unique_jp) > 1:
                collisions.append((en, sources))

        if not collisions:
            return

        # Build readable message (limit to first 15 to avoid huge dialog)
        lines = []
        for en, sources in sorted(collisions)[:15]:
            unique_sources = {}
            for jp, file, field in sources:
                unique_sources.setdefault(jp, []).append(f"{file}:{field}")
            detail_parts = [f'  "{jp}" ({", ".join(locs)})' for jp, locs in unique_sources.items()]
            lines.append(f'"{en}" ← different JP sources:\n' + "\n".join(detail_parts))

        extra = ""
        if len(collisions) > 15:
            extra = f"\n... and {len(collisions) - 15} more collision(s)"

        # Save collisions to file for later review
        if self.project.project_path:
            col_path = os.path.join(
                self.project.project_path, "_name_collisions.txt")
            try:
                with open(col_path, "w", encoding="utf-8") as f:
                    f.write(f"# Name Collisions Report\n")
                    f.write(f"# {len(collisions)} collision(s) found\n")
                    f.write(f"# Different JP terms translated to the same EN text.\n")
                    f.write(f"# Fix these before running Batch Dialogue.\n\n")
                    for en, sources in sorted(collisions):
                        f.write(f'EN: "{en}"\n')
                        unique_sources: dict[str, list[str]] = {}
                        for jp, file, field in sources:
                            unique_sources.setdefault(jp, []).append(
                                f"{file}:{field}")
                        for jp, locs in unique_sources.items():
                            f.write(f'  JP: "{jp}" ({", ".join(locs)})\n')
                        f.write("\n")
            except OSError:
                pass

        QMessageBox.warning(
            self, "Name Collisions Detected",
            f"Found {len(collisions)} name collision(s) — different Japanese "
            f"terms translated to the same English text.\n\n"
            f"Review these in the table and retranslate or edit as needed "
            f"before running Batch Dialogue.\n\n"
            + "\n\n".join(lines) + extra
            + "\n\nFull list saved to _name_collisions.txt",
        )

    def _on_checkpoint(self):
        """Auto-save during batch translation (every 25 entries)."""
        # Auto-fix dropped control codes before saving
        self._restore_missing_codes()
        self._autosave()
        # Run translation memory to fill duplicates from newly translated entries
        tm_count = self._run_translation_memory()
        if tm_count:
            # Track TM fills separately — workers skip these silently
            self._tm_checkpoint_count += tm_count
            # Immediately update progress bar (workers won't emit for these)
            effective = self._batch_done_count + self._tm_checkpoint_count
            total = self.progress_bar.maximum()
            self.progress_bar.setValue(effective)
            # Recalculate ETA with TM fills counted
            elapsed = time.time() - self._batch_start_time
            remaining_count = max(0, total - effective)
            if effective > 0 and elapsed > 0:
                rate = elapsed / effective
                remaining = remaining_count * rate
                if remaining > 60:
                    eta = f" | ETA: {remaining/60:.0f}m"
                else:
                    eta = f" | ETA: {remaining:.0f}s"
            else:
                eta = ""
            self.progress_label.setText(
                f"Translating {effective}/{total}{eta}: "
                f"TM filled {tm_count} duplicate(s)"
            )

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

    # ── Glossary merge ─────────────────────────────────────────────

    def _on_glossary_add(self, jp_term: str, en_term: str, glossary_type: str):
        """Handle glossary entry added from translation table right-click."""
        if glossary_type == "project":
            self.project.glossary[jp_term] = en_term
        else:
            self._general_glossary[jp_term] = en_term
            self._save_settings()
        self._rebuild_glossary()
        label = "project" if glossary_type == "project" else "general"
        self.statusBar().showMessage(
            f"Added to {label} glossary: {jp_term} \u2192 {en_term}", 5000
        )

    def _rebuild_glossary(self):
        """Merge general + project glossaries into client.glossary.

        Project-specific entries override general entries if both define
        the same Japanese term.
        """
        self.client.glossary = {**self._general_glossary, **self.project.glossary}

    # ── Persistent settings ───────────────────────────────────────

    def _load_settings(self):
        """Load saved settings from _settings.json on startup."""
        try:
            with open(self._SETTINGS_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return  # No saved settings — use defaults

        if "ollama_url" in cfg:
            self.client.base_url = cfg["ollama_url"]
        if "model" in cfg:
            self.client.model = cfg["model"]
        if "system_prompt" in cfg:
            self.client.system_prompt = cfg["system_prompt"]
        if "workers" in cfg:
            self.engine.num_workers = cfg["workers"]
        if "context_size" in cfg:
            self.parser.context_size = cfg["context_size"]
        if "dark_mode" in cfg:
            self._dark_mode = cfg["dark_mode"]
        if "wordwrap_override" in cfg and cfg["wordwrap_override"] > 0:
            self.plugin_analyzer._manual_chars_per_line = cfg["wordwrap_override"]
            self.plugin_analyzer.chars_per_line = cfg["wordwrap_override"]
        if "general_glossary" in cfg and isinstance(cfg["general_glossary"], dict):
            self._general_glossary = cfg["general_glossary"]
        if "target_language" in cfg:
            self.client.target_language = cfg["target_language"]
        if "batch_size" in cfg:
            self.engine.batch_size = cfg["batch_size"]
        if "max_history" in cfg:
            self.engine.max_history = cfg["max_history"]
        if "vision_model" in cfg:
            self.client.vision_model = cfg["vision_model"]
        if "extract_script_strings" in cfg:
            self.parser.extract_script_strings = cfg["extract_script_strings"]
        if "single_401_mode" in cfg:
            self.parser.single_401_mode = cfg["single_401_mode"]
        if "provider" in cfg:
            self.client.provider = cfg["provider"]
        if "api_key" in cfg:
            self.client.api_key = cfg["api_key"]
        if "prompt_preset" in cfg:
            self.client._prompt_preset = cfg["prompt_preset"]
        if "dazed_mode" in cfg:
            self.client.dazed_mode = cfg["dazed_mode"]
        if "auto_tune" in cfg:
            self.engine.auto_tune = cfg["auto_tune"]
        if "export_review_file" in cfg:
            self._export_review_file = cfg["export_review_file"]

    def _save_settings(self):
        """Persist current settings to _settings.json."""
        cfg = {
            "ollama_url": self.client.base_url,
            "model": self.client.model,
            "system_prompt": self.client.system_prompt,
            "provider": self.client.provider,
            "api_key": self.client.api_key,
            "prompt_preset": getattr(self.client, "_prompt_preset", "Custom"),
            "dazed_mode": self.client.dazed_mode,
            "workers": self.engine.num_workers,
            "batch_size": self.engine.batch_size,
            "max_history": self.engine.max_history,
            "context_size": self.parser.context_size,
            "dark_mode": self._dark_mode,
            "wordwrap_override": getattr(self.plugin_analyzer, "_manual_chars_per_line", 0),
            "general_glossary": self._general_glossary,
            "target_language": self.client.target_language,
            "vision_model": getattr(self.client, "vision_model", ""),
            "extract_script_strings": self.parser.extract_script_strings,
            "single_401_mode": self.parser.single_401_mode,
            "auto_tune": self.engine.auto_tune,
            "export_review_file": self._export_review_file,
        }
        try:
            with open(self._SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except OSError:
            pass  # Non-critical — settings just won't persist

    # ── Auto-save ──────────────────────────────────────────────────

    def _autosave(self):
        """Auto-save project state if there are entries and a save path exists."""
        if not self.project.entries:
            return
        if not self._last_save_path:
            # Auto-save next to project if possible
            if self.project.project_path:
                self._last_save_path = os.path.join(
                    self.project.project_path, "_translation_state.json"
                )
            else:
                return
        try:
            self.project.save_state(self._last_save_path)
            self.statusbar.showMessage("Auto-saved", 2000)
        except Exception as e:
            self.statusbar.showMessage(f"Auto-save failed: {e}", 5000)

    # ── Model suggestion ────────────────────────────────────────────

    def _show_model_suggestion(self):
        """Show GPU-aware model recommendation dialog (first launch)."""
        installed = self.client.list_models() if self.client.is_available() else []
        dlg = ModelSuggestionDialog(installed_models=installed, parent=self)
        dlg.model_selected.connect(self._on_suggested_model)
        dlg.exec()

    def _on_suggested_model(self, tag: str):
        """Apply model from suggestion dialog."""
        self.client.model = tag
        self._save_settings()
        self.statusbar.showMessage(f"Model set to {tag}", 5000)

    # ── Auto-tune callbacks ─────────────────────────────────────────

    def _on_calibrating(self, status: str):
        """Show auto-tune calibration status in progress label."""
        self.progress_label.setText(f"Auto-tuning: {status}")

    def _on_calibration_done(self, optimal_batch_size: int):
        """Show calibration result in status bar."""
        self.statusbar.showMessage(
            f"Auto-tune complete: batch_size = {optimal_batch_size}", 8000)

    # ── Progress ETA ───────────────────────────────────────────────

    def _on_progress(self, current: int, total: int, text: str):
        """Update progress bar with ETA during batch translation."""
        self._batch_done_count = current
        # Effective progress = engine progress + TM checkpoint fills
        tm_offset = getattr(self, "_tm_checkpoint_count", 0)
        effective = current + tm_offset

        self.progress_bar.setValue(effective)

        # Calculate ETA based on effective progress
        eta_str = ""
        elapsed = time.time() - self._batch_start_time
        remaining_count = max(0, total - effective)
        if effective > 0 and elapsed > 0:
            rate = elapsed / effective  # seconds per entry (including TM)
            remaining = remaining_count * rate
            if remaining > 3600:
                eta_str = f" | ETA: {remaining/3600:.1f}h"
            elif remaining > 60:
                eta_str = f" | ETA: {remaining/60:.0f}m"
            else:
                eta_str = f" | ETA: {remaining:.0f}s"

        # Append running cost for cloud providers
        cost_str = ""
        if self.client.is_cloud and self.client.session_cost > 0:
            cost_str = f" | ${self.client.session_cost:,.4f}"

        self.progress_label.setText(
            f"Translating {effective}/{total}{eta_str}{cost_str}: {text}"
        )

    # ── Translation memory ─────────────────────────────────────────

    def _batch_translate(self):
        """Batch All: DB first → auto-glossary → dialogue second."""
        self._batch_all_chained = True
        self._start_batch(mode="db")

    def _batch_translate_db(self):
        """Stage 1: Translate only DB entries (names, descriptions, terms).

        Translate DB first so the user can QA names before they become
        glossary entries used in dialogue.
        """
        self._start_batch(mode="db")

    def _batch_translate_dialogue(self):
        """Stage 2: Translate only dialogue/event entries.

        Warns if no DB name glossary entries exist yet, since translating
        dialogue without glossary terms may produce inconsistent names.
        """
        # Check if any DB names have been glossary'd
        db_glossary_count = sum(
            1 for e in self.project.entries
            if e.file in self._DB_FILES
            and e.field in (self._AUTO_GLOSSARY_FIELDS.get(e.file) or ())
            and e.status in ("translated", "reviewed")
        )
        if db_glossary_count == 0:
            reply = QMessageBox.warning(
                self, "No DB Names Translated",
                "No database names (items, skills, enemies, etc.) have been "
                "translated yet.\n\n"
                "Translating dialogue without glossary terms may produce "
                "inconsistent item/character names.\n\n"
                "Recommended: Run 'Batch DB' first to translate names, "
                "then QA them before translating dialogue.\n\n"
                "Continue anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self._start_batch(mode="dialogue")

    def _batch_translate_by_actor(self):
        """Translate dialogue grouped by speaker — female speakers first.

        Groups all untranslated entries by the speaker identified in code 101
        headers, cross-references with actor gender data, and orders:
          1. Female speaker dialogue
          2. Male speaker dialogue
          3. Unknown/no speaker dialogue
          4. Non-dialogue entries (DB, choices, plugins, etc.)

        This gives the LLM strong, consistent gender context per character
        and lets the user QA one character's lines at a time.
        """
        if not self.client.is_available():
            QMessageBox.warning(
                self, "Ollama Not Available",
                "Cannot connect to Ollama. Make sure it's running:\n  ollama serve"
            )
            return

        # First batch: run actor pre-translate + gender dialog
        if not self._ensure_actors_ready():
            return

        if not self.client.actor_genders:
            QMessageBox.warning(
                self, "No Actor Data",
                "No actor gender data available.\n\n"
                "Open a project first (or load a state) so the translator\n"
                "knows which characters are male/female."
            )
            return

        # Glossary prefill + translation memory (same as _start_batch)
        self._current_batch_mode = "all"
        gp_count = self._run_glossary_prefill()
        tm_count = self._run_translation_memory()

        untranslated = [e for e in self.project.entries if e.status == "untranslated"]
        if not untranslated:
            QMessageBox.information(self, "Done", "All entries are already translated!")
            return

        # Build name → gender lookup from actor data
        name_to_gender = {}
        for actor_id, name in self.client.actor_names.items():
            gender = self.client.actor_genders.get(actor_id, "")
            if gender in ("female", "male"):
                name_to_gender[name] = gender

        # Group entries by speaker gender
        _speaker_re = re.compile(r'\[Speaker:\s*(.+?)\]')
        female_entries = []
        male_entries = []
        other_dialog = []
        non_dialog = []

        for entry in untranslated:
            # Only dialogue/scroll/choice entries have speaker context
            if entry.field not in ("dialog", "scroll_text", "choice"):
                non_dialog.append(entry)
                continue

            speaker = ""
            if entry.context:
                m = _speaker_re.search(entry.context)
                if m:
                    speaker = m.group(1).strip()

            gender = name_to_gender.get(speaker, "")
            if gender == "female":
                female_entries.append(entry)
            elif gender == "male":
                male_entries.append(entry)
            else:
                other_dialog.append(entry)

        # Sort each group for TM priority (dup seeds first, unique in order, dup copies last)
        female_entries = self._sort_for_tm_priority(female_entries)
        male_entries = self._sort_for_tm_priority(male_entries)
        other_dialog = self._sort_for_tm_priority(other_dialog)
        non_dialog = self._sort_for_tm_priority(non_dialog)

        # Combine: female → male → ungendered → non-dialogue
        ordered = female_entries + male_entries + other_dialog + non_dialog

        # Build summary for confirmation
        parts = []
        if female_entries:
            parts.append(f"  Female speakers: {len(female_entries)}")
        if male_entries:
            parts.append(f"  Male speakers: {len(male_entries)}")
        if other_dialog:
            parts.append(f"  Other dialogue: {len(other_dialog)}")
        if non_dialog:
            parts.append(f"  Non-dialogue (DB/plugins): {len(non_dialog)}")
        summary = "\n".join(parts)

        prefill_notes = []
        if gp_count:
            prefill_notes.append(f"glossary: {gp_count}")
        if tm_count:
            prefill_notes.append(f"TM: {tm_count}")
        if prefill_notes:
            summary += f"\n\n  (Pre-filled {', '.join(prefill_notes)} entries)"

        reply = QMessageBox.question(
            self, "Batch by Actor",
            f"Translating {len(ordered)} entries grouped by speaker gender:\n\n"
            f"{summary}\n\n"
            "Female speakers are translated first, then male, then the rest.\n"
            "Each entry gets explicit speaker gender hints for the LLM.\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.batch_action.setEnabled(False)
        self.batch_db_action.setEnabled(False)
        self.batch_dialogue_action.setEnabled(False)
        self.batch_actor_action.setEnabled(False)
        self.stop_action.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(len(ordered))
        self.progress_bar.setValue(0)
        self._batch_start_time = time.time()
        self._batch_done_count = 0
        self._tm_checkpoint_count = 0

        self.engine.translate_batch(ordered)

    def _run_translation_memory(self) -> int:
        """Fill untranslated entries that match already-translated text.

        Uses self._current_batch_mode to respect db/dialogue filtering.
        Returns the number of entries filled.
        """
        mode = getattr(self, "_current_batch_mode", "all")
        translated_map = {}
        for e in self.project.entries:
            if e.status in ("translated", "reviewed") and e.translation:
                translated_map[e.original] = e.translation

        tm_count = 0
        for e in self.project.entries:
            if e.status == "untranslated" and e.original in translated_map:
                if mode == "db" and e.file not in self._DB_FILES:
                    continue
                if mode == "dialogue" and e.file in self._DB_FILES:
                    continue
                e.translation = translated_map[e.original]
                e.status = "translated"
                self.trans_table.update_entry(e.id, e.translation)
                self._maybe_add_to_glossary(e)
                tm_count += 1

        if tm_count:
            self.file_tree.refresh_stats(self.project)

        return tm_count

    def _run_glossary_prefill(self) -> int:
        """Fill untranslated entries whose full text is an exact glossary key.

        When the entire original text (stripped) matches a glossary key exactly,
        we use the glossary translation directly, skipping the LLM call.
        Respects self._current_batch_mode for db/dialogue filtering.
        Returns the number of entries filled.
        """
        glossary = self.client.glossary
        if not glossary:
            return 0

        mode = getattr(self, "_current_batch_mode", "all")
        count = 0
        for e in self.project.entries:
            if e.status != "untranslated":
                continue
            if mode == "db" and e.file not in self._DB_FILES:
                continue
            if mode == "dialogue" and e.file in self._DB_FILES:
                continue
            stripped = e.original.strip()
            if stripped in glossary:
                e.translation = glossary[stripped]
                e.status = "translated"
                self.trans_table.update_entry(e.id, e.translation)
                self._maybe_add_to_glossary(e)
                count += 1

        if count:
            self.file_tree.refresh_stats(self.project)

        return count

    @staticmethod
    def _sort_for_tm_priority(entries: list) -> list:
        """Sort entries to maximize translation memory hits at checkpoints.

        Returns a new list ordered as:
        1. Seeds — first copy of each duplicated text, shortest first
           (translate one, TM fills all copies at the next checkpoint)
        2. Unique — entries appearing only once, in original order
           (preserves dialogue locality for the translation history window)
        3. Dupes — remaining duplicate copies, at the end
           (workers skip these after TM fills them)
        """
        dup_counts = Counter(e.original for e in entries)

        seen = set()
        seeds = []
        unique = []
        dupes = []

        for e in entries:
            if dup_counts[e.original] > 1:
                if e.original not in seen:
                    seeds.append(e)
                    seen.add(e.original)
                else:
                    dupes.append(e)
            else:
                unique.append(e)

        seeds.sort(key=lambda e: len(e.original))
        return seeds + unique + dupes

    def _start_batch(self, mode: str = "all"):
        """Shared batch translation logic.

        Args:
            mode: "all" = everything, "db" = DB/System only,
                  "dialogue" = non-DB only (maps, events, plugins).
        """
        if not self.client.is_available():
            QMessageBox.warning(
                self, "Ollama Not Available",
                "Cannot connect to Ollama. Make sure it's running:\n  ollama serve"
            )
            return

        # First batch: run actor pre-translate + gender dialog
        if not self._ensure_actors_ready():
            return

        self._current_batch_mode = mode

        # Glossary prefill: exact-match entries skip LLM entirely
        gp_count = self._run_glossary_prefill()

        # Translation memory: auto-fill exact duplicates from already-translated entries
        tm_count = self._run_translation_memory()

        prefill_parts = []
        if gp_count:
            prefill_parts.append(f"glossary: {gp_count}")
        if tm_count:
            prefill_parts.append(f"TM: {tm_count}")
        if prefill_parts:
            self.statusbar.showMessage(
                f"Pre-filled {', '.join(prefill_parts)} entries", 3000
            )

        untranslated = [e for e in self.project.entries if e.status == "untranslated"]
        if mode == "db":
            untranslated = [e for e in untranslated if e.file in self._DB_FILES]
        elif mode == "dialogue":
            untranslated = [e for e in untranslated if e.file not in self._DB_FILES]

        if not untranslated:
            labels = {"all": "All entries", "db": "DB entries", "dialogue": "Dialogue entries"}
            QMessageBox.information(self, "Done", f"{labels[mode]} are already translated!")
            return

        # Sort: duplicated short strings first → unique in order → dup copies last
        untranslated = self._sort_for_tm_priority(untranslated)

        self.batch_action.setEnabled(False)
        self.batch_db_action.setEnabled(False)
        self.batch_dialogue_action.setEnabled(False)
        self.batch_actor_action.setEnabled(False)
        self.stop_action.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(len(untranslated))
        self.progress_bar.setValue(0)
        self._batch_start_time = time.time()
        self._batch_done_count = 0
        self._tm_checkpoint_count = 0  # TM fills during batch (not counted by engine)

        # Reset cost tracking for this batch
        if self.client.is_cloud:
            self.client.reset_session_cost()

        # Populate the queue panel and switch to it
        self.queue_panel.load_queue(untranslated)
        self.tabs.setCurrentWidget(self.queue_panel)

        self.engine.translate_batch(untranslated)

    # ── Polish Grammar ──────────────────────────────────────────────

    def _polish_translations(self):
        """Run all translated entries through the LLM for grammar cleanup."""
        if not self.client.is_available():
            QMessageBox.warning(
                self, "Ollama Not Available",
                "Cannot connect to Ollama. Make sure it's running:\n  ollama serve"
            )
            return

        to_polish = [
            e for e in self.project.entries
            if e.status in ("translated", "reviewed")
            and e.translation and e.translation.strip()
        ]
        if not to_polish:
            QMessageBox.information(self, "Nothing to Polish",
                                    "No translated entries to polish.")
            return

        reply = QMessageBox.question(
            self, "Polish Grammar",
            f"This will run {len(to_polish)} translated entries through the LLM\n"
            "to fix grammar and improve fluency (English → English).\n\n"
            "Original Japanese text is not changed — only the English translation\n"
            "gets cleaned up. This may take a while.\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.batch_action.setEnabled(False)
        self.batch_db_action.setEnabled(False)
        self.batch_dialogue_action.setEnabled(False)
        self.batch_actor_action.setEnabled(False)
        self.polish_action.setEnabled(False)
        self.stop_action.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(len(to_polish))
        self.progress_bar.setValue(0)
        self._batch_start_time = time.time()
        self._batch_done_count = 0

        # Populate queue panel for polish
        self.queue_panel.load_queue(to_polish)
        self.tabs.setCurrentWidget(self.queue_panel)

        self.engine.polish_batch(to_polish)

    # ── Word Wrap ──────────────────────────────────────────────────

    def _apply_wordwrap(self):
        """Apply word wrapping to all translated entries."""
        if not self.project.entries:
            return

        cpl = self.plugin_analyzer.chars_per_line
        summary = self.plugin_analyzer.get_summary()

        from PyQt6.QtWidgets import QCheckBox, QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle("Apply Word Wrap")
        dlg.setMinimumWidth(420)
        layout = QVBoxLayout(dlg)

        header = QLabel("Word Wrap Settings")
        header.setStyleSheet("font-size: 15px; font-weight: bold; margin-bottom: 4px;")
        layout.addWidget(header)

        info = QLabel(
            f"Detected settings:\n\n{summary}\n\n"
            f"This will redistribute text across lines (~{cpl} chars/line).\n"
            "Entries that overflow their text box will be flagged."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        checkbox = QCheckBox("I understand this will modify my translations")
        layout.addWidget(checkbox)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setText("Apply Word Wrap")
        ok_btn.setEnabled(False)
        checkbox.toggled.connect(ok_btn.setEnabled)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        # Disable plugin injection — manual breaks only
        self.plugin_analyzer.inject_wordwrap = False

        count = self.text_processor.process_all(self.project.entries)
        self.trans_table.refresh()

        overflows = self.text_processor.overflow_entries
        expanded = self.text_processor.expanded_count
        extra = self.text_processor.extra_lines
        msg = f"Modified {count} entries.\nWrapped to ~{cpl} chars/line."
        if expanded:
            msg += (f"\n\n{expanded} entries needed extra lines "
                    f"(+{extra} 401 commands will be added on export).")
        if overflows:
            msg += (f"\n\n{len(overflows)} entries exceed one message box "
                    "and will auto-paginate in-game.")
            # Show first few overflow files
            files = sorted(set(f for _, f in overflows))
            if len(files) <= 10:
                msg += "\n\nAffected files:\n" + "\n".join(f"  {f}" for f in files)
            else:
                msg += f"\n\nAcross {len(files)} files."
        QMessageBox.information(self, "Word Wrap Applied", msg)

    # Same regex as ollama_client for fixing contraction spacing:
    #   "I 've" → "I've"   "Couldn' t" → "Couldn't"   "do n't" → "don't"
    # Handles space before and/or after the apostrophe (ASCII ' or curly ')
    _CONTRACTION_RE = re.compile(
        r"\b(\w+)\s*(['\u2019])\s*(ve|re|ll|t|s|d|m)\b", re.IGNORECASE)

    # Japanese speech/quote brackets that produce redundant "" in translations
    _JP_SPEECH_BRACKETS = set('\u300c\u300d\u300e\u300f')  # 「」『』

    def _cleanup_translations(self):
        """Strip redundant dialogue quotes and fix contraction spacing."""
        if not self.project.entries:
            return

        quotes_fixed = 0
        contractions_fixed = 0
        for entry in self.project.entries:
            if not entry.translation or entry.status not in ("translated", "reviewed"):
                continue
            original_text = entry.translation

            # Strip dialogue quotes: if original had 「」or『』, remove first/last "
            if self._JP_SPEECH_BRACKETS & set(entry.original):
                t = entry.translation
                first = t.find('"')
                last = t.rfind('"')
                if first != -1 and last > first:
                    entry.translation = t[:first] + t[first + 1:last] + t[last + 1:]

            # Fix contraction spacing (I 've → I've, Couldn' t → Couldn't)
            entry.translation = self._CONTRACTION_RE.sub(r"\1\2\3", entry.translation)

            if entry.translation != original_text:
                if '"' in original_text and '"' not in entry.translation:
                    quotes_fixed += 1
                else:
                    contractions_fixed += 1

        if quotes_fixed or contractions_fixed:
            self.trans_table.refresh()

        parts = []
        if quotes_fixed:
            parts.append(f"Stripped quotes from {quotes_fixed} entries")
        if contractions_fixed:
            parts.append(f"Fixed contractions in {contractions_fixed} entries")

        QMessageBox.information(
            self, "Clean Up Translations",
            "\n".join(parts) if parts else "No issues found — translations are clean."
        )

    def _strip_wordwrap_tags(self):
        """Remove all <WordWrap> tags from translations."""
        if not self.project.entries:
            return
        count = 0
        for entry in self.project.entries:
            if not entry.translation:
                continue
            stripped = re.sub(r'<WordWrap>', '', entry.translation, flags=re.IGNORECASE)
            if stripped != entry.translation:
                entry.translation = stripped
                count += 1
        if count:
            self.trans_table.refresh()
            self.plugin_analyzer.inject_wordwrap = False
        QMessageBox.information(
            self, "Strip Word Wrap Tags",
            f"Removed <WordWrap> tags from {count} entries." if count
            else "No <WordWrap> tags found."
        )

    # ── Fix Missing Codes ─────────────────────────────────────────

    def _restore_missing_codes(self) -> int:
        """Silently restore missing control codes in all translated entries.

        Scans every translated entry, compares control codes in the original
        to the translation, and auto-inserts any missing codes at the
        position they occupied in the original (start → prepend, end → append).

        Called automatically at each checkpoint and batch finish, so the LLM
        never needs to be re-invoked for dropped codes.

        Returns the number of entries fixed.
        """
        if not self.project.entries:
            return 0

        from .. import CONTROL_CODE_RE

        fixed = 0
        for entry in self.project.entries:
            if entry.status not in ("translated", "reviewed"):
                continue
            if not entry.translation:
                continue

            orig_codes = CONTROL_CODE_RE.findall(entry.original)
            if not orig_codes:
                continue

            # Check which codes are missing (handle duplicates correctly)
            trans_check = entry.translation
            missing = []
            for code in orig_codes:
                if code in trans_check:
                    trans_check = trans_check.replace(code, "", 1)
                else:
                    missing.append(code)

            if not missing:
                continue

            # Insert missing codes based on their position in the original
            orig_len = len(entry.original)
            prepend = []
            append = []
            for code in missing:
                pos = entry.original.find(code)
                if pos < 0:
                    prepend.append(code)
                elif orig_len > 0 and pos / orig_len > 0.85:
                    append.append(code)
                else:
                    prepend.append(code)

            new_translation = "".join(prepend) + entry.translation + "".join(append)
            if new_translation != entry.translation:
                entry.translation = new_translation
                fixed += 1

        return fixed

    def _fix_missing_codes(self):
        """Manual menu action — runs _restore_missing_codes() with a result dialog."""
        fixed = self._restore_missing_codes()

        if fixed:
            self.trans_table.refresh()
            self.file_tree.refresh_stats(self.project)

        QMessageBox.information(
            self, "Fix Missing Codes",
            f"Fixed {fixed} entries with missing control codes."
            if fixed else "All entries have their control codes intact."
        )

    # ── Apply Glossary ─────────────────────────────────────────────

    def _apply_glossary(self):
        """Find translated entries with glossary mismatches and fix via replacement.

        For each glossary entry (JP → EN), scans translated entries where
        the original contains the JP term but the translation doesn't
        contain the expected EN term.  Builds a reverse lookup of old
        translations for each JP term and does direct string replacement.
        """
        if not self.project.entries or not self.client.glossary:
            QMessageBox.information(
                self, "Apply Glossary", "No entries or glossary is empty."
            )
            return

        # Build reverse lookup: for each glossary JP term, find what it was
        # previously translated as (from entries where original == jp_term exactly)
        old_translations: dict[str, set[str]] = {}  # jp_term → {old_en, ...}
        for entry in self.project.entries:
            if not entry.translation or entry.status not in ("translated", "reviewed"):
                continue
            jp = entry.original.strip()
            if jp in self.client.glossary:
                en = entry.translation.strip()
                expected = self.client.glossary[jp]
                if en and en != expected:
                    old_translations.setdefault(jp, set()).add(en)

        # Build list of mismatches: (entry, jp_term, expected_en)
        mismatches = []
        for entry in self.project.entries:
            if entry.status not in ("translated", "reviewed"):
                continue
            if not entry.translation:
                continue
            for jp_term, en_term in self.client.glossary.items():
                if jp_term in entry.original and en_term not in entry.translation:
                    mismatches.append((entry, jp_term, en_term))

        if not mismatches:
            QMessageBox.information(
                self, "Apply Glossary",
                f"All {self.project.translated_count} translated entries "
                f"are consistent with the glossary ({len(self.client.glossary)} terms)."
            )
            return

        # Summarize by glossary term
        from collections import Counter
        term_counts = Counter(jp for _, jp, _en in mismatches)
        summary_lines = []
        for jp_term, count in term_counts.most_common(20):
            en_term = self.client.glossary[jp_term]
            old = old_translations.get(jp_term)
            if old:
                old_str = ", ".join(sorted(old)[:3])
                summary_lines.append(
                    f"  {jp_term}: {old_str} \u2192 {en_term} ({count} entries)")
            else:
                summary_lines.append(
                    f"  {jp_term} \u2192 {en_term} ({count} entries)")
        if len(term_counts) > 20:
            summary_lines.append(f"  ... and {len(term_counts) - 20} more terms")

        # Unique entries affected
        mismatch_ids = set()
        for entry, _jp, _en in mismatches:
            mismatch_ids.add(entry.id)

        # Build replacement map: old_en → new_en (longest first to avoid partial matches)
        replacements: dict[str, str] = {}
        for jp_term, en_term in self.client.glossary.items():
            for old_en in old_translations.get(jp_term, set()):
                replacements[old_en] = en_term

        can_replace = bool(replacements)
        summary = (
            f"Found {len(mismatch_ids)} entries with glossary mismatches "
            f"({len(term_counts)} terms):\n\n"
            + "\n".join(summary_lines)
        )
        if can_replace:
            summary += (
                f"\n\nApply will replace old terms with glossary terms "
                f"({len(replacements)} replacements). No LLM needed."
            )
        else:
            summary += (
                "\n\nNo old translations found to replace automatically.\n"
                "Use Retranslate to send these entries back to the LLM."
            )

        msg = QMessageBox(self)
        msg.setWindowTitle("Apply Glossary")
        msg.setText(summary)
        if can_replace:
            apply_btn = msg.addButton("Apply", QMessageBox.ButtonRole.AcceptRole)
        else:
            apply_btn = None
        retranslate_btn = msg.addButton("Retranslate", QMessageBox.ButtonRole.ActionRole)
        view_btn = msg.addButton("View Only", QMessageBox.ButtonRole.ActionRole)
        msg.addButton(QMessageBox.StandardButton.Cancel)
        msg.exec()

        clicked = msg.clickedButton()
        if clicked == apply_btn and can_replace:
            # Direct string replacement — sort longest old terms first
            sorted_replacements = sorted(
                replacements.items(), key=lambda x: len(x[0]), reverse=True)
            fixed = 0
            for entry in self.project.entries:
                if entry.id not in mismatch_ids:
                    continue
                original_translation = entry.translation
                for old_en, new_en in sorted_replacements:
                    if old_en in entry.translation:
                        entry.translation = entry.translation.replace(old_en, new_en)
                if entry.translation != original_translation:
                    fixed += 1
            self.trans_table.refresh()
            self.file_tree.load_project(self.project)
            # Check how many are still mismatched after replacement
            still_mismatched = 0
            for entry in self.project.entries:
                if entry.id not in mismatch_ids:
                    continue
                for jp_term, en_term in self.client.glossary.items():
                    if jp_term in entry.original and en_term not in entry.translation:
                        still_mismatched += 1
                        break
            msg_text = f"Fixed {fixed} entries via text replacement."
            if still_mismatched:
                msg_text += (
                    f"\n{still_mismatched} entries still have mismatches "
                    f"(may need retranslation)."
                )
            QMessageBox.information(self, "Apply Glossary", msg_text)
        elif clicked == retranslate_btn:
            # Reset mismatched entries to untranslated and start batch
            for entry in self.project.entries:
                if entry.id in mismatch_ids:
                    entry.status = "untranslated"
                    entry.translation = ""
            self.trans_table.refresh()
            self.file_tree.load_project(self.project)
            self.statusbar.showMessage(
                f"Reset {len(mismatch_ids)} entries. Starting retranslation...", 3000
            )
            self._start_batch("all")
        elif clicked == view_btn:
            # Filter table to show only mismatch entries
            mismatch_entries = [e for e in self.project.entries if e.id in mismatch_ids]
            self.trans_table._entries = mismatch_entries
            self.trans_table._apply_filter()
            self.file_tree.clearSelection()
            self.statusbar.showMessage(
                f"Showing {len(mismatch_entries)} entries with glossary mismatches "
                f"({len(term_counts)} terms). Click a file or clear search to reset.",
                10000,
            )

    # ── Consistency Pass ──────────────────────────────────────────

    def _consistency_pass(self):
        """Fix name variants, capitalization, and term inconsistencies."""
        if not self.project.entries:
            return

        translated = sum(
            1 for e in self.project.entries
            if e.status in ("translated", "reviewed") and e.translation
        )
        if translated == 0:
            QMessageBox.information(
                self, "Consistency Pass", "No translated entries to check."
            )
            return

        reply = QMessageBox.question(
            self, "Consistency Pass",
            f"Run consistency checks on {translated} translated entries?\n\n"
            "This will fix:\n"
            "  1. Name capitalization (knight \u2192 Knight)\n"
            "  2. Duplicate-original standardization (most common wins)\n"
            "  3. Glossary name variant spelling\n\n"
            "Reviewed entries keep capitalization and variant fixes\n"
            "but are excluded from duplicate standardization.\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        caps, dupes, variants = self._run_consistency_fixes()
        total = caps + dupes + variants

        self.trans_table.refresh()
        self.file_tree.refresh_stats(self.project)

        if total == 0:
            QMessageBox.information(
                self, "Consistency Pass",
                "No inconsistencies found \u2014 all entries look consistent."
            )
        else:
            lines = []
            if caps:
                lines.append(f"  Capitalization fixed: {caps} entries")
            if dupes:
                lines.append(f"  Duplicates standardized: {dupes} entries")
            if variants:
                lines.append(f"  Name variants replaced: {variants} entries")
            QMessageBox.information(
                self, "Consistency Pass Complete",
                f"Fixed {total} entries:\n\n" + "\n".join(lines)
            )
            self._autosave()

    def _run_consistency_fixes(self) -> tuple:
        """Pure-Python consistency fixes across all translated entries.

        Phase 1: Capitalize name/displayName fields (skip prepositions).
        Phase 2: Standardize entries with identical originals to most-common
                 translation (skip reviewed entries).
        Phase 3: Fix name spelling variants using DB name entries as canonical
                 source (fuzzy-match, safe against substring collisions).

        Returns (caps_fixed, dupes_fixed, variants_fixed).
        """
        from collections import Counter, defaultdict
        from difflib import SequenceMatcher

        entries = self.project.entries
        caps_fixed = 0
        dupes_fixed = 0
        variants_fixed = 0

        # ── Phase 1: Capitalize name fields ──
        for entry in entries:
            if entry.status not in ("translated", "reviewed"):
                continue
            if not entry.translation or entry.field not in self._CAPITALIZE_FIELDS:
                continue
            capped = self._title_case(entry.translation)
            if capped != entry.translation:
                entry.translation = capped
                caps_fixed += 1

        # ── Phase 2: Same-original standardization ──
        # Group by original text (only "translated" entries — skip "reviewed")
        groups: dict[str, list] = defaultdict(list)
        for entry in entries:
            if entry.status == "translated" and entry.translation:
                groups[entry.original].append(entry)

        for _original, group in groups.items():
            if len(group) < 2:
                continue
            translations = Counter(e.translation for e in group)
            if len(translations) < 2:
                continue
            canonical = translations.most_common(1)[0][0]
            for entry in group:
                if entry.translation != canonical:
                    entry.translation = canonical
                    dupes_fixed += 1

        # ── Phase 3: Name variant replacement ──
        # Build canonical names from proper-noun entries only:
        # actor names/nicknames and map displayNames.
        # NOT classes, items, enemies, etc. — those are common nouns that
        # would cause false positives ("Warrior" replacing "warrior" in dialogue).
        _PROPER_NOUN_FIELDS = {
            "Actors.json": ("name", "nickname"),
        }
        name_map: dict[str, str] = {}  # JP name → EN canonical
        for entry in entries:
            if entry.status not in ("translated", "reviewed"):
                continue
            if not entry.translation:
                continue
            fields = _PROPER_NOUN_FIELDS.get(entry.file)
            is_proper = (fields and entry.field in fields) or (
                entry.file.startswith("Map")
                and entry.field == self._AUTO_GLOSSARY_MAP_FIELD
            )
            if is_proper:
                name_map[entry.original] = entry.translation

        if not name_map:
            return caps_fixed, dupes_fixed, variants_fixed

        # Build set of all canonical EN names (to avoid replacing one
        # canonical name with another — e.g. "Lilian" is NOT a variant
        # of "Lian" even though they're similar)
        canonical_en = set(name_map.values())

        # Sort longest JP first to prevent substring collisions
        # (リリアン before リアン)
        sorted_names = sorted(name_map.items(), key=lambda x: -len(x[0]))

        for entry in entries:
            if entry.status not in ("translated", "reviewed"):
                continue
            if not entry.translation:
                continue

            modified = entry.translation
            for jp_name, en_canonical in sorted_names:
                if jp_name not in entry.original:
                    continue
                if en_canonical in modified:
                    continue  # Already correct

                # Tokenize and fuzzy-match each word
                words = modified.split()
                new_words = []
                replaced = False
                for word in words:
                    # Strip trailing punctuation for comparison
                    stripped = word.rstrip(".,!?;:'\")-]}")
                    suffix = word[len(stripped):]

                    # Skip if this word is itself a canonical name
                    if stripped in canonical_en:
                        new_words.append(word)
                        continue

                    # Only match proper-noun-like words (capitalized)
                    if (
                        stripped
                        and stripped[0].isupper()
                        and len(stripped) >= 3
                        and abs(len(stripped) - len(en_canonical)) <= 2
                        and stripped != en_canonical
                        and SequenceMatcher(
                            None, stripped.lower(), en_canonical.lower()
                        ).ratio() > 0.75
                    ):
                        new_words.append(en_canonical + suffix)
                        replaced = True
                    else:
                        new_words.append(word)

                if replaced:
                    modified = " ".join(new_words)

            if modified != entry.translation:
                entry.translation = modified
                variants_fixed += 1

        return caps_fixed, dupes_fixed, variants_fixed

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

    def _auto_export_review(self):
        """Auto-export a review TXT file after batch translation.

        File is named: Review_{Provider}_{Model}_{Date}.txt
        Saved next to the project state file.
        """
        if not self.project.entries or not self.project.project_path:
            return
        from datetime import datetime

        # Build filename: Review_DeepSeek_deepseek-chat_2026-02-15.txt
        provider = self.client.provider.replace(" ", "").replace("(", "").replace(")", "")
        model = self.client.model.replace("/", "-").replace(":", "-")
        date_str = datetime.now().strftime("%Y-%m-%d")
        filename = f"Review_{provider}_{model}_{date_str}.txt"
        path = os.path.join(self.project.project_path, filename)

        translated = [e for e in self.project.entries
                      if e.status in ("translated", "reviewed")]
        if not translated:
            return

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"# Translation Review\n")
                f.write(f"# Project: {self.project.project_path}\n")
                f.write(f"# Provider: {self.client.provider}\n")
                f.write(f"# Model: {self.client.model}\n")
                f.write(f"# Date: {date_str}\n")
                cost_str = self.client.format_session_cost()
                if cost_str:
                    f.write(f"# {cost_str}\n")
                f.write(f"# Entries: {len(translated)} translated"
                        f" / {self.project.total} total\n\n")

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

            self.statusbar.showMessage(
                f"Review file saved: {filename}", 8000)
        except OSError as e:
            self.statusbar.showMessage(
                f"Review file export failed: {e}", 5000)

    # ── Translation patch create / apply ─────────────────────────

    def _create_patch(self):
        """Create a distributable translation patch zip."""
        if not self.project.entries:
            return

        translated = self.project.translated_count
        if translated == 0:
            QMessageBox.information(
                self, "Create Patch",
                "No translated entries to export."
            )
            return

        # Get game title for metadata
        game_title = ""
        for e in self.project.entries:
            if e.field == "gameTitle" and e.translation:
                game_title = e.translation
                break
        if not game_title:
            for e in self.project.entries:
                if e.field == "gameTitle":
                    game_title = e.original
                    break

        # Prompt for patch version
        version, ok = QInputDialog.getText(
            self, "Patch Version",
            f"Creating patch for: {game_title or 'RPG Maker Game'}\n"
            f"Entries: {translated} translated, {self.project.reviewed_count} reviewed\n\n"
            f"Patch version:",
            text="1.0",
        )
        if not ok:
            return

        # Default filename
        safe_title = "".join(c for c in game_title if c.isalnum() or c in " _-")[:50].strip()
        default_name = f"{safe_title} Translation Patch v{version}.zip" if safe_title else "translation_patch.zip"

        path, _ = QFileDialog.getSaveFileName(
            self, "Save Translation Patch", default_name, "Zip Files (*.zip)"
        )
        if not path:
            return

        try:
            self.project.export_patch(path, game_title=game_title,
                                      patch_version=version)
            QMessageBox.information(
                self, "Patch Created",
                f"Translation patch saved to:\n{path}\n\n"
                f"{translated} translated entries\n"
                f"{len(self.project.glossary)} glossary entries\n\n"
                f"This file contains NO game data — safe to distribute."
            )
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to create patch:\n{e}")

    def _apply_patch(self):
        """Apply a translation patch zip to the current project."""
        if not self.project.entries:
            return

        path, _ = QFileDialog.getOpenFileName(
            self, "Select Translation Patch", "", "Zip Files (*.zip)"
        )
        if not path:
            return

        try:
            patch_project = TranslationProject.import_patch(path)
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to read patch:\n{e}")
            return

        # Show patch info and confirm
        meta = getattr(patch_project, "_patch_metadata", {})
        patch_translated = sum(
            1 for e in patch_project.entries
            if e.status in ("translated", "reviewed")
        )
        current_untranslated = self.project.untranslated_count

        info = (
            f"Patch: {meta.get('game_title', 'Unknown')}"
            f" v{meta.get('patch_version', '?')}\n"
            f"Created: {meta.get('created', 'Unknown')}\n\n"
            f"Patch contains: {patch_translated} translations, "
            f"{len(patch_project.glossary)} glossary entries\n"
            f"Current project: {self.project.total} entries "
            f"({current_untranslated} untranslated)\n\n"
            f"Apply patch translations to untranslated entries?"
        )

        reply = QMessageBox.question(
            self, "Apply Translation Patch", info,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Reuse existing import_translations logic
        stats = self.project.import_translations(patch_project)

        # Import glossary entries that don't conflict
        imported_glossary = 0
        for jp, en in patch_project.glossary.items():
            if jp not in self.project.glossary:
                self.project.glossary[jp] = en
                imported_glossary += 1
        self._rebuild_glossary()

        # Import actor genders if not already set
        imported_genders = 0
        for actor_id, gender in patch_project.actor_genders.items():
            if actor_id not in self.project.actor_genders:
                self.project.actor_genders[actor_id] = gender
                imported_genders += 1

        # Refresh UI
        self.trans_table.set_entries(self.project.entries)
        self.file_tree.load_project(self.project)

        total_imported = stats["by_id"] + stats["by_text"]
        QMessageBox.information(
            self, "Patch Applied",
            f"Imported {total_imported} translations:\n"
            f"  \u2022 {stats['by_id']} matched by exact position\n"
            f"  \u2022 {stats['by_text']} matched by identical text\n"
            f"  \u2022 {stats['new']} entries not in patch (need translation)\n"
            f"  \u2022 {stats['skipped']} already translated (kept)\n"
            + (f"  \u2022 {imported_glossary} glossary entries imported\n"
               if imported_glossary else "")
            + (f"  \u2022 {imported_genders} actor genders imported\n"
               if imported_genders else "")
        )

    def _export_patch_zip(self):
        """Export translated game files + install.bat as a distributable zip."""
        if not self.project.entries:
            return
        if not self.project.project_path or not os.path.isdir(self.project.project_path):
            QMessageBox.warning(
                self, "Project Folder Not Found",
                f"The project folder no longer exists:\n"
                f"{self.project.project_path or '(not set)'}\n\n"
                "Open the game project first, then try again."
            )
            return
        data_dir = self.parser._find_data_dir(self.project.project_path)
        if not data_dir:
            QMessageBox.warning(
                self, "Data Directory Not Found",
                f"Could not find a data/ folder in:\n"
                f"{self.project.project_path}\n\n"
                "The patch zip needs the original game files to produce\n"
                "translated JSON files.\n\n"
                "Make sure the game's data/ directory exists in this folder."
            )
            return

        translated = [e for e in self.project.entries
                      if e.status in ("translated", "reviewed")]
        if not translated:
            QMessageBox.information(
                self, "Nothing to Export",
                "No translated entries to export."
            )
            return

        # Get game title
        game_title = ""
        for e in self.project.entries:
            if e.field == "gameTitle" and e.translation:
                game_title = e.translation
                break
        if not game_title:
            for e in self.project.entries:
                if e.field == "gameTitle":
                    game_title = e.original
                    break

        # Default filename — prefer English game title, fall back to folder name
        safe_title = "".join(
            c for c in game_title if c.isalnum() or c in " _-"
        )[:50].strip()
        if not safe_title:
            safe_title = os.path.basename(self.project.project_path)

        # Detect RJ/RE/VJ number from folder name (DLsite product ID)
        folder = os.path.basename(self.project.project_path)
        rj_match = re.search(r'((?:RJ|RE|VJ)\d+)', folder, re.IGNORECASE)
        rj_prefix = rj_match.group(1).upper() if rj_match else ""

        if rj_prefix and rj_prefix not in safe_title.upper():
            default_name = f"{rj_prefix} - {safe_title} - ENG Translation Patch.zip"
        else:
            default_name = f"{safe_title} - ENG Translation Patch.zip"

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Patch Zip", default_name, "Zip Files (*.zip)"
        )
        if not path:
            return

        try:
            self.parser.export_patch_zip(
                self.project.project_path, self.project.entries,
                path, game_title=game_title,
                inject_wordwrap=self.plugin_analyzer.should_inject_plugin())
            QMessageBox.information(
                self, "Install Package Created",
                f"Saved to:\n{path}\n\n"
                f"{len(translated)} translated entries.\n"
                f"Includes complete data folder + install/uninstall scripts.\n\n"
                f"End users: extract into the game folder and run install.bat."
            )
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to create patch zip:\n{e}")

    def _export_game_as_patch(self):
        """Package a game folder's current files into a distributable zip.

        Works without an active project.  Useful when the game already has
        translations applied (e.g. from a previous patch) and you want to
        create a redistributable install package from the current state.
        """
        # Default to current project folder if one is loaded
        default_dir = ""
        if (self.project and self.project.project_path
                and os.path.isdir(self.project.project_path)):
            default_dir = self.project.project_path

        game_path = QFileDialog.getExistingDirectory(
            self, "Select Game Folder (containing data/ and Game.exe)",
            default_dir
        )
        if not game_path:
            return

        # Validate data folder exists
        data_dir = self.parser._find_data_dir(game_path)
        if not data_dir:
            QMessageBox.warning(
                self, "Data Directory Not Found",
                f"Could not find a data/ folder in:\n{game_path}\n\n"
                "Select the game folder that contains the data/ directory."
            )
            return

        # Warn if no backup exists (might be packaging untranslated files)
        backup_dir = data_dir + "_original"
        if not os.path.isdir(backup_dir):
            reply = QMessageBox.question(
                self, "No Original Backup Found",
                f"No backup folder found at:\n{backup_dir}\n\n"
                "This means the data/ folder may still contain untranslated "
                "Japanese files, or originals were never backed up.\n\n"
                "Package the current data/ files anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        # Get game title from System.json
        game_title = self.parser.get_game_title(game_path)

        # Build default filename
        safe_title = "".join(
            c for c in game_title if c.isalnum() or c in " _-"
        )[:50].strip()
        if not safe_title:
            safe_title = os.path.basename(game_path)

        folder = os.path.basename(game_path)
        rj_match = re.search(r'((?:RJ|RE|VJ)\d+)', folder, re.IGNORECASE)
        rj_prefix = rj_match.group(1).upper() if rj_match else ""

        if rj_prefix and rj_prefix not in safe_title.upper():
            default_name = f"{rj_prefix} - {safe_title} - ENG Translation Patch.zip"
        else:
            default_name = f"{safe_title} - ENG Translation Patch.zip"

        path, _ = QFileDialog.getSaveFileName(
            self, "Save Patch Zip", default_name, "Zip Files (*.zip)"
        )
        if not path:
            return

        try:
            stats = self.parser.export_game_folder_as_patch(
                game_path, path, game_title=game_title
            )
            msg = (
                f"Saved to:\n{path}\n\n"
                f"{stats['data_files']} data file(s) packaged"
            )
            if stats['has_plugins']:
                msg += " + plugins.js"
            msg += ".\n"
            if stats['data_original_exists']:
                msg += (
                    "\ndata_original/ detected \u2014 data/ contains "
                    "translated files (as expected)."
                )
            msg += (
                "\n\nEnd users: extract into the game folder and run install.bat."
            )
            QMessageBox.information(self, "Patch Created", msg)
        except Exception as e:
            QMessageBox.warning(
                self, "Error", f"Failed to create patch zip:\n{e}"
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

        self.batch_db_action.setEnabled(False)
        self.batch_dialogue_action.setEnabled(False)
        self.batch_action.setEnabled(False)
        self.batch_actor_action.setEnabled(False)
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
            # Title-case name-type fields (e.g. "iron sword" → "Iron Sword")
            if entry.field in self._CAPITALIZE_FIELDS and translation:
                translation = self._title_case(translation)
            entry.translation = translation
            entry.status = "translated"
            # Auto-glossary: add translated DB names so the LLM
            # uses them consistently in subsequent dialogue entries
            self._maybe_add_to_glossary(entry)
        self.trans_table.update_entry(entry_id, translation)
        self.file_tree.refresh_stats(self.project)
        # Update queue panel
        self.queue_panel.mark_entry_done(entry_id, translation, source="LLM")

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
            self._correction_thread = None
            self._correction_worker = None

        worker.done.connect(on_done)
        worker.failed.connect(on_failed)
        thread.started.connect(worker.run)
        thread.finished.connect(on_thread_finished)
        thread.start()

        # Keep references alive until thread completes
        self._correction_thread = thread
        self._correction_worker = worker

    # ── Polish selected entries ───────────────────────────────────

    def _polish_selected(self, entry_ids: list):
        """Polish grammar on selected entries via background thread."""
        if not self.client.is_available():
            QMessageBox.warning(
                self, "Ollama Not Available",
                "Cannot connect to Ollama. Make sure it's running:\n  ollama serve"
            )
            return

        entries = [self.project.get_entry_by_id(eid) for eid in entry_ids]
        entries = [e for e in entries if e and e.translation and e.translation.strip()]
        if not entries:
            return

        self.statusbar.showMessage(f"Polishing {len(entries)} entries...")

        from PyQt6.QtCore import QThread, QObject, pyqtSignal as Signal

        class _PolishWorker(QObject):
            entry_done = Signal(str, str)  # entry_id, polished_text
            finished = Signal(int)         # count polished

            def __init__(self, client, entries_to_polish):
                super().__init__()
                self.client = client
                self.entries = entries_to_polish

            def run(self):
                count = 0
                for e in self.entries:
                    result = self.client.polish(text=e.translation)
                    if result and result != e.translation:
                        self.entry_done.emit(e.id, result)
                        count += 1
                self.finished.emit(count)

        thread = QThread(self)
        worker = _PolishWorker(self.client, entries)
        worker.moveToThread(thread)

        def on_entry(eid, polished):
            entry = self.project.get_entry_by_id(eid)
            if entry:
                entry.translation = polished
                self.trans_table.update_entry(eid, polished)

        def on_finished(count):
            self.file_tree.refresh_stats(self.project)
            self.statusbar.showMessage(
                f"Polished {count}/{len(entries)} entries", 5000
            )
            thread.quit()

        def on_polish_thread_finished():
            self._polish_thread = None
            self._polish_worker = None

        worker.entry_done.connect(on_entry)
        worker.finished.connect(on_finished)
        thread.started.connect(worker.run)
        thread.finished.connect(on_polish_thread_finished)
        thread.start()

        self._polish_thread = thread
        self._polish_worker = worker

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
            if len(variants) == 1:
                # Only one unique translation — apply it directly
                reply = QMessageBox.question(
                    self, "Single Variant",
                    "The model produced only one unique translation "
                    "(all attempts gave the same result).\n\n"
                    "Apply it?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if reply == QMessageBox.StandardButton.Yes:
                    entry.translation = variants[0]
                    entry.status = "translated"
                    self.trans_table.update_entry(entry_id, variants[0])
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

        def on_variant_thread_finished():
            self._variant_thread = None
            self._variant_worker = None

        worker.done.connect(on_done)
        worker.failed.connect(on_failed)
        thread.started.connect(worker.run)
        thread.finished.connect(on_variant_thread_finished)
        thread.start()

        self._variant_thread = thread
        self._variant_worker = worker

    # ── Image Translation ─────────────────────────────────────────

    def _translate_images(self):
        """Switch to Image Translation tab and initialize it."""
        if not self.project.project_path:
            QMessageBox.warning(self, "Error", "Open a project first.")
            return

        vision_model = getattr(self.client, "vision_model", "")
        if not vision_model:
            QMessageBox.warning(
                self, "No Vision Model",
                "Set a vision model in Settings first.\n\n"
                "Recommended: qwen3-vl:8b\n"
                "Install with: ollama pull qwen3-vl:8b",
            )
            return

        self.image_panel.set_project(self.project.project_path, self.client)
        self.tabs.setCurrentWidget(self.image_panel)

    # ── Window close cleanup ──────────────────────────────────────

    def closeEvent(self, event):
        """Clean up background threads and managed Ollama on window close."""
        # Stop image translation worker if running
        self.image_panel.stop_worker()
        # Stop batch translation if running
        self.engine.cancel()
        for thread in self.engine._threads:
            if thread.isRunning():
                thread.quit()
                thread.wait(3000)

        # Stop correction/polish/variant threads if running
        for attr in ("_correction_thread", "_polish_thread", "_variant_thread"):
            thread = getattr(self, attr, None)
            if thread is not None:
                thread.quit()
                thread.wait(3000)

        # Clean up managed Ollama subprocess (if we started one)
        self.client.cleanup()

        super().closeEvent(event)
