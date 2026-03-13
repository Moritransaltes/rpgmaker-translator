"""RPG Maker VX Ace (.rvdata2) parser and writer.

Handles extraction of translatable strings from RPG Maker VX Ace data files
(Ruby Marshal format) and writing translations back.  Uses the same event
command codes as MV/MZ (101, 401, 102, 405, 320, 324, 325, 356) since the
format is identical — just stored as Ruby objects instead of JSON.

Requires: rubymarshal  (pip install rubymarshal)

File layout expected::

    GameFolder/
    ├── Game.exe / Game.rgss3a (optional encrypted archive)
    ├── Data/
    │   ├── Actors.rvdata2
    │   ├── Items.rvdata2
    │   ├── Map001.rvdata2
    │   ├── System.rvdata2
    │   ├── CommonEvents.rvdata2
    │   ├── Troops.rvdata2
    │   └── ...
    └── Graphics/, Audio/, ...

Ruby Marshal objects use @-prefixed attribute names:
  RPG::Actor  → @name, @nickname, @description, @note, @face_name, @face_index
  RPG::Item   → @name, @description, @note
  RPG::Map    → @events {id → RPG::Event}, @display_name
  RPG::Event  → @name, @pages [RPG::Event::Page]
  Page        → @list [RPG::EventCommand]
  EventCommand→ @code, @parameters
"""

import logging
import os
import re
import shutil
from collections import deque
from typing import Optional

from rubymarshal.reader import loads as ruby_loads
from rubymarshal.writer import writes as ruby_writes

from . import JAPANESE_RE
from .project_model import TranslationEntry

log = logging.getLogger(__name__)

# ── Event command codes (identical to MV/MZ) ─────────────────────────
CODE_SHOW_TEXT_HEADER = 101   # face/position setup — not translatable itself
CODE_SHOW_TEXT = 401          # Show Text line — @parameters[0] is text
CODE_SHOW_CHOICES = 102       # Show Choices — @parameters[0] is list of strings
CODE_SCROLL_TEXT_HEADER = 105 # Scroll Text setup — not translatable
CODE_SCROLL_TEXT = 405        # Scroll Text line — @parameters[0] is text
CODE_CHANGE_NAME = 320        # Change Actor Name — params[0]=actorId, params[1]=name
CODE_CHANGE_NICKNAME = 324    # Change Actor Nickname
CODE_CHANGE_PROFILE = 325     # Change Actor Profile
CODE_PLUGIN_COMMAND = 356     # Script-based plugin command (MV-style, rare in Ace)

# Database files and their translatable fields (@ prefix stripped for lookup)
DATABASE_FILES = {
    "Actors.rvdata2":   ["name", "nickname", "description"],
    "Classes.rvdata2":  ["name"],
    "Items.rvdata2":    ["name", "description"],
    "Weapons.rvdata2":  ["name", "description"],
    "Armors.rvdata2":   ["name", "description"],
    "Skills.rvdata2":   ["name", "description", "message1", "message2"],
    "States.rvdata2":   ["name", "message1", "message2", "message3", "message4"],
    "Enemies.rvdata2":  ["name"],
}

# Gender detection keywords (same as MV/MZ parser)
_FEMALE_HINTS = re.compile(
    r'彼女|お姉|少女|王女|巫女|メイド|おかあ|女|姫|嬢|娘|母|姉|妹|妻|'
    r'\bactress\b|\bfemale\b|\bgirl\b|\bwoman\b|\bprincess\b|\bqueen\b|\blady\b|\bwitch\b|\bpriestess\b|\bmaid\b',
    re.IGNORECASE
)
_MALE_HINTS = re.compile(
    r'おとうさん|少年|勇者|騎士|王子|息子|男|父|兄|弟|夫|彼|'
    r'\bactor\b|\bmale\b|\bboy\b|\bman\b|\bprince\b|\bking\b|\bknight\b|\bhero\b|\blord\b',
    re.IGNORECASE
)

# Namebox: \N<name> prefix used by some VX Ace plugins
_NAMEBOX_RE = re.compile(r'\\[Nn]<([^>]+)>')
_ACTOR_CODE_RE = re.compile(r'\\[Nn]\[(\d+)\]')


def _str(val) -> str:
    """Coerce RubyString or any string-like value to plain str."""
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if hasattr(val, "encode"):  # RubyString
        return str(val)
    return ""


def _has_japanese(text: str) -> bool:
    return bool(JAPANESE_RE.search(text))


def _detect_gender(description: str, note: str, nickname: str) -> str:
    all_text = f"{description} {note} {nickname}"
    female = len(_FEMALE_HINTS.findall(all_text))
    male = len(_MALE_HINTS.findall(all_text))
    if female > male:
        return "female"
    if male > female:
        return "male"
    return ""


def _attr(obj, key, default=None):
    """Read an @-prefixed attribute from a Ruby object.

    Strings are cast to plain str (rubymarshal returns RubyString).
    """
    if obj is None:
        return default
    attrs = getattr(obj, "attributes", None)
    if attrs is None:
        return default
    val = attrs.get(f"@{key}", default)
    # rubymarshal returns RubyString — cast to plain str for regex compat
    if val is not None and not isinstance(val, str) and hasattr(val, "encode"):
        return str(val)
    return val


def _set_attr(obj, key, value):
    """Write an @-prefixed attribute on a Ruby object."""
    obj.attributes[f"@{key}"] = value


class RPGMakerAceParser:
    """Parser for RPG Maker VX Ace .rvdata2 data files."""

    def __init__(self):
        self.context_size = 3
        self._require_japanese = True
        self._actor_names: dict[int, str] = {}
        self._face_to_actor: dict[tuple, int] = {}

    # ── Public interface (matches MV/MZ parser) ──────────────────────

    @staticmethod
    def is_ace_project(path: str) -> bool:
        """Check if path contains a VX Ace project (Data/*.rvdata2)."""
        data_dir = os.path.join(path, "Data")
        if not os.path.isdir(data_dir):
            return False
        return any(f.endswith(".rvdata2") for f in os.listdir(data_dir))

    def _should_extract(self, text: str) -> bool:
        if not text or not text.strip():
            return False
        if self._require_japanese:
            return _has_japanese(text)
        return True

    def get_game_title(self, project_dir: str) -> str:
        data_dir = self._find_data_dir(project_dir)
        if not data_dir:
            return ""
        sys_path = os.path.join(data_dir, "System.rvdata2")
        if not os.path.exists(sys_path):
            return ""
        try:
            data = self._read_rvdata2(sys_path)
            return _attr(data, "game_title", "")
        except Exception:
            return ""

    def load_actors_raw(self, project_dir: str) -> list:
        """Load raw actor data for the gender assignment dialog."""
        data_dir = self._find_data_dir(project_dir)
        if not data_dir:
            return []
        path = os.path.join(data_dir, "Actors.rvdata2")
        if not os.path.exists(path):
            return []
        try:
            data = self._read_rvdata2(path)
        except Exception:
            return []
        if not isinstance(data, list):
            return []

        actors = []
        for item in data:
            if item is None:
                continue
            name = (_attr(item, "name") or "").strip()
            if not name:
                continue
            description = (_attr(item, "description") or "").strip()
            nickname = (_attr(item, "nickname") or "").strip()
            note = (_attr(item, "note") or "").strip()
            auto_gender = _detect_gender(description, note, nickname)
            actors.append({
                "id": _attr(item, "id", 0),
                "name": name,
                "nickname": nickname,
                "profile": description,  # VX Ace uses "description" not "profile"
                "auto_gender": auto_gender or "unknown",
            })
        return actors

    def load_project(self, project_dir: str) -> list:
        """Load all translatable entries from a VX Ace project."""
        data_dir = self._find_data_dir(project_dir)
        if not data_dir:
            raise FileNotFoundError(
                f"No 'Data' folder found in {project_dir}.\n"
                "Please select an RPG Maker VX Ace project folder."
            )

        self._actor_names = self._load_actor_names(data_dir)
        self._face_to_actor = self._load_face_to_actor(data_dir)

        entries = []
        entries.extend(self._parse_database_files(data_dir))
        entries.extend(self._parse_system(data_dir))
        entries.extend(self._parse_common_events(data_dir))
        entries.extend(self._parse_troops(data_dir))
        entries.extend(self._parse_maps(data_dir))

        # Deduplicate speaker names
        seen_speakers = set()
        deduped = []
        for e in entries:
            if e.field == "speaker_name":
                if e.original in seen_speakers:
                    continue
                seen_speakers.add(e.original)
            deduped.append(e)
        return deduped

    def save_project(self, project_dir: str, entries: list):
        """Write translations back into .rvdata2 files."""
        data_dir = self._find_data_dir(project_dir)
        if not data_dir:
            raise FileNotFoundError(
                f"Could not find Data/ folder in {project_dir}."
            )

        # Backup originals on first export
        self._backup_data_dir(data_dir)

        backup_dir = data_dir + "_original"
        source_dir = backup_dir if os.path.isdir(backup_dir) else data_dir

        # Build lookup: file → [entries]
        by_file: dict[str, list] = {}
        for e in entries:
            if not (e.translation and e.status in ("translated", "reviewed")):
                continue
            by_file.setdefault(e.file, []).append(e)

        for filename, file_entries in by_file.items():
            source_path = os.path.join(source_dir, filename)
            if not os.path.exists(source_path):
                continue
            try:
                data = self._read_rvdata2(source_path)
            except Exception as exc:
                log.error("Failed to read %s: %s", source_path, exc)
                continue

            self._apply_translations(data, filename, file_entries)

            out_path = os.path.join(data_dir, filename)
            self._write_rvdata2(out_path, data)

    def restore_originals(self, project_dir: str):
        """Restore Data_original/ → Data/."""
        data_dir = self._find_data_dir(project_dir)
        if not data_dir:
            return
        backup_dir = data_dir + "_original"
        if not os.path.isdir(backup_dir):
            return
        shutil.rmtree(data_dir)
        shutil.copytree(backup_dir, data_dir)

    # ── Ruby Marshal I/O ─────────────────────────────────────────────

    @staticmethod
    def _read_rvdata2(path: str):
        with open(path, "rb") as f:
            return ruby_loads(f.read())

    @staticmethod
    def _write_rvdata2(path: str, data):
        with open(path, "wb") as f:
            f.write(ruby_writes(data))

    # ── Data dir discovery ───────────────────────────────────────────

    @staticmethod
    def _find_data_dir(project_dir: str) -> Optional[str]:
        for name in ("Data", "data"):
            d = os.path.join(project_dir, name)
            if os.path.isdir(d):
                return d
        return None

    @staticmethod
    def _backup_data_dir(data_dir: str):
        backup = data_dir + "_original"
        if not os.path.isdir(backup):
            shutil.copytree(data_dir, backup)
            log.info("Backed up %s → %s", data_dir, backup)

    # ── Actor lookup helpers ─────────────────────────────────────────

    def _load_actor_names(self, data_dir: str) -> dict[int, str]:
        path = os.path.join(data_dir, "Actors.rvdata2")
        if not os.path.exists(path):
            return {}
        try:
            data = self._read_rvdata2(path)
        except Exception:
            return {}
        names = {}
        if isinstance(data, list):
            for item in data:
                if item is None:
                    continue
                aid = _attr(item, "id", 0)
                name = (_attr(item, "name") or "").strip()
                if aid and name:
                    names[aid] = name
        return names

    def _load_face_to_actor(self, data_dir: str) -> dict[tuple, int]:
        path = os.path.join(data_dir, "Actors.rvdata2")
        if not os.path.exists(path):
            return {}
        try:
            data = self._read_rvdata2(path)
        except Exception:
            return {}
        lookup = {}
        if isinstance(data, list):
            for item in data:
                if item is None:
                    continue
                aid = _attr(item, "id", 0)
                face = _attr(item, "face_name", "")
                idx = _attr(item, "face_index", 0)
                if aid and face:
                    lookup[(face, idx)] = aid
        return lookup

    # ── Database parsing ─────────────────────────────────────────────

    def _parse_database_files(self, data_dir: str) -> list:
        entries = []
        for filename, fields in DATABASE_FILES.items():
            path = os.path.join(data_dir, filename)
            if not os.path.exists(path):
                continue
            try:
                data = self._read_rvdata2(path)
            except Exception as exc:
                log.error("Failed to read %s: %s", path, exc)
                continue

            if not isinstance(data, list):
                continue

            for item in data:
                if item is None:
                    continue
                item_id = _attr(item, "id", 0)
                for fld in fields:
                    text = _str(_attr(item, fld, ""))
                    if text and self._should_extract(text):
                        entry_id = f"{filename}/{item_id}/{fld}"
                        entries.append(TranslationEntry(
                            id=entry_id,
                            file=filename,
                            field=fld,
                            original=text,
                        ))
        return entries

    def _parse_system(self, data_dir: str) -> list:
        """Parse System.rvdata2 for terms, currency, elements, etc."""
        path = os.path.join(data_dir, "System.rvdata2")
        if not os.path.exists(path):
            return []
        try:
            data = self._read_rvdata2(path)
        except Exception:
            return []

        entries = []
        filename = "System.rvdata2"

        # Game title
        title = _str(_attr(data, "game_title", ""))
        if title and self._should_extract(title):
            entries.append(TranslationEntry(
                id=f"{filename}/game_title",
                file=filename, field="game_title", original=title,
            ))

        # Currency unit
        currency = _str(_attr(data, "currency_unit", ""))
        if currency and self._should_extract(currency):
            entries.append(TranslationEntry(
                id=f"{filename}/currency_unit",
                file=filename, field="currency_unit", original=currency,
            ))

        # Elements list (index 0 is empty)
        elements = _attr(data, "elements", [])
        for i, elem in enumerate(elements):
            s = _str(elem)
            if s and self._should_extract(s):
                entries.append(TranslationEntry(
                    id=f"{filename}/elements/{i}",
                    file=filename, field="element", original=s,
                ))

        # Skill types, weapon types, armor types
        for list_key, field_name in [
            ("skill_types", "skill_type"),
            ("weapon_types", "weapon_type"),
            ("armor_types", "armor_type"),
        ]:
            items = _attr(data, list_key, [])
            for i, item in enumerate(items):
                s = _str(item)
                if s and self._should_extract(s):
                    entries.append(TranslationEntry(
                        id=f"{filename}/{list_key}/{i}",
                        file=filename, field=field_name, original=s,
                    ))

        # Terms (basic + params + etypes + commands)
        terms = _attr(data, "terms")
        if terms is not None:
            for term_key in ("basic", "params", "etypes", "commands"):
                term_list = _attr(terms, term_key, [])
                for i, term in enumerate(term_list):
                    s = _str(term)
                    if s and self._should_extract(s):
                        entries.append(TranslationEntry(
                            id=f"{filename}/terms/{term_key}/{i}",
                            file=filename, field="term", original=s,
                        ))

        # Switches and variables with JP names (rare but possible)
        for list_key in ("switches", "variables"):
            items = _attr(data, list_key, [])
            for i, item in enumerate(items):
                s = _str(item)
                if s and self._should_extract(s):
                    entries.append(TranslationEntry(
                        id=f"{filename}/{list_key}/{i}",
                        file=filename, field=list_key[:-1], original=s,
                    ))

        return entries

    # ── Event parsing (maps, common events, troops) ──────────────────

    def _parse_maps(self, data_dir: str) -> list:
        entries = []
        for fname in sorted(os.listdir(data_dir)):
            if not fname.startswith("Map") or not fname.endswith(".rvdata2"):
                continue
            if fname == "MapInfos.rvdata2":
                continue
            path = os.path.join(data_dir, fname)
            try:
                data = self._read_rvdata2(path)
            except Exception as exc:
                log.error("Failed to read %s: %s", path, exc)
                continue

            # Map display name
            display_name = _str(_attr(data, "display_name", ""))
            if display_name and self._should_extract(display_name):
                entries.append(TranslationEntry(
                    id=f"{fname}/display_name",
                    file=fname, field="display_name", original=display_name,
                ))

            events = _attr(data, "events") or {}
            for event_id, event in events.items():
                if event is None:
                    continue
                ev_name = _attr(event, "name", "")
                pages = _attr(event, "pages") or []
                for page_idx, page in enumerate(pages):
                    cmd_list = _attr(page, "list") or []
                    prefix = f"{fname}/Ev{event_id}/p{page_idx}"
                    entries.extend(self._parse_event_commands(
                        cmd_list, fname, prefix))
        return entries

    def _parse_common_events(self, data_dir: str) -> list:
        path = os.path.join(data_dir, "CommonEvents.rvdata2")
        if not os.path.exists(path):
            return []
        try:
            data = self._read_rvdata2(path)
        except Exception:
            return []
        if not isinstance(data, list):
            return []

        entries = []
        for item in data:
            if item is None:
                continue
            ce_id = _attr(item, "id", 0)
            cmd_list = _attr(item, "list") or []
            prefix = f"CommonEvents.rvdata2/CE{ce_id}"
            entries.extend(self._parse_event_commands(
                cmd_list, "CommonEvents.rvdata2", prefix))
        return entries

    def _parse_troops(self, data_dir: str) -> list:
        path = os.path.join(data_dir, "Troops.rvdata2")
        if not os.path.exists(path):
            return []
        try:
            data = self._read_rvdata2(path)
        except Exception:
            return []
        if not isinstance(data, list):
            return []

        entries = []
        for item in data:
            if item is None:
                continue
            troop_id = _attr(item, "id", 0)
            pages = _attr(item, "pages") or []
            for page_idx, page in enumerate(pages):
                cmd_list = _attr(page, "list") or []
                prefix = f"Troops.rvdata2/T{troop_id}/p{page_idx}"
                entries.extend(self._parse_event_commands(
                    cmd_list, "Troops.rvdata2", prefix))
        return entries

    def _parse_event_commands(self, cmd_list: list, filename: str,
                              prefix: str) -> list:
        """Parse a list of RPG::EventCommand objects into TranslationEntry.

        Groups consecutive 401/405 commands into single dialogue blocks,
        exactly like the MV/MZ parser.
        """
        entries = []
        recent_context: deque = deque(maxlen=self.context_size)
        i = 0
        dialogue_index = 0

        while i < len(cmd_list):
            cmd = cmd_list[i]
            code = _attr(cmd, "code", 0)
            params = _attr(cmd, "parameters") or []

            # ── 101 + 401 block: Show Text ─────────────────────────
            if code == CODE_SHOW_TEXT_HEADER:
                speaker = ""
                has_face = False
                # VX Ace 101: [faceName, faceIndex, background, position]
                if len(params) >= 2:
                    face_name = _str(params[0])
                    face_index = params[1] if isinstance(params[1], int) else 0
                    if face_name:
                        has_face = True
                        # Try to resolve face to actor name
                        actor_id = self._face_to_actor.get(
                            (face_name, face_index))
                        if actor_id and actor_id in self._actor_names:
                            speaker = self._actor_names[actor_id]

                # Collect consecutive 401 lines
                lines = []
                i += 1
                while i < len(cmd_list):
                    nc = cmd_list[i]
                    if _attr(nc, "code", 0) != CODE_SHOW_TEXT:
                        break
                    np = _attr(nc, "parameters") or []
                    if np:
                        s = _str(np[0])
                        if s:
                            lines.append(s)
                    i += 1

                if not lines:
                    continue

                full_text = "\n".join(lines)

                # Check for \N<name> namebox prefix
                namebox = ""
                m = _NAMEBOX_RE.match(full_text)
                if m:
                    namebox = m.group(0)
                    inner = m.group(1)
                    # Resolve \n[N] actor references
                    actor_m = _ACTOR_CODE_RE.match(inner)
                    if actor_m:
                        aid = int(actor_m.group(1))
                        if aid in self._actor_names:
                            speaker = self._actor_names[aid]
                    else:
                        speaker = inner
                    full_text = full_text[m.end():].lstrip("\n")

                # Always increment dialogue_index for ID stability on export
                entry_id = f"{prefix}/{dialogue_index}"
                dialogue_index += 1

                if not self._should_extract(full_text):
                    continue

                # Build context
                context_parts = []
                if speaker:
                    context_parts.append(f"[Speaker: {speaker}]")
                for ctx in recent_context:
                    context_parts.append(ctx)
                context = "\n".join(context_parts)

                entries.append(TranslationEntry(
                    id=entry_id,
                    file=filename,
                    field="dialogue",
                    original=full_text,
                    context=context,
                    namebox=namebox,
                    has_face=has_face,
                ))

                # Update context window
                label = f"[{speaker}]: " if speaker else ""
                recent_context.append(f"{label}{full_text[:80]}")
                continue

            # ── 105 + 405 block: Scroll Text ───────────────────────
            if code == CODE_SCROLL_TEXT_HEADER:
                lines = []
                i += 1
                while i < len(cmd_list):
                    nc = cmd_list[i]
                    if _attr(nc, "code", 0) != CODE_SCROLL_TEXT:
                        break
                    np = _attr(nc, "parameters") or []
                    if np:
                        s = _str(np[0])
                        if s:
                            lines.append(s)
                    i += 1

                if not lines:
                    continue
                full_text = "\n".join(lines)
                entry_id = f"{prefix}/{dialogue_index}"
                dialogue_index += 1
                if not self._should_extract(full_text):
                    continue

                entries.append(TranslationEntry(
                    id=entry_id,
                    file=filename,
                    field="dialogue",
                    original=full_text,
                ))
                continue

            # ── 102: Show Choices ──────────────────────────────────
            if code == CODE_SHOW_CHOICES and params:
                choices = params[0] if isinstance(params[0], list) else []
                for ci, choice in enumerate(choices):
                    choice = _str(choice)
                    if choice and self._should_extract(choice):
                        entry_id = f"{prefix}/c{dialogue_index}_{ci}"
                        entries.append(TranslationEntry(
                            id=entry_id,
                            file=filename,
                            field="choice",
                            original=choice,
                        ))
                dialogue_index += 1  # Always increment for ID stability
                i += 1
                continue

            # ── 320/324/325: Change Actor Name/Nickname/Profile ───
            if code in (CODE_CHANGE_NAME, CODE_CHANGE_NICKNAME,
                        CODE_CHANGE_PROFILE):
                entry_id = f"{prefix}/{dialogue_index}"
                dialogue_index += 1  # Always increment
                if len(params) >= 2:
                    text = _str(params[1])
                    if text and self._should_extract(text):
                        field_map = {
                            CODE_CHANGE_NAME: "actor_name",
                            CODE_CHANGE_NICKNAME: "actor_nickname",
                            CODE_CHANGE_PROFILE: "actor_profile",
                        }
                        entries.append(TranslationEntry(
                            id=entry_id,
                            file=filename,
                            field=field_map[code],
                            original=text,
                        ))
                i += 1
                continue

            i += 1

        return entries

    # ── Apply translations back to Ruby data ─────────────────────────

    def _apply_translations(self, data, filename: str, entries: list):
        """Apply translation entries back into a loaded Ruby object tree."""
        # Build lookup: entry_id → translation
        trans_map = {e.id: e for e in entries
                     if e.translation and e.status in ("translated", "reviewed")}
        if not trans_map:
            return

        if filename.startswith("Map") and filename != "MapInfos.rvdata2":
            self._apply_map(data, filename, trans_map)
        elif filename == "CommonEvents.rvdata2":
            self._apply_common_events(data, trans_map)
        elif filename == "Troops.rvdata2":
            self._apply_troops(data, trans_map)
        elif filename == "System.rvdata2":
            self._apply_system(data, filename, trans_map)
        elif filename in DATABASE_FILES:
            self._apply_database(data, filename, trans_map)

    def _apply_database(self, data, filename: str, trans_map: dict):
        if not isinstance(data, list):
            return
        fields = DATABASE_FILES.get(filename, [])
        for item in data:
            if item is None:
                continue
            item_id = _attr(item, "id", 0)
            for fld in fields:
                eid = f"{filename}/{item_id}/{fld}"
                entry = trans_map.get(eid)
                if entry:
                    _set_attr(item, fld, entry.translation)

    def _apply_system(self, data, filename: str, trans_map: dict):
        # Game title
        e = trans_map.get(f"{filename}/game_title")
        if e:
            _set_attr(data, "game_title", e.translation)

        # Currency
        e = trans_map.get(f"{filename}/currency_unit")
        if e:
            _set_attr(data, "currency_unit", e.translation)

        # Elements
        elements = _attr(data, "elements", [])
        for i in range(len(elements)):
            e = trans_map.get(f"{filename}/elements/{i}")
            if e:
                elements[i] = e.translation

        # Type lists
        for list_key in ("skill_types", "weapon_types", "armor_types"):
            items = _attr(data, list_key, [])
            for i in range(len(items)):
                e = trans_map.get(f"{filename}/{list_key}/{i}")
                if e:
                    items[i] = e.translation

        # Terms
        terms = _attr(data, "terms")
        if terms is not None:
            for term_key in ("basic", "params", "etypes", "commands"):
                term_list = _attr(terms, term_key, [])
                for i in range(len(term_list)):
                    e = trans_map.get(f"{filename}/terms/{term_key}/{i}")
                    if e:
                        term_list[i] = e.translation

        # Switches/variables
        for list_key in ("switches", "variables"):
            items = _attr(data, list_key, [])
            for i in range(len(items)):
                e = trans_map.get(f"{filename}/{list_key}/{i}")
                if e:
                    items[i] = e.translation

    def _apply_map(self, data, filename: str, trans_map: dict):
        # Display name
        e = trans_map.get(f"{filename}/display_name")
        if e:
            _set_attr(data, "display_name", e.translation)

        events = _attr(data, "events") or {}
        for event_id, event in events.items():
            if event is None:
                continue
            pages = _attr(event, "pages") or []
            for page_idx, page in enumerate(pages):
                cmd_list = _attr(page, "list") or []
                prefix = f"{filename}/Ev{event_id}/p{page_idx}"
                self._apply_event_commands(cmd_list, prefix, trans_map)

    def _apply_common_events(self, data, trans_map: dict):
        if not isinstance(data, list):
            return
        for item in data:
            if item is None:
                continue
            ce_id = _attr(item, "id", 0)
            cmd_list = _attr(item, "list") or []
            prefix = f"CommonEvents.rvdata2/CE{ce_id}"
            self._apply_event_commands(cmd_list, prefix, trans_map)

    def _apply_troops(self, data, trans_map: dict):
        if not isinstance(data, list):
            return
        for item in data:
            if item is None:
                continue
            troop_id = _attr(item, "id", 0)
            pages = _attr(item, "pages") or []
            for page_idx, page in enumerate(pages):
                cmd_list = _attr(page, "list") or []
                prefix = f"Troops.rvdata2/T{troop_id}/p{page_idx}"
                self._apply_event_commands(cmd_list, prefix, trans_map)

    def _apply_event_commands(self, cmd_list: list, prefix: str,
                              trans_map: dict):
        """Write translations back into event command parameters.

        Mirrors the parsing logic: groups 401/405 blocks, splits translations
        back into individual commands.
        """
        i = 0
        dialogue_index = 0

        while i < len(cmd_list):
            cmd = cmd_list[i]
            code = _attr(cmd, "code", 0)
            params = _attr(cmd, "parameters") or []

            if code == CODE_SHOW_TEXT_HEADER:
                # Collect 401 block
                start_401 = i + 1
                j = i + 1
                while j < len(cmd_list):
                    if _attr(cmd_list[j], "code", 0) != CODE_SHOW_TEXT:
                        break
                    j += 1

                orig_count = j - start_401
                if orig_count == 0:
                    i = j
                    continue

                entry_id = f"{prefix}/{dialogue_index}"
                entry = trans_map.get(entry_id)
                dialogue_index += 1

                if entry and entry.translation:
                    # Prepend namebox back if it was stripped
                    translated = entry.translation
                    if entry.namebox:
                        translated = entry.namebox + "\n" + translated

                    # Split translation into same number of 401 lines
                    trans_lines = translated.split("\n")

                    # Pad or trim to match original line count
                    while len(trans_lines) < orig_count:
                        trans_lines.append("")
                    if len(trans_lines) > orig_count:
                        last = orig_count - 1
                        trans_lines[last] = " ".join(
                            trans_lines[last:])
                        trans_lines = trans_lines[:orig_count]

                    # Write back
                    for idx, line_cmd in enumerate(
                            cmd_list[start_401:start_401 + orig_count]):
                        lp = _attr(line_cmd, "parameters") or []
                        if lp:
                            lp[0] = trans_lines[idx]

                i = j
                continue

            if code == CODE_SCROLL_TEXT_HEADER:
                start_405 = i + 1
                j = i + 1
                while j < len(cmd_list):
                    if _attr(cmd_list[j], "code", 0) != CODE_SCROLL_TEXT:
                        break
                    j += 1

                orig_count = j - start_405
                if orig_count == 0:
                    i = j
                    continue

                entry_id = f"{prefix}/{dialogue_index}"
                entry = trans_map.get(entry_id)
                dialogue_index += 1

                if entry and entry.translation:
                    trans_lines = entry.translation.split("\n")
                    while len(trans_lines) < orig_count:
                        trans_lines.append("")
                    if len(trans_lines) > orig_count:
                        last = orig_count - 1
                        trans_lines[last] = " ".join(trans_lines[last:])
                        trans_lines = trans_lines[:orig_count]
                    for idx, line_cmd in enumerate(
                            cmd_list[start_405:start_405 + orig_count]):
                        lp = _attr(line_cmd, "parameters") or []
                        if lp:
                            lp[0] = trans_lines[idx]

                i = j
                continue

            if code == CODE_SHOW_CHOICES and params:
                choices = params[0] if isinstance(params[0], list) else []
                for ci in range(len(choices)):
                    eid = f"{prefix}/c{dialogue_index}_{ci}"
                    entry = trans_map.get(eid)
                    if entry and entry.translation:
                        choices[ci] = entry.translation
                dialogue_index += 1
                i += 1
                continue

            if code in (CODE_CHANGE_NAME, CODE_CHANGE_NICKNAME,
                        CODE_CHANGE_PROFILE):
                entry_id = f"{prefix}/{dialogue_index}"
                entry = trans_map.get(entry_id)
                if entry and entry.translation and len(params) >= 2:
                    params[1] = entry.translation
                dialogue_index += 1
                i += 1
                continue

            i += 1
