"""Translation Wizard — one-click automated translation pipeline.

Shows after opening a project, offering Wizard (automated) or Manual mode.
Wizard chains: DB translate → Actor gender → Dialogue translate → Cleanup →
Retranslate broken → Word wrap → Export → Patch zip.
"""

import logging
from enum import Enum, auto

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)


class WizardStep(Enum):
    IDLE = auto()
    TRANSLATE_DB = auto()
    TRANSLATE_DIALOGUE = auto()
    CLEANUP = auto()
    RETRANSLATE = auto()
    WORD_WRAP = auto()
    EXPORT = auto()
    PATCH_ZIP = auto()
    DONE = auto()


class TranslationWizard(QDialog):
    """Wizard dialog for automated translation pipeline."""

    def __init__(self, main_window, parent=None):
        super().__init__(parent or main_window)
        self.mw = main_window
        self.setWindowTitle("Translation Wizard")
        self.setMinimumWidth(520)
        self.setMinimumHeight(420)
        self._current_step = WizardStep.IDLE
        self._entries_done = 0
        self._entries_total = 0
        self._build_ui()
        self._update_state()

    # ── UI ──────────────────────────────────────────────────────────

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Header
        project = self.mw.project
        total = project.total if project else 0
        translated = project.translated_count if project else 0
        remaining = total - translated

        header = QLabel(f"<b>{total}</b> entries total, "
                        f"<b>{translated}</b> translated, "
                        f"<b>{remaining}</b> remaining")
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(header)

        if translated > 0 and remaining > 0:
            note = QLabel("<i>Resuming — only untranslated entries will be processed.</i>")
            note.setAlignment(Qt.AlignmentFlag.AlignCenter)
            note.setWordWrap(True)
            layout.addWidget(note)

        layout.addSpacing(10)

        # Model selector
        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("Model:"))
        self.model_combo = QComboBox()
        self.model_combo.setMinimumWidth(280)
        model_row.addWidget(self.model_combo)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_models)
        model_row.addWidget(refresh_btn)

        model_row.addStretch()
        layout.addLayout(model_row)

        self.model_status = QLabel("")
        self.model_status.setStyleSheet("font-size: 11px;")
        layout.addWidget(self.model_status)

        # Populate models
        self._populate_models()

        layout.addSpacing(10)

        # Steps group
        steps_group = QGroupBox("Translation Pipeline")
        steps_layout = QVBoxLayout(steps_group)

        self.cb_db = QCheckBox("1. Translate database (names, items, skills, terms)")
        self.cb_dialogue = QCheckBox("2. Translate dialogue and events")
        self.cb_cleanup = QCheckBox("3. Clean up artifacts (spacing, codes, capitalization)")
        self.cb_retranslate = QCheckBox("4. Retranslate broken entries")
        self.cb_wordwrap = QCheckBox("5. Apply word wrap for message windows")
        self.cb_export = QCheckBox("6. Export translations to game files")
        self.cb_patch = QCheckBox("7. Create translation patch zip")

        # Smart defaults
        entries = project.entries if project else []
        db_entries = [e for e in entries if e.file in self.mw._DB_FILES]
        dialogue_entries = [e for e in entries if e.file not in self.mw._DB_FILES]
        has_db_untranslated = any(e.status == "untranslated" for e in db_entries)
        has_dialogue_untranslated = any(e.status == "untranslated" for e in dialogue_entries)

        self.cb_db.setChecked(has_db_untranslated)
        if not db_entries:
            self.cb_db.setText("1. Translate database (no DB entries)")
            self.cb_db.setEnabled(False)
        elif not has_db_untranslated:
            self.cb_db.setText("1. Translate database (all done)")
        self.cb_dialogue.setChecked(has_dialogue_untranslated)
        if not dialogue_entries:
            self.cb_dialogue.setText("2. Translate dialogue (no dialogue entries)")
            self.cb_dialogue.setEnabled(False)
        elif not has_dialogue_untranslated:
            self.cb_dialogue.setText("2. Translate dialogue (all done)")
        self.cb_cleanup.setChecked(True)
        self.cb_retranslate.setChecked(True)
        self.cb_wordwrap.setChecked(True)
        self.cb_export.setChecked(True)
        self.cb_patch.setChecked(False)

        if not has_db_untranslated and not has_dialogue_untranslated:
            self.cb_retranslate.setChecked(False)

        for cb in [self.cb_db, self.cb_dialogue, self.cb_cleanup,
                    self.cb_retranslate, self.cb_wordwrap, self.cb_export,
                    self.cb_patch]:
            steps_layout.addWidget(cb)

        layout.addWidget(steps_group)

        # Progress area (hidden until running)
        self.progress_widget = QWidget()
        prog_layout = QVBoxLayout(self.progress_widget)
        prog_layout.setContentsMargins(0, 0, 0, 0)

        self.step_label = QLabel("Ready")
        self.step_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.step_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        prog_layout.addWidget(self.step_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        prog_layout.addWidget(self.progress_bar)

        self.detail_label = QLabel("")
        self.detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_label.setWordWrap(True)
        prog_layout.addWidget(self.detail_label)

        self.progress_widget.setVisible(False)
        layout.addWidget(self.progress_widget)

        layout.addStretch()

        # Buttons
        self.button_box = QDialogButtonBox()
        self.start_btn = self.button_box.addButton(
            "Start Translation", QDialogButtonBox.ButtonRole.AcceptRole)
        self.start_btn.setDefault(True)
        self.cancel_btn = self.button_box.addButton(
            QDialogButtonBox.StandardButton.Cancel)

        self.start_btn.clicked.connect(self._on_start)
        self.cancel_btn.clicked.connect(self._on_cancel)

        layout.addWidget(self.button_box)

    def _populate_models(self):
        """Fetch and populate the model dropdown."""
        current_model = self.mw.client.model or ""
        try:
            models = self.mw.client.list_models()
        except Exception:
            models = []

        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        if models:
            self.model_combo.addItems(sorted(models))
            self.model_status.setText(f"{len(models)} model(s) available")
            self.model_status.setStyleSheet("font-size: 11px; color: #a6e3a1;")
        else:
            self.model_status.setText("No models found — is Ollama running?")
            self.model_status.setStyleSheet("font-size: 11px; color: #f38ba8;")

        if current_model:
            if current_model not in models:
                self.model_combo.addItem(current_model)
            self.model_combo.setCurrentText(current_model)
        self.model_combo.blockSignals(False)

    def _refresh_models(self):
        """Re-fetch models from Ollama."""
        self._populate_models()

    def _update_state(self):
        """Update button states based on current step."""
        running = self._current_step not in (WizardStep.IDLE, WizardStep.DONE)
        self.start_btn.setEnabled(not running)
        self.cancel_btn.setText("Stop" if running else "Cancel")

    # ── Step execution ──────────────────────────────────────────────

    def _on_start(self):
        """Begin the wizard pipeline."""
        # Apply selected model
        selected_model = self.model_combo.currentText().strip()
        if selected_model:
            self.mw.client.model = selected_model

        self._current_step = WizardStep.IDLE
        self.progress_widget.setVisible(True)
        self._update_state()

        # Disable all checkboxes and model selector while running
        self.model_combo.setEnabled(False)
        for cb in [self.cb_db, self.cb_dialogue, self.cb_cleanup,
                    self.cb_retranslate, self.cb_wordwrap, self.cb_export,
                    self.cb_patch]:
            cb.setEnabled(False)

        # Build step queue from checked boxes
        self._steps = []
        if self.cb_db.isChecked():
            self._steps.append(WizardStep.TRANSLATE_DB)
        if self.cb_dialogue.isChecked():
            self._steps.append(WizardStep.TRANSLATE_DIALOGUE)
        if self.cb_cleanup.isChecked():
            self._steps.append(WizardStep.CLEANUP)
        if self.cb_retranslate.isChecked():
            self._steps.append(WizardStep.RETRANSLATE)
        if self.cb_wordwrap.isChecked():
            self._steps.append(WizardStep.WORD_WRAP)
        if self.cb_export.isChecked():
            self._steps.append(WizardStep.EXPORT)
        if self.cb_patch.isChecked():
            self._steps.append(WizardStep.PATCH_ZIP)

        self._step_index = 0

        # Connect to batch engine signals for live progress
        self._connect_signals()

        # Start first step
        self._run_next_step()

    def _connect_signals(self):
        """Connect to main window's batch engine signals."""
        self.mw.engine.finished.connect(self._on_batch_step_finished)
        self.mw.engine.progress.connect(self._on_progress)

    def _disconnect_signals(self):
        """Disconnect wizard signals from batch engine."""
        for sig, slot in [
            (self.mw.engine.finished, self._on_batch_step_finished),
            (self.mw.engine.progress, self._on_progress),
        ]:
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    def _on_progress(self, done: int, total: int, text: str = ""):
        """Live progress update from batch engine."""
        if not getattr(self.mw, '_wizard_active', False):
            return
        self._entries_done = done
        self._entries_total = total
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(done)
            pct = int(done / total * 100)
            step = self._current_step
            if step == WizardStep.TRANSLATE_DB:
                self.detail_label.setText(f"Database: {done}/{total} ({pct}%)")
            elif step == WizardStep.TRANSLATE_DIALOGUE:
                self.detail_label.setText(f"Dialogue: {done}/{total} ({pct}%)")
            elif step == WizardStep.RETRANSLATE:
                self.detail_label.setText(f"Fixing: {done}/{total} ({pct}%)")

    def _run_next_step(self):
        """Execute the next step in the pipeline."""
        if self._step_index >= len(self._steps):
            self._finish()
            return

        step = self._steps[self._step_index]
        self._current_step = step
        self._update_state()

        step_num = self._step_index + 1
        total_steps = len(self._steps)

        if step == WizardStep.TRANSLATE_DB:
            self.step_label.setText(
                f"Step {step_num}/{total_steps}: Translating database...")
            self.progress_bar.setRange(0, 0)  # indeterminate until first progress
            self.detail_label.setText("Names, items, skills, enemies, terms...")
            self.mw._wizard_active = True
            self.mw._batch_all_chained = False
            QTimer.singleShot(200, lambda: self._start_batch_step("db"))

        elif step == WizardStep.TRANSLATE_DIALOGUE:
            self.step_label.setText(
                f"Step {step_num}/{total_steps}: Translating dialogue...")
            self.progress_bar.setRange(0, 0)
            self.detail_label.setText("Building glossary from database, then translating...")
            # Backfill glossary from DB translations
            self.mw._backfill_db_glossary()
            self.mw._rebuild_glossary()
            self.mw._wizard_active = True
            QTimer.singleShot(200, lambda: self._start_batch_step("dialogue"))

        elif step == WizardStep.CLEANUP:
            self.step_label.setText(
                f"Step {step_num}/{total_steps}: Cleaning up translations...")
            self.progress_bar.setRange(0, 0)
            self.detail_label.setText("Fixing artifacts, spacing, codes, capitalization...")
            QTimer.singleShot(200, self._run_cleanup)

        elif step == WizardStep.RETRANSLATE:
            untranslated = sum(1 for e in self.mw.project.entries
                               if e.status == "untranslated")
            if untranslated == 0:
                self.step_label.setText(
                    f"Step {step_num}/{total_steps}: Retranslate broken entries")
                self.detail_label.setText("No broken entries — skipping.")
                self.progress_bar.setRange(0, 1)
                self.progress_bar.setValue(1)
                self._step_index += 1
                QTimer.singleShot(500, self._run_next_step)
                return
            self.step_label.setText(
                f"Step {step_num}/{total_steps}: Retranslating {untranslated} broken entries...")
            self.progress_bar.setRange(0, 0)
            self.detail_label.setText(f"{untranslated} entries need retranslation...")
            self.mw._wizard_active = True
            QTimer.singleShot(200, lambda: self._start_batch_step("all"))

        elif step == WizardStep.WORD_WRAP:
            self.step_label.setText(
                f"Step {step_num}/{total_steps}: Applying word wrap...")
            self.progress_bar.setRange(0, 0)
            self.detail_label.setText("Wrapping text to fit message windows...")
            QTimer.singleShot(200, self._run_wordwrap)

        elif step == WizardStep.EXPORT:
            self.step_label.setText(
                f"Step {step_num}/{total_steps}: Exporting to game...")
            self.progress_bar.setRange(0, 0)
            self.detail_label.setText("Writing translations to game files...")
            QTimer.singleShot(200, self._run_export)

        elif step == WizardStep.PATCH_ZIP:
            self.step_label.setText(
                f"Step {step_num}/{total_steps}: Creating patch zip...")
            self.progress_bar.setRange(0, 0)
            self.detail_label.setText("Packaging translation patch...")
            QTimer.singleShot(200, self._run_patch_zip)

    def _start_batch_step(self, mode: str):
        """Start a batch and advance if nothing to translate."""
        log.info("Wizard: starting batch step mode=%s", mode)
        started = self.mw._start_batch(mode=mode)
        log.info("Wizard: _start_batch returned %s", started)
        if not started:
            # Nothing to translate — skip to next step
            self.mw._wizard_active = False
            self.detail_label.setText("Nothing to translate — skipping.")
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(1)
            self._step_index += 1
            QTimer.singleShot(500, self._run_next_step)

    def _on_batch_step_finished(self):
        """Called when a batch translate step finishes."""
        log.info("Wizard: _on_batch_step_finished called, _wizard_active=%s, step=%s",
                 getattr(self.mw, '_wizard_active', None), self._current_step)
        if not getattr(self.mw, '_wizard_active', False):
            return
        self.mw._wizard_active = False

        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)

        step = self._current_step
        if step == WizardStep.TRANSLATE_DB:
            db_count = sum(1 for e in self.mw.project.entries
                           if e.file in self.mw._DB_FILES
                           and e.status in ("translated", "reviewed"))
            self.detail_label.setText(f"Done — {db_count} database entries translated.")
        elif step == WizardStep.TRANSLATE_DIALOGUE:
            total_translated = self.mw.project.translated_count
            self.detail_label.setText(
                f"Done — {total_translated}/{self.mw.project.total} total entries translated.")
        elif step == WizardStep.RETRANSLATE:
            # Re-run post-processing on the freshly retranslated entries
            from ..post_processor import run_post_processing
            result = run_post_processing(self.mw.project.entries,
                                        glossary=self.mw.project.glossary)
            self.mw._autosave()
            parts = ["Retranslation complete"]
            if result.total_entries_fixed:
                parts.append(str(result))
            if result.retranslate_ids:
                parts.append(f"{len(result.retranslate_ids)} still broken")
            self.detail_label.setText(" — ".join(parts) + ".")

        self._step_index += 1
        QTimer.singleShot(500, self._run_next_step)

    def _run_cleanup(self):
        """Run post-processing cleanup (synchronous)."""
        from ..post_processor import run_post_processing

        codes_fixed = self.mw._restore_missing_codes()
        result = run_post_processing(self.mw.project.entries,
                                     glossary=self.mw.project.glossary)

        # Quote/contraction cleanup
        quotes_fixed = 0
        for entry in self.mw.project.entries:
            if not entry.translation or entry.status not in ("translated", "reviewed"):
                continue
            original_text = entry.translation
            if self.mw._JP_SPEECH_BRACKETS & set(entry.original):
                t = entry.translation
                first = t.find('"')
                last = t.rfind('"')
                if first != -1 and last > first:
                    entry.translation = t[:first] + t[first + 1:last] + t[last + 1:]
            entry.translation = self.mw._CONTRACTION_RE.sub(r"\1\2\3", entry.translation)
            if entry.translation != original_text:
                quotes_fixed += 1

        self.mw.trans_table.refresh()
        self.mw.file_tree.refresh_stats(self.mw.project)
        self.mw._autosave()

        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)

        parts = []
        if result.total_entries_fixed:
            parts.append(str(result))
        if codes_fixed:
            parts.append(f"{codes_fixed} codes restored")
        if quotes_fixed:
            parts.append(f"{quotes_fixed} quotes fixed")
        retrans = len(result.retranslate_ids)
        if retrans:
            parts.append(f"{retrans} flagged for retranslation")
        self.detail_label.setText(
            "Done — " + (", ".join(parts) if parts else "no issues found."))

        self._step_index += 1
        QTimer.singleShot(500, self._run_next_step)

    def _run_wordwrap(self):
        """Apply word wrap (synchronous)."""
        from ..text_processor import TextProcessor

        analyzer = self.mw.plugin_analyzer
        processor = TextProcessor(analyzer)
        count = 0
        for entry in self.mw.project.entries:
            if not entry.translation or entry.status not in ("translated", "reviewed"):
                continue
            if "dialog" not in entry.field and "scroll" not in entry.field:
                continue
            new_text = processor.process_entry(entry.original, entry.translation)
            if new_text != entry.translation:
                entry.translation = new_text
                count += 1

        self.mw.trans_table.refresh()
        self.mw._autosave()

        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        self.detail_label.setText(
            f"Done — {count} entries wrapped." if count else "Done — no wrapping needed.")

        self._step_index += 1
        QTimer.singleShot(500, self._run_next_step)

    def _run_export(self):
        """Export translations to game files."""
        try:
            from ..rpgmaker_mv import RPGMakerMVParser
            import re

            parser = RPGMakerMVParser()
            translated = [e for e in self.mw.project.entries
                          if e.status in ("translated", "reviewed") and e.translation]

            if not translated:
                self.detail_label.setText("No translated entries to export.")
                self.progress_bar.setRange(0, 1)
                self.progress_bar.setValue(1)
                self._step_index += 1
                QTimer.singleShot(500, self._run_next_step)
                return

            project_path = self.mw.project.project_path

            # Strip WordWrap tags if no plugin and not injecting
            inject_ww = getattr(self.mw, '_inject_wordwrap', False)
            has_plugin = (self.mw.plugin_analyzer.has_wordwrap_plugin
                          if self.mw.plugin_analyzer else False)
            if not has_plugin and not inject_ww:
                for e in translated:
                    if e.translation and "<WordWrap>" in e.translation:
                        e.translation = re.sub(
                            r'<WordWrap>', '', e.translation, flags=re.IGNORECASE)

            parser.save_project(project_path, translated)

            if inject_ww:
                chars = getattr(self.mw, '_chars_per_line', 0)
                parser.inject_wordwrap_plugin(project_path, max_chars=chars)

            disable_splash = getattr(self.mw, '_disable_splash', False)
            if disable_splash:
                parser.disable_splash_plugin(project_path)

            self.mw._autosave()
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(1)
            self.detail_label.setText(f"Done — {len(translated)} entries exported to game.")

        except Exception as exc:
            log.exception("Export failed in wizard")
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(1)
            self.detail_label.setText(f"Export failed: {exc}")

        self._step_index += 1
        QTimer.singleShot(500, self._run_next_step)

    def _run_patch_zip(self):
        """Create a translation patch zip."""
        try:
            import os
            from ..rpgmaker_mv import RPGMakerMVParser

            parser = RPGMakerMVParser()
            project_path = self.mw.project.project_path
            translated = [e for e in self.mw.project.entries
                          if e.status in ("translated", "reviewed") and e.translation]

            if not translated:
                self.detail_label.setText("No translated entries for patch.")
                self.progress_bar.setRange(0, 1)
                self.progress_bar.setValue(1)
                self._step_index += 1
                QTimer.singleShot(500, self._run_next_step)
                return

            folder_name = os.path.basename(project_path)
            zip_path = os.path.join(
                os.path.dirname(project_path),
                f"{folder_name} - Translation Patch.zip",
            )
            parser.export_patch_zip(project_path, translated, zip_path)

            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(1)
            self.detail_label.setText(f"Done — patch saved to {zip_path}")

        except Exception as exc:
            log.exception("Patch zip failed in wizard")
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(1)
            self.detail_label.setText(f"Patch zip failed: {exc}")

        self._step_index += 1
        QTimer.singleShot(500, self._run_next_step)

    def _finish(self):
        """All steps complete."""
        self._current_step = WizardStep.DONE
        self._disconnect_signals()
        self.mw._wizard_active = False
        self._update_state()

        self.step_label.setText("Translation complete!")
        self.step_label.setStyleSheet(
            "font-weight: bold; font-size: 14px; color: #a6e3a1;")
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)

        total = self.mw.project.total
        translated = self.mw.project.translated_count
        remaining = total - translated

        parts = [f"{translated}/{total} translated"]
        if remaining:
            parts.append(f"{remaining} remaining")
        self.detail_label.setText(" | ".join(parts))

        self.mw.trans_table.refresh()
        self.mw.file_tree.refresh_stats(self.mw.project)
        self.mw.event_viewer.refresh_stats()

        self.start_btn.setText("Close")
        self.start_btn.setEnabled(True)
        self.start_btn.clicked.disconnect()
        self.start_btn.clicked.connect(self.accept)
        self.cancel_btn.setVisible(False)

    def _on_cancel(self):
        """Cancel or close the wizard."""
        if self._current_step not in (WizardStep.IDLE, WizardStep.DONE):
            self.mw.engine.cancel()
            self._disconnect_signals()
            self.mw._wizard_active = False
            self._current_step = WizardStep.DONE
            self._update_state()
            self.step_label.setText("Stopped")
            self.detail_label.setText("Translation stopped. Progress has been saved.")
            self.mw._autosave()
            self.start_btn.setText("Close")
            self.start_btn.setEnabled(True)
            self.start_btn.clicked.disconnect()
            self.start_btn.clicked.connect(self.accept)
            self.cancel_btn.setVisible(False)
        else:
            self.reject()


class WizardChoiceDialog(QDialog):
    """Simple dialog: Wizard mode or Manual mode?"""

    WIZARD = 1
    MANUAL = 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Translation Mode")
        self.setMinimumWidth(420)
        self.choice = self.MANUAL

        layout = QVBoxLayout(self)

        label = QLabel("How would you like to translate this game?")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("font-size: 14px; font-weight: bold; margin: 10px;")
        layout.addWidget(label)

        layout.addSpacing(10)

        wizard_btn = self._make_option(
            "Translation Wizard",
            "Automated pipeline — translates, cleans up, and exports\n"
            "in one click. Recommended for most users.",
            self.WIZARD,
        )
        layout.addWidget(wizard_btn)

        layout.addSpacing(8)

        manual_btn = self._make_option(
            "Manual Mode",
            "Full control — use menus to translate step by step.\n"
            "For experienced users or targeted re-translations.",
            self.MANUAL,
        )
        layout.addWidget(manual_btn)

        layout.addStretch()

    def _make_option(self, title: str, description: str, choice: int) -> QWidget:
        """Create a clickable option card."""
        btn = QPushButton()
        btn.setMinimumHeight(70)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet("""
            QPushButton {
                text-align: left;
                padding: 12px 16px;
                border: 2px solid #555;
                border-radius: 8px;
                font-size: 13px;
            }
            QPushButton:hover {
                border-color: #89b4fa;
                background-color: rgba(137, 180, 250, 0.1);
            }
        """)
        btn.setText(f"{title}\n{description}")
        btn.clicked.connect(lambda: self._select(choice))
        return btn

    def _select(self, choice: int):
        self.choice = choice
        self.accept()
