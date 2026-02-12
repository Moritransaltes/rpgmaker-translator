"""Translation engine — orchestrates LLM translation with Qt threading."""

import logging

import requests

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from .ollama_client import OllamaClient
from .project_model import TranslationEntry

log = logging.getLogger(__name__)


class TranslationWorker(QObject):
    """Worker that runs translations in a background thread."""

    entry_done = pyqtSignal(str, str)       # entry_id, translation
    item_processed = pyqtSignal(str)        # text preview (for progress tracking)
    finished = pyqtSignal()
    error = pyqtSignal(str, str)            # entry_id, error_message

    def __init__(self, client: OllamaClient, entries: list,
                 mode: str = "translate", max_history: int = 10):
        super().__init__()
        self.client = client
        self.entries = entries
        self.mode = mode  # "translate" or "polish"
        self.max_history = max_history
        self._cancelled = False
        self._history: list[tuple[str, str]] = []

    def cancel(self):
        self._cancelled = True

    def run(self):
        """Process all entries in this worker's chunk."""
        for entry in self.entries:
            if self._cancelled:
                break

            if self.mode == "translate":
                # Skip already translated/reviewed (e.g. filled by TM at checkpoint)
                if entry.status in ("translated", "reviewed", "skipped"):
                    continue
                if not entry.original.strip():
                    entry.status = "skipped"
                    continue
            else:
                # Polish mode: skip entries without translations
                if not entry.translation or not entry.translation.strip():
                    continue

            preview = (entry.translation if self.mode == "polish" else entry.original)
            preview = preview[:50].replace("\n", " ")
            self.item_processed.emit(preview)

            try:
                if self.mode == "polish":
                    result = self.client.polish(text=entry.translation)
                else:
                    result = self.client.translate(
                        text=entry.original,
                        context=entry.context,
                        field=entry.field,
                        history=self._history if self.max_history > 0 else None,
                    )
                self.entry_done.emit(entry.id, result)
                # Update sliding history window after successful translation
                if self.mode == "translate" and self.max_history > 0:
                    self._history.append((entry.original, result))
                    if len(self._history) > self.max_history:
                        self._history = self._history[-self.max_history:]
            except (ConnectionError, requests.RequestException, ValueError, OSError) as e:
                self.error.emit(entry.id, str(e))

        self.finished.emit()


class BatchTranslationWorker(QObject):
    """Worker that translates entries in JSON batches with single-entry fallback.

    DEPRECATED: Batch JSON mode tested with Sugoi Ultra 14B and Qwen3-14B —
    quality noticeably worse than single-entry.  Local models' small context
    windows (~4K tokens) can't handle system prompt + glossary + N entries
    well.  Kept for potential future use with larger cloud models.
    Use batch_size=1 (single-entry via TranslationWorker) for best results.
    """

    entry_done = pyqtSignal(str, str)       # entry_id, translation
    item_processed = pyqtSignal(str)        # text preview (for progress tracking)
    finished = pyqtSignal()
    error = pyqtSignal(str, str)            # entry_id, error_message

    MAX_RETRIES = 2

    def __init__(self, client: OllamaClient, entries: list,
                 mode: str = "translate", batch_size: int = 5,
                 max_history: int = 10):
        super().__init__()
        self.client = client
        self.entries = entries
        self.mode = mode
        self.batch_size = batch_size
        self.max_history = max_history
        self._cancelled = False
        self._history: list[tuple[str, str]] = []

    def cancel(self):
        self._cancelled = True

    def run(self):
        """Process entries in JSON batches, falling back to single-entry on failure."""
        # Filter entries based on mode
        to_process = []
        for entry in self.entries:
            if self._cancelled:
                break
            if self.mode == "translate":
                if entry.status in ("translated", "reviewed", "skipped"):
                    continue  # TM-filled at checkpoint — already counted
                if not entry.original.strip():
                    entry.status = "skipped"
                    continue
            else:
                if not entry.translation or not entry.translation.strip():
                    continue
            to_process.append(entry)

        # Group into batches
        for i in range(0, len(to_process), self.batch_size):
            if self._cancelled:
                break
            batch = to_process[i:i + self.batch_size]
            self._process_batch(batch)

        self.finished.emit()

    def _process_batch(self, batch: list):
        """Try batch translation, fall back to single-entry on failure."""
        # Build batch payload
        if self.mode == "translate":
            payload = [
                (f"Line{j+1}", e.original, e.context, e.field)
                for j, e in enumerate(batch)
            ]
        else:
            payload = [
                (f"Line{j+1}", e.translation)
                for j, e in enumerate(batch)
            ]

        # Map Line keys back to entries
        key_to_entry = {f"Line{j+1}": e for j, e in enumerate(batch)}

        # Try batch (with retries)
        for attempt in range(self.MAX_RETRIES):
            if self._cancelled:
                return
            try:
                if self.mode == "translate":
                    results = self.client.translate_batch(payload)
                else:
                    results = self.client.polish_batch(payload)

                # Emit results for entries we got back
                got_keys = set()
                for key, translation in results.items():
                    entry = key_to_entry.get(key)
                    if entry and translation:
                        preview = translation[:50].replace("\n", " ")
                        self.item_processed.emit(preview)
                        self.entry_done.emit(entry.id, translation)
                        got_keys.add(key)
                        # Update history with batch results
                        if self.mode == "translate" and self.max_history > 0:
                            self._history.append((entry.original, translation))
                            if len(self._history) > self.max_history:
                                self._history = self._history[-self.max_history:]

                # Handle missing entries (partial success) via single-entry fallback
                missing = [key_to_entry[k] for k in key_to_entry if k not in got_keys]
                if missing:
                    log.warning("Batch returned %d/%d entries, falling back for %d missing",
                                len(got_keys), len(batch), len(missing))
                    self._fallback_single(missing)
                return  # Done with this batch

            except (ConnectionError, ValueError, OSError) as e:
                log.warning("Batch attempt %d failed: %s", attempt + 1, e)
                if attempt < self.MAX_RETRIES - 1:
                    continue  # Retry
                # All retries exhausted — fall back to single-entry
                log.warning("Batch failed after %d attempts, falling back to single-entry",
                            self.MAX_RETRIES)
                self._fallback_single(batch)

    def _fallback_single(self, entries: list):
        """Translate entries one at a time (fallback when batch fails)."""
        for entry in entries:
            if self._cancelled:
                return
            preview = (entry.translation if self.mode == "polish" else entry.original)
            preview = preview[:50].replace("\n", " ")
            self.item_processed.emit(preview)
            try:
                if self.mode == "polish":
                    result = self.client.polish(text=entry.translation)
                else:
                    result = self.client.translate(
                        text=entry.original,
                        context=entry.context,
                        field=entry.field,
                        history=self._history if self.max_history > 0 else None,
                    )
                self.entry_done.emit(entry.id, result)
                # Update history after successful single-entry translation
                if self.mode == "translate" and self.max_history > 0:
                    self._history.append((entry.original, result))
                    if len(self._history) > self.max_history:
                        self._history = self._history[-self.max_history:]
            except (ConnectionError, requests.RequestException, ValueError, OSError) as e:
                self.error.emit(entry.id, str(e))


class TranslationEngine(QObject):
    """Manages parallel translation workers and threads."""

    progress = pyqtSignal(int, int, str)    # current, total, current_text
    entry_done = pyqtSignal(str, str)
    finished = pyqtSignal()
    error = pyqtSignal(str, str)
    checkpoint = pyqtSignal()

    CHECKPOINT_INTERVAL = 25  # auto-save every N translated entries

    def __init__(self, client: OllamaClient, parent=None):
        super().__init__(parent)
        self.client = client
        self.num_workers = 2
        self.batch_size = 1  # entries per JSON batch (1 = recommended; >1 deprecated — quality degrades on local models)
        self.max_history = 10  # translation history window (0 = disabled)
        self._threads = []
        self._workers = []
        self._total = 0
        self._progress_count = 0
        self._translate_count = 0
        self._finished_workers = 0

    @property
    def is_running(self) -> bool:
        return any(t.isRunning() for t in self._threads)

    def translate_batch(self, entries: list):
        """Start batch translation with parallel workers."""
        if self.is_running:
            return

        # Filter to only untranslated entries
        to_translate = [e for e in entries if e.status == "untranslated"]
        if not to_translate:
            self.finished.emit()
            return

        self._total = len(to_translate)
        self._progress_count = 0
        self._translate_count = 0
        self._finished_workers = 0
        self._threads = []
        self._workers = []

        # Split into N sequential chunks (preserves context locality)
        n = min(self.num_workers, len(to_translate))
        chunks = self._split_chunks(to_translate, n)

        for chunk in chunks:
            thread = QThread()
            if self.batch_size > 1:
                worker = BatchTranslationWorker(
                    self.client, chunk, mode="translate",
                    batch_size=self.batch_size, max_history=self.max_history)
            else:
                worker = TranslationWorker(
                    self.client, chunk, max_history=self.max_history)
            worker.moveToThread(thread)

            thread.started.connect(worker.run)
            worker.item_processed.connect(self._on_item_processed)
            worker.entry_done.connect(self._on_entry_done)
            worker.error.connect(self.error.emit)
            worker.finished.connect(self._on_worker_finished)

            self._threads.append(thread)
            self._workers.append(worker)

        # Start all threads
        for thread in self._threads:
            thread.start()

    def polish_batch(self, entries: list):
        """Start batch grammar polish with parallel workers."""
        if self.is_running:
            return

        # Filter to entries that have translations
        to_polish = [e for e in entries
                     if e.status in ("translated", "reviewed")
                     and e.translation and e.translation.strip()]
        if not to_polish:
            self.finished.emit()
            return

        self._total = len(to_polish)
        self._progress_count = 0
        self._translate_count = 0
        self._finished_workers = 0
        self._threads = []
        self._workers = []

        n = min(self.num_workers, len(to_polish))
        chunks = self._split_chunks(to_polish, n)

        for chunk in chunks:
            thread = QThread()
            if self.batch_size > 1:
                worker = BatchTranslationWorker(
                    self.client, chunk, mode="polish",
                    batch_size=self.batch_size, max_history=0)
            else:
                worker = TranslationWorker(self.client, chunk, mode="polish",
                                           max_history=0)
            worker.moveToThread(thread)

            thread.started.connect(worker.run)
            worker.item_processed.connect(self._on_item_processed)
            worker.entry_done.connect(self._on_entry_done)
            worker.error.connect(self.error.emit)
            worker.finished.connect(self._on_worker_finished)

            self._threads.append(thread)
            self._workers.append(worker)

        for thread in self._threads:
            thread.start()

    def translate_single(self, entry: TranslationEntry) -> str:
        """Translate a single entry synchronously (for right-click translate)."""
        return self.client.translate(
            text=entry.original,
            context=entry.context,
            field=entry.field,
        )

    def cancel(self):
        """Cancel all running workers."""
        for worker in self._workers:
            worker.cancel()

    def _on_item_processed(self, text: str):
        """Track global progress across all workers."""
        self._progress_count += 1
        self.progress.emit(self._progress_count, self._total, text)

    def _on_entry_done(self, entry_id: str, translation: str):
        """Relay entry completion and trigger checkpoints."""
        self.entry_done.emit(entry_id, translation)
        self._translate_count += 1
        if self._translate_count % self.CHECKPOINT_INTERVAL == 0:
            self.checkpoint.emit()

    def _on_worker_finished(self):
        """Track worker completion; emit finished when all done."""
        self._finished_workers += 1
        if self._finished_workers >= len(self._workers):
            # All workers done — clean up
            for thread in self._threads:
                thread.quit()
                thread.wait()
            self._threads = []
            self._workers = []
            self.finished.emit()

    @staticmethod
    def _split_chunks(items: list, n: int) -> list:
        """Split a list into n roughly equal sequential chunks."""
        if n <= 1:
            return [items]
        k, remainder = divmod(len(items), n)
        chunks = []
        start = 0
        for i in range(n):
            size = k + (1 if i < remainder else 0)
            chunks.append(items[start:start + size])
            start += size
        return chunks
