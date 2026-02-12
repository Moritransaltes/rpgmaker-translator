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
    r'女|姫|嬢|娘|母|姉|妹|妻|彼女|お姉|おかあ|少女|王女|巫女|メイド|'
    r'actress|female|girl|woman|princess|queen|lady|witch|priestess|maid',
    re.IGNORECASE
)
_MALE_HINTS = re.compile(
    r'男|王子|父|兄|弟|夫|彼|息子|少年|勇者|騎士|おとうさん|'
    r'actor|male|boy|man|prince|king|knight|hero|lord',
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
        entries.extend(self._parse_plugins(project_dir))
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

        # Group entries by file
        by_file = {}
        for e in entries:
            if e.translation and e.status in ("translated", "reviewed"):
                by_file.setdefault(e.file, []).append(e)

        for filename, file_entries in by_file.items():
            source_path = os.path.join(source_dir, filename)
            if not os.path.exists(source_path):
                continue

            with open(source_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            for entry in file_entries:
                self._apply_translation(data, entry)

            # Always write to the live data/ directory
            out_path = os.path.join(data_dir, filename)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

        # Export plugin translations (plugins.js is outside data/)
        self._save_plugins(project_dir, entries)

    def export_patch_zip(self, project_dir: str, entries: list,
                         zip_path: str, game_title: str = "",
                         inject_wordwrap: bool = False):
        """Export translated game files as a ready-to-install zip.

        Zip layout::

            _translation/
                data/Actors.json
                data/Map001.json
                ...
                js/plugins.js       (if applicable)
            install.bat             (backs up originals, copies translations)
            uninstall.bat           (restores originals from backup)
            README.txt

        End users extract into the game folder and run install.bat.
        """
        data_dir = self._find_data_dir(project_dir)
        if not data_dir:
            raise FileNotFoundError("Could not find data directory in project")

        # Use backup (original JP) as source, same as save_project
        backup_dir = data_dir + "_original"
        source_dir = backup_dir if os.path.isdir(backup_dir) else data_dir

        # Relative path from project root to data dir (e.g. "data" or "www/data")
        data_rel = os.path.relpath(data_dir, project_dir).replace("\\", "/")

        # Group translated entries by file
        by_file = {}
        for e in entries:
            if e.translation and e.status in ("translated", "reviewed"):
                by_file.setdefault(e.file, []).append(e)

        # Determine js/ relative path for plugins
        js_rel = None
        plugins_path = self._find_plugins_file(project_dir)
        if plugins_path:
            js_rel = os.path.relpath(
                os.path.dirname(plugins_path), project_dir
            ).replace("\\", "/")  # e.g. "js" or "www/js"

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # Write translated data JSON files into _translation/data/
            for filename, file_entries in by_file.items():
                if filename == "plugins.js":
                    continue
                source_path = os.path.join(source_dir, filename)
                if not os.path.exists(source_path):
                    continue
                with open(source_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for entry in file_entries:
                    self._apply_translation(data, entry)
                arc_path = f"_translation/{data_rel}/{filename}"
                zf.writestr(arc_path,
                            json.dumps(data, ensure_ascii=False, indent=2))

            # Write translated plugins.js into _translation/js/
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

                    # Inject word wrap plugin entry if requested
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

            # Include word wrap JS plugin file in zip
            if inject_wordwrap and js_rel:
                from .text_processor import WORDWRAP_PLUGIN_JS
                arc = f"_translation/{js_rel}/plugins/{self.INJECTED_PLUGIN_NAME}.js"
                zf.writestr(arc, WORDWRAP_PLUGIN_JS.strip() + "\n")

            # Count files
            data_files = [f for f in by_file if f != "plugins.js"]
            total_entries = sum(len(v) for v in by_file.values())

            # install.bat
            zf.writestr("install.bat", self._build_install_bat(
                data_rel, js_rel, data_files, has_plugins, game_title,
                inject_wordwrap=inject_wordwrap))

            # uninstall.bat
            zf.writestr("uninstall.bat", self._build_uninstall_bat(
                data_rel, js_rel, has_plugins, game_title,
                inject_wordwrap=inject_wordwrap))

            # README.txt
            readme = (
                f"English Translation — {game_title or 'RPG Maker Game'}\n"
                f"{'=' * 50}\n\n"
                f"Files: {len(data_files)} data file(s)"
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

    @staticmethod
    def _build_install_bat(data_rel: str, js_rel: str,
                           data_files: list, has_plugins: bool,
                           game_title: str,
                           inject_wordwrap: bool = False) -> str:
        """Generate install.bat: back up originals, copy translations from _translation/."""
        dr = data_rel.replace("/", "\\")           # e.g. "data" or "www\\data"
        tr = f"_translation\\{dr}"                  # e.g. "_translation\\data"
        n = len(data_files)
        lines = [
            "@echo off",
            "chcp 65001 >nul 2>&1",
            f"title Install English Translation",
            "echo.",
            f"echo  {game_title or 'RPG Maker Game'} — English Translation",
            "echo.",
            f"echo  This will install the English translation ({n} files).",
            "echo  Original files will be backed up automatically.",
            "echo.",
            "pause",
            "",
            # Sanity check
            f'if not exist "{dr}" (',
            f'    echo ERROR: "{dr}\\" folder not found.',
            "    echo Make sure you extracted this zip into the game folder",
            "    echo  ^(the folder containing Game.exe^).",
            "    pause",
            "    exit /b 1",
            ")",
            f'if not exist "{tr}" (',
            "    echo ERROR: _translation folder not found.",
            "    echo Make sure you extracted the FULL zip, not just install.bat.",
            "    pause",
            "    exit /b 1",
            ")",
            "",
            # Step 1: Back up originals
            f'echo [Step 1] Backing up original files...',
            f'if not exist "{dr}_original\\" (',
            f'    xcopy "{dr}" "{dr}_original\\" /E /I /Q /Y >nul',
            "    echo   Created backup: %s_original\\" % dr,
            ") else (",
            f'    echo   Backup already exists ({dr}_original\\), skipping.',
            ")",
            "",
        ]

        if has_plugins and js_rel:
            jr = js_rel.replace("/", "\\")
            lines += [
                f'if exist "{jr}\\plugins.js" if not exist "{jr}\\plugins_original.js" (',
                f'    copy "{jr}\\plugins.js" "{jr}\\plugins_original.js" >nul',
                f"    echo   Backed up {jr}\\plugins.js",
                ")",
                "",
            ]

        # Step 2: Copy translations
        lines += [
            f'echo [Step 2] Installing translated files...',
            f'xcopy "{tr}" "{dr}" /E /I /Q /Y >nul',
            f"echo   Copied {n} file(s) to {dr}\\",
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

        lines += [
            "",
            "echo.",
            f"echo  Installation complete!",
            "echo  To restore Japanese originals, run uninstall.bat",
            "echo.",
            "pause",
        ]
        return "\r\n".join(lines) + "\r\n"

    @staticmethod
    def _build_uninstall_bat(data_rel: str, js_rel: str,
                             has_plugins: bool, game_title: str,
                             inject_wordwrap: bool = False) -> str:
        """Generate uninstall.bat: restore originals from backup."""
        dr = data_rel.replace("/", "\\")
        lines = [
            "@echo off",
            "chcp 65001 >nul 2>&1",
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
            "    exit /b 1",
            ")",
            "",
            f'echo Restoring {dr}\\ from {dr}_original\\...',
            f'xcopy "{dr}_original" "{dr}\\" /E /I /Q /Y >nul',
            "echo   Data files restored.",
        ]

        if has_plugins and js_rel:
            jr = js_rel.replace("/", "\\")
            lines += [
                f'if exist "{jr}\\plugins_original.js" (',
                f'    copy /Y "{jr}\\plugins_original.js" "{jr}\\plugins.js" >nul',
                "    echo   plugins.js restored.",
                ")",
            ]

        if inject_wordwrap and js_rel:
            jr = js_rel.replace("/", "\\")
            lines += [
                f'if exist "{jr}\\plugins\\TranslatorWordWrap.js" (',
                f'    del "{jr}\\plugins\\TranslatorWordWrap.js"',
                "    echo   Removed word wrap plugin.",
                ")",
            ]

        lines += [
            "",
            "echo.",
            "echo  Done! Original Japanese files restored.",
            "echo.",
            "pause",
        ]
        return "\r\n".join(lines) + "\r\n"

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

            # Change Actor Name / Nickname / Profile (320, 324, 325)
            if code in (CODE_CHANGE_NAME, CODE_CHANGE_NICKNAME, CODE_CHANGE_PROFILE):
                text = params[1] if len(params) > 1 else ""
                field_map = {
                    CODE_CHANGE_NAME: "name",
                    CODE_CHANGE_NICKNAME: "nickname",
                    CODE_CHANGE_PROFILE: "profile",
                }
                fld = field_map[code]
                if isinstance(text, str) and _is_translatable(text):
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

            i += 1

        return entries

    # ── Private: apply translation back to JSON ────────────────────────

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

    def _apply_event_translation(self, data, entry: TranslationEntry):
        """Apply event dialogue/choice translation back into map or common event data."""
        original_lines = entry.original.split("\n")
        translation_lines = entry.translation.split("\n")

        # Pad or trim translation to match original line count for dialogue/scroll
        if entry.field in ("dialog", "scroll_text"):
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
                if process_commands(data.get("list", [])):
                    return
        elif isinstance(data, list):
            # CommonEvents.json is a list
            for event in data:
                if event and isinstance(event, dict):
                    if process_commands(event.get("list", [])):
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

        if isinstance(data, dict):
            for event in (data.get("events") or []):
                if not event or not isinstance(event, dict):
                    continue
                for page in (event.get("pages") or []):
                    if page and isinstance(page, dict):
                        if process_commands(page.get("list", [])):
                            return
            if "list" in data:
                if process_commands(data.get("list", [])):
                    return
        elif isinstance(data, list):
            for event in data:
                if event and isinstance(event, dict):
                    if process_commands(event.get("list", [])):
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

        if isinstance(data, dict):
            for event in (data.get("events") or []):
                if not event or not isinstance(event, dict):
                    continue
                for page in (event.get("pages") or []):
                    if page and isinstance(page, dict):
                        if process_commands(page.get("list", [])):
                            return
            if "list" in data:
                if process_commands(data.get("list", [])):
                    return
        elif isinstance(data, list):
            for event in data:
                if event and isinstance(event, dict):
                    if process_commands(event.get("list", [])):
                        return
        log.warning("Export: MZ plugin %s/%s not matched — original %r",
                    plugin_name, param_key, original[:60])

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
        # Match the array assigned to $plugins specifically, not any []
        m = re.search(r'var\s+\$plugins\s*=\s*(\[.*?\])\s*;', content, re.DOTALL)
        if not m:
            # Fallback: non-greedy match of first complete JSON array
            m = re.search(r'(\[.*?\])\s*;', content, re.DOTALL)
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

    def _parse_plugins(self, project_dir: str) -> list:
        """Plugin parameter extraction from plugins.js — DISABLED.

        The scan-everything approach extracted internal lookup keys, asset IDs,
        and command identifiers alongside display text.  Translating these broke
        games.  Plugin display text is now extracted via the whitelist approach
        in _extract_event_commands() (codes 356/357) which only targets known-
        safe text from known plugins.

        The original scan methods are preserved below (commented out) in case
        a curated plugins.js whitelist is ever added.
        """
        return []

    # ── DISABLED: plugins.js parameter scanning ──────────────────
    # Over-extracted internal identifiers, config keys, and command
    # keywords that broke games when translated.  Replaced by the
    # whitelist approach for event codes 356/357 above.
    #
    # def _scan_plugin_param(self, value, id_prefix, entries):
    #     stripped = value.strip()
    #     if stripped.startswith(("{", "[")):
    #         try:
    #             parsed = json.loads(stripped)
    #             self._scan_parsed_value(parsed, id_prefix, entries)
    #             return
    #         except (json.JSONDecodeError, ValueError):
    #             pass
    #     if _is_plugin_display_text(value):
    #         entries.append(TranslationEntry(
    #             id=id_prefix, file="plugins.js", field="plugin_param",
    #             original=value, status="skipped",
    #         ))
    #
    # def _scan_parsed_value(self, obj, id_prefix, entries):
    #     if isinstance(obj, str):
    #         stripped = obj.strip()
    #         if stripped.startswith(("{", "[")):
    #             try:
    #                 inner = json.loads(stripped)
    #                 self._scan_parsed_value(inner, id_prefix, entries)
    #                 return
    #             except (json.JSONDecodeError, ValueError):
    #                 pass
    #         if _is_plugin_display_text(obj):
    #             entries.append(TranslationEntry(
    #                 id=id_prefix, file="plugins.js", field="plugin_param",
    #                 original=obj, status="skipped",
    #             ))
    #     elif isinstance(obj, list):
    #         for i, item in enumerate(obj):
    #             self._scan_parsed_value(item, f"{id_prefix}/[{i}]", entries)
    #     elif isinstance(obj, dict):
    #         for k, v in obj.items():
    #             self._scan_parsed_value(v, f"{id_prefix}/{k}", entries)

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
                # Simple string replacement
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
                if obj[segment] == original:
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

    def inject_wordwrap_plugin(self, project_dir: str):
        """Write TranslatorWordWrap.js and register it in plugins.js.

        Called during export when no existing word wrap plugin was detected
        and the user chose to inject one.
        """
        from .text_processor import WORDWRAP_PLUGIN_JS

        plugins_path = self._find_plugins_file(project_dir)
        if not plugins_path:
            return

        # Write the JS file next to plugins.js (js/plugins/ folder)
        js_dir = os.path.dirname(plugins_path)
        plugins_dir = os.path.join(js_dir, "plugins")
        os.makedirs(plugins_dir, exist_ok=True)
        js_path = os.path.join(plugins_dir, f"{self.INJECTED_PLUGIN_NAME}.js")
        with open(js_path, "w", encoding="utf-8") as f:
            f.write(WORDWRAP_PLUGIN_JS.strip() + "\n")

        # Add entry to $plugins array (read from backup if available)
        backup_path = os.path.join(
            js_dir,
            os.path.basename(plugins_path).replace("plugins.", "plugins_original."),
        )
        source = backup_path if os.path.exists(backup_path) else plugins_path
        try:
            plugins = self._load_plugins_js(source)
        except (json.JSONDecodeError, OSError):
            return

        # Don't duplicate if already present
        if any(p.get("name") == self.INJECTED_PLUGIN_NAME for p in plugins):
            self._write_plugins_js(plugins_path, plugins)
            return

        plugins.append({
            "name": self.INJECTED_PLUGIN_NAME,
            "status": True,
            "description": "Word wrap for translated text (auto-injected)",
            "parameters": {},
        })
        self._write_plugins_js(plugins_path, plugins)

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
