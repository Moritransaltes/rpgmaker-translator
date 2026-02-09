"""Translation engine — orchestrates LLM translation with Qt threading."""

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from .ollama_client import OllamaClient
from .project_model import TranslationEntry


class TranslationWorker(QObject):
    """Worker that runs translations in a background thread."""

    progress = pyqtSignal(int, int, str)   # current, total, current_text
    entry_done = pyqtSignal(str, str)       # entry_id, translation
    finished = pyqtSignal()
    error = pyqtSignal(str, str)            # entry_id, error_message
    checkpoint = pyqtSignal()              # emitted every N entries for auto-save

    CHECKPOINT_INTERVAL = 25  # auto-save every N translated entries

    def __init__(self, client: OllamaClient, entries: list):
        super().__init__()
        self.client = client
        self.entries = entries
        self._cancelled = False
        self._since_checkpoint = 0

    def cancel(self):
        self._cancelled = True

    def run(self):
        """Translate all entries sequentially."""
        total = len(self.entries)
        for i, entry in enumerate(self.entries):
            if self._cancelled:
                break

            # Skip already translated/reviewed or empty
            if entry.status in ("translated", "reviewed", "skipped"):
                self.progress.emit(i + 1, total, "(skipped)")
                continue

            if not entry.original.strip():
                entry.status = "skipped"
                self.progress.emit(i + 1, total, "(empty)")
                continue

            preview = entry.original[:50].replace("\n", " ")
            self.progress.emit(i + 1, total, preview)

            try:
                translation = self.client.translate(
                    text=entry.original,
                    context=entry.context,
                    field=entry.field,
                )
                self.entry_done.emit(entry.id, translation)
                self._since_checkpoint += 1
                if self._since_checkpoint >= self.CHECKPOINT_INTERVAL:
                    self._since_checkpoint = 0
                    self.checkpoint.emit()
            except Exception as e:
                self.error.emit(entry.id, str(e))

        self.finished.emit()


class TranslationEngine(QObject):
    """Manages translation workers and threads."""

    progress = pyqtSignal(int, int, str)
    entry_done = pyqtSignal(str, str)
    finished = pyqtSignal()
    error = pyqtSignal(str, str)
    checkpoint = pyqtSignal()

    def __init__(self, client: OllamaClient, parent=None):
        super().__init__(parent)
        self.client = client
        self._thread = None
        self._worker = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def translate_batch(self, entries: list):
        """Start batch translation in a background thread."""
        if self.is_running:
            return

        # Filter to only untranslated entries
        to_translate = [e for e in entries if e.status == "untranslated"]
        if not to_translate:
            self.finished.emit()
            return

        self._thread = QThread()
        self._worker = TranslationWorker(self.client, to_translate)
        self._worker.moveToThread(self._thread)

        # Wire signals
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.progress.emit)
        self._worker.entry_done.connect(self.entry_done.emit)
        self._worker.error.connect(self.error.emit)
        self._worker.checkpoint.connect(self.checkpoint.emit)
        self._worker.finished.connect(self._on_finished)

        self._thread.start()

    def translate_single(self, entry: TranslationEntry) -> str:
        """Translate a single entry synchronously (for right-click translate)."""
        return self.client.translate(
            text=entry.original,
            context=entry.context,
            field=entry.field,
        )

    def cancel(self):
        """Cancel the running batch translation."""
        if self._worker:
            self._worker.cancel()

    def _on_finished(self):
        """Clean up thread when worker finishes."""
        if self._thread:
            self._thread.quit()
            self._thread.wait()
            self._thread = None
            self._worker = None
        self.finished.emit()
