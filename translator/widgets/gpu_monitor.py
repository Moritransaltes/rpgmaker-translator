"""GPU monitor panel — polls nvidia-smi for VRAM, utilization, temp, power."""

import subprocess
import logging

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar
from PyQt6.QtCore import QTimer, Qt

log = logging.getLogger(__name__)

_QUERY = "name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw"
_CMD = [
    "nvidia-smi",
    f"--query-gpu={_QUERY}",
    "--format=csv,noheader,nounits",
]


class GPUMonitorPanel(QWidget):
    """Compact GPU stats panel that auto-updates via nvidia-smi."""

    def __init__(self, parent=None, poll_ms: int = 2000):
        super().__init__(parent)
        self._available = False
        self._build_ui()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(poll_ms)
        # Initial poll
        self._poll()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(2)

        # GPU name
        self._name_label = QLabel("GPU: detecting...")
        self._name_label.setStyleSheet("font-weight: bold; font-size: 11px;")
        layout.addWidget(self._name_label)

        # VRAM bar
        vram_row = QHBoxLayout()
        vram_row.setSpacing(4)
        self._vram_label = QLabel("VRAM:")
        self._vram_label.setFixedWidth(42)
        self._vram_label.setStyleSheet("font-size: 11px;")
        vram_row.addWidget(self._vram_label)

        self._vram_bar = QProgressBar()
        self._vram_bar.setRange(0, 100)
        self._vram_bar.setFixedHeight(16)
        self._vram_bar.setTextVisible(True)
        vram_row.addWidget(self._vram_bar)
        layout.addLayout(vram_row)

        # GPU utilization bar
        util_row = QHBoxLayout()
        util_row.setSpacing(4)
        self._util_label = QLabel("GPU:")
        self._util_label.setFixedWidth(42)
        self._util_label.setStyleSheet("font-size: 11px;")
        util_row.addWidget(self._util_label)

        self._util_bar = QProgressBar()
        self._util_bar.setRange(0, 100)
        self._util_bar.setFixedHeight(16)
        self._util_bar.setTextVisible(True)
        util_row.addWidget(self._util_bar)
        layout.addLayout(util_row)

        # Temp + Power row
        stats_row = QHBoxLayout()
        stats_row.setSpacing(8)
        self._temp_label = QLabel("Temp: --")
        self._temp_label.setStyleSheet("font-size: 11px;")
        stats_row.addWidget(self._temp_label)
        self._power_label = QLabel("Power: --")
        self._power_label.setStyleSheet("font-size: 11px;")
        stats_row.addWidget(self._power_label)
        stats_row.addStretch()
        layout.addLayout(stats_row)

    def _poll(self):
        """Query nvidia-smi and update display."""
        try:
            result = subprocess.run(
                _CMD,
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode != 0:
                if self._available:
                    self._set_unavailable()
                return

            line = result.stdout.strip().split("\n")[0]
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 6:
                return

            name = parts[0]
            mem_used = float(parts[1])
            mem_total = float(parts[2])
            gpu_util = int(float(parts[3]))
            temp = int(float(parts[4]))
            power = float(parts[5])

            self._available = True
            self._name_label.setText(f"GPU: {name}")

            # VRAM
            mem_pct = int(mem_used / mem_total * 100) if mem_total > 0 else 0
            self._vram_bar.setValue(mem_pct)
            self._vram_bar.setFormat(
                f"{mem_used:.0f} / {mem_total:.0f} MB ({mem_pct}%)"
            )
            self._color_bar(self._vram_bar, mem_pct)

            # Utilization
            self._util_bar.setValue(gpu_util)
            self._util_bar.setFormat(f"{gpu_util}%")
            self._color_bar(self._util_bar, gpu_util)

            # Temp + Power
            self._temp_label.setText(f"Temp: {temp}\u00b0C")
            if temp >= 80:
                self._temp_label.setStyleSheet("font-size: 11px; color: #f38ba8;")
            elif temp >= 65:
                self._temp_label.setStyleSheet("font-size: 11px; color: #fab387;")
            else:
                self._temp_label.setStyleSheet("font-size: 11px; color: #a6e3a1;")

            self._power_label.setText(f"Power: {power:.0f}W")

        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            if self._available or not hasattr(self, '_init_done'):
                self._set_unavailable()
            self._init_done = True

    def _set_unavailable(self):
        """Mark GPU as unavailable."""
        self._available = False
        self._name_label.setText("GPU: not detected")
        self._vram_bar.setValue(0)
        self._vram_bar.setFormat("N/A")
        self._util_bar.setValue(0)
        self._util_bar.setFormat("N/A")
        self._temp_label.setText("Temp: --")
        self._power_label.setText("Power: --")

    @staticmethod
    def _color_bar(bar: QProgressBar, pct: int):
        """Color the progress bar based on percentage (Catppuccin palette)."""
        if pct >= 90:
            color = "#f38ba8"  # red
        elif pct >= 70:
            color = "#fab387"  # peach
        elif pct >= 50:
            color = "#f9e2af"  # yellow
        else:
            color = "#a6e3a1"  # green
        bar.setStyleSheet(
            f"QProgressBar {{ border: 1px solid #585b70; border-radius: 3px; "
            f"background: #313244; font-size: 10px; color: #cdd6f4; }}"
            f"QProgressBar::chunk {{ background: {color}; border-radius: 2px; }}"
        )

    @property
    def is_available(self) -> bool:
        """Whether an NVIDIA GPU was detected."""
        return self._available
