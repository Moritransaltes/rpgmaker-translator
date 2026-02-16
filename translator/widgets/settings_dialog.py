"""Settings dialog for configuring translation provider and options."""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLineEdit, QComboBox, QPlainTextEdit, QPushButton,
    QLabel, QGroupBox, QMessageBox, QSpinBox,
    QCheckBox, QApplication, QProgressDialog,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal

from ..ai_client import (
    AIClient, SYSTEM_PROMPT, SUGOI_SYSTEM_PROMPT, TARGET_LANGUAGES,
    build_system_prompt, is_sugoi_model,
    PROVIDERS, PROVIDER_MODELS, PROMPT_PRESETS, DAZEDMTL_FULL_PROMPT,
    get_model_pricing, CLOUD_DEFAULT_WORKERS, LOCAL_DEFAULT_WORKERS,
)
from ..rpgmaker_mv import RPGMakerMVParser
from .model_suggestion_dialog import ModelSuggestionDialog


class _ModelFetcher(QThread):
    """Background thread to fetch model list from Ollama without blocking UI."""
    done = pyqtSignal(list)

    def __init__(self, client, url):
        super().__init__()
        self._client = client
        self._url = url

    def run(self):
        # Fetch models without mutating the shared client
        import requests as _req
        try:
            r = _req.get(f"{self._url}/api/tags", timeout=10)
            r.raise_for_status()
            models = [m["name"] for m in r.json().get("models", []) if "name" in m]
        except Exception:
            models = []
        self.done.emit(models)


class SettingsDialog(QDialog):
    """Dialog for configuring translation provider, model, prompt, and options."""

    def __init__(self, client: AIClient, parent=None, parser: RPGMakerMVParser = None,
                 dark_mode: bool = True, plugin_analyzer=None, engine=None,
                 export_review_file: bool = False):
        super().__init__(parent)
        self.client = client
        self.parser = parser
        self.dark_mode = dark_mode
        self.export_review_file = export_review_file
        self.plugin_analyzer = plugin_analyzer
        self.engine = engine
        self.setWindowTitle("Settings")
        self.setMinimumSize(600, 500)
        self._build_ui()
        self._load_current()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # ── Translation Provider ─────────────────────────────────────
        conn_group = QGroupBox("Translation Provider")
        conn_form = QFormLayout(conn_group)

        # Provider dropdown
        self.provider_combo = QComboBox()
        for p in PROVIDERS:
            self.provider_combo.addItem(p)
        self.provider_combo.currentTextChanged.connect(self._on_provider_changed)
        conn_form.addRow("Provider:", self.provider_combo)

        # API Key (hidden for Ollama)
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("Enter API key...")
        self._api_key_label = QLabel("API Key:")
        conn_form.addRow(self._api_key_label, self.api_key_edit)

        # URL (shown for Ollama and Custom)
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("http://localhost:11434")
        self._url_label = QLabel("Server URL:")
        conn_form.addRow(self._url_label, self.url_edit)

        # Model
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

        self.suggest_btn = QPushButton("Suggest Model")
        self.suggest_btn.setToolTip("Detect your GPU and recommend the best model")
        self.suggest_btn.clicked.connect(self._suggest_model)
        model_row.addWidget(self.suggest_btn)

        conn_form.addRow("Model:", model_row)

        self.model_hint_label = QLabel("")
        self.model_hint_label.setWordWrap(True)
        conn_form.addRow("", self.model_hint_label)

        # Vision model (for image translation OCR — Ollama only)
        vision_row = QHBoxLayout()
        self.vision_combo = QComboBox()
        self.vision_combo.setEditable(True)
        self.vision_combo.setMinimumWidth(250)
        self.vision_combo.setToolTip(
            "Vision model for image OCR (e.g. qwen3-vl:8b).\n"
            "Used by Translate Images to detect Japanese text in game images.\n"
            "Leave empty to disable image translation.\n"
            "(Ollama only)"
        )
        vision_row.addWidget(self.vision_combo)

        self.vision_refresh_btn = QPushButton("Refresh")
        self.vision_refresh_btn.clicked.connect(self._refresh_vision_models)
        vision_row.addWidget(self.vision_refresh_btn)

        self._vision_label = QLabel("Vision Model:")
        conn_form.addRow(self._vision_label, vision_row)

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

        # ── Prompt ───────────────────────────────────────────────────
        prompt_group = QGroupBox("Translation Prompt")
        prompt_layout = QVBoxLayout(prompt_group)

        # Prompt preset dropdown + buttons
        preset_row = QHBoxLayout()
        self.prompt_preset_combo = QComboBox()
        for name in PROMPT_PRESETS:
            self.prompt_preset_combo.addItem(name)
        self.prompt_preset_combo.currentTextChanged.connect(self._on_preset_changed)
        preset_row.addWidget(QLabel("Preset:"))
        preset_row.addWidget(self.prompt_preset_combo, 1)

        reset_btn = QPushButton("Reset Default")
        reset_btn.setToolTip("Reset prompt to the recommended default for the current model")
        reset_btn.clicked.connect(self._reset_prompt_default)
        preset_row.addWidget(reset_btn)

        clear_btn = QPushButton("Clear")
        clear_btn.setToolTip("Clear the prompt (uses built-in default at translation time)")
        clear_btn.clicked.connect(self._clear_prompt)
        preset_row.addWidget(clear_btn)

        prompt_layout.addLayout(preset_row)

        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setMinimumHeight(120)
        self.prompt_edit.textChanged.connect(self._on_prompt_edited)
        prompt_layout.addWidget(self.prompt_edit)

        layout.addWidget(prompt_group)

        # Translation options
        opts_group = QGroupBox("Translation Options")
        opts_form = QFormLayout(opts_group)

        self.dazed_mode_check = QCheckBox("DazedMTL Mode")
        self.dazed_mode_check.setToolTip(
            "Mirrors DazedMTL's translation settings:\n"
            "  - Batch size: 30 lines per request\n"
            "  - DazedMTL Full prompt\n"
            "  - Cloud: 4 parallel workers\n"
            "  - Local Ollama: 1 worker (GPU can't parallelize)\n\n"
            "Best for cloud APIs. For local Sugoi, batching\n"
            "still helps (fewer round trips) but workers=1\n"
            "is optimal since the GPU processes sequentially."
        )
        self.dazed_mode_check.stateChanged.connect(self._on_dazed_mode_changed)
        opts_form.addRow(self.dazed_mode_check)

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
        self.batch_spin.setRange(1, 50)
        self.batch_spin.setSpecialValueText("Disabled (single-entry)")
        self.batch_spin.setToolTip(
            "Number of entries sent per LLM request as a JSON batch.\n"
            "1 = single-entry mode (recommended for local Ollama).\n"
            "30 = cloud APIs (DazedMTL default — reduces round trips).\n\n"
            "For local LLMs, batching does NOT improve speed\n"
            "(GPU generates the same tokens either way).\n"
            "It's mainly useful for cloud APIs where per-request\n"
            "latency and rate limits are the bottleneck.\n\n"
            "If the LLM returns invalid JSON, entries automatically\n"
            "fall back to single-entry translation."
        )
        opts_form.addRow("Batch size:", self.batch_spin)

        self.auto_tune_check = QCheckBox("Auto-tune batch size")
        self.auto_tune_check.setToolTip(
            "Automatically calibrate optimal batch size before each\n"
            "batch translation by testing sizes 5→30 and measuring\n"
            "throughput (entries/sec).\n\n"
            "Calibration uses ~105 real entries (translations are kept).\n"
            "Only runs for local Ollama with batch_size > 1.\n"
            "Cloud APIs skip calibration (fixed batch 30)."
        )
        self.auto_tune_check.toggled.connect(self._on_auto_tune_toggled)
        opts_form.addRow(self.auto_tune_check)

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

        self.single_401_check = QCheckBox("Merge dialogue into single 401 command")
        self.single_401_check.setToolTip(
            "On export, merge all dialogue lines into a single 401 event\n"
            "command with embedded newlines, instead of one 401 per line.\n\n"
            "Enable this if your game has an auto-advance plugin that\n"
            "paginates after every 4 consecutive 401 commands.\n"
            "With this on, you can manually control pagination by\n"
            "inserting \\! (wait for input) codes in the translation."
        )
        opts_form.addRow(self.single_401_check)

        self.face_speaker_check = QCheckBox("Resolve face graphics to actor names")
        self.face_speaker_check.setToolTip(
            "In MV games, 101 headers use face graphic filenames (e.g. 'Actor1')\n"
            "instead of character names. With this enabled, the tool matches\n"
            "face graphics to actors from Actors.json and uses their real names\n"
            "as speaker context for the LLM.\n\n"
            "Disable if actor face assignments don't match the actual speakers\n"
            "(e.g. reused face sheets for different NPCs)."
        )
        opts_form.addRow(self.face_speaker_check)

        self.review_file_check = QCheckBox("Export review file after batch translation")
        self.review_file_check.setToolTip(
            "Automatically saves a side-by-side review TXT file\n"
            "after each batch translation completes.\n\n"
            "Named: Review_{Provider}_{Model}_{Date}.txt\n"
            "Includes cost/token summary and all JP/EN pairs.\n"
            "Share with reviewers who don't have the tool installed."
        )
        opts_form.addRow(self.review_file_check)

        layout.addWidget(opts_group)

        # Appearance
        appear_group = QGroupBox("Appearance")
        appear_form = QFormLayout(appear_group)

        self.dark_mode_check = QCheckBox("Enable dark mode (Catppuccin theme)")
        appear_form.addRow(self.dark_mode_check)

        layout.addWidget(appear_group)

        # Experimental
        exp_group = QGroupBox("Experimental")
        exp_form = QFormLayout(exp_group)

        self.script_strings_check = QCheckBox(
            "Extract strings from Script commands (355/655)"
        )
        self.script_strings_check.setToolTip(
            "Extracts Japanese text from $gameVariables.setValue() calls\n"
            "in event Script commands. Used for quest text, dynamic labels, etc.\n\n"
            "WARNING: Modifying script commands can break game logic.\n"
            "Review extracted entries carefully before exporting."
        )
        exp_form.addRow(self.script_strings_check)

        layout.addWidget(exp_group)

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
        self._orig_provider = self.client.provider
        self._orig_workers = self.engine.num_workers if self.engine else 2
        self._orig_language = self.client.target_language
        self._suppress_preset_change = False  # Flag to avoid feedback loops
        self._loading = True  # Suppress auto-set of batch/workers during load

        # Provider
        idx = self.provider_combo.findText(self.client.provider)
        if idx >= 0:
            self.provider_combo.setCurrentIndex(idx)
        self.api_key_edit.setText(self.client.api_key)
        self.url_edit.setText(self.client.base_url)

        # Prompt preset — match current prompt to a known preset
        self._prompt_preset = getattr(self.client, "_prompt_preset", "Custom")
        preset_idx = self.prompt_preset_combo.findText(self._prompt_preset)
        if preset_idx >= 0:
            self.prompt_preset_combo.setCurrentIndex(preset_idx)
        self.prompt_edit.setPlainText(self.client.system_prompt)

        self.model_combo.setCurrentText(self.client.model)
        for i in range(self.lang_combo.count()):
            if self.lang_combo.itemData(i) == self.client.target_language:
                self.lang_combo.setCurrentIndex(i)
                break
        self.context_spin.setValue(self.parser.context_size if self.parser else 3)
        self.workers_spin.setValue(self.engine.num_workers if self.engine else 2)
        self.batch_spin.setValue(self.engine.batch_size if self.engine else 5)
        self.auto_tune_check.setChecked(self.engine.auto_tune if self.engine else False)
        self._on_auto_tune_toggled(self.auto_tune_check.isChecked())
        self.history_spin.setValue(self.engine.max_history if self.engine else 10)
        if self.plugin_analyzer and getattr(self.plugin_analyzer, '_manual_chars_per_line', 0):
            self.wordwrap_spin.setValue(self.plugin_analyzer._manual_chars_per_line)
        else:
            self.wordwrap_spin.setValue(0)
        self.single_401_check.setChecked(
            self.parser.single_401_mode if self.parser else False)
        self.face_speaker_check.setChecked(
            self.parser.face_speaker_resolve if self.parser else True)
        self.review_file_check.setChecked(self.export_review_file)
        self.dark_mode_check.setChecked(self.dark_mode)
        self.dazed_mode_check.setChecked(getattr(self.client, "dazed_mode", False))
        self.script_strings_check.setChecked(
            self.parser.extract_script_strings if self.parser else False
        )
        self.vision_combo.setCurrentText(getattr(self.client, "vision_model", "") or "")

        # Apply provider visibility and fetch models
        self._on_provider_changed(self.client.provider)
        self._loading = False

    # ── Provider / Prompt preset handlers ─────────────────────────────

    def _on_provider_changed(self, provider: str):
        """Show/hide fields based on the selected provider."""
        is_ollama = provider == "Ollama (Local)"
        is_custom = provider == "Custom"
        is_cloud = not is_ollama

        # API key: visible for cloud providers
        self._api_key_label.setVisible(is_cloud)
        self.api_key_edit.setVisible(is_cloud)

        # URL: visible for Ollama and Custom
        self._url_label.setVisible(is_ollama or is_custom)
        self.url_edit.setVisible(is_ollama or is_custom)

        # Vision model: Ollama only
        self._vision_label.setVisible(is_ollama)
        self.vision_combo.setVisible(is_ollama)
        self.vision_refresh_btn.setVisible(is_ollama)

        # Refresh button: only for Ollama (cloud uses preset models)
        self.refresh_btn.setVisible(is_ollama or is_custom)

        # Populate model combo with provider presets (cloud) or fetch (Ollama)
        if is_cloud and not is_custom:
            models = PROVIDER_MODELS.get(provider, [])
            current = self.model_combo.currentText()
            self.model_combo.blockSignals(True)
            self.model_combo.clear()
            for m in models:
                self.model_combo.addItem(m)
            # Keep current if it's in the new list, else pick first
            if current in models:
                self.model_combo.setCurrentText(current)
            self.model_combo.blockSignals(False)
            self.status_label.setText(f"{provider}: {len(models)} model(s) available")
            self.status_label.setStyleSheet("color: #89b4fa;")
        elif is_ollama:
            # Fetch models from Ollama in background
            self._model_fetcher = _ModelFetcher(
                self.client,
                self.url_edit.text().strip() or "http://localhost:11434",
            )
            self._model_fetcher.done.connect(self._on_models_fetched)
            self.status_label.setText("Fetching models...")
            self.status_label.setStyleSheet("")
            self._model_fetcher.start()

        # Auto-set batch size and workers based on provider/model (DazedMTL defaults)
        # Skip during initial load — saved values should be preserved
        if not self._loading:
            if is_cloud:
                model = self.model_combo.currentText()
                config = get_model_pricing(model)
                self.batch_spin.setValue(config.get("batch_size", 10))
                self.workers_spin.setValue(CLOUD_DEFAULT_WORKERS)
            elif is_ollama:
                self.batch_spin.setValue(1)
                self.workers_spin.setValue(LOCAL_DEFAULT_WORKERS)

    def _on_preset_changed(self, preset_name: str):
        """Load the selected prompt preset into the editor."""
        if self._suppress_preset_change:
            return
        self._prompt_preset = preset_name
        if preset_name != "Custom":
            prompt_text = PROMPT_PRESETS.get(preset_name, "")
            if prompt_text:
                self._suppress_preset_change = True
                self.prompt_edit.setPlainText(prompt_text)
                self._suppress_preset_change = False

    def _on_prompt_edited(self):
        """Switch preset to Custom when user manually edits the prompt."""
        if self._suppress_preset_change:
            return
        current_preset = self.prompt_preset_combo.currentText()
        if current_preset == "Custom":
            return
        # Check if the text still matches the preset
        preset_text = PROMPT_PRESETS.get(current_preset, "")
        if preset_text and self.prompt_edit.toPlainText().strip() != preset_text.strip():
            self._suppress_preset_change = True
            idx = self.prompt_preset_combo.findText("Custom")
            if idx >= 0:
                self.prompt_preset_combo.setCurrentIndex(idx)
            self._prompt_preset = "Custom"
            self._suppress_preset_change = False

    def _reset_prompt_default(self):
        """Reset prompt to the recommended default for the current model/language."""
        model = self.model_combo.currentText()
        lang = self.lang_combo.currentData() or "English"
        default_prompt = build_system_prompt(lang, model=model)
        self._suppress_preset_change = True
        self.prompt_edit.setPlainText(default_prompt)
        # Match the prompt to the correct preset name
        for name, text in PROMPT_PRESETS.items():
            if text and text.strip() == default_prompt.strip():
                idx = self.prompt_preset_combo.findText(name)
                if idx >= 0:
                    self.prompt_preset_combo.setCurrentIndex(idx)
                self._prompt_preset = name
                break
        self._suppress_preset_change = False

    def _clear_prompt(self):
        """Clear the prompt text box and switch preset to Custom."""
        self._suppress_preset_change = True
        self.prompt_edit.clear()
        idx = self.prompt_preset_combo.findText("Custom")
        if idx >= 0:
            self.prompt_preset_combo.setCurrentIndex(idx)
        self._prompt_preset = "Custom"
        self._suppress_preset_change = False

    def _on_dazed_mode_changed(self, state: int):
        """Toggle DazedMTL mode — batch 30, DazedMTL Full prompt, 4 workers."""
        if self._loading:
            return  # Don't override saved values during initial load
        enabled = state == Qt.CheckState.Checked.value
        if enabled:
            self.batch_spin.setValue(30)
            provider = self.provider_combo.currentText()
            if provider == "Ollama (Local)":
                self.workers_spin.setValue(LOCAL_DEFAULT_WORKERS)
            else:
                self.workers_spin.setValue(CLOUD_DEFAULT_WORKERS)
            # Switch prompt to DazedMTL Full
            self._suppress_preset_change = True
            preset_name = "Sugoi (DazedMTL Full)"
            idx = self.prompt_preset_combo.findText(preset_name)
            if idx >= 0:
                self.prompt_preset_combo.setCurrentIndex(idx)
            self._prompt_preset = preset_name
            self.prompt_edit.setPlainText(DAZEDMTL_FULL_PROMPT)
            self._suppress_preset_change = False
        else:
            # Restore defaults based on current provider
            provider = self.provider_combo.currentText()
            if provider == "Ollama (Local)":
                self.batch_spin.setValue(1)
                self.workers_spin.setValue(LOCAL_DEFAULT_WORKERS)
            else:
                model = self.model_combo.currentText()
                config = get_model_pricing(model)
                self.batch_spin.setValue(config.get("batch_size", 10))
                self.workers_spin.setValue(CLOUD_DEFAULT_WORKERS)
            # Reset prompt to model default
            self._reset_prompt_default()

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
        """Test if the translation backend is reachable without mutating shared client."""
        import requests as _req
        provider = self.provider_combo.currentText()
        url = self.url_edit.text().strip() or "http://localhost:11434"
        api_key = self.api_key_edit.text().strip()

        ok = False
        if provider == "Ollama (Local)":
            try:
                r = _req.get(f"{url}/api/tags", timeout=5)
                ok = r.status_code == 200
            except Exception:
                ok = False
        else:
            # Cloud provider — test with a lightweight models list call
            try:
                import openai
                from ..ai_client import PROVIDER_URLS
                base = PROVIDER_URLS.get(provider)
                if not base and provider == "Custom":
                    base = url
                client = openai.OpenAI(api_key=api_key, base_url=base, timeout=10)
                client.models.list()
                ok = True
            except Exception:
                ok = False

        if ok:
            QMessageBox.information(
                self, "Connection OK",
                f"Successfully connected to {provider}!"
            )
        else:
            if provider == "Ollama (Local)":
                msg = "Cannot reach Ollama. Make sure it's running:\n  ollama serve"
            else:
                msg = (
                    f"Cannot reach {provider}.\n\n"
                    "Check that your API key is correct and the service is available."
                )
            QMessageBox.warning(self, "Connection Failed", msg)

    def _suggest_model(self):
        """Show GPU-aware model recommendation dialog."""
        # Get currently installed models
        installed = []
        for i in range(self.model_combo.count()):
            installed.append(self.model_combo.itemText(i))

        dlg = ModelSuggestionDialog(
            installed_models=installed, parent=self)
        dlg.model_selected.connect(self._on_suggested_model_selected)
        dlg.exec()

    def _on_suggested_model_selected(self, tag: str):
        """Apply the model selected from suggestion dialog."""
        # Check if model is already in combo
        for i in range(self.model_combo.count()):
            if tag.lower() in self.model_combo.itemText(i).lower():
                self.model_combo.setCurrentIndex(i)
                return
        # Not in combo — add it and select
        self.model_combo.addItem(tag)
        self.model_combo.setCurrentText(tag)
        # Refresh model list to pick up newly pulled models
        self._refresh_models()

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
        """Auto-update system prompt, hint label, and batch size when model changes."""
        current_lang = self.lang_combo.currentData() or "English"
        current_prompt = self.prompt_edit.toPlainText().strip()

        if is_sugoi_model(model_name):
            if current_lang in ("English", "Pig Latin"):
                self.model_hint_label.setText(
                    "Sugoi detected \u2014 DazedMTL Full prompt recommended (click Reset Default)"
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

        # Auto-set batch size from model config (cloud providers only)
        # Skip during initial load — saved values should be preserved
        if not self._loading:
            provider = self.provider_combo.currentText()
            if provider != "Ollama (Local)":
                config = get_model_pricing(model_name)
                batch = config.get("batch_size", 10)
                if batch != self.batch_spin.value():
                    self.batch_spin.setValue(batch)

    def _is_known_prompt_template(self, prompt: str) -> bool:
        """Check if the prompt matches any known preset or auto-generated template."""
        p = prompt.strip()
        # Check all presets
        for name, text in PROMPT_PRESETS.items():
            if text and p == text.strip():
                return True
        return p == build_system_prompt(self.lang_combo.currentData() or "English").strip()

    # ── Save / Cancel ────────────────────────────────────────────────

    def reject(self):
        """Revert any URL/model/provider changes made during the dialog."""
        self.client.base_url = self._orig_url
        self.client.model = self._orig_model
        self.client.provider = self._orig_provider
        super().reject()

    def _save(self):
        """Apply settings and close."""
        self.client.provider = self.provider_combo.currentText()
        self.client.api_key = self.api_key_edit.text().strip()
        self.client.base_url = self.url_edit.text().strip() or "http://localhost:11434"
        self.client.model = self.model_combo.currentText().strip()
        self.client.system_prompt = self.prompt_edit.toPlainText().strip() or SYSTEM_PROMPT
        self.client._prompt_preset = self.prompt_preset_combo.currentText()
        self.client.target_language = self.lang_combo.currentData() or "English"
        self.client.vision_model = self.vision_combo.currentText().strip()
        if self.parser:
            self.parser.context_size = self.context_spin.value()

        new_workers = self.workers_spin.value()
        if self.engine:
            self.engine.num_workers = new_workers
            self.engine.batch_size = self.batch_spin.value()
            self.engine.max_history = self.history_spin.value()
            self.engine.auto_tune = self.auto_tune_check.isChecked()

        if new_workers != self._orig_workers and not self.client.is_cloud:
            self._restart_ollama(new_workers)

        if self.plugin_analyzer:
            manual = self.wordwrap_spin.value()
            self.plugin_analyzer._manual_chars_per_line = manual
            if manual > 0:
                self.plugin_analyzer.chars_per_line = manual
        self.dark_mode = self.dark_mode_check.isChecked()
        self.export_review_file = self.review_file_check.isChecked()
        self.client.dazed_mode = self.dazed_mode_check.isChecked()
        if self.parser:
            self.parser.extract_script_strings = self.script_strings_check.isChecked()
            self.parser.single_401_mode = self.single_401_check.isChecked()
            self.parser.face_speaker_resolve = self.face_speaker_check.isChecked()
        self.accept()

    def _on_auto_tune_toggled(self, checked: bool):
        """Grey out batch size spinner when auto-tune is enabled."""
        self.batch_spin.setEnabled(not checked)

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
