"""Data model for translation entries and project state."""

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class TranslationEntry:
    """A single translatable text entry from an RPG Maker project."""
    id: str                # Unique key e.g. "Actors/1/name", "Map001/Event3/page0/dialog_5"
    file: str              # Source filename e.g. "Actors.json", "Map001.json"
    field: str             # Field path e.g. "name", "description", "dialog"
    original: str          # Original Japanese text
    translation: str = ""  # English translation (empty until translated)
    status: str = "untranslated"  # "untranslated" | "translated" | "reviewed" | "skipped"
    context: str = ""      # Surrounding text for LLM context


@dataclass
class TranslationProject:
    """Holds all translation entries for an RPG Maker project."""
    project_path: str = ""
    entries: list = field(default_factory=list)
    glossary: dict = field(default_factory=dict)  # JP -> EN forced mappings
    actor_genders: dict = field(default_factory=dict)  # actor_id -> gender

    @property
    def total(self) -> int:
        return len(self.entries)

    @property
    def translated_count(self) -> int:
        return sum(1 for e in self.entries if e.status in ("translated", "reviewed"))

    @property
    def reviewed_count(self) -> int:
        return sum(1 for e in self.entries if e.status == "reviewed")

    @property
    def untranslated_count(self) -> int:
        return sum(1 for e in self.entries if e.status == "untranslated")

    def _build_index(self):
        """Build internal lookup dicts from the entries list."""
        self._by_file = defaultdict(list)
        self._by_id = {}
        for e in self.entries:
            self._by_file[e.file].append(e)
            self._by_id[e.id] = e

    def get_entries_for_file(self, filename: str) -> list:
        """Return entries belonging to a specific file."""
        if not hasattr(self, "_by_file"):
            self._build_index()
        return self._by_file.get(filename, [])

    def get_files(self) -> list:
        """Return sorted unique filenames."""
        if not hasattr(self, "_by_file"):
            self._build_index()
        return sorted(self._by_file.keys())

    def get_entry_by_id(self, entry_id: str) -> Optional[TranslationEntry]:
        """Find an entry by its unique ID."""
        if not hasattr(self, "_by_id"):
            self._build_index()
        return self._by_id.get(entry_id)

    def search(self, query: str) -> list:
        """Search entries by original or translation text."""
        q = query.lower()
        return [e for e in self.entries if q in e.original.lower() or q in e.translation.lower()]

    def save_state(self, path: str):
        """Save project state to a JSON file for resume support."""
        data = {
            "project_path": self.project_path,
            "entries": [asdict(e) for e in self.entries],
            "glossary": self.glossary,
            "actor_genders": self.actor_genders,
        }
        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @classmethod
    def load_state(cls, path: str) -> "TranslationProject":
        """Load project state from a saved JSON file."""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        project = cls(project_path=data.get("project_path", ""))
        project.entries = [TranslationEntry(**e) for e in data.get("entries", [])]
        project.glossary = data.get("glossary", {})
        # JSON converts int keys to strings — convert back to int
        raw_genders = data.get("actor_genders", {})
        project.actor_genders = {int(k): v for k, v in raw_genders.items()}
        project._build_index()
        return project

    def import_translations(self, old_project: "TranslationProject") -> dict:
        """Import translations from an older version of the same project.

        Matching strategy:
        1. Exact ID match — same entry position in the game files
        2. Original text match — catches entries that moved location

        Only imports entries that have a translation in the old project
        and are currently untranslated in this project.

        Returns:
            Dict with stats: {"by_id": int, "by_text": int, "skipped": int, "new": int}
        """
        if not hasattr(self, "_by_id"):
            self._build_index()

        old_by_id = {}
        old_by_text = defaultdict(list)
        for e in old_project.entries:
            if e.translation and e.status in ("translated", "reviewed"):
                old_by_id[e.id] = e
                old_by_text[e.original].append(e)

        stats = {"by_id": 0, "by_text": 0, "skipped": 0, "new": 0}

        for entry in self.entries:
            if entry.status != "untranslated":
                stats["skipped"] += 1
                continue

            # Strategy 1: exact ID match
            old = old_by_id.get(entry.id)
            if old and old.original == entry.original:
                entry.translation = old.translation
                entry.status = old.status
                stats["by_id"] += 1
                continue

            # Strategy 2: same original text (first match)
            candidates = old_by_text.get(entry.original, [])
            if candidates:
                entry.translation = candidates[0].translation
                entry.status = candidates[0].status
                stats["by_text"] += 1
                continue

            stats["new"] += 1

        return stats

    def stats_for_file(self, filename: str) -> tuple:
        """Return (translated_count, total_count) for a file."""
        file_entries = self.get_entries_for_file(filename)
        translated = sum(1 for e in file_entries if e.status in ("translated", "reviewed"))
        return translated, len(file_entries)
