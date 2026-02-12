"""Settings dialog for configuring Ollama connection and translation options."""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLineEdit, QComboBox, QPlainTextEdit, QPushButton,
    QLabel, QGroupBox, QMessageBox, QSpinBox,
    QCheckBox, QApplication, QProgressDialog,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal

from ..ollama_client import (
    OllamaClient, SYSTEM_PROMPT, SUGOI_SYSTEM_PROMPT, TARGET_LANGUAGES,
    build_system_prompt, is_sugoi_model,
)
from ..rpgmaker_mv import RPGMakerMVParser


class _ModelFetcher(QThread):
    """Background thread to fetch model list from Ollama without blocking UI."""
    done = pyqtSignal(list)

    def __init__(self, client, url):
        super().__init__()
        self._client = client
        self._url = url

    def run(self):
        old_url = self._client.base_url
        self._client.base_url = self._url
        models = self._client.list_models()
        self._client.base_url = old_url
        self.done.emit(models)


class SettingsDialog(QDialog):
    """Dialog for configuring Ollama URL, model, prompt, and options."""

    def __init__(self, client: OllamaClient, parent=None, parser: RPGMakerMVParser = None,
                 dark_mode: bool = True, plugin_analyzer=None, engine=None):
        super().__init__(parent)
        self.client = client
        self.parser = parser
        self.dark_mode = dark_mode
        self.plugin_analyzer = plugin_analyzer
        self.engine = engine
        self.setWindowTitle("Settings")
        self.setMinimumSize(600, 500)
        self._build_ui()
        self._load_current()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # ── Connection & Prompt ──────────────────────────────────────
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

        # Vision model (for image translation OCR)
        vision_row = QHBoxLayout()
        self.vision_combo = QComboBox()
        self.vision_combo.setEditable(True)
        self.vision_combo.setMinimumWidth(250)
        self.vision_combo.setToolTip(
            "Vision model for image OCR (e.g. qwen3-vl:8b).\n"
            "Used by Translate Images to detect Japanese text in game images.\n"
            "Leave empty to disable image translation."
        )
        vision_row.addWidget(self.vision_combo)

        self.vision_refresh_btn = QPushButton("Refresh")
        self.vision_refresh_btn.clicked.connect(self._refresh_vision_models)
        vision_row.addWidget(self.vision_refresh_btn)

        conn_form.addRow("Vision Model:", vision_row)

        self.status_label = QLabel("")
        conn_form.addRow("", self.status_label)

        self.lang_combo = QComboBox()
        for name, stars, tip in TARGET_LANGUAGES:
            self.lang_combo.addItem(f"{name}  {stars}", userData=name)
            self.lang_combo.setItemData(self.lang_combo.count() - 1, tip, Qt.ItemDataRole.ToolTipRole)
        self.lang_combo.currentIndexChanged.connect(self._on_language_changed)
        conn_form.addRow("Target Language:", self.lang_combo)

        self.model_combo.currentTextChanged.connect(self._on_model_changed)

        layout.addWidget(conn_group)

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

        layout.addWidget(prompt_group)

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

        layout.addWidget(opts_group)

        # Appearance
        appear_group = QGroupBox("Appearance")
        appear_form = QFormLayout(appear_group)

        self.dark_mode_check = QCheckBox("Enable dark mode (Catppuccin theme)")
        appear_form.addRow(self.dark_mode_check)

        layout.addWidget(appear_group)

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
        self._orig_url = self.client.base_url
        self._orig_model = self.client.model
        self._orig_workers = self.engine.num_workers if self.engine else 2
        self._orig_language = self.client.target_language
        self.url_edit.setText(self.client.base_url)
        self.model_combo.setCurrentText(self.client.model)
        for i in range(self.lang_combo.count()):
            if self.lang_combo.itemData(i) == self.client.target_language:
                self.lang_combo.setCurrentIndex(i)
                break
        self.prompt_edit.setPlainText(self.client.system_prompt)
        self.context_spin.setValue(self.parser.context_size if self.parser else 3)
        self.workers_spin.setValue(self.engine.num_workers if self.engine else 2)
        self.batch_spin.setValue(self.engine.batch_size if self.engine else 5)
        self.history_spin.setValue(self.engine.max_history if self.engine else 10)
        if self.plugin_analyzer and getattr(self.plugin_analyzer, '_manual_chars_per_line', 0):
            self.wordwrap_spin.setValue(self.plugin_analyzer._manual_chars_per_line)
        else:
            self.wordwrap_spin.setValue(0)
        self.dark_mode_check.setChecked(self.dark_mode)
        self.vision_combo.setCurrentText(getattr(self.client, "vision_model", "") or "")
        # Fetch models in background thread so the dialog appears instantly
        self._model_fetcher = _ModelFetcher(
            self.client,
            self.url_edit.text().strip() or "http://localhost:11434",
        )
        self._model_fetcher.done.connect(self._on_models_fetched)
        self.status_label.setText("Fetching models...")
        self._model_fetcher.start()

    # ── Model refresh ────────────────────────────────────────────────

    def _on_models_fetched(self, models: list):
        """Called when the background model fetch completes."""
        self._populate_model_combo(models)
        self._populate_vision_combo(models)

    def _populate_vision_combo(self, all_models: list):
        """Populate the vision model combo from an already-fetched model list."""
        _VISION_KEYWORDS = ("vl", "vision", "llava", "minicpm-v", "bakllava")
        models = [m for m in all_models
                  if any(kw in m.lower() for kw in _VISION_KEYWORDS)]
        current = self.vision_combo.currentText()
        self.vision_combo.blockSignals(True)
        self.vision_combo.clear()
        if models:
            for m in sorted(models):
                self.vision_combo.addItem(m)
            if current in models:
                self.vision_combo.setCurrentText(current)
        else:
            self.vision_combo.setCurrentText(current)
        self.vision_combo.blockSignals(False)

    def _populate_model_combo(self, models: list):
        """Populate the model combo from an already-fetched model list."""
        current = self.model_combo.currentText()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        if models:
            sugoi = sorted(m for m in models if is_sugoi_model(m))
            others = sorted(m for m in models if not is_sugoi_model(m))
            for m in sugoi:
                self.model_combo.addItem(m)
                idx = self.model_combo.count() - 1
                self.model_combo.setItemData(
                    idx, "Recommended for JP\u2192EN (Sugoi \u2014 VN/RPG specialized)",
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
        self._on_model_changed(self.model_combo.currentText())

    def _refresh_models(self):
        """Fetch available models from Ollama (used by Refresh button)."""
        self._model_fetcher = _ModelFetcher(
            self.client,
            self.url_edit.text().strip() or "http://localhost:11434",
        )
        self._model_fetcher.done.connect(self._populate_model_combo)
        self.status_label.setText("Fetching models...")
        self._model_fetcher.start()

    def _refresh_vision_models(self):
        """Fetch available vision models from Ollama (used by Refresh button)."""
        self._model_fetcher = _ModelFetcher(
            self.client,
            self.url_edit.text().strip() or "http://localhost:11434",
        )
        self._model_fetcher.done.connect(self._populate_vision_combo)
        self.status_label.setText("Fetching models...")
        self._model_fetcher.start()

    def _test_connection(self):
        """Test if Ollama is reachable."""
        self.client.base_url = self.url_edit.text().strip() or "http://localhost:11434"
        if self.client.is_available():
            QMessageBox.information(self, "Connection OK", "Successfully connected to Ollama!")
        else:
            QMessageBox.warning(self, "Connection Failed",
                                "Cannot reach Ollama. Make sure it's running:\n  ollama serve")

    # ── Language / model auto-update ─────────────────────────────────

    def _on_language_changed(self, index: int):
        """Auto-update system prompt when target language changes."""
        new_lang = self.lang_combo.itemData(index)
        if not new_lang:
            return
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

        if is_sugoi_model(model_name):
            if current_lang in ("English", "Pig Latin"):
                self.model_hint_label.setText(
                    "Sugoi detected \u2014 optimized JP\u2192EN prompt will be used"
                )
                self.model_hint_label.setStyleSheet("color: #a6e3a1;")
            else:
                self.model_hint_label.setText(
                    "Sugoi is JP\u2192EN only \u2014 using general prompt for " + current_lang
                )
                self.model_hint_label.setStyleSheet("color: #fab387;")
        else:
            self.model_hint_label.setText("")

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

    # ── Save / Cancel ────────────────────────────────────────────────

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
        self.client.vision_model = self.vision_combo.currentText().strip()
        if self.parser:
            self.parser.context_size = self.context_spin.value()

        new_workers = self.workers_spin.value()
        if self.engine:
            self.engine.num_workers = new_workers
            self.engine.batch_size = self.batch_spin.value()
            self.engine.max_history = self.history_spin.value()

        if new_workers != self._orig_workers:
            self._restart_ollama(new_workers)

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
