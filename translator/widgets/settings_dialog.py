"""Settings dialog for configuring Ollama connection and translation options."""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLineEdit, QComboBox, QPlainTextEdit, QPushButton,
    QLabel, QGroupBox, QMessageBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QTabWidget, QWidget, QSpinBox,
    QCheckBox, QMenu,
)
from PyQt6.QtCore import Qt

from ..ollama_client import OllamaClient, SYSTEM_PROMPT
from ..rpgmaker_mv import RPGMakerMVParser
from ..default_glossary import CATEGORIES as DEFAULT_GLOSSARY_CATEGORIES


class SettingsDialog(QDialog):
    """Dialog for configuring Ollama URL, model, prompt, and glossary."""

    def __init__(self, client: OllamaClient, parent=None, parser: RPGMakerMVParser = None, dark_mode: bool = True, plugin_analyzer=None):
        super().__init__(parent)
        self.client = client
        self.parser = parser
        self.dark_mode = dark_mode
        self.plugin_analyzer = plugin_analyzer
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

        self.status_label = QLabel("")
        conn_form.addRow("", self.status_label)

        conn_layout.addWidget(conn_group)

        # Prompt
        prompt_group = QGroupBox("Translation Prompt")
        prompt_layout = QVBoxLayout(prompt_group)
        prompt_layout.addWidget(QLabel("System prompt sent to the LLM:"))
        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setMinimumHeight(120)
        prompt_layout.addWidget(self.prompt_edit)

        reset_btn = QPushButton("Reset to Default")
        reset_btn.clicked.connect(lambda: self.prompt_edit.setPlainText(SYSTEM_PROMPT))
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

        # ── Tab 2: Glossary ────────────────────────────────────────
        glossary_tab = QWidget()
        glossary_layout = QVBoxLayout(glossary_tab)

        glossary_layout.addWidget(QLabel(
            "Define forced term translations. The LLM will always use these exact mappings.\n"
            "Example: Character names, locations, items, recurring phrases."
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

        # Load Defaults dropdown — lets users pick preset categories
        defaults_btn = QPushButton("Load Defaults \u25bc")
        defaults_menu = QMenu(self)
        defaults_menu.addAction("All Categories", self._load_all_defaults)
        defaults_menu.addSeparator()
        for cat_name in DEFAULT_GLOSSARY_CATEGORIES:
            defaults_menu.addAction(cat_name, lambda c=cat_name: self._load_default_category(c))
        defaults_btn.setMenu(defaults_menu)
        glossary_btn_row.addWidget(defaults_btn)

        glossary_layout.addLayout(glossary_btn_row)

        tabs.addTab(glossary_tab, "Glossary")

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
        # Save originals so we can restore on cancel
        self._orig_url = self.client.base_url
        self._orig_model = self.client.model
        self.url_edit.setText(self.client.base_url)
        self.model_combo.setCurrentText(self.client.model)
        self.prompt_edit.setPlainText(self.client.system_prompt)
        self.context_spin.setValue(self.parser.context_size if self.parser else 3)
        # Word wrap: 0 = auto-detect, >0 = manual override
        if self.plugin_analyzer and getattr(self.plugin_analyzer, '_manual_chars_per_line', 0):
            self.wordwrap_spin.setValue(self.plugin_analyzer._manual_chars_per_line)
        else:
            self.wordwrap_spin.setValue(0)
        self.dark_mode_check.setChecked(self.dark_mode)
        self._load_glossary()
        self._refresh_models()

    def _load_glossary(self):
        """Load glossary from client into table."""
        self.glossary_table.setRowCount(0)
        for jp, en in self.client.glossary.items():
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

    def _load_default_category(self, category: str):
        """Merge a default glossary category into the table without overwriting existing entries."""
        entries = DEFAULT_GLOSSARY_CATEGORIES.get(category, {})
        self._merge_glossary_entries(entries)

    def _load_all_defaults(self):
        """Merge all default glossary categories into the table."""
        from ..default_glossary import get_all_defaults
        self._merge_glossary_entries(get_all_defaults())

    def _merge_glossary_entries(self, entries: dict):
        """Add entries to the glossary table, skipping any JP terms already present."""
        existing = self._get_glossary()
        added = 0
        for jp, en in entries.items():
            if jp in existing:
                continue
            row = self.glossary_table.rowCount()
            # Replace the trailing empty row if it exists
            if row > 0:
                last_jp = self.glossary_table.item(row - 1, 0)
                last_en = self.glossary_table.item(row - 1, 1)
                if last_jp and not last_jp.text().strip() and last_en and not last_en.text().strip():
                    row -= 1
                    self.glossary_table.setItem(row, 0, QTableWidgetItem(jp))
                    self.glossary_table.setItem(row, 1, QTableWidgetItem(en))
                    added += 1
                    continue
            self.glossary_table.insertRow(row)
            self.glossary_table.setItem(row, 0, QTableWidgetItem(jp))
            self.glossary_table.setItem(row, 1, QTableWidgetItem(en))
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
        self.model_combo.clear()
        if models:
            self.model_combo.addItems(models)
            if current in models:
                self.model_combo.setCurrentText(current)
            self.status_label.setText(f"Found {len(models)} model(s)")
            self.status_label.setStyleSheet("color: green;")
        else:
            self.model_combo.setCurrentText(current)
            self.status_label.setText("Could not fetch models -- is Ollama running?")
            self.status_label.setStyleSheet("color: red;")

    def _test_connection(self):
        """Test if Ollama is reachable."""
        self.client.base_url = self.url_edit.text().strip() or "http://localhost:11434"
        if self.client.is_available():
            QMessageBox.information(self, "Connection OK", "Successfully connected to Ollama!")
        else:
            QMessageBox.warning(self, "Connection Failed",
                                "Cannot reach Ollama. Make sure it's running:\n  ollama serve")

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
        self.client.glossary = self._get_glossary()
        if self.parser:
            self.parser.context_size = self.context_spin.value()
        # Word wrap override
        if self.plugin_analyzer:
            manual = self.wordwrap_spin.value()
            self.plugin_analyzer._manual_chars_per_line = manual
            if manual > 0:
                self.plugin_analyzer.chars_per_line = manual
        self.dark_mode = self.dark_mode_check.isChecked()
        self.accept()

    def get_system_prompt(self) -> str:
        return self.prompt_edit.toPlainText()
