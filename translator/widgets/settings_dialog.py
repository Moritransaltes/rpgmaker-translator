"""Settings dialog for configuring Ollama connection and translation options."""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLineEdit, QComboBox, QPlainTextEdit, QPushButton,
    QLabel, QGroupBox, QMessageBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QTabWidget, QWidget, QSpinBox,
    QCheckBox, QMenu, QApplication, QProgressDialog,
)
from PyQt6.QtCore import Qt

from ..ollama_client import (
    OllamaClient, SYSTEM_PROMPT, SUGOI_SYSTEM_PROMPT, TARGET_LANGUAGES,
    build_system_prompt, is_sugoi_model,
)
from ..rpgmaker_mv import RPGMakerMVParser
from ..default_glossary import CATEGORIES as DEFAULT_GLOSSARY_CATEGORIES


class SettingsDialog(QDialog):
    """Dialog for configuring Ollama URL, model, prompt, and glossary."""

    def __init__(self, client: OllamaClient, parent=None, parser: RPGMakerMVParser = None,
                 dark_mode: bool = True, plugin_analyzer=None, engine=None,
                 general_glossary=None, project_glossary=None):
        super().__init__(parent)
        self.client = client
        self.parser = parser
        self.dark_mode = dark_mode
        self.plugin_analyzer = plugin_analyzer
        self.engine = engine
        self._general_glossary_init = general_glossary or {}
        self._project_glossary_init = project_glossary or {}
        self.general_glossary = {}   # result after save
        self.project_glossary = {}   # result after save
        self.setWindowTitle("Settings")
        self.setMinimumSize(600, 500)
        self._build_ui()
        self._load_current()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        tabs = QTabWidget()
        layout.addWidget(tabs)

        # ── Tab 1: Connection ──────────────────────────────────────
        conn_tab = QWidget()
        conn_layout = QVBoxLayout(conn_tab)

        conn_group = QGroupBox("Ollama Connection")
        conn_form = QFormLayout(conn_group)

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("http://localhost:11434")
        conn_form.addRow("Ollama URL:", self.url_edit)

        model_row = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setMinimumWidth(250)
        model_row.addWidget(self.model_combo)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self._refresh_models)
        model_row.addWidget(self.refresh_btn)

        self.test_btn = QPushButton("Test Connection")
        self.test_btn.clicked.connect(self._test_connection)
        model_row.addWidget(self.test_btn)

        conn_form.addRow("Model:", model_row)

        self.model_hint_label = QLabel("")
        self.model_hint_label.setWordWrap(True)
        conn_form.addRow("", self.model_hint_label)

        self.status_label = QLabel("")
        conn_form.addRow("", self.status_label)

        self.lang_combo = QComboBox()
        for name, stars, tip in TARGET_LANGUAGES:
            self.lang_combo.addItem(f"{name}  {stars}", userData=name)
            self.lang_combo.setItemData(self.lang_combo.count() - 1, tip, Qt.ItemDataRole.ToolTipRole)
        self.lang_combo.currentIndexChanged.connect(self._on_language_changed)
        conn_form.addRow("Target Language:", self.lang_combo)

        self.model_combo.currentTextChanged.connect(self._on_model_changed)

        conn_layout.addWidget(conn_group)

        # Prompt
        prompt_group = QGroupBox("Translation Prompt")
        prompt_layout = QVBoxLayout(prompt_group)
        prompt_layout.addWidget(QLabel("System prompt sent to the LLM:"))
        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setMinimumHeight(120)
        prompt_layout.addWidget(self.prompt_edit)

        reset_btn = QPushButton("Reset to Default")
        reset_btn.clicked.connect(self._reset_prompt_to_default)
        prompt_layout.addWidget(reset_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        conn_layout.addWidget(prompt_group)

        # Translation options
        opts_group = QGroupBox("Translation Options")
        opts_form = QFormLayout(opts_group)

        self.context_spin = QSpinBox()
        self.context_spin.setRange(0, 20)
        self.context_spin.setToolTip(
            "Number of recent dialogue lines included as context for the LLM.\n"
            "Higher = better coherence but slower and uses more VRAM."
        )
        opts_form.addRow("Context window size:", self.context_spin)

        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 16)
        self.workers_spin.setToolTip(
            "Number of parallel translation requests sent to Ollama.\n"
            "Higher = faster batch translation, but uses more VRAM.\n"
            "Ollama will be automatically restarted when this changes."
        )
        opts_form.addRow("Parallel workers:", self.workers_spin)

        self.batch_spin = QSpinBox()
        self.batch_spin.setRange(1, 20)
        self.batch_spin.setSpecialValueText("Disabled (single-entry)")
        self.batch_spin.setToolTip(
            "Number of entries sent per LLM request as a JSON batch.\n"
            "1 = single-entry mode (recommended for local Ollama).\n"
            "5-10 = useful for cloud APIs (reduces network round trips).\n\n"
            "For local LLMs, batching does NOT improve speed\n"
            "(GPU generates the same tokens either way).\n"
            "It's mainly useful for cloud APIs where per-request\n"
            "latency and rate limits are the bottleneck.\n\n"
            "If the LLM returns invalid JSON, entries automatically\n"
            "fall back to single-entry translation."
        )
        opts_form.addRow("Batch size:", self.batch_spin)

        self.history_spin = QSpinBox()
        self.history_spin.setRange(0, 30)
        self.history_spin.setSpecialValueText("Disabled")
        self.history_spin.setToolTip(
            "Number of recent translation pairs sent to the LLM as context.\n"
            "The LLM sees its own previous translations, improving style\n"
            "consistency and pronoun resolution across sequential dialogue.\n\n"
            "0 = disabled (no history sent).\n"
            "10 = recommended (good balance of context vs. speed).\n"
            "Higher values use more context window but may improve consistency."
        )
        opts_form.addRow("Translation history:", self.history_spin)

        self.wordwrap_spin = QSpinBox()
        self.wordwrap_spin.setRange(0, 200)
        self.wordwrap_spin.setSpecialValueText("Auto-detect")
        self.wordwrap_spin.setToolTip(
            "Characters per line for word wrapping.\n"
            "0 = auto-detect from game plugins (default).\n"
            "Set manually if auto-detection gives wrong results."
        )
        opts_form.addRow("Word wrap chars/line:", self.wordwrap_spin)

        conn_layout.addWidget(opts_group)

        # Appearance
        appear_group = QGroupBox("Appearance")
        appear_form = QFormLayout(appear_group)

        self.dark_mode_check = QCheckBox("Enable dark mode (Catppuccin theme)")
        appear_form.addRow(self.dark_mode_check)

        conn_layout.addWidget(appear_group)
        tabs.addTab(conn_tab, "Connection && Prompt")

        # ── Tab 2: General Glossary (persists across all projects) ─
        general_tab = QWidget()
        general_layout = QVBoxLayout(general_tab)

        general_layout.addWidget(QLabel(
            "General glossary terms apply to ALL projects and persist across sessions.\n"
            "Use this for common eroge/RPG terms, honorifics, and recurring vocabulary."
        ))

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

        # Load Defaults dropdown — preset categories for common terms
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

        # ── Tab 3: Project Glossary (per-project terms) ───────────
        glossary_tab = QWidget()
        glossary_layout = QVBoxLayout(glossary_tab)

        glossary_layout.addWidget(QLabel(
            "Project-specific term translations (character names, locations, items).\n"
            "These are saved with the project state. Overrides general glossary if both define a term."
        ))

        self.glossary_table = QTableWidget()
        self.glossary_table.setColumnCount(2)
        self.glossary_table.setHorizontalHeaderLabels(["Japanese Term", "English Translation"])
        header = self.glossary_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.glossary_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        glossary_layout.addWidget(self.glossary_table)

        glossary_btn_row = QHBoxLayout()
        add_btn = QPushButton("Add Row")
        add_btn.clicked.connect(self._add_glossary_row)
        glossary_btn_row.addWidget(add_btn)

        remove_btn = QPushButton("Remove Selected")
        remove_btn.clicked.connect(self._remove_glossary_rows)
        glossary_btn_row.addWidget(remove_btn)

        clear_btn = QPushButton("Clear All")
        clear_btn.clicked.connect(self._clear_glossary)
        glossary_btn_row.addWidget(clear_btn)

        glossary_btn_row.addStretch()
        glossary_layout.addLayout(glossary_btn_row)

        tabs.addTab(glossary_tab, "Project Glossary")

        # ── Bottom buttons ─────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        layout.addLayout(btn_row)

    def _load_current(self):
        """Populate fields from current client settings."""
        # Save originals so we can restore on cancel / detect changes
        self._orig_url = self.client.base_url
        self._orig_model = self.client.model
        self._orig_workers = self.engine.num_workers if self.engine else 2
        self._orig_language = self.client.target_language
        self.url_edit.setText(self.client.base_url)
        self.model_combo.setCurrentText(self.client.model)
        # Set language dropdown (find matching item by userData)
        for i in range(self.lang_combo.count()):
            if self.lang_combo.itemData(i) == self.client.target_language:
                self.lang_combo.setCurrentIndex(i)
                break
        self.prompt_edit.setPlainText(self.client.system_prompt)
        self.context_spin.setValue(self.parser.context_size if self.parser else 3)
        self.workers_spin.setValue(self.engine.num_workers if self.engine else 2)
        self.batch_spin.setValue(self.engine.batch_size if self.engine else 5)
        self.history_spin.setValue(self.engine.max_history if self.engine else 10)
        # Word wrap: 0 = auto-detect, >0 = manual override
        if self.plugin_analyzer and getattr(self.plugin_analyzer, '_manual_chars_per_line', 0):
            self.wordwrap_spin.setValue(self.plugin_analyzer._manual_chars_per_line)
        else:
            self.wordwrap_spin.setValue(0)
        self.dark_mode_check.setChecked(self.dark_mode)
        self._load_general_glossary()
        self._load_glossary()
        self._refresh_models()

    def _load_glossary(self):
        """Load project glossary into table."""
        self.glossary_table.setRowCount(0)
        for jp, en in self._project_glossary_init.items():
            row = self.glossary_table.rowCount()
            self.glossary_table.insertRow(row)
            self.glossary_table.setItem(row, 0, QTableWidgetItem(jp))
            self.glossary_table.setItem(row, 1, QTableWidgetItem(en))
        # Always have at least one empty row for easy adding
        if self.glossary_table.rowCount() == 0:
            self._add_glossary_row()

    def _add_glossary_row(self):
        """Add an empty row to the glossary table."""
        row = self.glossary_table.rowCount()
        self.glossary_table.insertRow(row)
        self.glossary_table.setItem(row, 0, QTableWidgetItem(""))
        self.glossary_table.setItem(row, 1, QTableWidgetItem(""))

    def _remove_glossary_rows(self):
        """Remove selected rows from the glossary table."""
        rows = sorted(set(idx.row() for idx in self.glossary_table.selectedIndexes()), reverse=True)
        for row in rows:
            self.glossary_table.removeRow(row)

    def _clear_glossary(self):
        """Clear all glossary rows."""
        self.glossary_table.setRowCount(0)
        self._add_glossary_row()

    # ── General glossary table operations ─────────────────────────

    def _load_general_glossary(self):
        """Load general glossary into table."""
        self.general_table.setRowCount(0)
        for jp, en in self._general_glossary_init.items():
            row = self.general_table.rowCount()
            self.general_table.insertRow(row)
            self.general_table.setItem(row, 0, QTableWidgetItem(jp))
            self.general_table.setItem(row, 1, QTableWidgetItem(en))
        if self.general_table.rowCount() == 0:
            self._add_general_row()

    def _add_general_row(self):
        """Add an empty row to the general glossary table."""
        row = self.general_table.rowCount()
        self.general_table.insertRow(row)
        self.general_table.setItem(row, 0, QTableWidgetItem(""))
        self.general_table.setItem(row, 1, QTableWidgetItem(""))

    def _remove_general_rows(self):
        """Remove selected rows from the general glossary table."""
        rows = sorted(set(idx.row() for idx in self.general_table.selectedIndexes()), reverse=True)
        for row in rows:
            self.general_table.removeRow(row)

    def _clear_general(self):
        """Clear all general glossary rows."""
        self.general_table.setRowCount(0)
        self._add_general_row()

    def _get_general_glossary(self) -> dict:
        """Read general glossary from table into a dict."""
        glossary = {}
        for row in range(self.general_table.rowCount()):
            jp_item = self.general_table.item(row, 0)
            en_item = self.general_table.item(row, 1)
            jp = jp_item.text().strip() if jp_item else ""
            en = en_item.text().strip() if en_item else ""
            if jp and en:
                glossary[jp] = en
        return glossary

    # ── Default glossary loading (into general table) ─────────────

    def _load_default_category(self, category: str):
        """Merge a default glossary category into the general glossary table."""
        entries = DEFAULT_GLOSSARY_CATEGORIES.get(category, {})
        self._merge_into_general(entries)

    def _load_all_defaults(self):
        """Merge all default glossary categories into the general glossary table."""
        from ..default_glossary import get_all_defaults
        self._merge_into_general(get_all_defaults())

    def _merge_into_general(self, entries: dict):
        """Add entries to the general glossary table, skipping any JP terms already present."""
        existing = self._get_general_glossary()
        added = 0
        for jp, en in entries.items():
            if jp in existing:
                continue
            row = self.general_table.rowCount()
            # Replace the trailing empty row if it exists
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

    def _get_glossary(self) -> dict:
        """Read glossary from table into a dict."""
        glossary = {}
        for row in range(self.glossary_table.rowCount()):
            jp_item = self.glossary_table.item(row, 0)
            en_item = self.glossary_table.item(row, 1)
            jp = jp_item.text().strip() if jp_item else ""
            en = en_item.text().strip() if en_item else ""
            if jp and en:
                glossary[jp] = en
        return glossary

    def _refresh_models(self):
        """Fetch available models from Ollama."""
        self.client.base_url = self.url_edit.text().strip() or "http://localhost:11434"
        models = self.client.list_models()
        current = self.model_combo.currentText()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        if models:
            # Sort: Sugoi models first (recommended for JP→EN), then alphabetical
            sugoi = sorted(m for m in models if is_sugoi_model(m))
            others = sorted(m for m in models if not is_sugoi_model(m))
            for m in sugoi:
                self.model_combo.addItem(m)
                idx = self.model_combo.count() - 1
                self.model_combo.setItemData(
                    idx, "Recommended for JP→EN (Sugoi — VN/RPG specialized)",
                    Qt.ItemDataRole.ToolTipRole,
                )
            for m in others:
                self.model_combo.addItem(m)
            if current in models:
                self.model_combo.setCurrentText(current)
            self.status_label.setText(f"Found {len(models)} model(s)")
            self.status_label.setStyleSheet("color: green;")
        else:
            self.model_combo.setCurrentText(current)
            self.status_label.setText("Could not fetch models -- is Ollama running?")
            self.status_label.setStyleSheet("color: red;")
        self.model_combo.blockSignals(False)
        # Update hint label for current model
        self._on_model_changed(self.model_combo.currentText())

    def _test_connection(self):
        """Test if Ollama is reachable."""
        self.client.base_url = self.url_edit.text().strip() or "http://localhost:11434"
        if self.client.is_available():
            QMessageBox.information(self, "Connection OK", "Successfully connected to Ollama!")
        else:
            QMessageBox.warning(self, "Connection Failed",
                                "Cannot reach Ollama. Make sure it's running:\n  ollama serve")

    def _on_language_changed(self, index: int):
        """Auto-update system prompt when target language changes."""
        new_lang = self.lang_combo.itemData(index)
        if not new_lang:
            return
        # Only auto-update if the prompt matches the template for the old language/model
        old_lang = self._orig_language
        current_model = self.model_combo.currentText()
        current_prompt = self.prompt_edit.toPlainText().strip()
        old_prompt = build_system_prompt(old_lang, model=current_model)
        if current_prompt == old_prompt.strip():
            self.prompt_edit.setPlainText(build_system_prompt(new_lang, model=current_model))
            self._orig_language = new_lang

    def _on_model_changed(self, model_name: str):
        """Auto-update system prompt and hint label when model changes."""
        current_lang = self.lang_combo.currentData() or "English"
        current_prompt = self.prompt_edit.toPlainText().strip()

        # Update model hint label
        if is_sugoi_model(model_name):
            if current_lang in ("English", "Pig Latin"):
                self.model_hint_label.setText(
                    "Sugoi detected — optimized JP→EN prompt will be used"
                )
                self.model_hint_label.setStyleSheet("color: #a6e3a1;")
            else:
                self.model_hint_label.setText(
                    "Sugoi is JP→EN only — using general prompt for " + current_lang
                )
                self.model_hint_label.setStyleSheet("color: #fab387;")
        else:
            self.model_hint_label.setText("")

        # Auto-switch prompt if it matches any known template
        if self._is_known_prompt_template(current_prompt):
            new_prompt = build_system_prompt(current_lang, model=model_name)
            self.prompt_edit.setPlainText(new_prompt)

    def _is_known_prompt_template(self, prompt: str) -> bool:
        """Check if the prompt matches any auto-generated template."""
        p = prompt.strip()
        return (
            p == SYSTEM_PROMPT.strip()
            or p == SUGOI_SYSTEM_PROMPT.strip()
            or p == build_system_prompt(self.lang_combo.currentData() or "English").strip()
        )

    def _reset_prompt_to_default(self):
        """Reset prompt to the correct default for the current model."""
        current_model = self.model_combo.currentText()
        current_lang = self.lang_combo.currentData() or "English"
        self.prompt_edit.setPlainText(build_system_prompt(current_lang, model=current_model))

    def reject(self):
        """Revert any URL/model changes made during the dialog."""
        self.client.base_url = self._orig_url
        self.client.model = self._orig_model
        super().reject()

    def _save(self):
        """Apply settings and close."""
        self.client.base_url = self.url_edit.text().strip() or "http://localhost:11434"
        self.client.model = self.model_combo.currentText().strip()
        self.client.system_prompt = self.prompt_edit.toPlainText().strip() or SYSTEM_PROMPT
        self.client.target_language = self.lang_combo.currentData() or "English"
        # Store glossaries as results — main window handles the merge
        self.general_glossary = self._get_general_glossary()
        self.project_glossary = self._get_glossary()
        if self.parser:
            self.parser.context_size = self.context_spin.value()

        new_workers = self.workers_spin.value()
        if self.engine:
            self.engine.num_workers = new_workers
            self.engine.batch_size = self.batch_spin.value()
            self.engine.max_history = self.history_spin.value()

        # Auto-restart Ollama if workers count changed
        if new_workers != self._orig_workers:
            self._restart_ollama(new_workers)

        # Word wrap override
        if self.plugin_analyzer:
            manual = self.wordwrap_spin.value()
            self.plugin_analyzer._manual_chars_per_line = manual
            if manual > 0:
                self.plugin_analyzer.chars_per_line = manual
        self.dark_mode = self.dark_mode_check.isChecked()
        self.accept()

    def _restart_ollama(self, num_parallel: int):
        """Restart Ollama with OLLAMA_NUM_PARALLEL matching the new worker count."""
        progress = QProgressDialog(
            f"Restarting Ollama with {num_parallel} parallel slots...",
            None, 0, 0, self,
        )
        progress.setWindowTitle("Restarting Ollama")
        progress.setMinimumDuration(0)
        progress.setCancelButton(None)
        progress.show()
        QApplication.processEvents()

        ok = self.client.restart_server(num_parallel)

        progress.close()

        if ok:
            QMessageBox.information(
                self, "Ollama Restarted",
                f"Ollama restarted with OLLAMA_NUM_PARALLEL={num_parallel}.\n"
                f"Workers set to {num_parallel}.",
            )
        else:
            QMessageBox.warning(
                self, "Ollama Restart Failed",
                f"Could not restart Ollama with {num_parallel} parallel slots.\n\n"
                "You may need to restart it manually:\n"
                f"  1. net stop OllamaService\n"
                f"  2. set OLLAMA_NUM_PARALLEL={num_parallel}\n"
                f"  3. ollama serve",
            )

    def get_system_prompt(self) -> str:
        return self.prompt_edit.toPlainText()
