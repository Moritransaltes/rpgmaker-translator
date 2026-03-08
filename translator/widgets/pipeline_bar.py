"""Pipeline progress bar — shows translation workflow steps with completion tracking."""

from PyQt6.QtWidgets import QWidget, QHBoxLayout, QLabel, QPushButton
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont


class _StepLabel(QLabel):
    """A single step indicator: number + name, styled by state."""

    def __init__(self, number: int, name: str, parent=None):
        super().__init__(parent)
        self.number = number
        self.name = name
        self._state = "pending"  # pending | active | done
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._refresh()

    def _refresh(self):
        if self._state == "done":
            icon = "\u2713"  # checkmark
            color = "#a6e3a1"  # green
        elif self._state == "active":
            icon = "\u25b6"  # play triangle
            color = "#89b4fa"  # blue
        else:
            icon = str(self.number)
            color = "#6c7086"  # muted

        self.setText(f"{icon} {self.name}")
        self.setStyleSheet(f"color: {color}; padding: 2px 8px;")
        font = self.font()
        font.setBold(self._state in ("active", "done"))
        self.setFont(font)

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, value: str):
        self._state = value
        self._refresh()


# Steps definition: (key, display_name)
PIPELINE_STEPS = [
    ("db", "Translate DB"),
    ("dialogue", "Translate Dialogue"),
    ("cleanup", "Clean Up"),
    ("wordwrap", "Word Wrap"),
    ("export", "Export"),
]


class PipelineBar(QWidget):
    """Horizontal pipeline progress bar with step indicators and Next Step button."""

    step_requested = pyqtSignal(str)  # emits step key when Next Step clicked

    def __init__(self, parent=None):
        super().__init__(parent)
        self._step_labels: dict[str, _StepLabel] = {}
        self._current_index = -1  # no step active
        self._build_ui()
        self.setVisible(False)  # hidden until project loaded

    def _build_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(0)

        # Step labels with arrows between them
        for i, (key, name) in enumerate(PIPELINE_STEPS):
            label = _StepLabel(i + 1, name)
            self._step_labels[key] = label
            layout.addWidget(label)
            if i < len(PIPELINE_STEPS) - 1:
                arrow = QLabel("\u2192")
                arrow.setStyleSheet("color: #45475a; padding: 0 4px;")
                arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
                layout.addWidget(arrow)

        layout.addSpacing(12)

        # Next Step nudge
        self.next_btn = QPushButton("Next Step")
        self.next_btn.setFixedHeight(24)
        self.next_btn.setStyleSheet(
            "QPushButton { background-color: #89b4fa; color: #1e1e2e; "
            "border: none; padding: 2px 12px; border-radius: 3px; font-weight: bold; }"
            "QPushButton:hover { background-color: #74c7ec; }"
            "QPushButton:disabled { background-color: #313244; color: #6c7086; }"
        )
        self.next_btn.clicked.connect(self._on_next_clicked)
        self.next_btn.setVisible(False)
        layout.addWidget(self.next_btn)

        layout.addStretch()

        # Status hint
        self.hint_label = QLabel("")
        self.hint_label.setStyleSheet("color: #a6adc8; font-size: 11px;")
        layout.addWidget(self.hint_label)

    def reset(self):
        """Reset all steps to pending (call when opening a new project)."""
        self._current_index = -1
        for label in self._step_labels.values():
            label.state = "pending"
        self.next_btn.setVisible(False)
        self.hint_label.setText("")
        self.setVisible(True)
        self._nudge_next()

    def mark_active(self, step_key: str):
        """Mark a step as currently running."""
        if step_key not in self._step_labels:
            return
        # Clear any previous active
        for key, label in self._step_labels.items():
            if label.state == "active":
                label.state = "pending"
        self._step_labels[step_key].state = "active"
        idx = [k for k, _ in PIPELINE_STEPS].index(step_key)
        self._current_index = idx
        self.next_btn.setVisible(False)
        self.hint_label.setText(f"{PIPELINE_STEPS[idx][1]} in progress...")

    def mark_done(self, step_key: str):
        """Mark a step as completed and show Next Step nudge."""
        if step_key not in self._step_labels:
            return
        self._step_labels[step_key].state = "done"
        self._nudge_next()

    def mark_done_up_to(self, step_key: str):
        """Mark all steps up to and including step_key as done."""
        keys = [k for k, _ in PIPELINE_STEPS]
        if step_key not in keys:
            return
        target = keys.index(step_key)
        for i, (key, _) in enumerate(PIPELINE_STEPS):
            if i <= target:
                self._step_labels[key].state = "done"
        self._nudge_next()

    def _nudge_next(self):
        """Find next pending step and show the nudge button."""
        for i, (key, name) in enumerate(PIPELINE_STEPS):
            if self._step_labels[key].state == "pending":
                self._current_index = i
                self.next_btn.setText(f"Next: {name}")
                self.next_btn.setVisible(True)
                self.hint_label.setText("")
                return
        # All done
        self.next_btn.setVisible(False)
        self.hint_label.setText("All steps complete!")

    def _on_next_clicked(self):
        """Emit the step key for the next pending step."""
        for key, _ in PIPELINE_STEPS:
            if self._step_labels[key].state == "pending":
                self.step_requested.emit(key)
                return
