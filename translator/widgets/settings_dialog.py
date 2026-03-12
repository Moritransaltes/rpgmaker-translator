"""Settings dialog for configuring translation provider and options."""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLineEdit, QComboBox, QPlainTextEdit, QPushButton,
    QLabel, QGroupBox, QMessageBox, QSpinBox,
    QCheckBox, QApplication, QProgressDialog, QTabWidget, QWidget,
    QSizePolicy,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal

from ..ai_client import (
    AIClient, SYSTEM_PROMPT, SUGOI_SYSTEM_PROMPT, TYRANO_SYSTEM_PROMPT,
    TARGET_LANGUAGES, build_system_prompt, is_sugoi_model,
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
                 export_review_file: bool = False, disable_splash: bool = True,
                 show_translation_splash: bool = True):
        super().__init__(parent)
        self.client = client
        self.parser = parser
        self.dark_mode = dark_mode
        self.export_review_file = export_review_file
        self.disable_splash = disable_splash
        self.show_translation_splash = show_translation_splash
        self.plugin_analyzer = plugin_analyzer
        self.engine = engine
        self.setWindowTitle("Settings")
        self.setMinimumSize(600, 500)
        self._build_ui()
        self._load_current()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # ── Tab widget ────────────────────────────────────────────────
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        self._build_provider_tab()
        self._build_prompt_tab()
        self._build_options_tab()

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

    # ── Tab 1: Provider ───────────────────────────────────────────────

    def _build_provider_tab(self):
        tab = QWidget()
        form = QFormLayout(tab)

        # Provider dropdown
        self.provider_combo = QComboBox()
        for p in PROVIDERS:
            self.provider_combo.addItem(p)
        self.provider_combo.currentTextChanged.connect(self._on_provider_changed)
        form.addRow("Provider:", self.provider_combo)

        # API Key (hidden for Ollama)
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("Enter API key...")
        self._api_key_label = QLabel("API Key:")
        form.addRow(self._api_key_label, self.api_key_edit)

        # URL (shown for Ollama and Custom)
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("http://localhost:11434")
        self._url_label = QLabel("Server URL:")
        form.addRow(self._url_label, self.url_edit)

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

        form.addRow("Model:", model_row)

        self.model_hint_label = QLabel("")
        self.model_hint_label.setWordWrap(True)
        form.addRow("", self.model_hint_label)

        self.status_label = QLabel("")
        form.addRow("", self.status_label)

        self.lang_combo = QComboBox()
        for name, stars, tip in TARGET_LANGUAGES:
            self.lang_combo.addItem(f"{name}  {stars}", userData=name)
            self.lang_combo.setItemData(self.lang_combo.count() - 1, tip, Qt.ItemDataRole.ToolTipRole)
        self.lang_combo.currentIndexChanged.connect(self._on_language_changed)
        form.addRow("Target Language:", self.lang_combo)

        self.model_combo.currentTextChanged.connect(self._on_model_changed)

        self.tabs.addTab(tab, "Provider")

    # ── Tab 2: Prompt ─────────────────────────────────────────────────

    def _build_prompt_tab(self):
        tab = QWidget()
        vbox = QVBoxLayout(tab)

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

        vbox.addLayout(preset_row)

        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.prompt_edit.textChanged.connect(self._on_prompt_edited)
        vbox.addWidget(self.prompt_edit)

        self.tabs.addTab(tab, "Prompt")

    # ── Tab 3: Options ────────────────────────────────────────────────

    def _build_options_tab(self):
        tab = QWidget()
        vbox = QVBoxLayout(tab)

        # ── Translation group ──
        trans_group = QGroupBox("Translation")
        trans_form = QFormLayout(trans_group)

        self.context_spin = QSpinBox()
        self.context_spin.setRange(0, 20)
        self.context_spin.setToolTip(
            "Number of recent dialogue lines included as context for the LLM.\n"
            "Higher = better coherence but slower and uses more VRAM."
        )
        trans_form.addRow("Context window size:", self.context_spin)

        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 16)
        self.workers_spin.setToolTip(
            "Number of parallel translation requests sent to Ollama.\n"
            "Higher = faster batch translation, but uses more VRAM.\n"
            "Ollama will be automatically restarted when this changes."
        )
        trans_form.addRow("Parallel workers:", self.workers_spin)

        self.batch_spin = QSpinBox()
        self.batch_spin.setRange(1, 50)
        self.batch_spin.setSpecialValueText("Disabled (single-entry)")
        self.batch_spin.setToolTip(
            "Number of entries sent per LLM request as a JSON batch.\n"
            "1 = single-entry mode (one line per request).\n"
            "5-10 = good for local models with large context (Qwen3.5, etc.).\n"
            "30 = cloud APIs (DazedMTL default — reduces round trips).\n\n"
            "If the LLM returns invalid JSON, entries automatically\n"
            "fall back to single-entry translation."
        )
        trans_form.addRow("Batch size:", self.batch_spin)

        self.auto_tune_check = QCheckBox("Auto-tune batch size")
        self.auto_tune_check.setToolTip(
            "Automatically calibrate optimal batch size before each\n"
            "batch translation by testing sizes 5→30 and measuring\n"
            "throughput (entries/sec)."
        )
        self.auto_tune_check.toggled.connect(self._on_auto_tune_toggled)
        trans_form.addRow(self.auto_tune_check)

        self.history_spin = QSpinBox()
        self.history_spin.setRange(0, 30)
        self.history_spin.setSpecialValueText("Disabled")
        self.history_spin.setToolTip(
            "Number of recent translation pairs sent to the LLM as context.\n"
            "0 = disabled. 10 = recommended."
        )
        trans_form.addRow("Translation history:", self.history_spin)

        self.dazed_mode_check = QCheckBox("DazedMTL Mode")
        self.dazed_mode_check.setToolTip(
            "Mirrors DazedMTL's translation settings:\n"
            "  - Batch size: 30 lines per request\n"
            "  - DazedMTL Full prompt\n"
            "  - Cloud: 4 workers / Local: 1 worker"
        )
        self.dazed_mode_check.stateChanged.connect(self._on_dazed_mode_changed)
        trans_form.addRow(self.dazed_mode_check)

        vbox.addWidget(trans_group)

        # ── Export & Behavior group ──
        export_group = QGroupBox("Export && Behavior")
        export_form = QFormLayout(export_group)

        self.wordwrap_spin = QSpinBox()
        self.wordwrap_spin.setRange(0, 200)
        self.wordwrap_spin.setSpecialValueText("Auto-detect")
        self.wordwrap_spin.setToolTip(
            "Characters per line for word wrapping.\n"
            "0 = auto-detect from game plugins (default).\n"
            "Set manually if auto-detection gives wrong results."
        )
        export_form.addRow("Word wrap chars/line:", self.wordwrap_spin)

        self.inject_wordwrap_check = QCheckBox("Inject word wrap plugin on export")
        self.inject_wordwrap_check.setToolTip(
            "If the game has no word wrap plugin, inject TranslatorWordWrap.js\n"
            "on export. When enabled, Apply Word Wrap adds <WordWrap> tags.\n"
            "When disabled, Apply Word Wrap uses manual line breaks."
        )
        export_form.addRow(self.inject_wordwrap_check)

        self.single_401_check = QCheckBox("Merge dialogue into single 401 command")
        self.single_401_check.setToolTip(
            "On export, merge all dialogue lines into a single 401 event\n"
            "command with embedded newlines, instead of one 401 per line.\n\n"
            "Enable this if your game has an auto-advance plugin that\n"
            "paginates after every 4 consecutive 401 commands."
        )
        export_form.addRow(self.single_401_check)

        self.speaker_processing_check = QCheckBox("Enable speaker text processing")
        self.speaker_processing_check.setToolTip(
            "Strips \\N<name> namebox prefixes from dialogue text,\n"
            "resolves face graphics to actor names, and replaces\n"
            "Japanese speaker names with English in contexts."
        )
        export_form.addRow(self.speaker_processing_check)

        self.disable_splash_check = QCheckBox("Disable 'Made with RPG Maker' splash on export")
        self.disable_splash_check.setToolTip(
            "Automatically disables the MadeWithMv/MadeWithMz splash\n"
            "screen plugin when exporting translations to the game."
        )
        export_form.addRow(self.disable_splash_check)

        self.review_file_check = QCheckBox("Export review file after batch translation")
        self.review_file_check.setToolTip(
            "Saves a side-by-side review TXT file after each batch.\n"
            "Named: Review_{Provider}_{Model}_{Date}.txt"
        )
        export_form.addRow(self.review_file_check)

        self.translation_splash_check = QCheckBox("Show translation splash on export")
        self.translation_splash_check.setToolTip(
            "Injects a 'Translated with RPG Maker Translator' splash screen\n"
            "that displays before the title screen when the game starts.\n"
            "RPG Maker games only."
        )
        export_form.addRow(self.translation_splash_check)

        self.script_strings_check = QCheckBox(
            "Extract strings from Script commands (355/655)"
        )
        self.script_strings_check.setToolTip(
            "Extracts Japanese text from $gameVariables.setValue() calls\n"
            "in event Script commands.\n\n"
            "WARNING: Modifying script commands can break game logic."
        )
        export_form.addRow(self.script_strings_check)

        vbox.addWidget(export_group)

        # ── Appearance ──
        self.dark_mode_check = QCheckBox("Enable dark mode (Catppuccin theme)")
        vbox.addWidget(self.dark_mode_check)

        vbox.addStretch()
        self.tabs.addTab(tab, "Options")

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
        self.speaker_processing_check.setChecked(
            self.parser.speaker_processing if self.parser else True)
        self.review_file_check.setChecked(self.export_review_file)
        self.inject_wordwrap_check.setChecked(
            self.plugin_analyzer.inject_wordwrap if self.plugin_analyzer else False)
        self.disable_splash_check.setChecked(self.disable_splash)
        self.translation_splash_check.setChecked(self.show_translation_splash)
        self.dark_mode_check.setChecked(self.dark_mode)
        self.dazed_mode_check.setChecked(getattr(self.client, "dazed_mode", False))
        self.script_strings_check.setChecked(
            self.parser.extract_script_strings if self.parser else False
        )
        # Vision model removed — main model is now multimodal (handles OCR + translate)

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
        # Vision model UI removed — main model handles image OCR

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
                self.batch_spin.setValue(5)
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
        default_prompt = build_system_prompt(lang, model=model,
                                            project_type=self.client.project_type)
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
                self.batch_spin.setValue(5)
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
        ptype = self.client.project_type
        old_prompt = build_system_prompt(old_lang, model=current_model, project_type=ptype)
        if current_prompt == old_prompt.strip():
            self.prompt_edit.setPlainText(build_system_prompt(new_lang, model=current_model,
                                                              project_type=ptype))
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
            new_prompt = build_system_prompt(current_lang, model=model_name,
                                            project_type=self.client.project_type)
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
        # Check TyranoScript prompt
        if p == TYRANO_SYSTEM_PROMPT.strip():
            return True
        return p == build_system_prompt(self.lang_combo.currentData() or "English",
                                       project_type=self.client.project_type).strip()

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
        # Vision model removed — main model handles image OCR
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
                # Scale face width: face takes ~10 chars at standard font
                from ..text_processor import FACE_OFFSET_PX
                char_width = self.plugin_analyzer.font_size * 0.55
                face_offset_chars = int(FACE_OFFSET_PX / char_width) if char_width > 0 else 10
                self.plugin_analyzer.face_chars_per_line = max(15, manual - face_offset_chars)
        self.dark_mode = self.dark_mode_check.isChecked()
        self.export_review_file = self.review_file_check.isChecked()
        if self.plugin_analyzer:
            self.plugin_analyzer.inject_wordwrap = self.inject_wordwrap_check.isChecked()
        self.disable_splash = self.disable_splash_check.isChecked()
        self.show_translation_splash = self.translation_splash_check.isChecked()
        self.client.dazed_mode = self.dazed_mode_check.isChecked()
        if self.parser:
            self.parser.extract_script_strings = self.script_strings_check.isChecked()
            self.parser.single_401_mode = self.single_401_check.isChecked()
            self.parser.speaker_processing = self.speaker_processing_check.isChecked()
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
