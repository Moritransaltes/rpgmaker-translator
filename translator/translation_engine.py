"""Translation engine — orchestrates LLM translation with Qt threading."""

import logging
import time

import requests

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from .ai_client import AIClient
from .project_model import TranslationEntry

log = logging.getLogger(__name__)


def _is_server_down_error(exc: Exception) -> bool:
    """Heuristic — does this exception indicate Ollama/the server is down?"""
    if isinstance(exc, ConnectionError):
        return True
    if isinstance(exc, requests.exceptions.ConnectionError):
        return True
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return True
    if isinstance(exc, OSError):
        # WinError 10054 (connection forcibly closed by remote host) etc.
        msg = str(exc).lower()
        if "10054" in msg or "forcibly closed" in msg or "connection reset" in msg:
            return True
        if "connection aborted" in msg or "connection refused" in msg:
            return True
    msg = str(exc).lower()
    if "read timeout" in msg or "connection refused" in msg:
        return True
    return False


# Fields that benefit from event-grouped translation (conversational flow).
# Other fields (name, description, message1, terms, etc.) are DB-style and
# don't have meaningful event order — they stay batch-flat.
_EVENT_FIELDS = {"dialog", "choice", "comment", "scroll_text",
                 "plugin_command", "comment_408"}


def _event_key(entry) -> str | None:
    """Return a grouping key for entries that belong to the same event.

    Returns None for entries that should be treated as flat (DB fields).

    Example IDs and their keys:
      "Map001.json/Ev3(EV001)/p0/dialog_5"  -> "Map001.json/Ev3(EV001)/p0"
      "CommonEvents.json/CE15(name)/dialog_2" -> "CommonEvents.json/CE15(name)"
      "Actors.json/1/name"                   -> None (DB)
    """
    if entry.field not in _EVENT_FIELDS:
        return None
    parts = entry.id.rsplit("/", 1)
    return parts[0] if len(parts) == 2 else entry.id


def _group_by_event(entries: list) -> tuple[list, list]:
    """Split entries into (event_buckets, flat_entries).

    event_buckets: list of lists, each holding entries for one event,
        sorted in original order.
    flat_entries: DB / non-event entries that don't need grouping.
    """
    buckets: dict[str, list] = {}
    bucket_order: list[str] = []
    flat: list = []
    for e in entries:
        key = _event_key(e)
        if key is None:
            flat.append(e)
        else:
            if key not in buckets:
                buckets[key] = []
                bucket_order.append(key)
            buckets[key].append(e)
    event_buckets = [buckets[k] for k in bucket_order]
    return event_buckets, flat


def _distribute_events(event_buckets: list, flat: list, n_workers: int) -> list:
    """Round-robin distribute events across workers, then append flat entries.

    Each worker receives a list-of-lists where each inner list is one event.
    Flat (DB) entries are sliced equally as a final pseudo-event per worker
    so they still get translated, but with no shared history.
    """
    assignments: list[list] = [[] for _ in range(n_workers)]
    # Round-robin events for fair load distribution
    for i, bucket in enumerate(event_buckets):
        assignments[i % n_workers].append(bucket)
    # Slice flat entries roughly equally and append as standalone "events"
    if flat:
        k, rem = divmod(len(flat), n_workers)
        start = 0
        for w in range(n_workers):
            size = k + (1 if w < rem else 0)
            if size > 0:
                assignments[w].append(flat[start:start + size])
            start += size
    return assignments


class TranslationWorker(QObject):
    """Worker that runs translations in a background thread."""

    entry_done = pyqtSignal(str, str)       # entry_id, translation
    item_processed = pyqtSignal(str)        # text preview (for progress tracking)
    finished = pyqtSignal()
    error = pyqtSignal(str, str)            # entry_id, error_message
    connection_error = pyqtSignal(str)      # error message — fired on each server-down event

    def __init__(self, client: AIClient, entries: list,
                 mode: str = "translate", max_history: int = 10,
                 events: list | None = None):
        super().__init__()
        self.client = client
        # `events` is a list-of-lists: each inner list is one event.
        if events is not None:
            self.events = events
        else:
            self.events = [entries] if entries else []
        self.mode = mode  # "translate" or "polish"
        self.max_history = max_history
        self._cancelled = False
        self._history: list[tuple[str, str]] = []

    def cancel(self):
        self._cancelled = True

    def run(self):
        """Process events in order; reset history between events."""
        for event_entries in self.events:
            if self._cancelled:
                break
            # Reset history at event boundary
            self._history = []
            for entry in event_entries:
                if self._cancelled:
                    break
                self._process_entry(entry)
        self.finished.emit()

    def _process_entry(self, entry):
        """Translate or polish a single entry, updating history."""
        if self.mode == "translate":
            if entry.status in ("translated", "reviewed", "skipped"):
                return
            if not entry.original.strip():
                entry.status = "skipped"
                return
        else:
            if not entry.translation or not entry.translation.strip():
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
            if self.mode == "translate" and self.max_history > 0:
                self._history.append((entry.original, result))
                if len(self._history) > self.max_history:
                    self._history = self._history[-self.max_history:]
        except (ConnectionError, requests.RequestException, ValueError, OSError) as e:
            if _is_server_down_error(e):
                self.connection_error.emit(str(e))
            self.error.emit(entry.id, str(e))


class BatchTranslationWorker(QObject):
    """Worker that translates entries in JSON batches with single-entry fallback.

    Used by DazedMTL Mode and cloud APIs (batch_size=30).  Sends N entries
    per request as a JSON payload with Line1/Line2 keys.  Includes
    translation history for context continuity and strict JSON schema
    enforcement on cloud providers.  Falls back to single-entry on failure.
    """

    entry_done = pyqtSignal(str, str)       # entry_id, translation
    item_processed = pyqtSignal(str)        # text preview (for progress tracking)
    finished = pyqtSignal()
    error = pyqtSignal(str, str)            # entry_id, error_message
    connection_error = pyqtSignal(str)      # error message — fired on each server-down event

    MAX_RETRIES = 2

    RECOVER_AFTER = 50  # Successes before trying to restore original batch size

    def __init__(self, client: AIClient, entries: list,
                 mode: str = "translate", batch_size: int = 5,
                 max_history: int = 10, events: list | None = None):
        super().__init__()
        self.client = client
        # `events` is a list-of-lists: each inner list is one event's entries.
        # If only `entries` is passed (legacy path), treat as a single flat group.
        if events is not None:
            self.events = events
        else:
            self.events = [entries] if entries else []
        self.mode = mode
        self.batch_size = batch_size
        self._target_batch_size = batch_size  # Remember original for recovery
        self.max_history = max_history
        self._cancelled = False
        self._history: list[tuple[str, str]] = []
        self._successes_since_fail = 0  # Track consecutive successes for recovery

    def cancel(self):
        self._cancelled = True

    def run(self):
        """Process events in order; within each event, batch and reset history."""
        for event_entries in self.events:
            if self._cancelled:
                break
            # Reset history at event boundary so prior scenes don't leak in
            self._history = []
            self._run_event(event_entries)

        self.finished.emit()

    def _run_event(self, event_entries: list):
        """Process a single event's entries in batches without crossing into others."""
        i = 0
        while i < len(event_entries):
            if self._cancelled:
                return

            # Build next batch from this event only — never crosses event boundary
            batch = []
            while i < len(event_entries) and len(batch) < self.batch_size:
                entry = event_entries[i]
                i += 1
                if self.mode == "translate":
                    if entry.status in ("translated", "reviewed", "skipped"):
                        continue
                    if not entry.original.strip():
                        entry.status = "skipped"
                        continue
                else:
                    if not entry.translation or not entry.translation.strip():
                        continue
                batch.append(entry)

            if batch:
                self._process_batch(batch)

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
                    results = self.client.translate_batch(
                        payload,
                        history=self._history if self.max_history > 0 else None,
                    )
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

                # Track successes for batch size recovery
                self._successes_since_fail += len(batch)
                if (self.batch_size < self._target_batch_size
                        and self._successes_since_fail >= self.RECOVER_AFTER):
                    self.batch_size = self._target_batch_size
                    self._successes_since_fail = 0
                    log.info("Batch recovered to original size %d after %d successes",
                             self.batch_size, self.RECOVER_AFTER)
                return  # Done with this batch

            except (ConnectionError, ValueError, OSError) as e:
                log.warning("Batch attempt %d failed: %s", attempt + 1, e)
                if _is_server_down_error(e):
                    self.connection_error.emit(str(e))
                if attempt < self.MAX_RETRIES - 1:
                    continue  # Retry
                # All retries exhausted — halve batch size and fall back for this batch
                old_size = self.batch_size
                self.batch_size = max(1, self.batch_size // 2)
                self._successes_since_fail = 0
                log.warning("Batch of %d failed after %d attempts, "
                            "halving to %d. Falling back for this batch.",
                            old_size, self.MAX_RETRIES, self.batch_size)
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
                if _is_server_down_error(e):
                    self.connection_error.emit(str(e))
                self.error.emit(entry.id, str(e))


class TranslationEngine(QObject):
    """Manages parallel translation workers and threads."""

    progress = pyqtSignal(int, int, str)    # current, total, current_text
    entry_done = pyqtSignal(str, str)
    finished = pyqtSignal()
    error = pyqtSignal(str, str)
    checkpoint = pyqtSignal()
    server_down = pyqtSignal(str)           # server appears down — reason msg

    CHECKPOINT_INTERVAL = 25  # auto-save every N translated entries
    SERVER_DOWN_THRESHOLD = 5  # consecutive connection errors within window
    SERVER_DOWN_WINDOW_S = 30  # …seconds, triggers server_down signal

    def __init__(self, client: AIClient, parent=None):
        super().__init__(parent)
        self.client = client
        self.num_workers = 2
        self.batch_size = 5  # entries per JSON batch (5 = local default; 30 = DazedMTL/cloud)
        self.max_history = 10  # translation history window (0 = disabled)
        self._threads = []
        self._workers = []
        self._total = 0
        self._progress_count = 0
        self._translate_count = 0
        self._finished_workers = 0
        self._cancelled = False
        self._connection_failures: list[float] = []  # timestamps for window
        self._server_down_emitted = False

    @property
    def is_running(self) -> bool:
        return any(t.isRunning() for t in self._threads)

    def translate_batch(self, entries: list):
        """Start batch translation with parallel workers.

        Pre-fills untranslated entries whose original text was already
        translated elsewhere in the project (cross-project translation
        memory), saving the LLM trips for repeated lines.
        """
        if self.is_running:
            return

        # Filter to only untranslated entries
        to_translate = [e for e in entries if e.status == "untranslated"]
        if not to_translate:
            self.finished.emit()
            return

        # Cross-project translation memory: pre-fill duplicates from
        # entries that are already translated elsewhere in the project.
        memory = self._build_translation_memory(entries)
        prefilled = 0
        if memory:
            for e in to_translate:
                tl = memory.get(e.original)
                if tl:
                    e.translation = tl
                    e.status = "translated"
                    prefilled += 1
                    # Emit so UI updates and checkpoint counter increments
                    self.entry_done.emit(e.id, tl)
            if prefilled:
                log.info("Translation memory: pre-filled %d/%d entries from "
                         "already-translated duplicates", prefilled, len(to_translate))
            # Re-filter — the freshly translated ones drop out
            to_translate = [e for e in to_translate if e.status == "untranslated"]
            if not to_translate:
                self.checkpoint.emit()  # save the prefilled work
                self.finished.emit()
                return

        self._total = len(to_translate)
        self._progress_count = 0
        self._translate_count = 0
        self._cancelled = False
        self._connection_failures = []
        self._server_down_emitted = False

        self._start_workers(to_translate)

    def _start_workers(self, to_translate: list):
        """Spawn parallel worker threads for the main translation batch.

        Distribution: each worker gets whole events round-robin so dialogue
        within a scene is always handled by one worker in order. DB entries
        (no event) are sliced equally across workers as standalone groups.
        """
        self._finished_workers = 0
        self._threads = []
        self._workers = []

        event_buckets, flat = _group_by_event(to_translate)
        n = min(self.num_workers, max(1, len(event_buckets) + (1 if flat else 0)))
        worker_assignments = _distribute_events(event_buckets, flat, n)

        log.info("Translation: %d events + %d flat entries across %d workers",
                 len(event_buckets), len(flat), n)

        for events in worker_assignments:
            if not events:
                continue
            thread = QThread()
            if self.batch_size > 1:
                worker = BatchTranslationWorker(
                    self.client, entries=None, events=events, mode="translate",
                    batch_size=self.batch_size, max_history=self.max_history)
            else:
                worker = TranslationWorker(
                    self.client, entries=None, events=events,
                    max_history=self.max_history)
            worker.moveToThread(thread)

            thread.started.connect(worker.run)
            worker.item_processed.connect(self._on_item_processed)
            worker.entry_done.connect(self._on_entry_done)
            worker.error.connect(self.error.emit)
            worker.connection_error.connect(self._on_connection_error)
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
        self._cancelled = False
        self._connection_failures = []
        self._server_down_emitted = False

        # Polish event-grouped, with history enabled so the polisher sees prior
        # polished lines from the same scene — keeps tone/voice consistent
        # across a polish pass on long events.
        event_buckets, flat = _group_by_event(to_polish)
        n = min(self.num_workers, max(1, len(event_buckets) + (1 if flat else 0)))
        worker_assignments = _distribute_events(event_buckets, flat, n)

        for events in worker_assignments:
            if not events:
                continue
            thread = QThread()
            if self.batch_size > 1:
                worker = BatchTranslationWorker(
                    self.client, entries=None, events=events, mode="polish",
                    batch_size=self.batch_size, max_history=self.max_history)
            else:
                worker = TranslationWorker(self.client, entries=None,
                                           events=events, mode="polish",
                                           max_history=self.max_history)
            worker.moveToThread(thread)

            thread.started.connect(worker.run)
            worker.item_processed.connect(self._on_item_processed)
            worker.entry_done.connect(self._on_entry_done)
            worker.error.connect(self.error.emit)
            worker.connection_error.connect(self._on_connection_error)
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
        self._cancelled = True
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

    def _on_connection_error(self, msg: str):
        """Track connection errors; trigger server_down if rate threshold hit.

        Stops all workers when too many connection errors fire in a short
        window, so we don't grind through 1000 failed batches.
        """
        if self._server_down_emitted or self._cancelled:
            return
        now = time.monotonic()
        # Drop timestamps outside the window
        cutoff = now - self.SERVER_DOWN_WINDOW_S
        self._connection_failures = [t for t in self._connection_failures if t > cutoff]
        self._connection_failures.append(now)

        if len(self._connection_failures) >= self.SERVER_DOWN_THRESHOLD:
            self._server_down_emitted = True
            log.error("Server down detected (%d connection errors in %ds): %s",
                      len(self._connection_failures), self.SERVER_DOWN_WINDOW_S, msg)
            # Cancel all running workers — checkpoint already saved completed work
            self._cancelled = True
            for w in self._workers:
                if hasattr(w, "cancel"):
                    w.cancel()
            self.server_down.emit(msg)

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
    def _build_translation_memory(entries: list) -> dict[str, str]:
        """Build {original: translation} from already-translated entries.

        Used to pre-fill untranslated entries that match an already-translated
        original elsewhere in the project — saves redundant LLM calls for
        repeated lines (greetings, "...", "Yes", boss intros said by 50 NPCs).
        """
        memory: dict[str, str] = {}
        for e in entries:
            if e.status not in ("translated", "reviewed"):
                continue
            if not e.original or not e.translation:
                continue
            # First-write-wins; reviewed translations are already preferred
            # over fresh ones because they appear later in batches typically.
            if e.original not in memory:
                memory[e.original] = e.translation
        return memory

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
