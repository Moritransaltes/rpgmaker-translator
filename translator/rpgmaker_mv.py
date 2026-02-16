"""RPG Maker MV/MZ JSON parser and writer.

Handles extraction of translatable strings from RPG Maker MV/MZ data files
and writing translations back into the original JSON structure.
"""

import json
import logging
import os
import re
import shutil
import zipfile
from collections import deque
from difflib import SequenceMatcher
from typing import Optional

log = logging.getLogger(__name__)

from .project_model import TranslationEntry

# RPG Maker event command codes that contain translatable text
CODE_SHOW_TEXT_HEADER = 101   # Show Text setup (face, position) — not translatable itself
CODE_SHOW_TEXT = 401          # Show Text continuation — parameters[0] is text
CODE_SHOW_CHOICES = 102       # Show Choices — parameters[0] is list of strings
CODE_SCROLL_TEXT_HEADER = 105 # Scroll Text setup — not translatable
CODE_SCROLL_TEXT = 405        # Scroll Text line — parameters[0] is text
CODE_CHANGE_NAME = 320        # Change Actor Name — params[0]=actorId, params[1]=name
CODE_CHANGE_NICKNAME = 324    # Change Actor Nickname — params[0]=actorId, params[1]=nickname
CODE_CHANGE_PROFILE = 325     # Change Actor Profile — params[0]=actorId, params[1]=profile
CODE_PLUGIN_COMMAND_MV = 356  # Plugin Command (MV) — params[0]=command string
CODE_PLUGIN_COMMAND_MZ = 357  # Plugin Command (MZ) — params vary by plugin
CODE_CONTROL_VARIABLES = 122  # Control Variables — params[3]=operand type, params[4]=expression
CODE_SCRIPT = 355             # Script (first line) — params[0]=JS code
CODE_SCRIPT_CONT = 655        # Script (continuation) — params[0]=JS code

# Database files and their translatable fields
DATABASE_FILES = {
    "Actors.json":   ["name", "nickname", "profile"],
    "Classes.json":  ["name"],
    "Items.json":    ["name", "description"],
    "Weapons.json":  ["name", "description"],
    "Armors.json":   ["name", "description"],
    "Skills.json":   ["name", "description", "message1", "message2"],
    "States.json":   ["name", "message1", "message2", "message3", "message4"],
    "Enemies.json":  ["name"],
    "Troops.json":   ["name"],
}

# Regex to detect Japanese characters (Hiragana, Katakana, CJK)
JP_REGEX = re.compile(r'[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\uFF00-\uFFEF]')

# Namebox: \N<name> prefix used by Lunatlazur_ActorNameWindow and similar plugins.
# Matches \N<...> or \n<...> at start of text (case-insensitive N).
_NAMEBOX_RE = re.compile(r'\\[Nn]<([^>]+)>')
# Actor code inside namebox: \n[1], \N[2], etc.
_ACTOR_CODE_RE = re.compile(r'\\[Nn]\[(\d+)\]')


def _has_japanese(text: str) -> bool:
    """Check if text contains any Japanese characters."""
    return bool(JP_REGEX.search(text))


# ── Plugin command whitelists (based on DazedMTL's proven approach) ──
# Only these known plugins/commands have display text safe to translate.
# Everything else is internal identifiers that break games if translated.

# MZ plugin commands (code 357): plugin name → list of safe param keys.
# params[0]=pluginName, params[1]=commandName, params[3]=JSON args dict.
_MZ_PLUGIN_COMMAND_WHITELIST: dict[str, list[str]] = {
    "LL_InfoPopupWIndow": ["messageText"],
    "QuestSystem": ["DetailNote"],
    "BalloonInBattle": ["text"],
    "MNKR_CommonPopupCoreMZ": ["text"],
    "DestinationWindow": ["destination"],
    "_TMLogWindowMZ": ["text"],
    "TorigoyaMZ_NotifyMessage": ["message"],
    "SoR_GabWindow": ["arg1"],
    "DarkPlasma_CharacterText": ["text"],
    "DTextPicture": ["text"],
    "TextPicture": ["text"],
    "LogWindow": ["text"],
    "BattleLogOutput": ["message"],
    "TorigoyaMZ_NotifyMessage_CommandMessage": ["message"],
    "NUUN_SaveScreen": ["AnyName"],
    "build/ARPG_Core": ["Text", "SkillByName"],
}

# MV plugin commands (code 356): (prefix, compiled_regex) tuples.
# prefix is checked via startswith() for fast filtering.
# regex capture group 1 = the translatable text portion.
_MV_PLUGIN_COMMAND_WHITELIST: list[tuple[str, re.Pattern]] = [
    ("D_TEXT",
     re.compile(r"D_TEXT\s+([^\s]+)\s?\d*")),
    ("Tachie showName",
     re.compile(r"Tachie showName (.+)")),
    ("ShowInfo",
     re.compile(r"ShowInfo\s(.*)")),
    ("PushGab",
     re.compile(r"PushGab\s(.*)")),
    ("addLog",
     re.compile(r"addLog\s(.*)")),
    ("DW_",
     re.compile(r"DW_.*\s\d+\s(.+)")),
    ("CommonPopup",
     re.compile(r"CommonPopup\sadd\stext:(.*?)\\}")),
    ("AddCustomChoice",
     re.compile(r"AddCustomChoice\s\d+\s(.+)\s\d")),
    ("namePop",
     re.compile(r"<namePop:\s*([^>]+)>")),
    ("namePop",
     re.compile(r"\bnamePop\b\s*(?:-?\d+)?\s*([^\r\n<>]+)")),
    ("LL_InfoPopupWIndowMV",
     re.compile(r"LL_InfoPopupWIndowMV\sshowWindow\s(.+?) .+")),
    ("OriginMenuStatus SetParam",
     re.compile(r"OriginMenuStatus\sSetParam\sparam[\d]\s(.*)")),
    ("LL_GalgeChoiceWindowMV setMessageText",
     re.compile(r"LL_GalgeChoiceWindowMV setMessageText (.+)")),
    ("LL_GalgeChoiceWindowMV setChoices",
     re.compile(r"LL_GalgeChoiceWindowMV setChoices (.+)")),
]


def _substitute_mv_plugin_command(full_cmd: str, original_text: str,
                                   translation: str) -> str:
    """Substitute translated text back into an MV plugin command string.

    Uses the whitelist regex to find the original text within the full
    command and replaces it with the translation, preserving command
    structure (prefix, numeric args, etc.).
    """
    for cmd_prefix, cmd_pattern in _MV_PLUGIN_COMMAND_WHITELIST:
        if not full_cmd.startswith(cmd_prefix):
            continue
        m = cmd_pattern.search(full_cmd)
        if m and m.group(1) == original_text:
            start, end = m.span(1)
            return full_cmd[:start] + translation + full_cmd[end:]
    # Fallback: direct substring replacement
    return full_cmd.replace(original_text, translation, 1)


# Patterns that indicate a plugin param is NOT display text
_PLUGIN_TAG_RE = re.compile(r'^<[^>]+>$')  # <選択肢ヘルプ> — plugin tag
_ASSET_ID_RE = re.compile(r'^[^\s]*_[^\s]*$')  # 立ち絵_通常 — asset filename (no spaces, has _)
_FILE_PATH_RE = re.compile(r'^[a-zA-Z][\w]*[/\\]')  # img/pictures/foo — file path (starts with ASCII dir)
_JS_CODE_RE = re.compile(r'[;{}()\[\]=]')  # contains JS syntax chars — likely code, not text
_COLOR_RE = re.compile(r'^#[0-9a-fA-F]{3,8}$')  # CSS color: #FFF, #FF0000, #FF000080
_EVAL_RE = re.compile(r'\b(function|var |let |const |this\.|return |if\s*\()', re.IGNORECASE)

# Script command (355/655) patterns for extractable string literals
# Matches: $gameVariables.setValue(N, "text") or $gameVariables.setValue(N, 'text')
# Also matches: $gameVariables._data[N] = "text"
_SCRIPT_VAR_SET_RE = re.compile(
    r'\$gameVariables\.setValue\(\s*(\d+)\s*,\s*(["\'])(.*?)\2\s*\)')
_SCRIPT_VAR_DATA_RE = re.compile(
    r'\$gameVariables\._data\[\s*(\d+)\s*\]\s*=\s*(["\'])(.*?)\2')

# Control Variables (122) with operand type 4 (script): params[4] is a JS expression.
# When it's a string literal like "\"text\"", extract the inner text.
_CONTROL_VAR_STRING_RE = re.compile(r'^"(.*)"$')


def _is_plugin_display_text(text: str) -> bool:
    """Check if a plugin parameter value is likely display text (not a tag/ID/path).

    Returns False for plugin tags, asset filenames, file paths, code snippets,
    and other values that would break the game if translated.
    """
    stripped = text.strip()
    if not _has_japanese(stripped):
        return False
    # Plugin command tags: <選択肢ヘルプ>
    if _PLUGIN_TAG_RE.match(stripped):
        return False
    # Asset identifiers: 立ち絵_通常 (no spaces + underscore = filename)
    if _ASSET_ID_RE.match(stripped):
        return False
    # File paths: img/pictures/立ち絵
    if _FILE_PATH_RE.search(stripped):
        return False
    # CSS color codes
    if _COLOR_RE.match(stripped):
        return False
    # JavaScript code (semicolons, braces, assignments, etc.)
    if _JS_CODE_RE.search(stripped):
        return False
    # Eval-like patterns (function, var, this., etc.)
    if _EVAL_RE.search(stripped):
        return False
    return True


# Gender detection keywords
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


def _detect_gender(profile: str, note: str, nickname: str) -> str:
    """Try to detect gender from actor metadata. Returns 'male', 'female', or ''."""
    all_text = f"{profile} {note} {nickname}"

    female_score = len(_FEMALE_HINTS.findall(all_text))
    male_score = len(_MALE_HINTS.findall(all_text))

    if female_score > male_score:
        return "female"
    if male_score > female_score:
        return "male"
    return ""


class RPGMakerMVParser:
    """Parser for RPG Maker MV/MZ JSON data files."""

    def __init__(self):
        self.context_size = 3  # Number of recent dialogue entries for LLM context
        self._require_japanese = True  # False = extract all text (for import)
        self.extract_script_strings = False  # Experimental: extract strings from Script (355/655)
        self.single_401_mode = False  # Merge all dialogue lines into one 401 command

    def _should_extract(self, text: str) -> bool:
        """Check if text should be extracted as a translatable entry."""
        if not text or not text.strip():
            return False
        if self._require_japanese:
            return _has_japanese(text)
        return True

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

        # Build actor name lookup for \n[N] resolution in namebox
        self._actor_names = self._load_actor_names(data_dir)

        entries = []
        entries.extend(self._parse_database_files(data_dir))
        entries.extend(self._parse_system(data_dir))
        entries.extend(self._parse_common_events(data_dir))
        entries.extend(self._parse_troops(data_dir))
        entries.extend(self._parse_maps(data_dir))
        entries.extend(self._parse_plugins(project_dir))

        # Deduplicate speaker names globally — one entry per unique name.
        # Same speaker (e.g. 夢魔) may appear across many files;
        # we translate once and export applies to all occurrences.
        seen_speaker_originals = set()
        deduped = []
        for e in entries:
            if e.field == "speaker_name":
                if e.original in seen_speaker_originals:
                    continue
                seen_speaker_originals.add(e.original)
            deduped.append(e)
        return deduped

    def load_project_raw(self, project_dir: str) -> list:
        """Load ALL text entries regardless of language.

        Used to import translations from an already-translated game folder.
        Disables the Japanese-text filter so English entries are extracted too.
        """
        self._require_japanese = False
        try:
            return self.load_project(project_dir)
        finally:
            self._require_japanese = True

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
            note = item.get("note", "").strip()

            auto_gender = _detect_gender(profile, note, nickname)

            actors.append({
                "id": item.get("id", 0),
                "name": name,
                "nickname": nickname,
                "profile": profile,
                "auto_gender": auto_gender or "unknown",
            })
        return actors

    @staticmethod
    def _load_actor_names(data_dir: str) -> dict:
        """Load {actor_id: name} from Actors.json for \\n[N] resolution."""
        filepath = os.path.join(data_dir, "Actors.json")
        if not os.path.exists(filepath):
            return {}
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
        names = {}
        if isinstance(data, list):
            for item in data:
                if item and isinstance(item, dict):
                    aid = item.get("id", 0)
                    name = item.get("name", "").strip()
                    if aid and name:
                        names[aid] = name
        return names

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

        # Always read from the backup (original Japanese) so that
        # re-exports after inline edits still find the original text to match.
        backup_dir = data_dir + "_original"
        source_dir = backup_dir if os.path.isdir(backup_dir) else data_dir

        # Build global speaker name lookup (one translation applies to all files)
        global_speakers = {}
        by_file = {}
        for e in entries:
            if not (e.translation and e.status in ("translated", "reviewed")):
                continue
            if e.field == "speaker_name":
                global_speakers[e.original] = e.translation
            else:
                by_file.setdefault(e.file, []).append(e)

        for filename, file_entries in by_file.items():
            source_path = os.path.join(source_dir, filename)
            if not os.path.exists(source_path):
                continue

            with open(source_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            self._apply_translations_fast(
                data, file_entries, global_speakers=global_speakers)

            # Always write to the live data/ directory
            out_path = os.path.join(data_dir, filename)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

        # Export plugin translations (plugins.js is outside data/)
        self._save_plugins(project_dir, entries)

    def export_patch_zip(self, project_dir: str, entries: list,
                         zip_path: str, game_title: str = "",
                         inject_wordwrap: bool = False):
        """Export a complete translated game folder as a ready-to-install zip.

        Zip layout::

            data/               (complete translated data folder)
                Actors.json
                Map001.json
                ...
            js/plugins.js       (if applicable)
            js/plugins/TranslatorWordWrap.js  (if word wrap injected)
            install.bat         (renames originals, moves translations in)
            uninstall.bat       (restores originals)
            README.txt

        End users extract into the game folder and run install.bat.
        """
        data_dir = self._find_data_dir(project_dir)
        if not data_dir:
            raise FileNotFoundError(
                f"Could not find data/ directory in:\n{project_dir}"
            )

        # Use backup (original JP) as source, same as save_project
        backup_dir = data_dir + "_original"
        source_dir = backup_dir if os.path.isdir(backup_dir) else data_dir

        # Relative path from project root to data dir (e.g. "data" or "www/data")
        data_rel = os.path.relpath(data_dir, project_dir).replace("\\", "/")

        # Build global speaker lookup + group entries by file
        global_speakers = {}
        by_file = {}
        for e in entries:
            if not (e.translation and e.status in ("translated", "reviewed")):
                continue
            if e.field == "speaker_name":
                global_speakers[e.original] = e.translation
            else:
                by_file.setdefault(e.file, []).append(e)

        # Determine js/ relative path for plugins
        js_rel = None
        plugins_path = self._find_plugins_file(project_dir)
        if plugins_path:
            js_rel = os.path.relpath(
                os.path.dirname(plugins_path), project_dir
            ).replace("\\", "/")

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # Include ALL data files — apply translations where we have them
            # Stored under _translation/ so extracting the zip doesn't
            # immediately overwrite game files — install.bat handles the swap
            data_file_count = 0
            for filename in sorted(os.listdir(source_dir)):
                source_path = os.path.join(source_dir, filename)
                if not os.path.isfile(source_path):
                    continue
                if not filename.lower().endswith(".json"):
                    continue

                with open(source_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                file_entries = by_file.get(filename, [])
                if file_entries or global_speakers:
                    self._apply_translations_fast(
                        data, file_entries,
                        global_speakers=global_speakers)

                arc_path = f"_translation/{data_rel}/{filename}"
                zf.writestr(arc_path,
                            json.dumps(data, ensure_ascii=False, indent=2))
                data_file_count += 1

            # Write translated plugins.js
            plugin_entries = [
                e for e in entries
                if e.file == "plugins.js"
                and e.translation
                and e.status in ("translated", "reviewed")
            ]
            has_plugins = False
            need_plugins_js = plugin_entries or inject_wordwrap
            if need_plugins_js and plugins_path:
                plugins_backup = os.path.join(
                    os.path.dirname(plugins_path),
                    os.path.basename(plugins_path).replace(
                        "plugins.", "plugins_original."),
                )
                ps = plugins_backup if os.path.exists(plugins_backup) else plugins_path
                try:
                    plugins = self._load_plugins_js(ps)
                    plugin_by_name = {}
                    for p in plugins:
                        if isinstance(p, dict) and p.get("name"):
                            plugin_by_name[p["name"]] = p
                    for entry in plugin_entries:
                        parts = entry.id.split("/")
                        if len(parts) < 3:
                            continue
                        plugin = plugin_by_name.get(parts[1])
                        if not plugin:
                            continue
                        params = plugin.get("parameters", {})
                        if parts[2] not in params:
                            continue
                        if len(parts) <= 3:
                            # Check if value was JSON-encoded string scalar
                            raw_val = params[parts[2]]
                            try:
                                decoded = json.loads(raw_val)
                                if isinstance(decoded, str):
                                    params[parts[2]] = json.dumps(entry.translation, ensure_ascii=False)
                                    continue
                            except (json.JSONDecodeError, ValueError):
                                pass
                            params[parts[2]] = entry.translation
                        else:
                            try:
                                parsed = json.loads(params[parts[2]])
                                self._set_nested_value(
                                    parsed, parts[3:],
                                    entry.original, entry.translation)
                                params[parts[2]] = json.dumps(
                                    parsed, ensure_ascii=False)
                            except (json.JSONDecodeError, ValueError):
                                continue

                    if inject_wordwrap:
                        if not any(p.get("name") == self.INJECTED_PLUGIN_NAME
                                   for p in plugins):
                            plugins.append({
                                "name": self.INJECTED_PLUGIN_NAME,
                                "status": True,
                                "description": "Word wrap for translated text (auto-injected)",
                                "parameters": {},
                            })

                    js_content = "var $plugins =\n" + json.dumps(
                        plugins, ensure_ascii=False, indent=2) + ";\n"
                    zf.writestr(f"_translation/{js_rel}/plugins.js", js_content)
                    has_plugins = True
                except (json.JSONDecodeError, OSError):
                    pass

            # Include word wrap JS plugin file
            if inject_wordwrap and js_rel:
                from .text_processor import WORDWRAP_PLUGIN_JS
                arc = f"_translation/{js_rel}/plugins/{self.INJECTED_PLUGIN_NAME}.js"
                zf.writestr(arc, WORDWRAP_PLUGIN_JS.strip() + "\n")

            total_entries = sum(len(v) for v in by_file.values())

            # install.bat
            zf.writestr("install.bat", self._build_install_bat(
                data_rel, js_rel, data_file_count, has_plugins, game_title,
                inject_wordwrap=inject_wordwrap))

            # uninstall.bat
            zf.writestr("uninstall.bat", self._build_uninstall_bat(
                data_rel, js_rel, has_plugins, game_title,
                inject_wordwrap=inject_wordwrap))

            # README.txt
            readme = (
                f"English Translation — {game_title or 'RPG Maker Game'}\n"
                f"{'=' * 50}\n\n"
                f"Files: {data_file_count} data file(s)"
                f"{' + plugins.js' if has_plugins else ''}\n"
                f"Entries: {total_entries} translated\n\n"
                "HOW TO INSTALL:\n"
                "1. Extract this zip into the game folder\n"
                "   (the folder containing Game.exe)\n"
                "2. Run install.bat\n"
                "3. Play the game!\n\n"
                "TO REVERT TO JAPANESE:\n"
                "  Run uninstall.bat\n"
            )
            zf.writestr("README.txt", readme)

    def export_game_folder_as_patch(self, game_path: str, zip_path: str,
                                    game_title: str = "") -> dict:
        """Package a game folder's current data/ files into a distributable zip.

        Unlike export_patch_zip(), this does NOT apply translations from a
        project.  It takes the current state of data/ (and optionally
        js/plugins.js) and packages them as-is.  Useful when the game
        already has translations baked in from a previous patch.
        """
        data_dir = self._find_data_dir(game_path)
        if not data_dir:
            raise FileNotFoundError(
                f"Could not find data/ directory in:\n{game_path}"
            )

        backup_dir = data_dir + "_original"
        data_original_exists = os.path.isdir(backup_dir)

        # Relative path from game root to data dir (e.g. "data" or "www/data")
        data_rel = os.path.relpath(data_dir, game_path).replace("\\", "/")

        # Detect plugins.js
        js_rel = None
        plugins_path = self._find_plugins_file(game_path)
        if plugins_path:
            js_rel = os.path.relpath(
                os.path.dirname(plugins_path), game_path
            ).replace("\\", "/")

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # Copy all JSON files from data/ directly into zip
            data_file_count = 0
            for filename in sorted(os.listdir(data_dir)):
                src = os.path.join(data_dir, filename)
                if not os.path.isfile(src):
                    continue
                if not filename.lower().endswith(".json"):
                    continue
                with open(src, "r", encoding="utf-8") as f:
                    content = f.read()
                zf.writestr(f"_translation/{data_rel}/{filename}", content)
                data_file_count += 1

            # Copy plugins.js if present
            has_plugins = False
            if plugins_path and os.path.isfile(plugins_path):
                with open(plugins_path, "r", encoding="utf-8") as f:
                    plugins_content = f.read()
                zf.writestr(f"_translation/{js_rel}/plugins.js", plugins_content)
                has_plugins = True

            # Install / uninstall scripts (reuse existing builders)
            zf.writestr("install.bat", self._build_install_bat(
                data_rel, js_rel, data_file_count, has_plugins, game_title,
                inject_wordwrap=False))
            zf.writestr("uninstall.bat", self._build_uninstall_bat(
                data_rel, js_rel, has_plugins, game_title,
                inject_wordwrap=False))

            readme = (
                f"English Translation \u2014 {game_title or 'RPG Maker Game'}\n"
                f"{'=' * 50}\n\n"
                f"Files: {data_file_count} data file(s)"
                f"{' + plugins.js' if has_plugins else ''}\n\n"
                "HOW TO INSTALL:\n"
                "1. Extract this zip into the game folder\n"
                "   (the folder containing Game.exe)\n"
                "2. Run install.bat\n"
                "3. Play the game!\n\n"
                "TO REVERT TO JAPANESE:\n"
                "  Run uninstall.bat\n"
            )
            zf.writestr("README.txt", readme)

        return {
            "data_files": data_file_count,
            "has_plugins": has_plugins,
            "data_original_exists": data_original_exists,
        }

    @staticmethod
    def _build_install_bat(data_rel: str, js_rel: str,
                           n_files: int, has_plugins: bool,
                           game_title: str,
                           inject_wordwrap: bool = False) -> str:
        """Generate install.bat: rename originals aside, move translations in."""
        dr = data_rel.replace("/", "\\")
        dr_base = os.path.basename(data_rel)     # e.g. "data"
        tr = f"_translation\\{dr}"                # e.g. "_translation\\data"
        lines = [
            "@echo off",
            "chcp 65001 >nul 2>&1",
            'pushd "%~dp0"',
            f"title Install English Translation",
            "echo.",
            f"echo  {game_title or 'RPG Maker Game'} — English Translation",
            "echo.",
            f"echo  This will install the English translation ({n_files} files).",
            "echo  Original files will be backed up automatically.",
            "echo.",
            "pause",
            "",
            # Sanity checks
            f'if not exist "{dr}\\" (',
            f'    echo ERROR: "{dr}\\" folder not found.',
            "    echo Make sure you extracted this zip into the game folder",
            "    echo  ^(the folder containing Game.exe^).",
            "    pause",
            "    popd",
            "    exit /b 1",
            ")",
            f'if not exist "{tr}\\" (',
            "    echo ERROR: _translation folder not found.",
            "    echo Make sure you extracted the FULL zip, not just install.bat.",
            "    pause",
            "    popd",
            "    exit /b 1",
            ")",
            "",
            "set FAIL=0",
            "",
            # Step 1: Rename original data/ to data_original/
            f'echo [Step 1] Backing up original files...',
            f'if not exist "{dr}_original\\" (',
            f'    ren "{dr}" "{dr_base}_original"',
            "    if errorlevel 1 (",
            f'        echo   ERROR: Failed to rename {dr}\\ to {dr}_original\\',
            "        set FAIL=1",
            "        goto :done",
            "    )",
            f'    echo   Renamed {dr}\\ to {dr}_original\\',
            ") else (",
            f'    echo   Backup already exists ({dr}_original\\)',
            f'    echo   Removing current {dr}\\ to replace with translation...',
            f'    rmdir /S /Q "{dr}"',
            ")",
            "",
        ]

        if has_plugins and js_rel:
            jr = js_rel.replace("/", "\\")
            lines += [
                f'if exist "{jr}\\plugins.js" if not exist "{jr}\\plugins_original.js" (',
                f'    ren "{jr}\\plugins.js" "plugins_original.js"',
                f"    echo   Renamed {jr}\\plugins.js to plugins_original.js",
                ")",
                "",
            ]

        # Step 2: Move translated folder into place
        lines += [
            f'echo [Step 2] Installing translated files...',
            f'move "{tr}" "{dr}"',
            "if errorlevel 1 (",
            f'    echo   ERROR: Failed to move translated {dr}\\ into place',
            "    set FAIL=1",
            ") else (",
            f"    echo   Installed translated {dr}\\ ({n_files} files)",
            ")",
        ]

        if has_plugins and js_rel:
            jr = js_rel.replace("/", "\\")
            tjr = f"_translation\\{jr}"
            lines += [
                f'if exist "{tjr}\\plugins.js" (',
                f'    copy /Y "{tjr}\\plugins.js" "{jr}\\plugins.js" >nul',
                f"    echo   Installed translated plugins.js",
                ")",
            ]

        if inject_wordwrap and js_rel:
            jr = js_rel.replace("/", "\\")
            tjr = f"_translation\\{jr}"
            lines += [
                f'if exist "{tjr}\\plugins\\TranslatorWordWrap.js" (',
                f'    if not exist "{jr}\\plugins\\" mkdir "{jr}\\plugins"',
                f'    copy /Y "{tjr}\\plugins\\TranslatorWordWrap.js" "{jr}\\plugins\\TranslatorWordWrap.js" >nul',
                f"    echo   Installed word wrap plugin",
                ")",
            ]

        # Cleanup _translation folder
        lines += [
            "",
            'if exist "_translation\\" rmdir /S /Q "_translation"',
            "",
            ":done",
            "echo.",
            "if %FAIL%==1 (",
            "    echo  Installation FAILED — see errors above.",
            ") else (",
            "    echo  Installation complete!",
            "    echo  To restore Japanese originals, run uninstall.bat",
            ")",
            "echo.",
            "pause",
            "popd",
        ]
        return "\r\n".join(lines) + "\r\n"

    @staticmethod
    def _build_uninstall_bat(data_rel: str, js_rel: str,
                             has_plugins: bool, game_title: str,
                             inject_wordwrap: bool = False) -> str:
        """Generate uninstall.bat: remove translated data, rename originals back."""
        dr = data_rel.replace("/", "\\")
        dr_base = os.path.basename(data_rel)
        lines = [
            "@echo off",
            "chcp 65001 >nul 2>&1",
            'pushd "%~dp0"',
            f"title Restore Japanese — {game_title or 'RPG Maker Game'}",
            "echo.",
            f"echo  {game_title or 'RPG Maker Game'} — Restore Japanese",
            "echo.",
            "echo  This will restore the original Japanese files.",
            "echo.",
            "pause",
            "",
            f'if not exist "{dr}_original\\" (',
            f"    echo ERROR: No backup found ({dr}_original\\).",
            "    echo Cannot restore — the backup was never created.",
            "    pause",
            "    popd",
            "    exit /b 1",
            ")",
            "",
            "set FAIL=0",
            "",
            # Remove translated data/ and rename original back
            f'echo Removing translated {dr}\\...',
            f'if exist "{dr}\\" rmdir /S /Q "{dr}"',
            "",
            f'echo Restoring {dr}_original\\ to {dr}\\...',
            f'ren "{dr}_original" "{dr_base}"',
            "if errorlevel 1 (",
            f'    echo   ERROR: Failed to rename {dr}_original\\ back to {dr}\\',
            "    set FAIL=1",
            ") else (",
            "    echo   Data files restored.",
            ")",
        ]

        if has_plugins and js_rel:
            jr = js_rel.replace("/", "\\")
            lines += [
                "",
                f'if exist "{jr}\\plugins_original.js" (',
                f'    if exist "{jr}\\plugins.js" del "{jr}\\plugins.js"',
                f'    ren "{jr}\\plugins_original.js" "plugins.js"',
                "    if errorlevel 1 (",
                "        echo   ERROR: Failed to restore plugins.js",
                "        set FAIL=1",
                "    ) else (",
                "        echo   plugins.js restored.",
                "    )",
                ")",
            ]

        if inject_wordwrap and js_rel:
            jr = js_rel.replace("/", "\\")
            lines += [
                "",
                f'if exist "{jr}\\plugins\\TranslatorWordWrap.js" (',
                f'    del "{jr}\\plugins\\TranslatorWordWrap.js"',
                "    echo   Removed word wrap plugin.",
                ")",
            ]

        lines += [
            "",
            "echo.",
            "if %FAIL%==1 (",
            "    echo  Restore FAILED — see errors above.",
            ") else (",
            "    echo  Done! Original Japanese files restored.",
            ")",
            "echo.",
            "pause",
            "popd",
        ]
        return "\r\n".join(lines) + "\r\n"

    @staticmethod
    def _backup_data_dir(data_dir: str):
        """Copy the data/ folder to data_original/ if no backup exists yet."""
        backup_dir = data_dir + "_original"
        if os.path.isdir(backup_dir):
            return  # Already backed up
        shutil.copytree(data_dir, backup_dir)

    # ── Static helpers ────────────────────────────────────────────────

    @staticmethod
    def find_content_root(project_dir: str) -> Optional[str]:
        """Return the folder containing data/ and js/ (handles www/ layout).

        For distributed MV games the content lives under www/,
        for MZ (and MV editor projects) it's at the project root.
        """
        for base in (project_dir, os.path.join(project_dir, "www")):
            data = os.path.join(base, "data")
            if not os.path.isdir(data):
                data = os.path.join(base, "Data")
            if os.path.isdir(data):
                return base
        return None

    @staticmethod
    def detect_engine(project_dir: str) -> Optional[str]:
        """Detect whether a project is RPG Maker MV or MZ.

        Returns ``"mv"``, ``"mz"``, or ``None``.
        """
        content_root = RPGMakerMVParser.find_content_root(project_dir)
        if not content_root:
            return None
        js_dir = os.path.join(content_root, "js")
        if not os.path.isdir(js_dir):
            return None
        if os.path.isfile(os.path.join(js_dir, "rmmz_core.js")):
            return "mz"
        if os.path.isfile(os.path.join(js_dir, "rpg_core.js")):
            return "mv"
        return None

    # ── Private: find data dir ─────────────────────────────────────────

    def _find_data_dir(self, project_dir: str) -> Optional[str]:
        """Locate the data/ directory inside the project."""
        content_root = self.find_content_root(project_dir)
        if content_root:
            for name in ("data", "Data"):
                d = os.path.join(content_root, name)
                if os.path.isdir(d):
                    return d
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
                    if isinstance(text, str) and self._should_extract(text):
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
        if self._should_extract(title):
            entries.append(TranslationEntry(
                id="System.json/gameTitle",
                file="System.json",
                field="gameTitle",
                original=title,
            ))

        # Terms — messages (array in MZ, dict in MV)
        terms = data.get("terms", {})
        messages = terms.get("messages", {})
        if isinstance(messages, list):
            for i, msg in enumerate(messages):
                if isinstance(msg, str) and self._should_extract(msg):
                    entries.append(TranslationEntry(
                        id=f"System.json/terms/messages/{i}",
                        file="System.json",
                        field=f"terms.messages[{i}]",
                        original=msg,
                    ))
        elif isinstance(messages, dict):
            for key, msg in messages.items():
                if isinstance(msg, str) and self._should_extract(msg):
                    entries.append(TranslationEntry(
                        id=f"System.json/terms/messages/{key}",
                        file="System.json",
                        field=f"terms.messages.{key}",
                        original=msg,
                    ))

        # Terms — commands array
        commands = terms.get("commands", [])
        if isinstance(commands, list):
            for i, cmd in enumerate(commands):
                if isinstance(cmd, str) and self._should_extract(cmd):
                    entries.append(TranslationEntry(
                        id=f"System.json/terms/commands/{i}",
                        file="System.json",
                        field=f"terms.commands[{i}]",
                        original=cmd,
                    ))

        # Terms — params array (stat/parameter names: HP, MP, ATK, etc.)
        params = terms.get("params", [])
        if isinstance(params, list):
            for i, param in enumerate(params):
                if isinstance(param, str) and self._should_extract(param):
                    entries.append(TranslationEntry(
                        id=f"System.json/terms/params/{i}",
                        file="System.json",
                        field=f"terms.params[{i}]",
                        original=param,
                    ))

        # Terms — basic array (HP/MP abbreviations and similar)
        basic = terms.get("basic", [])
        if isinstance(basic, list):
            for i, b in enumerate(basic):
                if isinstance(b, str) and self._should_extract(b):
                    entries.append(TranslationEntry(
                        id=f"System.json/terms/basic/{i}",
                        file="System.json",
                        field=f"terms.basic[{i}]",
                        original=b,
                    ))

        # Type arrays — battle menus and equipment screens
        for arr_name in ("elements", "skillTypes", "weaponTypes",
                         "armorTypes", "equipTypes"):
            arr = data.get(arr_name, [])
            if not isinstance(arr, list):
                continue
            for i, val in enumerate(arr):
                if isinstance(val, str) and self._should_extract(val):
                    entries.append(TranslationEntry(
                        id=f"System.json/{arr_name}/{i}",
                        file="System.json",
                        field=arr_name,
                        original=val,
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

        seen_speakers = set()
        for event in data:
            if not event or not isinstance(event, dict):
                continue
            event_id = event.get("id", 0)
            event_name = event.get("name", "")
            cmd_list = event.get("list", [])
            entries.extend(self._extract_event_commands(
                cmd_list, "CommonEvents.json", f"CE{event_id}({event_name})",
                seen_speakers=seen_speakers,
            ))

        return entries

    # ── Private: Troops (battle events) ────────────────────────────────

    def _parse_troops(self, data_dir: str) -> list:
        """Parse Troops.json for battle event dialogue."""
        entries = []
        filepath = os.path.join(data_dir, "Troops.json")
        if not os.path.exists(filepath):
            return entries

        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, list):
            return entries

        seen_speakers = set()
        for troop in data:
            if not troop or not isinstance(troop, dict):
                continue
            troop_id = troop.get("id", 0)
            troop_name = troop.get("name", "")

            # Troop names extracted by _parse_database_files via DATABASE_FILES

            # Battle event pages — same structure as map event pages
            pages = troop.get("pages", [])
            for page_idx, page in enumerate(pages):
                if not page or not isinstance(page, dict):
                    continue
                cmd_list = page.get("list", [])
                prefix = f"Troop{troop_id}({troop_name})/p{page_idx}"
                entries.extend(self._extract_event_commands(
                    cmd_list, "Troops.json", prefix,
                    seen_speakers=seen_speakers,
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
            if self._should_extract(display_name):
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

            seen_speakers = set()
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
                        cmd_list, filename, prefix,
                        seen_speakers=seen_speakers,
                    ))

        return entries

    # ── Private: event command extraction ──────────────────────────────

    def _extract_event_commands(self, cmd_list: list, filename: str, prefix: str,
                                seen_speakers: set = None) -> list:
        """Extract translatable text from a list of event commands.

        Groups consecutive 401 (Show Text) commands into single dialogue blocks.
        Reads 101 (Show Text Header) to identify the speaker for each block.
        Extracts unique MZ speaker names from 101 param[4] for translation.
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
                # Extract MZ speaker name for translation (deduplicated per file)
                if (speaker_name and self._should_extract(speaker_name)
                        and seen_speakers is not None
                        and speaker_name not in seen_speakers):
                    seen_speakers.add(speaker_name)
                    entries.append(TranslationEntry(
                        id=f"{filename}/speaker/{speaker_name}",
                        file=filename,
                        field="speaker_name",
                        original=speaker_name,
                    ))
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

                # Detect name prefix on first line:
                #   \N<name> (Lunatlazur namebox plugin)
                #   \n[N] / \N[N] (bare actor code at line start)
                namebox = ""
                nb_match = _NAMEBOX_RE.match(full_text)
                if nb_match:
                    namebox = nb_match.group(0)       # e.g. \N<\n[1]>
                    nb_name = nb_match.group(1)       # e.g. \n[1] or 村人1
                    full_text = full_text[len(namebox):]
                    # Resolve \n[N] to actor name for speaker context
                    actor_match = _ACTOR_CODE_RE.match(nb_name)
                    if actor_match:
                        actor_id = int(actor_match.group(1))
                        current_speaker = getattr(
                            self, '_actor_names', {}).get(actor_id, nb_name)
                    else:
                        current_speaker = nb_name
                    # Literal JP name → create speaker_name entry for translation
                    if (not actor_match
                            and self._should_extract(nb_name)
                            and seen_speakers is not None
                            and nb_name not in seen_speakers):
                        seen_speakers.add(nb_name)
                        entries.append(TranslationEntry(
                            id=f"{filename}/speaker/{nb_name}",
                            file=filename,
                            field="speaker_name",
                            original=nb_name,
                        ))
                elif not namebox:
                    # Bare \n[N] or \N[N] at start of line (no angle brackets)
                    bare_match = _ACTOR_CODE_RE.match(full_text)
                    if bare_match:
                        namebox = bare_match.group(0)  # e.g. \n[1]
                        full_text = full_text[len(namebox):]
                        actor_id = int(bare_match.group(1))
                        current_speaker = getattr(
                            self, '_actor_names', {}).get(
                                actor_id, namebox)

                # Always create an entry for dialogue blocks so that the
                # dialog counter stays aligned across game versions (even
                # when some versions have blank/empty 401 lines).
                dialog_counter += 1
                extractable = self._should_extract(full_text)
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
                    namebox=namebox,
                    status="untranslated" if extractable else "skipped",
                ))
                if extractable:
                    recent_ctx.append(full_text)
                continue

            # Show Choices
            if code == CODE_SHOW_CHOICES and params:
                choices = params[0] if isinstance(params[0], list) else []
                ctx = "\n---\n".join(recent_ctx) if recent_ctx else ""
                for ci, choice in enumerate(choices):
                    if isinstance(choice, str) and self._should_extract(choice):
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
                dialog_counter += 1
                extractable = self._should_extract(full_text)
                ctx = "\n---\n".join(recent_ctx) if recent_ctx else ""
                entries.append(TranslationEntry(
                    id=f"{filename}/{prefix}/scroll_{dialog_counter}",
                    file=filename,
                    field="scroll_text",
                    original=full_text,
                    context=ctx,
                    status="untranslated" if extractable else "skipped",
                ))
                if extractable:
                    recent_ctx.append(full_text)
                continue

            # Change Actor Name / Nickname / Profile (320, 324, 325)
            if code in (CODE_CHANGE_NAME, CODE_CHANGE_NICKNAME, CODE_CHANGE_PROFILE):
                text = params[1] if len(params) > 1 else ""
                field_map = {
                    CODE_CHANGE_NAME: "name",
                    CODE_CHANGE_NICKNAME: "nickname",
                    CODE_CHANGE_PROFILE: "profile",
                }
                fld = field_map[code]
                if isinstance(text, str) and self._should_extract(text):
                    dialog_counter += 1
                    entries.append(TranslationEntry(
                        id=f"{filename}/{prefix}/change_{fld}_{dialog_counter}",
                        file=filename,
                        field=fld,
                        original=text,
                    ))

            # Plugin Command MV (356) — whitelist-based extraction.
            # Only extract text from known plugin commands via regex.
            # Full command stored in context for export reconstruction.
            if code == CODE_PLUGIN_COMMAND_MV and params:
                cmd_str = params[0] if isinstance(params[0], str) else ""
                if cmd_str:
                    for cmd_prefix, cmd_pattern in _MV_PLUGIN_COMMAND_WHITELIST:
                        if not cmd_str.startswith(cmd_prefix):
                            continue
                        m = cmd_pattern.search(cmd_str)
                        if m and m.group(1) and _has_japanese(m.group(1)):
                            dialog_counter += 1
                            entries.append(TranslationEntry(
                                id=f"{filename}/{prefix}/plugin_mv_{dialog_counter}",
                                file=filename,
                                field="plugin_command",
                                original=m.group(1),
                                context=f"[PLUGIN_CMD:{cmd_str}]",
                            ))
                            break  # first matching pattern wins

            # Plugin Command MZ (357) — whitelist-based extraction.
            # Only extract specific param keys from known plugins.
            if code == CODE_PLUGIN_COMMAND_MZ and len(params) >= 4:
                plugin_name = params[0] if isinstance(params[0], str) else ""
                allowed_keys = _MZ_PLUGIN_COMMAND_WHITELIST.get(plugin_name)
                if allowed_keys:
                    arg_str = params[3] if isinstance(params[3], str) else ""
                    if arg_str:
                        try:
                            arg_dict = json.loads(arg_str)
                        except (json.JSONDecodeError, ValueError):
                            arg_dict = {}
                        if isinstance(arg_dict, dict):
                            for key in allowed_keys:
                                val = arg_dict.get(key, "")
                                if isinstance(val, str) and _has_japanese(val):
                                    dialog_counter += 1
                                    entries.append(TranslationEntry(
                                        id=f"{filename}/{prefix}/plugin_mz_{dialog_counter}/{plugin_name}/{key}",
                                        file=filename,
                                        field="plugin_command",
                                        original=val,
                                    ))

            # Control Variables (122) with script operand — experimental:
            # params = [startVar, endVar, ?, operandType, expression]
            # operandType 4 = "Script", expression is a JS string.
            # When expression is a quoted string literal like '"quest text"',
            # extract the inner text for translation.
            if self.extract_script_strings and code == CODE_CONTROL_VARIABLES:
                if len(params) >= 5 and params[3] == 4:
                    expr = params[4] if isinstance(params[4], str) else ""
                    m = _CONTROL_VAR_STRING_RE.match(expr)
                    if m:
                        # Unescape JS string escapes (\" → ", \\ → \, etc.)
                        text = re.sub(r'\\(.)', r'\1', m.group(1))
                        if text and _has_japanese(text):
                            var_id = params[0]
                            dialog_counter += 1
                            entries.append(TranslationEntry(
                                id=f"{filename}/{prefix}/script_var_{dialog_counter}",
                                file=filename,
                                field="script_variable",
                                original=text,
                                context=f"[CONTROL_VAR:{var_id}]",
                            ))

            # Script (355/655) — experimental: extract string literals
            # from $gameVariables.setValue(N, "Japanese text") calls.
            if self.extract_script_strings and code == CODE_SCRIPT:
                # Collect full script: 355 line + all following 655 lines
                script_lines = [params[0] if params and isinstance(params[0], str) else ""]
                j = i + 1
                while j < len(cmd_list):
                    c = cmd_list[j]
                    if isinstance(c, dict) and c.get("code") == CODE_SCRIPT_CONT:
                        p = c.get("parameters", [""])
                        script_lines.append(p[0] if p and isinstance(p[0], str) else "")
                        j += 1
                    else:
                        break
                full_script = "\n".join(script_lines)
                # Try both patterns: .setValue(N, "text") and ._data[N] = "text"
                for pattern in (_SCRIPT_VAR_SET_RE, _SCRIPT_VAR_DATA_RE):
                    for m in pattern.finditer(full_script):
                        var_id, _quote, text = m.group(1), m.group(2), m.group(3)
                        if text and _has_japanese(text):
                            dialog_counter += 1
                            entries.append(TranslationEntry(
                                id=f"{filename}/{prefix}/script_var_{dialog_counter}",
                                file=filename,
                                field="script_variable",
                                original=text,
                                context=f"[SCRIPT_VAR:{var_id}:{m.group(0)}]",
                            ))

            i += 1

        return entries

    # ── Cross-version structural alignment ───────────────────────────

    @staticmethod
    def _extract_structural_items(cmd_list: list) -> list:
        """Extract a mixed sequence of structural anchors and text blocks.

        Returns list of tuples:
          ('A', fingerprint)           — structural anchor (non-text command)
          ('D', joined_text)           — dialog block (consecutive 401/405 lines)
          ('C', [choice1, choice2...]) — choice block (102 command)
        """
        items = []
        current_text = []
        text_code = None  # 401 or 405

        for cmd in cmd_list:
            if not isinstance(cmd, dict):
                continue
            code = cmd.get("code", 0)
            params = cmd.get("parameters", [])
            indent = cmd.get("indent", 0)

            if code in (CODE_SHOW_TEXT, CODE_SCROLL_TEXT):
                text = params[0] if params else ""
                current_text.append(str(text))
                text_code = code
                continue

            # Any non-text command flushes the current text block
            if current_text:
                items.append(("D", "\n".join(current_text)))
                current_text = []
                text_code = None

            if code == 0:
                continue  # null terminator, skip

            if code == CODE_SHOW_TEXT_HEADER:
                # Fingerprint: face, bg, pos — NOT speaker name (may be translated)
                face = params[0] if len(params) > 0 else ""
                bg = params[2] if len(params) > 2 else 0
                pos = params[3] if len(params) > 3 else 2
                items.append(("A", (code, indent, face, bg, pos)))

            elif code == CODE_SHOW_CHOICES:
                choices = params[0] if params and isinstance(params[0], list) else []
                items.append(("C", [str(c) for c in choices]))
                items.append(("A", (code, indent)))

            elif code == CODE_SCROLL_TEXT_HEADER:
                items.append(("A", (code, indent)))

            else:
                # Generic structural anchor
                items.append(("A", (code, indent)))

        # Flush trailing text
        if current_text:
            items.append(("D", "\n".join(current_text)))

        return items

    @staticmethod
    def _make_comparable_seq(items: list) -> list:
        """Convert structural items into a hashable sequence for SequenceMatcher.

        Dialog/choice blocks are identified by their preceding anchor + position,
        so SequenceMatcher can align them structurally even when text differs.
        """
        result = []
        last_anchor = None
        block_count = 0
        for item in items:
            if item[0] == "A":
                last_anchor = item[1]
                block_count = 0
                result.append(("A", item[1]))
            else:
                block_count += 1
                result.append(("B", last_anchor, block_count))
        return result

    @staticmethod
    def _pair_text_blocks(items_proj: list, items_donor: list) -> list:
        """Pair text blocks between project and donor using structural alignment.

        Returns list of (project_text, donor_text) tuples.
        """
        # Count text blocks (D and C types)
        proj_texts = [it for it in items_proj if it[0] in ("D", "C")]
        donor_texts = [it for it in items_donor if it[0] in ("D", "C")]

        if not proj_texts or not donor_texts:
            return []

        # Tier 1: same block count → direct 1:1 index matching
        if len(proj_texts) == len(donor_texts):
            return list(zip(proj_texts, donor_texts))

        # Tier 2: different block count → SequenceMatcher alignment
        seq_proj = RPGMakerMVParser._make_comparable_seq(items_proj)
        seq_donor = RPGMakerMVParser._make_comparable_seq(items_donor)

        sm = SequenceMatcher(None, seq_proj, seq_donor)
        pairs = []
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "equal":
                for k in range(i2 - i1):
                    p_item = items_proj[i1 + k]
                    d_item = items_donor[j1 + k]
                    if p_item[0] in ("D", "C") and d_item[0] in ("D", "C"):
                        pairs.append((p_item, d_item))
        return pairs

    def build_cross_version_map(self, donor_dir: str, project_dir: str) -> dict:
        """Build a {project_text: donor_text} translation map by structural alignment.

        Walks raw JSON command lists from both game versions, matches events
        by RPG Maker ID, then aligns dialogue blocks using non-text commands
        as structural anchors.

        Args:
            donor_dir: Path to the donor (translated) game folder.
            project_dir: Path to the project (original) game folder.

        Returns:
            Dict mapping project original text to donor translated text.
        """
        donor_data = self._find_data_dir(donor_dir)
        proj_data = self._find_data_dir(project_dir)
        if not donor_data or not proj_data:
            return {}

        text_map = {}

        # Process database fields (names, descriptions — stable IDs)
        self._align_database(donor_data, proj_data, text_map)

        # Process CommonEvents
        self._align_common_events(donor_data, proj_data, text_map)

        # Process Maps (includes displayNames)
        self._align_maps(donor_data, proj_data, text_map)

        # Process Troops
        self._align_troops(donor_data, proj_data, text_map)

        return text_map

    def _align_database(self, donor_data: str, proj_data: str,
                        text_map: dict):
        """Align database files (Actors, Items, etc.) between donor and project."""
        for filename, fields in DATABASE_FILES.items():
            d_path = os.path.join(donor_data, filename)
            p_path = os.path.join(proj_data, filename)
            if not os.path.exists(d_path) or not os.path.exists(p_path):
                continue

            with open(d_path, "r", encoding="utf-8") as f:
                d_data = json.load(f)
            with open(p_path, "r", encoding="utf-8") as f:
                p_data = json.load(f)

            if not isinstance(d_data, list) or not isinstance(p_data, list):
                continue

            # Build donor lookup by item ID
            d_by_id = {}
            for item in d_data:
                if isinstance(item, dict):
                    d_by_id[item.get("id", 0)] = item

            for item in p_data:
                if not isinstance(item, dict):
                    continue
                d_item = d_by_id.get(item.get("id", 0))
                if not d_item:
                    continue
                for fld in fields:
                    p_val = item.get(fld, "")
                    d_val = d_item.get(fld, "")
                    if (p_val and d_val and isinstance(p_val, str)
                            and isinstance(d_val, str) and p_val != d_val):
                        text_map[p_val] = d_val

    def _align_common_events(self, donor_data: str, proj_data: str,
                             text_map: dict):
        """Align CommonEvents.json between donor and project."""
        donor_path = os.path.join(donor_data, "CommonEvents.json")
        proj_path = os.path.join(proj_data, "CommonEvents.json")
        if not os.path.exists(donor_path) or not os.path.exists(proj_path):
            return

        with open(donor_path, "r", encoding="utf-8") as f:
            donor_events = json.load(f)
        with open(proj_path, "r", encoding="utf-8") as f:
            proj_events = json.load(f)

        if not isinstance(donor_events, list) or not isinstance(proj_events, list):
            return

        # Match events by array index (RPG Maker event IDs)
        for i in range(min(len(donor_events), len(proj_events))):
            d_ev = donor_events[i]
            p_ev = proj_events[i]
            if not d_ev or not p_ev:
                continue
            if not isinstance(d_ev, dict) or not isinstance(p_ev, dict):
                continue

            d_cmds = d_ev.get("list", [])
            p_cmds = p_ev.get("list", [])
            if not d_cmds or not p_cmds:
                continue

            self._align_cmd_lists(d_cmds, p_cmds, text_map)

    def _align_maps(self, donor_data: str, proj_data: str, text_map: dict):
        """Align Map###.json files between donor and project."""
        proj_maps = {f for f in os.listdir(proj_data)
                     if re.match(r'^Map\d+\.json$', f, re.IGNORECASE)}
        donor_maps = {f for f in os.listdir(donor_data)
                      if re.match(r'^Map\d+\.json$', f, re.IGNORECASE)}

        for mapfile in sorted(proj_maps & donor_maps):
            with open(os.path.join(donor_data, mapfile), "r",
                      encoding="utf-8") as f:
                d_map = json.load(f)
            with open(os.path.join(proj_data, mapfile), "r",
                      encoding="utf-8") as f:
                p_map = json.load(f)

            if not isinstance(d_map, dict) or not isinstance(p_map, dict):
                continue

            # Map displayName (top-level property, not inside events)
            p_dn = p_map.get("displayName", "")
            d_dn = d_map.get("displayName", "")
            if p_dn and d_dn and p_dn != d_dn:
                text_map[p_dn] = d_dn

            # Build event lookup by ID for both
            d_events = {}
            for ev in d_map.get("events", []):
                if isinstance(ev, dict):
                    d_events[ev.get("id", 0)] = ev
            p_events = {}
            for ev in p_map.get("events", []):
                if isinstance(ev, dict):
                    p_events[ev.get("id", 0)] = ev

            for eid in p_events:
                if eid not in d_events:
                    continue
                d_ev = d_events[eid]
                p_ev = p_events[eid]

                d_pages = d_ev.get("pages", [])
                p_pages = p_ev.get("pages", [])

                for pi in range(min(len(d_pages), len(p_pages))):
                    dp = d_pages[pi]
                    pp = p_pages[pi]
                    if not isinstance(dp, dict) or not isinstance(pp, dict):
                        continue
                    self._align_cmd_lists(
                        dp.get("list", []), pp.get("list", []), text_map)

    def _align_troops(self, donor_data: str, proj_data: str,
                      text_map: dict):
        """Align Troops.json between donor and project."""
        donor_path = os.path.join(donor_data, "Troops.json")
        proj_path = os.path.join(proj_data, "Troops.json")
        if not os.path.exists(donor_path) or not os.path.exists(proj_path):
            return

        with open(donor_path, "r", encoding="utf-8") as f:
            donor_troops = json.load(f)
        with open(proj_path, "r", encoding="utf-8") as f:
            proj_troops = json.load(f)

        if not isinstance(donor_troops, list) or not isinstance(proj_troops, list):
            return

        # Build lookup by troop ID
        d_by_id = {}
        for troop in donor_troops:
            if isinstance(troop, dict):
                d_by_id[troop.get("id", 0)] = troop

        for troop in proj_troops:
            if not isinstance(troop, dict):
                continue
            tid = troop.get("id", 0)
            d_troop = d_by_id.get(tid)
            if not d_troop:
                continue

            d_pages = d_troop.get("pages", [])
            p_pages = troop.get("pages", [])

            for pi in range(min(len(d_pages), len(p_pages))):
                dp = d_pages[pi]
                pp = p_pages[pi]
                if not isinstance(dp, dict) or not isinstance(pp, dict):
                    continue
                self._align_cmd_lists(
                    dp.get("list", []), pp.get("list", []), text_map)

    def _align_cmd_lists(self, donor_cmds: list, proj_cmds: list,
                         text_map: dict):
        """Align two command lists and add paired texts to text_map."""
        # Pair 101 speaker names by position
        proj_speakers = []
        donor_speakers = []
        for cmd in proj_cmds:
            if (isinstance(cmd, dict) and cmd.get("code") == CODE_SHOW_TEXT_HEADER
                    and len(cmd.get("parameters", [])) > 4):
                name = cmd["parameters"][4]
                if name:
                    proj_speakers.append(name)
        for cmd in donor_cmds:
            if (isinstance(cmd, dict) and cmd.get("code") == CODE_SHOW_TEXT_HEADER
                    and len(cmd.get("parameters", [])) > 4):
                name = cmd["parameters"][4]
                if name:
                    donor_speakers.append(name)
        for ps, ds in zip(proj_speakers, donor_speakers):
            if ps and ds and ps != ds:
                text_map[ps] = ds

        # Pair dialog/choice blocks via structural alignment
        items_donor = self._extract_structural_items(donor_cmds)
        items_proj = self._extract_structural_items(proj_cmds)

        pairs = self._pair_text_blocks(items_proj, items_donor)

        for p_item, d_item in pairs:
            if p_item[0] == "D" and d_item[0] == "D":
                p_text = p_item[1]
                d_text = d_item[1]
                if p_text and d_text and p_text != d_text:
                    text_map[p_text] = d_text
            elif p_item[0] == "C" and d_item[0] == "C":
                # Pair individual choices
                p_choices = p_item[1]
                d_choices = d_item[1]
                for pc, dc in zip(p_choices, d_choices):
                    if pc and dc and pc != dc:
                        text_map[pc] = dc

    # ── Private: apply translation back to JSON ────────────────────────

    @staticmethod
    def _translate_namebox(namebox: str, speaker_lookup: dict) -> str:
        """Translate the name inside a \\N<name> prefix for export.

        If the name is an actor code like \\n[1], keep as-is (game resolves
        at runtime).  If it's a literal name like 村人1, look it up in
        speaker_lookup and substitute the translated name.
        """
        m = _NAMEBOX_RE.match(namebox)
        if not m:
            return namebox
        inner = m.group(1)
        # Actor code references resolve at runtime — keep as-is
        if _ACTOR_CODE_RE.match(inner):
            return namebox
        # Literal name — translate if we have a translation
        translated = speaker_lookup.get(inner, inner)
        # Rebuild with same case as original (\N or \n)
        slash_n = namebox[0:2]  # e.g. \N or \n
        return f"{slash_n}<{translated}>"

    def _apply_translations_fast(self, data, entries: list,
                                global_speakers: dict = None):
        """Apply all translations for a file in a single pass.

        DB / System / displayName / plugin entries use direct indexed lookup
        (fast, O(1) per entry).  Dialog / scroll / choice entries are batched
        and applied in one walk of the event command lists — O(commands)
        instead of O(entries × commands).

        Args:
            global_speakers: Optional {original_name: translation} dict for
                101 speaker names, built globally in save_project().
        """
        from collections import deque

        scan_entries = []
        speaker_lookup = dict(global_speakers) if global_speakers else {}
        for entry in entries:
            if entry.field in ("dialog", "scroll_text", "choice"):
                scan_entries.append(entry)
            elif entry.field == "speaker_name":
                speaker_lookup[entry.original] = entry.translation
            else:
                # DB / System / plugin entries — already O(1)
                self._apply_translation(data, entry)

        if not scan_entries and not speaker_lookup:
            return

        # Build lookup dicts keyed by first original line
        dialog_lookup = {}   # first_line -> deque of (orig_lines, trans_lines)
        scroll_lookup = {}
        choice_lookup = {}   # original_text -> deque of translation_text

        for entry in scan_entries:
            orig_lines = entry.original.split("\n")
            trans_lines = entry.translation.split("\n")
            if entry.field in ("dialog", "scroll_text"):
                while len(trans_lines) < len(orig_lines):
                    trans_lines.append("")
                # Restore namebox prefix for export matching & output
                if entry.namebox:
                    # Translate the name inside the namebox for export
                    nb_translated = self._translate_namebox(
                        entry.namebox, speaker_lookup)
                    # Key must match raw 401 text (still has namebox)
                    orig_lines = orig_lines.copy()
                    orig_lines[0] = entry.namebox + orig_lines[0]
                    # Prepend translated namebox to first translation line
                    trans_lines = trans_lines.copy()
                    trans_lines[0] = nb_translated + trans_lines[0]
                # Allow extra lines — export inserts extra 401/405 commands
                first = orig_lines[0] if orig_lines else ""
                lookup = dialog_lookup if entry.field == "dialog" else scroll_lookup
                lookup.setdefault(first, deque()).append(
                    (orig_lines, trans_lines))
            elif entry.field == "choice":
                choice_lookup.setdefault(entry.original, deque()).append(
                    entry.translation)

        code_dialog = CODE_SHOW_TEXT
        code_scroll = CODE_SCROLL_TEXT
        code_choice = CODE_SHOW_CHOICES
        single_401 = self.single_401_mode

        code_header = CODE_SHOW_TEXT_HEADER

        def process_commands(cmd_list):
            i = 0
            while i < len(cmd_list):
                cmd = cmd_list[i]
                if not isinstance(cmd, dict):
                    i += 1
                    continue
                code = cmd.get("code", 0)

                # Speaker name in 101 header param[4]
                if code == code_header and speaker_lookup:
                    params = cmd.get("parameters", [])
                    if len(params) > 4 and params[4] in speaker_lookup:
                        params[4] = speaker_lookup[params[4]]
                    i += 1
                    continue

                # Dialog block (401)
                if code == code_dialog and dialog_lookup:
                    first_text = str(
                        (cmd.get("parameters") or [""])[0])
                    candidates = dialog_lookup.get(first_text)
                    if candidates:
                        applied = False
                        for idx, (ol, tl) in enumerate(candidates):
                            if i + len(ol) > len(cmd_list):
                                continue
                            match = True
                            for j, orig_line in enumerate(ol):
                                c = cmd_list[i + j]
                                if (not isinstance(c, dict)
                                        or c.get("code") != code_dialog):
                                    match = False
                                    break
                                ct = str((c.get("parameters") or [""])[0])
                                if ct != orig_line:
                                    match = False
                                    break
                            if match:
                                if single_401:
                                    # Merge all lines into first 401
                                    cmd_list[i]["parameters"][0] = "\n".join(tl)
                                    # Remove remaining original 401s
                                    del cmd_list[i + 1:i + len(ol)]
                                    advance = 1
                                else:
                                    # Replace text in existing 401 commands
                                    for j in range(len(ol)):
                                        cmd_list[i + j]["parameters"][0] = tl[j]
                                    # Insert extra 401 commands for overflow
                                    extra = tl[len(ol):]
                                    if extra:
                                        indent = cmd_list[i].get("indent", 0)
                                        ins = i + len(ol)
                                        for k, et in enumerate(extra):
                                            cmd_list.insert(ins + k, {
                                                "code": code_dialog,
                                                "indent": indent,
                                                "parameters": [et],
                                            })
                                    advance = len(ol) + len(extra)
                                del candidates[idx]
                                if not candidates:
                                    del dialog_lookup[first_text]
                                i += advance
                                applied = True
                                break
                        if not applied:
                            i += 1
                    else:
                        i += 1

                # Scroll text block (405)
                elif code == code_scroll and scroll_lookup:
                    first_text = str(
                        (cmd.get("parameters") or [""])[0])
                    candidates = scroll_lookup.get(first_text)
                    if candidates:
                        applied = False
                        for idx, (ol, tl) in enumerate(candidates):
                            if i + len(ol) > len(cmd_list):
                                continue
                            match = True
                            for j, orig_line in enumerate(ol):
                                c = cmd_list[i + j]
                                if (not isinstance(c, dict)
                                        or c.get("code") != code_scroll):
                                    match = False
                                    break
                                ct = str((c.get("parameters") or [""])[0])
                                if ct != orig_line:
                                    match = False
                                    break
                            if match:
                                if single_401:
                                    cmd_list[i]["parameters"][0] = "\n".join(tl)
                                    del cmd_list[i + 1:i + len(ol)]
                                    advance = 1
                                else:
                                    for j in range(len(ol)):
                                        cmd_list[i + j]["parameters"][0] = tl[j]
                                    extra = tl[len(ol):]
                                    if extra:
                                        indent = cmd_list[i].get("indent", 0)
                                        ins = i + len(ol)
                                        for k, et in enumerate(extra):
                                            cmd_list.insert(ins + k, {
                                                "code": code_scroll,
                                                "indent": indent,
                                                "parameters": [et],
                                            })
                                    advance = len(ol) + len(extra)
                                del candidates[idx]
                                if not candidates:
                                    del scroll_lookup[first_text]
                                i += advance
                                applied = True
                                break
                        if not applied:
                            i += 1
                    else:
                        i += 1

                # Choices (102)
                elif code == code_choice and choice_lookup:
                    params = cmd.get("parameters", [])
                    if params and isinstance(params[0], list):
                        for ci, ch in enumerate(params[0]):
                            if isinstance(ch, str) and ch in choice_lookup:
                                q = choice_lookup[ch]
                                params[0][ci] = q.popleft()
                                if not q:
                                    del choice_lookup[ch]
                    i += 1

                else:
                    i += 1

        # Walk all events / pages
        if isinstance(data, dict):
            for event in (data.get("events") or []):
                if not event or not isinstance(event, dict):
                    continue
                for page in (event.get("pages") or []):
                    if page and isinstance(page, dict):
                        process_commands(page.get("list", []))
            if "list" in data:
                process_commands(data.get("list", []))
        elif isinstance(data, list):
            for item in data:
                if not item or not isinstance(item, dict):
                    continue
                # CommonEvents — top-level "list"
                if item.get("list"):
                    process_commands(item.get("list", []))
                # Troops — pages with nested "list"
                for page in (item.get("pages") or []):
                    if page and isinstance(page, dict):
                        process_commands(page.get("list", []))

    def _apply_translation(self, data, entry: TranslationEntry):
        """Apply a single translation back into the loaded JSON data."""
        parts = entry.id.split("/")
        filename = parts[0]

        try:
            self._apply_translation_inner(data, entry, parts, filename)
        except (ValueError, IndexError, KeyError) as exc:
            log.warning("Export skip — malformed entry ID %r: %s", entry.id, exc)

    def _apply_translation_inner(self, data, entry, parts, filename):
        """Inner logic for _apply_translation (split out for safe int parsing)."""
        # Database entries: "Actors.json/1/name" (parts[1] must be numeric)
        if filename in DATABASE_FILES and len(parts) >= 3 and parts[1].isdigit():
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
            if entry.id == "System.json/gameTitle":
                data["gameTitle"] = entry.translation
            elif "terms/messages/" in entry.id:
                key = parts[-1]
                terms = data.get("terms", {})
                messages = terms.get("messages", {})
                if isinstance(messages, list):
                    idx = int(key)
                    if 0 <= idx < len(messages):
                        messages[idx] = entry.translation
                elif isinstance(messages, dict):
                    if key in messages:
                        messages[key] = entry.translation
            elif "terms/commands/" in entry.id:
                idx = int(parts[-1])
                terms = data.get("terms", {})
                commands = terms.get("commands", [])
                if 0 <= idx < len(commands):
                    commands[idx] = entry.translation
            elif "terms/params/" in entry.id:
                idx = int(parts[-1])
                terms = data.get("terms", {})
                params = terms.get("params", [])
                if 0 <= idx < len(params):
                    params[idx] = entry.translation
            elif "terms/basic/" in entry.id:
                idx = int(parts[-1])
                terms = data.get("terms", {})
                basic = terms.get("basic", [])
                if 0 <= idx < len(basic):
                    basic[idx] = entry.translation
            # Type arrays: elements, skillTypes, weaponTypes, armorTypes, equipTypes
            elif entry.field in ("elements", "skillTypes", "weaponTypes",
                                 "armorTypes", "equipTypes"):
                idx = int(parts[-1])
                arr = data.get(entry.field, [])
                if 0 <= idx < len(arr):
                    arr[idx] = entry.translation

        # Map displayName
        elif "displayName" in entry.id and entry.field == "displayName":
            data["displayName"] = entry.translation

        # Event dialogue — need to find and replace in command lists
        elif entry.field in ("dialog", "scroll_text", "choice"):
            self._apply_event_translation(data, entry)

        # Change Name/Nickname/Profile (320/324/325) — single parameter replacement
        elif entry.field in ("name", "nickname", "profile") and "/change_" in entry.id:
            code_map = {"name": CODE_CHANGE_NAME, "nickname": CODE_CHANGE_NICKNAME, "profile": CODE_CHANGE_PROFILE}
            self._replace_single_param(data, code_map[entry.field], 1, entry.original, entry.translation)

        # Plugin Command MV (356) — whitelist regex-based substitution
        elif entry.field == "plugin_command" and "/plugin_mv_" in entry.id:
            if entry.context and entry.context.startswith("[PLUGIN_CMD:"):
                # New whitelist format: context has full command, original is extracted text
                full_cmd = entry.context[len("[PLUGIN_CMD:"):-1]
                new_cmd = _substitute_mv_plugin_command(full_cmd, entry.original, entry.translation)
                if new_cmd:
                    self._replace_single_param(data, CODE_PLUGIN_COMMAND_MV, 0, full_cmd, new_cmd)
            else:
                # Legacy format: original is the full command string
                self._replace_single_param(data, CODE_PLUGIN_COMMAND_MV, 0,
                                           entry.original, entry.translation)

        # Plugin Command MZ (357) — whitelist-based parameter substitution
        elif entry.field == "plugin_command" and "/plugin_mz_" in entry.id:
            parts_id = entry.id.split("/")
            # Detect format: new has .../pluginName/paramKey, legacy has _pX suffix
            last_part = parts_id[-1]
            if "_p" in last_part and last_part.startswith("plugin_mz_"):
                # Legacy format: plugin_mz_N_pX
                pi = int(last_part.rsplit("_p", 1)[-1])
                self._replace_single_param(data, CODE_PLUGIN_COMMAND_MZ, pi,
                                           entry.original, entry.translation)
            elif len(parts_id) >= 2:
                # New whitelist format: .../plugin_mz_N/PluginName/paramKey
                param_key = parts_id[-1]
                plugin_name = parts_id[-2]
                self._replace_mz_plugin_param(data, plugin_name, param_key,
                                              entry.original, entry.translation)

        # Script variable — Control Variables (122) or Script (355/655)
        elif entry.field == "script_variable" and "/script_var_" in entry.id:
            if entry.context and entry.context.startswith("[CONTROL_VAR:"):
                # Code 122: params[4] = '"original"' → replace with '"translation"'
                self._replace_control_var_string(data, entry.original, entry.translation)
            elif entry.context and entry.context.startswith("[SCRIPT_VAR:"):
                # Code 355/655: inline string replacement
                self._replace_script_string(data, entry.original, entry.translation)

    def _apply_event_translation(self, data, entry: TranslationEntry):
        """Apply event dialogue/choice translation back into map or common event data."""
        original_lines = entry.original.split("\n")
        translation_lines = entry.translation.split("\n")

        # Pad translation if shorter; allow longer for extra 401/405 insertion
        if entry.field in ("dialog", "scroll_text"):
            while len(translation_lines) < len(original_lines):
                translation_lines.append("")

        if entry.field == "choice":
            # Choices: find in event commands with code 102
            self._replace_in_commands(data, CODE_SHOW_CHOICES, entry.original, entry.translation, is_choice=True)
        elif entry.field == "dialog":
            self._replace_dialog_block(data, original_lines, translation_lines)
        elif entry.field == "scroll_text":
            self._replace_dialog_block(data, original_lines, translation_lines, code=CODE_SCROLL_TEXT)

    @staticmethod
    def _walk_event_commands(data, callback) -> bool:
        """Walk all event command lists in *data* and call *callback(cmd_list)*.

        Handles Map (dict with events→pages), CommonEvents (list with list key),
        and Troops (list with pages key).  Returns True on first callback match.
        """
        if isinstance(data, dict):
            for event in (data.get("events") or []):
                if not event or not isinstance(event, dict):
                    continue
                for page in (event.get("pages") or []):
                    if page and isinstance(page, dict):
                        if callback(page.get("list", [])):
                            return True
            if "list" in data:
                if callback(data.get("list", [])):
                    return True
        elif isinstance(data, list):
            for item in data:
                if not item or not isinstance(item, dict):
                    continue
                if callback(item.get("list", [])):
                    return True
                for page in (item.get("pages") or []):
                    if page and isinstance(page, dict):
                        if callback(page.get("list", [])):
                            return True
        return False

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
                    # Replace text in existing commands
                    for j in range(len(original_lines)):
                        cmd_list[i + j]["parameters"][0] = translation_lines[j]
                    # Insert extra commands for overflow lines
                    extra = translation_lines[len(original_lines):]
                    if extra:
                        indent = cmd_list[i].get("indent", 0)
                        ins = i + len(original_lines)
                        for k, et in enumerate(extra):
                            cmd_list.insert(ins + k, {
                                "code": code, "indent": indent,
                                "parameters": [et],
                            })
                    return True
                i += 1
            return False

        if self._walk_event_commands(data, process_commands):
            return
        log.warning("Export: dialog block not found — original starts with %r",
                    original_lines[0][:60] if original_lines else "?")

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

        if self._walk_event_commands(data, process_commands):
            return
        log.warning("Export: command code %d not matched — original %r",
                    code, original[:60])

    def _replace_single_param(self, data, code: int, param_idx: int,
                              original: str, translation: str):
        """Replace a single parameter value in event commands matching the given code."""
        def process_commands(cmd_list):
            for cmd in cmd_list:
                if not isinstance(cmd, dict) or cmd.get("code") != code:
                    continue
                params = cmd.get("parameters", [])
                if len(params) > param_idx and params[param_idx] == original:
                    params[param_idx] = translation
                    return True
            return False

        if self._walk_event_commands(data, process_commands):
            return
        log.warning("Export: single param code %d[%d] not matched — original %r",
                    code, param_idx, original[:60])

    def _replace_mz_plugin_param(self, data, plugin_name: str, param_key: str,
                                   original: str, translation: str):
        """Replace a specific parameter in an MZ plugin command's JSON args.

        Finds code 357 commands where params[0] matches plugin_name, parses
        the JSON dict in params[3], replaces the value at param_key, and
        re-serializes.
        """
        def process_commands(cmd_list):
            for cmd in cmd_list:
                if not isinstance(cmd, dict) or cmd.get("code") != CODE_PLUGIN_COMMAND_MZ:
                    continue
                params = cmd.get("parameters", [])
                if len(params) < 4 or params[0] != plugin_name:
                    continue
                arg_str = params[3] if isinstance(params[3], str) else ""
                if not arg_str:
                    continue
                try:
                    arg_dict = json.loads(arg_str)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(arg_dict, dict):
                    continue
                if arg_dict.get(param_key) == original:
                    arg_dict[param_key] = translation
                    params[3] = json.dumps(arg_dict, ensure_ascii=False)
                    return True
            return False

        if self._walk_event_commands(data, process_commands):
            return
        log.warning("Export: MZ plugin %s/%s not matched — original %r",
                    plugin_name, param_key, original[:60])

    def _replace_control_var_string(self, data, original: str, translation: str):
        """Replace a string literal in Control Variables (code 122) script operand.

        Finds code 122 commands where params[3]==4 (script operand) and
        params[4] contains the original text as a quoted string literal,
        then replaces with the translated text.
        """
        old_expr = json.dumps(original)   # proper escaping of quotes/backslashes
        new_expr = json.dumps(translation)

        def process_commands(cmd_list):
            for cmd in cmd_list:
                if not isinstance(cmd, dict) or cmd.get("code") != CODE_CONTROL_VARIABLES:
                    continue
                params = cmd.get("parameters", [])
                if len(params) >= 5 and params[3] == 4 and params[4] == old_expr:
                    params[4] = new_expr
                    return True
            return False

        if self._walk_event_commands(data, process_commands):
            return
        log.warning("Export: control var string not matched — original %r",
                    original[:60])

    def _replace_script_string(self, data, original: str, translation: str):
        """Replace a Japanese string inside Script commands (355/655).

        Finds the original string literal in concatenated 355+655 script lines
        and replaces it in-place, preserving surrounding JS code.
        """
        def process_commands(cmd_list):
            i = 0
            while i < len(cmd_list):
                cmd = cmd_list[i]
                if not isinstance(cmd, dict) or cmd.get("code") != CODE_SCRIPT:
                    i += 1
                    continue
                # Collect 355 + following 655 lines
                script_cmds = [cmd]
                j = i + 1
                while j < len(cmd_list):
                    c = cmd_list[j]
                    if isinstance(c, dict) and c.get("code") == CODE_SCRIPT_CONT:
                        script_cmds.append(c)
                        j += 1
                    else:
                        break
                # Check if any line contains the original string
                for sc in script_cmds:
                    p = sc.get("parameters", [""])
                    line = p[0] if p and isinstance(p[0], str) else ""
                    if original in line:
                        p[0] = line.replace(original, translation, 1)
                        return True
                i = j
            return False

        if self._walk_event_commands(data, process_commands):
            return
        log.warning("Export: script string not matched — original %r",
                    original[:60])

    # ── plugins.js extraction & export ─────────────────────────────

    @staticmethod
    def _find_plugins_file(project_dir: str) -> Optional[str]:
        """Locate js/plugins.js in the project."""
        candidates = [
            os.path.join(project_dir, "js", "plugins.js"),
            os.path.join(project_dir, "www", "js", "plugins.js"),
        ]
        for path in candidates:
            if os.path.isfile(path):
                return path
        return None

    @staticmethod
    def _load_plugins_js(path: str) -> list:
        """Parse plugins.js into a Python list of plugin dicts."""
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        # Match the array assigned to $plugins — greedy so nested ] in
        # JSON strings don't cause premature truncation
        m = re.search(r'var\s+\$plugins\s*=\s*(\[.*\])\s*;', content, re.DOTALL)
        if not m:
            # Fallback: greedy match of first complete JSON array
            m = re.search(r'(\[.*\])\s*;', content, re.DOTALL)
        if not m:
            return []
        return json.loads(m.group(1))

    @staticmethod
    def _write_plugins_js(path: str, plugins: list):
        """Write plugin list back to plugins.js format."""
        json_str = json.dumps(plugins, ensure_ascii=False, indent=2)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"var $plugins =\n{json_str};\n")

    @staticmethod
    def _backup_plugins_file(path: str):
        """Copy plugins.js → plugins_original.js if no backup exists."""
        backup = os.path.join(os.path.dirname(path),
                              os.path.basename(path).replace("plugins.", "plugins_original."))
        if not os.path.exists(backup):
            shutil.copy2(path, backup)

    # ── Plugin diff (scan hand-edits) ────────────────────────────

    def diff_plugins(self, project_dir: str,
                     other_path: str = "") -> list:
        """Compare two plugins.js files and auto-detect which is JP/EN.

        Compares the project's plugins.js against *other_path* (or the
        plugins_original.js backup).  Automatically determines which
        side contains Japanese text and assigns original/translation
        accordingly.

        Args:
            project_dir: Game project folder (contains js/plugins.js).
            other_path: Path to the other plugins.js to compare.
                If empty, auto-detects plugins_original.js as the backup.

        Returns list of tuples:
            (entry_id, plugin_name, param_label, original_jp, translated_en)
        where entry_id matches the format used by _save_plugins() for export.
        """
        project_path = self._find_plugins_file(project_dir)
        if not project_path:
            return []

        if not other_path:
            # Auto-detect: plugins_original.js backup
            backup = os.path.join(
                os.path.dirname(project_path),
                os.path.basename(project_path).replace(
                    "plugins.", "plugins_original."),
            )
            if os.path.isfile(backup):
                other_path = backup
            else:
                return []
        if not os.path.isfile(other_path):
            return []

        try:
            project_plugins = self._load_plugins_js(project_path)
            other_plugins = self._load_plugins_js(other_path)
        except (json.JSONDecodeError, OSError):
            return []

        # Index by plugin name
        proj_by_name = {}
        for p in project_plugins:
            if isinstance(p, dict) and p.get("name"):
                proj_by_name[p["name"]] = p.get("parameters", {})

        other_by_name = {}
        for p in other_plugins:
            if isinstance(p, dict) and p.get("name"):
                other_by_name[p["name"]] = p.get("parameters", {})

        # Collect raw diffs as (id, plugin, label, project_text, other_text)
        raw_diffs = []
        for name in proj_by_name:
            if name not in other_by_name:
                continue
            proj_params = proj_by_name[name]
            other_params = other_by_name[name]
            for key in proj_params:
                if key not in other_params:
                    continue
                pv, ov = proj_params[key], other_params[key]
                if pv == ov:
                    continue
                prefix = f"plugins.js/{name}/{key}"
                self._diff_values(pv, ov, prefix, name, key, raw_diffs)

        if not raw_diffs:
            return []

        # Auto-detect direction: count Japanese on each side
        proj_jp = sum(1 for _, _, _, p, _ in raw_diffs if _has_japanese(p))
        other_jp = sum(1 for _, _, _, _, o in raw_diffs if _has_japanese(o))

        if other_jp >= proj_jp:
            # Other file has more JP → other = original, project = translated
            return [(eid, pn, pl, other_t, proj_t)
                    for eid, pn, pl, proj_t, other_t in raw_diffs]
        else:
            # Project file has more JP → project = original, other = translated
            return raw_diffs

    def _diff_values(self, orig, curr, id_prefix: str,
                     plugin_name: str, param_label: str, out: list):
        """Recursively diff two parameter values, appending to *out*.

        Handles plain strings and JSON-encoded nested structures.
        """
        # Both are simple strings — leaf diff
        if isinstance(orig, str) and isinstance(curr, str):
            # Try parsing as JSON for nested structures
            try:
                o_parsed = json.loads(orig)
                c_parsed = json.loads(curr)
                # Both parsed — walk recursively
                self._diff_parsed(o_parsed, c_parsed, id_prefix,
                                  plugin_name, param_label, out)
                return
            except (json.JSONDecodeError, ValueError):
                pass
            # Plain string diff
            if orig != curr and orig.strip():
                out.append((id_prefix, plugin_name, param_label, orig, curr))
            return

        # Already-parsed structures (shouldn't happen at top level, but be safe)
        if type(orig) == type(curr):
            self._diff_parsed(orig, curr, id_prefix,
                              plugin_name, param_label, out)

    def _diff_parsed(self, orig, curr, id_prefix: str,
                     plugin_name: str, param_label: str, out: list):
        """Walk parsed JSON structures and report leaf-level string diffs."""
        if isinstance(orig, list) and isinstance(curr, list):
            for i in range(min(len(orig), len(curr))):
                self._diff_values(orig[i], curr[i], f"{id_prefix}/[{i}]",
                                  plugin_name, param_label, out)
        elif isinstance(orig, dict) and isinstance(curr, dict):
            for key in orig:
                if key in curr:
                    self._diff_values(orig[key], curr[key],
                                      f"{id_prefix}/{key}",
                                      plugin_name, param_label, out)
        elif isinstance(orig, str) and isinstance(curr, str):
            if orig != curr and orig.strip():
                out.append((id_prefix, plugin_name, param_label, orig, curr))

    # Keys whose values are asset filenames / internal IDs — never translate.
    _PLUGIN_ASSET_KEY_RE = re.compile(
        r'(?:image|Image|pic(?:Name|ture)|BGM|BGS|SE |Sound|Skin|Windowskin'
        r'|Skeleton|Background Image|Back Image|Joker Image'
        r'|Spade|Club|Heart|Diamond|json file'
        r'|picOrigin|picX|picY|picOpacity|picZoom|picShow'
        r'|\.png|\.ogg|\.rpgmvp'
        r'|Button|Key$|triggerKey|triggerButton|SkipKey|Skip Key'
        r'|Help Commands|Command List)',
        re.IGNORECASE,
    )

    # Section header markers used by plugins as visual dividers.
    _PLUGIN_SECTION_RE = re.compile(r'^#{2,}[^#].*#{2,}$')

    # Looks like an asset filename: only word chars, underscores, %, digits —
    # no spaces, no Japanese particles/punctuation.
    _PLUGIN_ASSET_VALUE_RE = re.compile(
        r'^[\w%.\-/\\]+$', re.ASCII,
    )

    # Stricter Japanese check: actual hiragana/katakana/kanji required.
    # Excludes fullwidth Latin (ａ-ｚ) which JP_REGEX matches but isn't JP text.
    _JP_DISPLAY_RE = re.compile(
        r'[\u3040-\u309F'    # Hiragana
        r'\u30A0-\u30FF'     # Katakana
        r'\u4E00-\u9FFF'     # CJK kanji
        r'\u3400-\u4DBF]',   # CJK Extension A
    )

    # JavaScript code embedded in plugin parameters (SceneCustomMenu, etc.).
    _JS_CODE_RE = re.compile(
        r';\s*//'            # statement; // comment
        r'|^\s*\$(?:game|data)'  # $gameParty, $dataSystem, etc.
        r'|^\s*this[\._]'        # this.method() or this._property
        r'|^\s*\[this[\._]'      # [this._actor]
        r'|^\s*function\s'       # function keyword
    )

    # ID path segments that indicate audio/sound asset containers.
    _PLUGIN_AUDIO_ID_RE = re.compile(
        r'BgsSettings|BgmSettings|SeSettings|AudioManager',
        re.IGNORECASE,
    )

    def _parse_plugins(self, project_dir: str) -> list:
        """Extract translatable Japanese text from plugins.js parameters.

        Uses a conservative filter: only values containing Japanese characters
        are extracted, and asset filenames / internal IDs are skipped via key
        name patterns and value heuristics.
        """
        plugins_path = self._find_plugins_file(project_dir)
        if not plugins_path:
            return []

        # Read from backup (original JP) when available for idempotent re-load
        backup = os.path.join(
            os.path.dirname(plugins_path),
            os.path.basename(plugins_path).replace("plugins.", "plugins_original."),
        )
        source = backup if os.path.exists(backup) else plugins_path

        try:
            plugins = self._load_plugins_js(source)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Failed to load plugins.js for extraction: %s", exc)
            return []

        entries = []
        for plugin in plugins:
            if not isinstance(plugin, dict):
                continue
            name = plugin.get("name", "")
            if not name or name.startswith("---"):
                continue  # separator row
            params = plugin.get("parameters", {})
            if not isinstance(params, dict):
                continue

            for key, value in params.items():
                if not isinstance(value, str) or not value.strip():
                    continue
                entry_id = f"plugins.js/{name}/{key}"
                self._scan_plugin_value(
                    value, key, entry_id, name, entries,
                )

        log.info("Extracted %d translatable plugin parameters", len(entries))
        return entries

    def _scan_plugin_value(self, value: str, key: str, id_prefix: str,
                           plugin_name: str, out: list):
        """Recursively scan a plugin parameter value for translatable text.

        Handles plain strings and JSON-encoded nested arrays/objects.
        Creates TranslationEntry items for any Japanese display text found.
        """
        # Try JSON parse for nested structures
        try:
            parsed = json.loads(value)
            if isinstance(parsed, (list, dict)):
                self._scan_parsed_plugin(parsed, key, id_prefix,
                                         plugin_name, out)
                return
            # JSON decoded to a scalar (string, number) — fall through
            if not isinstance(parsed, str):
                return
            # It was a JSON string literal — use the decoded value
            value = parsed
        except (json.JSONDecodeError, ValueError):
            pass

        # Plain string — check if translatable
        if self._is_translatable_plugin_value(value, key, id_prefix):
            out.append(TranslationEntry(
                id=id_prefix,
                file="plugins.js",
                field=f"{plugin_name}/{key}",
                original=value,
            ))

    def _scan_parsed_plugin(self, obj, key: str, id_prefix: str,
                            plugin_name: str, out: list):
        """Walk a parsed JSON structure from a plugin parameter."""
        if isinstance(obj, list):
            for i, item in enumerate(obj):
                child_id = f"{id_prefix}/[{i}]"
                if isinstance(item, str):
                    # May be another JSON-encoded string
                    self._scan_plugin_value(item, key, child_id,
                                            plugin_name, out)
                elif isinstance(item, dict):
                    self._scan_parsed_plugin(item, key, child_id,
                                             plugin_name, out)
                elif isinstance(item, list):
                    self._scan_parsed_plugin(item, key, child_id,
                                             plugin_name, out)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                child_id = f"{id_prefix}/{k}"
                if isinstance(v, str):
                    self._scan_plugin_value(v, k, child_id,
                                            plugin_name, out)
                elif isinstance(v, (list, dict)):
                    self._scan_parsed_plugin(v, k, child_id,
                                             plugin_name, out)

    def _is_translatable_plugin_value(self, value: str, key: str,
                                       entry_id: str = "") -> bool:
        """Decide if a plugin parameter value is translatable display text."""
        value = value.strip()
        if not value:
            return False
        # Must contain actual Japanese (hiragana/katakana/kanji),
        # not just fullwidth Latin like ｐ
        if not self._JP_DISPLAY_RE.search(value):
            return False
        # Skip section headers (#### ピクチャ1 ####)
        if self._PLUGIN_SECTION_RE.match(value):
            return False
        # Skip if the key name indicates an asset reference
        if self._PLUGIN_ASSET_KEY_RE.search(key):
            return False
        # Skip JavaScript code embedded in plugin parameters
        if self._JS_CODE_RE.search(value):
            return False
        # Skip audio/sound asset containers
        if entry_id and self._PLUGIN_AUDIO_ID_RE.search(entry_id):
            return False
        # Skip values that look like filenames (ASCII-only word chars + _ / %)
        # but only if they're short — long Japanese sentences are never filenames
        if len(value) < 30 and self._PLUGIN_ASSET_VALUE_RE.match(value):
            return False
        return True

    def _save_plugins(self, project_dir: str, entries: list):
        """Write translated plugin parameter values back into plugins.js."""
        # Filter to only plugin entries with translations
        plugin_entries = [
            e for e in entries
            if e.file == "plugins.js"
            and e.translation
            and e.status in ("translated", "reviewed")
        ]
        if not plugin_entries:
            return

        plugins_path = self._find_plugins_file(project_dir)
        if not plugins_path:
            return

        # Backup before first modification
        self._backup_plugins_file(plugins_path)

        # Always read from backup (original Japanese) so re-exports work
        backup_path = os.path.join(
            os.path.dirname(plugins_path),
            os.path.basename(plugins_path).replace("plugins.", "plugins_original."),
        )
        source_path = backup_path if os.path.exists(backup_path) else plugins_path
        try:
            plugins = self._load_plugins_js(source_path)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Export: failed to load plugins.js from %s: %s", source_path, exc)
            return

        # Build lookup: plugin_name → {param_key → plugin_dict}
        plugin_by_name = {}
        for p in plugins:
            if isinstance(p, dict) and p.get("name"):
                plugin_by_name[p["name"]] = p

        for entry in plugin_entries:
            # Parse ID: plugins.js/PluginName/ParamKey[/nested/path...]
            parts = entry.id.split("/")
            if len(parts) < 3:
                continue
            plugin_name = parts[1]
            param_key = parts[2]
            nested_path = parts[3:]  # may be empty

            plugin = plugin_by_name.get(plugin_name)
            if not plugin:
                continue
            params = plugin.get("parameters", {})
            if param_key not in params:
                continue

            if not nested_path:
                # Check if original value was a JSON-encoded string scalar
                raw_val = params[param_key]
                try:
                    decoded = json.loads(raw_val)
                    if isinstance(decoded, str):
                        # Re-encode to preserve JSON string wrapping
                        params[param_key] = json.dumps(entry.translation, ensure_ascii=False)
                        continue
                except (json.JSONDecodeError, ValueError):
                    pass
                # Plain string replacement
                params[param_key] = entry.translation
            else:
                # Nested: parse JSON, navigate path, replace, re-serialize
                try:
                    parsed = json.loads(params[param_key])
                    self._set_nested_value(parsed, nested_path, entry.original,
                                           entry.translation)
                    params[param_key] = json.dumps(parsed, ensure_ascii=False)
                except (json.JSONDecodeError, ValueError):
                    continue

        self._write_plugins_js(plugins_path, plugins)

    def _set_nested_value(self, obj, path: list, original: str, translation: str):
        """Navigate a parsed JSON structure by path segments and replace a value.

        Path segments: "[0]" for array indices, "key" for object keys.
        Recursive so that intermediate JSON-encoded strings are re-serialized
        after modification (prevents [object Object] bugs in RPG Maker).
        """
        if not path:
            return
        segment = path[0]
        is_last = len(path) == 1

        if segment.startswith("[") and segment.endswith("]"):
            idx = int(segment[1:-1])
            if not isinstance(obj, list) or idx >= len(obj):
                return
            if is_last:
                if isinstance(obj[idx], str) and obj[idx] == original:
                    obj[idx] = translation
            else:
                val = obj[idx]
                was_string = isinstance(val, str)
                if was_string:
                    try:
                        val = json.loads(val)
                    except (json.JSONDecodeError, ValueError):
                        return
                self._set_nested_value(val, path[1:], original, translation)
                if was_string:
                    obj[idx] = json.dumps(val, ensure_ascii=False)
        else:
            if not isinstance(obj, dict) or segment not in obj:
                return
            if is_last:
                if isinstance(obj[segment], str) and obj[segment] == original:
                    obj[segment] = translation
            else:
                val = obj[segment]
                was_string = isinstance(val, str)
                if was_string:
                    try:
                        val = json.loads(val)
                    except (json.JSONDecodeError, ValueError):
                        return
                self._set_nested_value(val, path[1:], original, translation)
                if was_string:
                    obj[segment] = json.dumps(val, ensure_ascii=False)

    # ── Word wrap plugin injection ─────────────────────────────────

    INJECTED_PLUGIN_NAME = "TranslatorWordWrap"

    def inject_wordwrap_plugin(self, project_dir: str) -> bool:
        """Write TranslatorWordWrap.js and register it in plugins.js.

        Called during export when no existing word wrap plugin was detected
        and the user chose to inject one.

        Returns True if injection succeeded, False otherwise.
        """
        from .text_processor import WORDWRAP_PLUGIN_JS

        plugins_path = self._find_plugins_file(project_dir)
        if not plugins_path:
            log.warning("inject_wordwrap_plugin: plugins.js not found in %s", project_dir)
            return False

        # Write the JS file next to plugins.js (js/plugins/ folder)
        js_dir = os.path.dirname(plugins_path)
        plugins_dir = os.path.join(js_dir, "plugins")
        os.makedirs(plugins_dir, exist_ok=True)
        js_path = os.path.join(plugins_dir, f"{self.INJECTED_PLUGIN_NAME}.js")
        with open(js_path, "w", encoding="utf-8") as f:
            f.write(WORDWRAP_PLUGIN_JS.strip() + "\n")

        # Read from the LIVE plugins.js (not backup) because
        # save_project may have already written translated plugin
        # params to it — reading from backup would overwrite those.
        try:
            plugins = self._load_plugins_js(plugins_path)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("inject_wordwrap_plugin: failed to parse %s: %s", plugins_path, e)
            return False

        # Don't duplicate if already present
        if any(p.get("name") == self.INJECTED_PLUGIN_NAME for p in plugins):
            log.info("inject_wordwrap_plugin: already present in plugins.js")
            return True

        plugins.append({
            "name": self.INJECTED_PLUGIN_NAME,
            "status": True,
            "description": "Word wrap for translated text (auto-injected)",
            "parameters": {},
        })
        self._write_plugins_js(plugins_path, plugins)
        log.info("inject_wordwrap_plugin: added to %s (%d plugins total)",
                 plugins_path, len(plugins))
        return True

    def remove_wordwrap_plugin(self, project_dir: str):
        """Remove the injected word wrap plugin (cleanup during restore)."""
        plugins_path = self._find_plugins_file(project_dir)
        if not plugins_path:
            return

        # Remove JS file
        js_dir = os.path.dirname(plugins_path)
        js_path = os.path.join(js_dir, "plugins", f"{self.INJECTED_PLUGIN_NAME}.js")
        if os.path.isfile(js_path):
            os.remove(js_path)
