"""RPG Maker MV/MZ JSON parser and writer.

Handles extraction of translatable strings from RPG Maker MV/MZ data files
and writing translations back into the original JSON structure.
"""

import json
import os
import re
import shutil
from collections import deque
from typing import Optional

from .project_model import TranslationEntry

# RPG Maker event command codes that contain translatable text
CODE_SHOW_TEXT_HEADER = 101   # Show Text setup (face, position) — not translatable itself
CODE_SHOW_TEXT = 401          # Show Text continuation — parameters[0] is text
CODE_SHOW_CHOICES = 102       # Show Choices — parameters[0] is list of strings
CODE_SCROLL_TEXT_HEADER = 105 # Scroll Text setup — not translatable
CODE_SCROLL_TEXT = 405        # Scroll Text line — parameters[0] is text

# Database files and their translatable fields
DATABASE_FILES = {
    "Actors.json":   ["name", "nickname", "profile", "note"],
    "Classes.json":  ["name", "note"],
    "Items.json":    ["name", "description", "note"],
    "Weapons.json":  ["name", "description", "note"],
    "Armors.json":   ["name", "description", "note"],
    "Skills.json":   ["name", "description", "message1", "message2", "note"],
    "States.json":   ["name", "message1", "message2", "message3", "message4", "note"],
    "Enemies.json":  ["name", "note"],
}

# Regex to detect Japanese characters (Hiragana, Katakana, CJK)
JP_REGEX = re.compile(r'[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\uFF00-\uFFEF]')


def _has_japanese(text: str) -> bool:
    """Check if text contains any Japanese characters."""
    return bool(JP_REGEX.search(text))


# Gender detection keywords
_FEMALE_HINTS = re.compile(
    r'女|姫|嬢|娘|母|姉|妹|妻|彼女|お姉|おかあ|少女|王女|巫女|メイド|'
    r'actress|female|girl|woman|princess|queen|lady|witch|priestess|maid',
    re.IGNORECASE
)
_MALE_HINTS = re.compile(
    r'男|王子|父|兄|弟|夫|彼|息子|少年|勇者|騎士|おとうさん|'
    r'actor|male|boy|man|prince|king|knight|hero|lord',
    re.IGNORECASE
)


def _detect_gender(profile: str, note: str, face_name: str,
                   battler_name: str, nickname: str) -> str:
    """Try to detect gender from actor metadata. Returns 'male', 'female', or ''."""
    all_text = f"{profile} {note} {nickname} {face_name} {battler_name}"

    female_score = len(_FEMALE_HINTS.findall(all_text))
    male_score = len(_MALE_HINTS.findall(all_text))

    if female_score > male_score:
        return "female"
    if male_score > female_score:
        return "male"
    return ""


def _is_translatable(text: str) -> bool:
    """Check if a string is worth translating."""
    if not text or not text.strip():
        return False
    # Must contain at least some Japanese
    return _has_japanese(text)


class RPGMakerMVParser:
    """Parser for RPG Maker MV/MZ JSON data files."""

    def __init__(self):
        self.context_size = 3  # Number of recent dialogue entries for LLM context

    def load_project(self, project_dir: str) -> list:
        """Load all translatable entries from an RPG Maker MV/MZ project.

        Args:
            project_dir: Path to the game folder (parent of 'data/' or 'www/data/').

        Returns:
            List of TranslationEntry objects.
        """
        data_dir = self._find_data_dir(project_dir)
        if not data_dir:
            raise FileNotFoundError(
                f"No 'data' folder found in {project_dir}. "
                "Please select an RPG Maker MV/MZ project folder."
            )

        entries = []
        entries.extend(self._parse_database_files(data_dir))
        entries.extend(self._parse_system(data_dir))
        entries.extend(self._parse_common_events(data_dir))
        entries.extend(self._parse_maps(data_dir))
        return entries

    def get_game_title(self, project_dir: str) -> str:
        """Read the raw game title from System.json (regardless of language)."""
        data_dir = self._find_data_dir(project_dir)
        if not data_dir:
            return ""
        filepath = os.path.join(data_dir, "System.json")
        if not os.path.exists(filepath):
            return ""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("gameTitle", "")
        except (json.JSONDecodeError, OSError):
            return ""

    def load_actors_raw(self, project_dir: str) -> list:
        """Load raw actor data for the gender assignment dialog.

        Returns:
            List of dicts with: id, name, nickname, profile, auto_gender
        """
        data_dir = self._find_data_dir(project_dir)
        if not data_dir:
            return []

        filepath = os.path.join(data_dir, "Actors.json")
        if not os.path.exists(filepath):
            return []

        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, list):
            return []

        actors = []
        for item in data:
            if not item or not isinstance(item, dict):
                continue
            name = item.get("name", "").strip()
            if not name:
                continue

            profile = item.get("profile", "").strip()
            nickname = item.get("nickname", "").strip()
            face_name = item.get("faceName", "").strip()
            battler_name = item.get("battlerName", "").strip()
            note = item.get("note", "").strip()

            auto_gender = _detect_gender(profile, note, face_name, battler_name, nickname)

            actors.append({
                "id": item.get("id", 0),
                "name": name,
                "nickname": nickname,
                "profile": profile,
                "auto_gender": auto_gender or "unknown",
            })
        return actors

    def build_actor_context(self, actors: list, genders: dict) -> str:
        """Build a character reference string with confirmed genders.

        Args:
            actors: Raw actor list from load_actors_raw().
            genders: Dict of {actor_id: "male"/"female"} from user.

        Returns:
            Formatted string for LLM context.
        """
        lines = []
        for actor in actors:
            actor_id = actor["id"]
            name = actor["name"]
            nickname = actor.get("nickname", "")
            profile = actor.get("profile", "")

            gender = genders.get(actor_id, actor.get("auto_gender", ""))
            gender_label = ""
            if gender == "female":
                gender_label = "[female - use she/her]"
            elif gender == "male":
                gender_label = "[male - use he/him]"

            parts = [f"Actor {actor_id}: {name}"]
            if gender_label:
                parts.append(gender_label)
            if nickname:
                parts.append(f"aka \"{nickname}\"")
            if profile:
                parts.append(f"- {profile}")
            lines.append(" ".join(parts))

        if not lines:
            return ""
        return ("Characters in this game (ALWAYS use the listed pronouns):\n"
                + "\n".join(lines))

    def save_project(self, project_dir: str, entries: list):
        """Write translated entries back into the original JSON files.

        A backup of the original data/ folder is created as data_original/
        on the first export so the user can revert or retranslate later.

        Args:
            project_dir: Path to the game folder.
            entries: List of TranslationEntry with translations filled in.
        """
        data_dir = self._find_data_dir(project_dir)
        if not data_dir:
            return

        # Back up originals on first export
        self._backup_data_dir(data_dir)

        # Group entries by file
        by_file = {}
        for e in entries:
            if e.translation and e.status in ("translated", "reviewed"):
                by_file.setdefault(e.file, []).append(e)

        for filename, file_entries in by_file.items():
            filepath = os.path.join(data_dir, filename)
            if not os.path.exists(filepath):
                continue

            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            for entry in file_entries:
                self._apply_translation(data, entry)

            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _backup_data_dir(data_dir: str):
        """Copy the data/ folder to data_original/ if no backup exists yet."""
        backup_dir = data_dir + "_original"
        if os.path.isdir(backup_dir):
            return  # Already backed up
        shutil.copytree(data_dir, backup_dir)

    # ── Private: find data dir ─────────────────────────────────────────

    def _find_data_dir(self, project_dir: str) -> Optional[str]:
        """Locate the data/ directory inside the project."""
        candidates = [
            os.path.join(project_dir, "data"),
            os.path.join(project_dir, "Data"),
            os.path.join(project_dir, "www", "data"),
            os.path.join(project_dir, "www", "Data"),
        ]
        for c in candidates:
            if os.path.isdir(c):
                return c
        return None

    # ── Private: database files ────────────────────────────────────────

    def _parse_database_files(self, data_dir: str) -> list:
        """Parse standard database JSON files (Actors, Items, etc.)."""
        entries = []
        for filename, fields in DATABASE_FILES.items():
            filepath = os.path.join(data_dir, filename)
            if not os.path.exists(filepath):
                continue

            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            if not isinstance(data, list):
                continue

            for item in data:
                if not item or not isinstance(item, dict):
                    continue
                item_id = item.get("id", 0)
                for fld in fields:
                    text = item.get(fld, "")
                    if isinstance(text, str) and _is_translatable(text):
                        entry_id = f"{filename}/{item_id}/{fld}"
                        entries.append(TranslationEntry(
                            id=entry_id,
                            file=filename,
                            field=fld,
                            original=text,
                        ))
        return entries

    # ── Private: System.json ───────────────────────────────────────────

    def _parse_system(self, data_dir: str) -> list:
        """Parse System.json for game title and terms."""
        entries = []
        filepath = os.path.join(data_dir, "System.json")
        if not os.path.exists(filepath):
            return entries

        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Game title
        title = data.get("gameTitle", "")
        if _is_translatable(title):
            entries.append(TranslationEntry(
                id="System.json/gameTitle",
                file="System.json",
                field="gameTitle",
                original=title,
            ))

        # Terms — messages array
        terms = data.get("terms", {})
        messages = terms.get("messages", [])
        if isinstance(messages, list):
            for i, msg in enumerate(messages):
                if isinstance(msg, str) and _is_translatable(msg):
                    entries.append(TranslationEntry(
                        id=f"System.json/terms/messages/{i}",
                        file="System.json",
                        field=f"terms.messages[{i}]",
                        original=msg,
                    ))

        # Terms — commands array
        commands = terms.get("commands", [])
        if isinstance(commands, list):
            for i, cmd in enumerate(commands):
                if isinstance(cmd, str) and _is_translatable(cmd):
                    entries.append(TranslationEntry(
                        id=f"System.json/terms/commands/{i}",
                        file="System.json",
                        field=f"terms.commands[{i}]",
                        original=cmd,
                    ))

        return entries

    # ── Private: CommonEvents.json ─────────────────────────────────────

    def _parse_common_events(self, data_dir: str) -> list:
        """Parse CommonEvents.json for event dialogue."""
        entries = []
        filepath = os.path.join(data_dir, "CommonEvents.json")
        if not os.path.exists(filepath):
            return entries

        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, list):
            return entries

        for event in data:
            if not event or not isinstance(event, dict):
                continue
            event_id = event.get("id", 0)
            event_name = event.get("name", "")
            cmd_list = event.get("list", [])
            entries.extend(self._extract_event_commands(
                cmd_list, "CommonEvents.json", f"CE{event_id}({event_name})"
            ))

        return entries

    # ── Private: Map files ─────────────────────────────────────────────

    def _parse_maps(self, data_dir: str) -> list:
        """Parse Map###.json files for event dialogue."""
        entries = []
        for filename in sorted(os.listdir(data_dir)):
            if not re.match(r'^Map\d+\.json$', filename, re.IGNORECASE):
                continue

            filepath = os.path.join(data_dir, filename)
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Map display name
            display_name = data.get("displayName", "")
            if _is_translatable(display_name):
                entries.append(TranslationEntry(
                    id=f"{filename}/displayName",
                    file=filename,
                    field="displayName",
                    original=display_name,
                ))

            # Events
            events = data.get("events", [])
            if not isinstance(events, list):
                continue

            for event in events:
                if not event or not isinstance(event, dict):
                    continue
                event_id = event.get("id", 0)
                event_name = event.get("name", "")
                pages = event.get("pages", [])

                for page_idx, page in enumerate(pages):
                    if not page or not isinstance(page, dict):
                        continue
                    cmd_list = page.get("list", [])
                    prefix = f"Ev{event_id}({event_name})/p{page_idx}"
                    entries.extend(self._extract_event_commands(
                        cmd_list, filename, prefix
                    ))

        return entries

    # ── Private: event command extraction ──────────────────────────────

    def _extract_event_commands(self, cmd_list: list, filename: str, prefix: str) -> list:
        """Extract translatable text from a list of event commands.

        Groups consecutive 401 (Show Text) commands into single dialogue blocks.
        Reads 101 (Show Text Header) to identify the speaker for each block.
        """
        entries = []
        recent_ctx = deque(maxlen=self.context_size)  # O(1) sliding window for context
        i = 0
        dialog_counter = 0
        current_speaker = ""  # Track who is speaking

        while i < len(cmd_list):
            cmd = cmd_list[i]
            if not isinstance(cmd, dict):
                i += 1
                continue

            code = cmd.get("code", 0)
            params = cmd.get("parameters", [])

            # Show Text Header (101): captures speaker info
            # MV: parameters = [faceName, faceIndex, background, positionType]
            # MZ: parameters = [faceName, faceIndex, background, positionType, speakerName]
            if code == CODE_SHOW_TEXT_HEADER:
                face_name = params[0] if len(params) > 0 else ""
                speaker_name = params[4] if len(params) > 4 else ""
                current_speaker = speaker_name if speaker_name else face_name
                i += 1
                continue

            # Show Text: group consecutive 401 lines into one block
            if code == CODE_SHOW_TEXT:
                lines = []
                while i < len(cmd_list):
                    c = cmd_list[i]
                    if isinstance(c, dict) and c.get("code") == CODE_SHOW_TEXT:
                        text = c.get("parameters", [""])[0] if c.get("parameters") else ""
                        lines.append(str(text))
                        i += 1
                    else:
                        break
                full_text = "\n".join(lines)
                if _is_translatable(full_text):
                    dialog_counter += 1
                    ctx_parts = []
                    if current_speaker:
                        ctx_parts.append(f"[Speaker: {current_speaker}]")
                    if recent_ctx:
                        ctx_parts.append("\n---\n".join(recent_ctx))
                    ctx = "\n".join(ctx_parts)

                    entries.append(TranslationEntry(
                        id=f"{filename}/{prefix}/dialog_{dialog_counter}",
                        file=filename,
                        field="dialog",
                        original=full_text,
                        context=ctx,
                    ))
                    recent_ctx.append(full_text)
                continue

            # Show Choices
            if code == CODE_SHOW_CHOICES and params:
                choices = params[0] if isinstance(params[0], list) else []
                ctx = "\n---\n".join(recent_ctx) if recent_ctx else ""
                for ci, choice in enumerate(choices):
                    if isinstance(choice, str) and _is_translatable(choice):
                        dialog_counter += 1
                        entries.append(TranslationEntry(
                            id=f"{filename}/{prefix}/choice_{dialog_counter}_{ci}",
                            file=filename,
                            field="choice",
                            original=choice,
                            context=ctx,
                        ))
                        recent_ctx.append(choice)

            # Scrolling Text
            if code == CODE_SCROLL_TEXT:
                lines = []
                while i < len(cmd_list):
                    c = cmd_list[i]
                    if isinstance(c, dict) and c.get("code") == CODE_SCROLL_TEXT:
                        text = c.get("parameters", [""])[0] if c.get("parameters") else ""
                        lines.append(str(text))
                        i += 1
                    else:
                        break
                full_text = "\n".join(lines)
                if _is_translatable(full_text):
                    dialog_counter += 1
                    ctx = "\n---\n".join(recent_ctx) if recent_ctx else ""
                    entries.append(TranslationEntry(
                        id=f"{filename}/{prefix}/scroll_{dialog_counter}",
                        file=filename,
                        field="scroll_text",
                        original=full_text,
                        context=ctx,
                    ))
                    recent_ctx.append(full_text)
                continue

            i += 1

        return entries

    # ── Private: apply translation back to JSON ────────────────────────

    def _apply_translation(self, data, entry: TranslationEntry):
        """Apply a single translation back into the loaded JSON data."""
        parts = entry.id.split("/")
        filename = parts[0]

        # Database entries: "Actors.json/1/name"
        if filename in DATABASE_FILES and len(parts) >= 3:
            item_id = int(parts[1])
            field_name = parts[2]
            if isinstance(data, list):
                for item in data:
                    if item and isinstance(item, dict) and item.get("id") == item_id:
                        if field_name in item:
                            item[field_name] = entry.translation
                        break

        # System.json entries
        elif filename == "System.json":
            if "gameTitle" in entry.id and not entry.id.endswith("terms"):
                data["gameTitle"] = entry.translation
            elif "terms/messages/" in entry.id:
                idx = int(parts[-1])
                terms = data.get("terms", {})
                messages = terms.get("messages", [])
                if 0 <= idx < len(messages):
                    messages[idx] = entry.translation
            elif "terms/commands/" in entry.id:
                idx = int(parts[-1])
                terms = data.get("terms", {})
                commands = terms.get("commands", [])
                if 0 <= idx < len(commands):
                    commands[idx] = entry.translation

        # Map displayName
        elif "displayName" in entry.id and entry.field == "displayName":
            data["displayName"] = entry.translation

        # Event dialogue — need to find and replace in command lists
        elif entry.field in ("dialog", "scroll_text", "choice"):
            self._apply_event_translation(data, entry)

    def _apply_event_translation(self, data, entry: TranslationEntry):
        """Apply event dialogue/choice translation back into map or common event data."""
        original_lines = entry.original.split("\n")
        translation_lines = entry.translation.split("\n")

        # Pad or trim translation to match original line count for dialogue
        if entry.field == "dialog":
            while len(translation_lines) < len(original_lines):
                translation_lines.append("")
            translation_lines = translation_lines[:len(original_lines)]

        if entry.field == "choice":
            # Choices: find in event commands with code 102
            self._replace_in_commands(data, CODE_SHOW_CHOICES, entry.original, entry.translation, is_choice=True)
        elif entry.field == "dialog":
            self._replace_dialog_block(data, original_lines, translation_lines)
        elif entry.field == "scroll_text":
            self._replace_dialog_block(data, original_lines, translation_lines, code=CODE_SCROLL_TEXT)

    def _replace_dialog_block(self, data, original_lines: list, translation_lines: list, code: int = CODE_SHOW_TEXT):
        """Find and replace a consecutive block of 401/405 commands."""
        def process_commands(cmd_list):
            i = 0
            while i < len(cmd_list):
                cmd = cmd_list[i]
                if not isinstance(cmd, dict) or cmd.get("code") != code:
                    i += 1
                    continue

                # Check if this block matches
                match = True
                for j, orig_line in enumerate(original_lines):
                    idx = i + j
                    if idx >= len(cmd_list):
                        match = False
                        break
                    c = cmd_list[idx]
                    if not isinstance(c, dict) or c.get("code") != code:
                        match = False
                        break
                    c_text = c.get("parameters", [""])[0] if c.get("parameters") else ""
                    if str(c_text) != orig_line:
                        match = False
                        break

                if match and len(original_lines) > 0:
                    for j, tl in enumerate(translation_lines):
                        cmd_list[i + j]["parameters"][0] = tl
                    return True
                i += 1
            return False

        # Search in events (map data) or common events
        if isinstance(data, dict):
            # Map data
            for event in (data.get("events") or []):
                if not event or not isinstance(event, dict):
                    continue
                for page in (event.get("pages") or []):
                    if page and isinstance(page, dict):
                        if process_commands(page.get("list", [])):
                            return
            # Common event data
            if "list" in data:
                process_commands(data.get("list", []))
        elif isinstance(data, list):
            # CommonEvents.json is a list
            for event in data:
                if event and isinstance(event, dict):
                    if process_commands(event.get("list", [])):
                        return

    def _replace_in_commands(self, data, code: int, original: str, translation: str, is_choice: bool = False):
        """Replace a specific command parameter in event command lists."""
        def process_commands(cmd_list):
            for cmd in cmd_list:
                if not isinstance(cmd, dict) or cmd.get("code") != code:
                    continue
                params = cmd.get("parameters", [])
                if is_choice and params and isinstance(params[0], list):
                    try:
                        idx = params[0].index(original)
                        params[0][idx] = translation
                        return True
                    except ValueError:
                        pass
            return False

        if isinstance(data, dict):
            for event in (data.get("events") or []):
                if not event or not isinstance(event, dict):
                    continue
                for page in (event.get("pages") or []):
                    if page and isinstance(page, dict):
                        if process_commands(page.get("list", [])):
                            return
        elif isinstance(data, list):
            for event in data:
                if event and isinstance(event, dict):
                    if process_commands(event.get("list", [])):
                        return
