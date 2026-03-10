"""TyranoScript (.ks) parser — extracts translatable text from visual novels.

Handles:
- Dialogue lines (plain text between speaker tags and [p]/[l] breaks)
- Speaker names (jname= definitions and #speaker tags)
- Choice buttons ([glink text="..."])
- Dialog prompts ([dialog text="..."])
- Variable assignments ([eval exp="f.xxx = 'Japanese'"] — displayed via [emb])
- System UI strings (tyrano/lang.js)
- Game title (Config.tjs System.title)
- Inline tags preserved as placeholders: [r], [rr], [l], [p], [emb ...], [heart], etc.
"""

import json
import os
import re
from pathlib import Path

from . import JAPANESE_RE
from .project_model import TranslationEntry

# Inline tags that appear WITHIN dialogue text — must be preserved
# [r] [rr] [l] [p] [emb exp="..."] [heart] [ruby text="..."] [graph ...]
_INLINE_TAG_RE = re.compile(
    r'\[(?:r|rr|l|p|heart|emb\s[^\]]*|ruby\s[^\]]*|graph\s[^\]]*|font\s[^\]]*|resetfont)\]',
    re.IGNORECASE,
)

# Full-line command tags — skip these lines entirely
_COMMAND_LINE_RE = re.compile(r'^\s*[\[@]')

# Speaker tag: #name or # (clear)
_SPEAKER_RE = re.compile(r'^#(\w*)$')

# jname="..." in character definition tags
_JNAME_RE = re.compile(r'jname="([^"]+)"')

# glink/button with text="..." attribute
_GLINK_TEXT_RE = re.compile(r'\[glink\s[^\]]*text="([^"]+)"[^\]]*\]', re.IGNORECASE)

# dialog with text="..." attribute
_DIALOG_TEXT_RE = re.compile(r'\[dialog\s[^\]]*text="([^"]+)"[^\]]*\]', re.IGNORECASE)

# ptext with static text="..." attribute (not &expressions)
_PTEXT_TEXT_RE = re.compile(r'\[ptext\s[^\]]*text="([^"&][^"]*)"[^\]]*\]', re.IGNORECASE)

# Script blocks to skip entirely
_ISCRIPT_START = re.compile(r'^\s*\[iscript\]', re.IGNORECASE)
_ISCRIPT_END = re.compile(r'^\s*\[endscript\]', re.IGNORECASE)

# [eval exp="f.xxx = 'Japanese'"] — variable assignment with JP string value
# Captures: variable name (f.xxx) and string value (Japanese text)
_EVAL_ASSIGN_RE = re.compile(
    r"""\[eval\s+exp="(f\.\w+)\s*=\s*'([^']+)'"\]""", re.IGNORECASE)

# Config.tjs System.title line
_CONFIG_TITLE_RE = re.compile(r'^;System\.title=(.+)$', re.MULTILINE)


class TyranoScriptParser:
    """Parser for TyranoScript (.ks) visual novel games."""

    def __init__(self):
        self.context_size = 3

    # ── Public API ─────────────────────────────────────────────

    def load_project(self, project_dir: str) -> list[TranslationEntry]:
        """Load all .ks files from a TyranoScript project.

        Args:
            project_dir: Path to the game root (containing data/scenario/).

        Returns:
            List of TranslationEntry objects.
        """
        scenario_dir = self._find_scenario_dir(project_dir)
        if not scenario_dir:
            return []

        entries = []
        ks_files = sorted(Path(scenario_dir).rglob("*.ks"))

        # First pass: collect character name definitions
        self._char_names = {}  # name_id -> jname
        for ks_path in ks_files:
            self._scan_char_names(ks_path)

        # Second pass: extract translatable text
        for ks_path in ks_files:
            rel_path = str(ks_path.relative_to(Path(scenario_dir)))
            rel_path = rel_path.replace("\\", "/")
            file_entries = self._parse_ks_file(ks_path, rel_path)
            entries.extend(file_entries)

        # Extract system files (lang.js, Config.tjs)
        data_root = os.path.dirname(scenario_dir)  # data/ directory
        game_root = os.path.dirname(data_root)      # game root
        entries.extend(self._extract_lang_js(game_root))
        entries.extend(self._extract_config_title(data_root))

        return entries

    def save_project(self, project_dir: str, entries: list[TranslationEntry]):
        """Write translations back into .ks files.

        Reads from data_original/ backup (creating it on first export),
        then writes translated files to data/scenario/.
        """
        scenario_dir = self._find_scenario_dir(project_dir)
        if not scenario_dir:
            return

        # Create backup on first export
        original_dir = os.path.join(
            os.path.dirname(scenario_dir), "scenario_original")
        if not os.path.isdir(original_dir):
            import shutil
            shutil.copytree(scenario_dir, original_dir)

        # Build eval_var replacement map: 'original_jp' -> 'translated_en'
        # These are used to replace string values in [eval] and [if/elsif]
        eval_var_map: dict[str, str] = {}
        for entry in entries:
            if entry.field == "eval_var" and entry.translation and \
               entry.status in ("translated", "reviewed"):
                eval_var_map[entry.original] = entry.translation

        # Build lookup: file/line_num -> translation
        lookup: dict[str, dict[int, str]] = {}
        for entry in entries:
            if not entry.translation or entry.status not in ("translated", "reviewed"):
                continue
            if entry.field == "eval_var":
                continue  # handled via eval_var_map
            if entry.file.startswith("_system/"):
                continue  # system files handled separately
            file_key = entry.file
            # Parse line number from entry ID
            parts = entry.id.rsplit("/", 1)
            if len(parts) == 2 and parts[1].startswith("line_"):
                try:
                    line_num = int(parts[1][5:])
                except ValueError:
                    continue
                lookup.setdefault(file_key, {})[line_num] = entry.translation
            elif entry.field == "jname":
                # jname entries: replace in the tag
                lookup.setdefault(file_key, {})[entry.id] = entry.translation
            elif entry.field == "choice":
                lookup.setdefault(file_key, {})[entry.id] = entry.translation

        # Process each .ks file (apply dialogue + eval_var replacements)
        all_ks_files = set()
        for f in Path(original_dir).rglob("*.ks"):
            rel = str(f.relative_to(Path(original_dir))).replace("\\", "/")
            all_ks_files.add(rel)
        # Also include files from lookup that might not need eval_var
        all_ks_files.update(lookup.keys())

        for file_key in sorted(all_ks_files):
            src = os.path.join(original_dir, file_key)
            dst = os.path.join(scenario_dir, file_key)
            if not os.path.isfile(src):
                continue

            translations = lookup.get(file_key, {})

            with open(src, "r", encoding="utf-8") as f:
                lines = f.readlines()

            new_lines = []
            for i, line in enumerate(lines):
                line_num = i + 1  # 1-indexed

                if line_num in translations:
                    # Dialogue line — replace text content, preserve structure
                    new_lines.append(
                        self._apply_dialogue_translation(
                            line, translations[line_num]) + "\n")
                else:
                    # Check for jname/choice entries by ID
                    for entry_id, trans in translations.items():
                        if isinstance(entry_id, str):
                            if entry_id.startswith(file_key + "/jname/"):
                                old_name = entry_id.split("/jname/", 1)[1]
                                if f'jname="{old_name}"' in line:
                                    line = line.replace(
                                        f'jname="{old_name}"',
                                        f'jname="{trans}"')
                            elif entry_id.startswith(file_key + "/choice/"):
                                old_text = entry_id.split("/choice/", 1)[1]
                                if f'text="{old_text}"' in line:
                                    # Replace spaces with &nbsp; in glink/dialog text
                                    # TyranoScript parser strips spaces inside quoted attrs
                                    nbsp_trans = trans.replace(" ", "&nbsp;")
                                    line = line.replace(
                                        f'text="{old_text}"',
                                        f'text="{nbsp_trans}"')
                            elif entry_id.startswith(file_key + "/ptext/"):
                                old_text = entry_id.split("/ptext/", 1)[1]
                                if f'text="{old_text}"' in line:
                                    line = line.replace(
                                        f'text="{old_text}"',
                                        f'text="{trans}"')

                    # Apply eval_var replacements to [eval] and [if/elsif]
                    if eval_var_map:
                        line = self._apply_eval_var_replacements(
                            line, eval_var_map)

                    new_lines.append(line)

            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "w", encoding="utf-8") as f:
                f.writelines(new_lines)

        # Export system files
        self._export_lang_js(project_dir, entries)
        self._export_config_title(project_dir, entries)
        self._patch_game_fonts(project_dir)

    @staticmethod
    def _patch_game_fonts(project_dir: str):
        """Replace bundled Japanese fonts with a Latin font (Arial).

        TyranoScript games bundle JP .ttf fonts (loaded via @font-face in
        font.css) that render Latin space characters as zero-width, breaking
        all English text spacing.  This replaces each bundled .ttf with a
        copy of Arial so the engine's font-family references still resolve
        but now render Latin text correctly.
        """
        import shutil

        # Find the others/ dir where fonts live
        for sub in ["data/others", "extracted/data/others"]:
            others_dir = os.path.join(project_dir, sub)
            if os.path.isdir(others_dir):
                break
        else:
            return

        # System Arial as replacement
        arial_src = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                                 "Fonts", "arial.ttf")
        if not os.path.isfile(arial_src):
            return  # non-Windows or Arial missing

        # Find font names referenced in font.css
        font_css = None
        for sub in ["tyrano/font.css", "extracted/tyrano/font.css"]:
            candidate = os.path.join(project_dir, sub)
            if os.path.isfile(candidate):
                font_css = candidate
                break

        target_fonts = []
        if font_css:
            with open(font_css, "r", encoding="utf-8") as f:
                css_text = f.read()
            # Extract .ttf filenames from url("...") declarations
            for m in re.finditer(r'url\(["\']?\.\./data/others/([^"\')\s]+\.ttf)', css_text):
                target_fonts.append(m.group(1))

        # Fallback: replace all .ttf files in others/
        if not target_fonts:
            for f in os.listdir(others_dir):
                if f.lower().endswith(".ttf"):
                    target_fonts.append(f)

        for font_name in target_fonts:
            font_path = os.path.join(others_dir, font_name)
            if not os.path.isfile(font_path):
                continue
            # Backup original
            backup = font_path + ".bak"
            if not os.path.isfile(backup):
                shutil.copy2(font_path, backup)
            # Replace with Arial
            shutil.copy2(arial_src, font_path)

    # ── Detection ──────────────────────────────────────────────

    @staticmethod
    def is_tyranoscript_project(path: str) -> bool:
        """Check if a folder looks like a TyranoScript game."""
        scenario = os.path.join(path, "data", "scenario")
        if os.path.isdir(scenario):
            return any(f.endswith(".ks") for f in os.listdir(scenario))
        # Some games use extracted/ subfolder
        extracted = os.path.join(path, "extracted", "data", "scenario")
        if os.path.isdir(extracted):
            return any(f.endswith(".ks") for f in os.listdir(extracted))
        return False

    @staticmethod
    def find_nwjs_exe(path: str) -> str | None:
        """Find an NW.js executable with appended ZIP data in the folder.

        TyranoScript games are typically packaged as NW.js apps where the
        game data is appended to the exe as a ZIP archive.  Returns the
        exe path if found, None otherwise.
        """
        import zipfile
        for f in os.listdir(path):
            if not f.lower().endswith(".exe"):
                continue
            exe_path = os.path.join(path, f)
            try:
                if zipfile.is_zipfile(exe_path):
                    with zipfile.ZipFile(exe_path, "r") as zf:
                        names = zf.namelist()
                        # Check for TyranoScript signature: data/scenario/ in the zip
                        if any(n.startswith("data/scenario/") and n.endswith(".ks")
                               for n in names):
                            return exe_path
            except (OSError, zipfile.BadZipFile):
                continue
        return None

    @staticmethod
    def extract_nwjs(exe_path: str, dest_dir: str,
                     progress_cb=None) -> int:
        """Extract game data from an NW.js executable.

        Args:
            exe_path: Path to the NW.js .exe with appended ZIP.
            dest_dir: Destination folder (typically <game>/extracted/).
            progress_cb: Optional callback(current, total) for progress.

        Returns:
            Number of files extracted.
        """
        import zipfile
        os.makedirs(dest_dir, exist_ok=True)
        with zipfile.ZipFile(exe_path, "r") as zf:
            members = zf.namelist()
            total = len(members)
            for i, member in enumerate(members):
                zf.extract(member, dest_dir)
                if progress_cb and i % 50 == 0:
                    progress_cb(i, total)
            if progress_cb:
                progress_cb(total, total)
        return total

    # ── Internal ───────────────────────────────────────────────

    def _find_scenario_dir(self, project_dir: str) -> str | None:
        """Find the data/scenario/ directory."""
        candidates = [
            os.path.join(project_dir, "data", "scenario"),
            os.path.join(project_dir, "extracted", "data", "scenario"),
        ]
        for c in candidates:
            if os.path.isdir(c):
                return c
        return None

    def _scan_char_names(self, ks_path: Path):
        """Scan a .ks file for character jname definitions."""
        try:
            text = ks_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        # Find [chara_new ... name=X ... jname="Y"] and similar
        for line in text.splitlines():
            jname_match = _JNAME_RE.search(line)
            if jname_match:
                # Extract name= attribute
                name_match = re.search(r'\bname=(\w+)', line)
                if name_match:
                    self._char_names[name_match.group(1)] = jname_match.group(1)

    def _parse_ks_file(self, ks_path: Path, rel_path: str) -> list[TranslationEntry]:
        """Parse a single .ks file and extract translatable entries."""
        try:
            lines = ks_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return []

        entries = []
        recent_context = []
        current_speaker = ""
        current_speaker_id = ""
        in_script = False
        seen_jnames = set()
        seen_choices = set()
        seen_eval_vars = set()

        for i, line in enumerate(lines):
            line_num = i + 1
            stripped = line.strip()

            # Skip empty lines
            if not stripped:
                continue

            # Skip script blocks
            if _ISCRIPT_START.match(stripped):
                in_script = True
                continue
            if _ISCRIPT_END.match(stripped):
                in_script = False
                continue
            if in_script:
                continue

            # Skip comments
            if stripped.startswith(";"):
                continue

            # Speaker tag
            speaker_match = _SPEAKER_RE.match(stripped)
            if speaker_match:
                current_speaker_id = speaker_match.group(1)
                current_speaker = self._char_names.get(
                    current_speaker_id, current_speaker_id)
                continue

            # [eval] variable assignments with JP string values
            # e.g. [eval exp="f.mea = 'わたし'"] — displayed via [emb exp="f.mea"]
            eval_match = _EVAL_ASSIGN_RE.search(stripped)
            if eval_match:
                var_name = eval_match.group(1)   # e.g. "f.mea"
                var_value = eval_match.group(2)  # e.g. "わたし"
                if JAPANESE_RE.search(var_value):
                    entry_id = f"{rel_path}/eval/{var_name}/{var_value}"
                    if entry_id not in seen_eval_vars:
                        seen_eval_vars.add(entry_id)
                        entries.append(TranslationEntry(
                            id=entry_id,
                            file=rel_path,
                            field="eval_var",
                            original=var_value,
                            status="untranslated",
                            context=f"Variable {var_name} — displayed inline via [emb exp=\"{var_name}\"] in dialogue",
                        ))
                continue

            # jname definitions — extract for translation
            jname_match = _JNAME_RE.search(stripped)
            if jname_match:
                jname = jname_match.group(1)
                if JAPANESE_RE.search(jname):
                    entry_id = f"{rel_path}/jname/{jname}"
                    if entry_id not in seen_jnames:
                        seen_jnames.add(entry_id)
                        entries.append(TranslationEntry(
                            id=entry_id,
                            file=rel_path,
                            field="jname",
                            original=jname,
                            status="untranslated",
                        ))
                # Don't return — line may also be a command, fall through

            # Choice buttons: [glink ... text="日本語" ...]
            glink_match = _GLINK_TEXT_RE.search(stripped)
            if glink_match:
                text = glink_match.group(1)
                if JAPANESE_RE.search(text):
                    entry_id = f"{rel_path}/choice/{text}"
                    if entry_id not in seen_choices:
                        seen_choices.add(entry_id)
                        context = "\n---\n".join(recent_context[-self.context_size:])
                        entries.append(TranslationEntry(
                            id=entry_id,
                            file=rel_path,
                            field="choice",
                            original=text,
                            status="untranslated",
                            context=f"[Speaker: {current_speaker}]\n{context}" if current_speaker else context,
                        ))
                continue

            # Dialog prompts: [dialog ... text="日本語" ...]
            dialog_match = _DIALOG_TEXT_RE.search(stripped)
            if dialog_match:
                text = dialog_match.group(1)
                if JAPANESE_RE.search(text):
                    entry_id = f"{rel_path}/choice/{text}"
                    if entry_id not in seen_choices:
                        seen_choices.add(entry_id)
                        entries.append(TranslationEntry(
                            id=entry_id,
                            file=rel_path,
                            field="choice",
                            original=text,
                            status="untranslated",
                        ))
                continue

            # Static ptext labels: [ptext ... text="日本語" ...]
            ptext_match = _PTEXT_TEXT_RE.search(stripped)
            if ptext_match:
                text = ptext_match.group(1)
                if JAPANESE_RE.search(text):
                    entry_id = f"{rel_path}/ptext/{text}"
                    if entry_id not in seen_choices:
                        seen_choices.add(entry_id)
                        entries.append(TranslationEntry(
                            id=entry_id,
                            file=rel_path,
                            field="ptext",
                            original=text,
                            status="untranslated",
                            context="On-screen label / menu text",
                        ))
                continue

            # Skip full-line commands — but NOT dialogue that starts with
            # an inline tag like [emb exp="f.mea"]Japanese text[p]
            if _COMMAND_LINE_RE.match(stripped):
                # Check if it starts with a known inline tag followed by text
                if _INLINE_TAG_RE.match(stripped):
                    text_only = _INLINE_TAG_RE.sub('', stripped).strip()
                    if not JAPANESE_RE.search(text_only):
                        continue
                    # Falls through to dialogue extraction below
                else:
                    continue

            # If we get here, it's a text/dialogue line
            if not JAPANESE_RE.search(stripped):
                continue

            # Build context
            speaker_hint = ""
            if current_speaker:
                speaker_hint = f"[Speaker: {current_speaker}]"

            context_parts = []
            if speaker_hint:
                context_parts.append(speaker_hint)
            if recent_context:
                context_parts.append(
                    "\n---\n".join(recent_context[-self.context_size:]))
            context = "\n".join(context_parts)

            entry = TranslationEntry(
                id=f"{rel_path}/line_{line_num}",
                file=rel_path,
                field="dialog",
                original=stripped,
                status="untranslated",
                context=context,
            )
            entries.append(entry)

            # Add to recent context for next entries
            display = stripped
            if current_speaker:
                display = f"{current_speaker}: {stripped}"
            recent_context.append(display)
            if len(recent_context) > self.context_size + 2:
                recent_context.pop(0)

        return entries

    @staticmethod
    def _apply_eval_var_replacements(
            line: str, var_map: dict[str, str]) -> str:
        """Replace JP string values in [eval] and [if/elsif] with translations.

        Handles both assignment (f.x = 'JP') and comparison (f.x == 'JP').
        Only replaces inside single-quoted strings to avoid touching code.
        """
        for jp_val, en_val in var_map.items():
            if jp_val in line:
                line = line.replace(f"'{jp_val}'", f"'{en_val}'")
        return line

    def _export_lang_js(self, project_dir: str, entries: list[TranslationEntry]):
        """Write translated lang.js system strings back."""
        lang_entries = {e.id.split("/")[-1]: e.translation
                        for e in entries
                        if e.file == "_system/lang.js"
                        and e.translation
                        and e.status in ("translated", "reviewed")}
        if not lang_entries:
            return

        for sub in [project_dir, os.path.join(project_dir, "extracted")]:
            lang_path = os.path.join(sub, "tyrano", "lang.js")
            if not os.path.isfile(lang_path):
                continue
            # Backup
            backup = lang_path + ".bak"
            if not os.path.isfile(backup):
                import shutil
                shutil.copy2(lang_path, backup)

            text = Path(lang_path).read_text(encoding="utf-8")
            for key, trans in lang_entries.items():
                # Replace "key":"old_value" with "key":"new_value"
                text = re.sub(
                    rf'("{key}"\s*:\s*)"([^"]*)"',
                    rf'\1"{trans}"',
                    text)
            Path(lang_path).write_text(text, encoding="utf-8")
            break

    def _export_config_title(self, project_dir: str, entries: list[TranslationEntry]):
        """Write translated game title to Config.tjs."""
        title_entry = None
        for e in entries:
            if e.id == "_system/Config.tjs/title" and e.translation and \
               e.status in ("translated", "reviewed"):
                title_entry = e
                break
        if not title_entry:
            return

        # Find Config.tjs
        scenario_dir = self._find_scenario_dir(project_dir)
        if not scenario_dir:
            return
        data_root = os.path.dirname(scenario_dir)
        config_path = os.path.join(data_root, "system", "Config.tjs")
        if not os.path.isfile(config_path):
            return

        # Backup
        backup = config_path + ".bak"
        if not os.path.isfile(backup):
            import shutil
            shutil.copy2(config_path, backup)

        text = Path(config_path).read_text(encoding="utf-8")
        text = _CONFIG_TITLE_RE.sub(
            f";System.title={title_entry.translation}", text)
        # Also update projectID if present
        old_id_re = re.compile(r'^;projectID=(.+)$', re.MULTILINE)
        m = old_id_re.search(text)
        if m and JAPANESE_RE.search(m.group(1)):
            # Strip version suffix for projectID
            proj_title = re.sub(r'\s*ver?\s*[\d.]+\s*$', '',
                                title_entry.translation, flags=re.IGNORECASE)
            text = old_id_re.sub(f";projectID={proj_title}", text)
        Path(config_path).write_text(text, encoding="utf-8")

    def _extract_lang_js(self, game_root: str) -> list[TranslationEntry]:
        """Extract translatable strings from tyrano/lang.js."""
        entries = []
        for sub in [game_root, os.path.join(game_root, "extracted")]:
            lang_path = os.path.join(sub, "tyrano", "lang.js")
            if not os.path.isfile(lang_path):
                continue
            try:
                text = Path(lang_path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            # Parse the word:{} section — key:"value" pairs
            # Match "key":"value" patterns where value contains Japanese
            for m in re.finditer(r'"(\w+)"\s*:\s*"([^"]+)"', text):
                key, value = m.group(1), m.group(2)
                if JAPANESE_RE.search(value):
                    entries.append(TranslationEntry(
                        id=f"_system/lang.js/{key}",
                        file="_system/lang.js",
                        field="system_ui",
                        original=value,
                        status="untranslated",
                        context="TyranoScript system UI string",
                    ))
            break  # only process first found
        return entries

    def _extract_config_title(self, data_root: str) -> list[TranslationEntry]:
        """Extract game title from data/system/Config.tjs."""
        entries = []
        config_path = os.path.join(data_root, "system", "Config.tjs")
        if not os.path.isfile(config_path):
            return entries
        try:
            text = Path(config_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return entries
        m = _CONFIG_TITLE_RE.search(text)
        if m and JAPANESE_RE.search(m.group(1)):
            entries.append(TranslationEntry(
                id="_system/Config.tjs/title",
                file="_system/Config.tjs",
                field="game_title",
                original=m.group(1).strip(),
                status="untranslated",
                context="Game window title bar",
            ))
        return entries

    # Tags that act as line terminators in TyranoScript —
    # if a dialogue line ends with one of these, no trailing space needed
    _LINE_TERM_RE = re.compile(
        r'\[(?:r|rr|p|l|cm)\]\s*$', re.IGNORECASE)

    def _apply_dialogue_translation(self, original_line: str, translation: str) -> str:
        """Replace the text content of a dialogue line with its translation.

        Preserves leading whitespace.  Appends a trailing space when the
        translation doesn't end with an inline break tag ([r], [p], etc.)
        so that TyranoScript's line-concatenation doesn't fuse English
        words across source lines (e.g. "succubus's" + "status" → ok).
        """
        # Preserve original indentation
        indent = ""
        for ch in original_line:
            if ch in " \t":
                indent += ch
            else:
                break

        # If translation doesn't end with a break tag, append a Unicode
        # non-breaking space (U+00A0) so consecutive dialogue lines don't
        # fuse words together.  Regular spaces get stripped by $.trim() in
        # the TyranoScript parser, but U+00A0 is not ASCII whitespace so
        # $.trim() preserves it, and it renders as a normal space.
        trimmed = translation.rstrip()
        if trimmed and not self._LINE_TERM_RE.search(trimmed):
            translation = trimmed + "\u00A0"

        return indent + translation

    def get_game_title(self, project_dir: str) -> str:
        """Try to extract game title from Config.tjs or package.json."""
        # Try Config.tjs first (most reliable for TyranoScript)
        scenario_dir = self._find_scenario_dir(project_dir)
        if scenario_dir:
            config_path = os.path.join(
                os.path.dirname(scenario_dir), "system", "Config.tjs")
            if os.path.isfile(config_path):
                try:
                    text = Path(config_path).read_text(encoding="utf-8")
                    m = _CONFIG_TITLE_RE.search(text)
                    if m:
                        return m.group(1).strip()
                except (OSError, UnicodeDecodeError):
                    pass

        # Fallback to package.json
        for sub in ["", "extracted"]:
            base = os.path.join(project_dir, sub) if sub else project_dir
            pkg = os.path.join(base, "package.json")
            if os.path.isfile(pkg):
                try:
                    with open(pkg, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    title = data.get("window", {}).get("title", "")
                    if title:
                        return title
                    title = data.get("name", "")
                    if title:
                        return title
                except Exception:
                    pass
        return ""

    # ── Word Wrap ─────────────────────────────────────────────

    @staticmethod
    def detect_line_budget(ks_contents: list[str]) -> int:
        """Derive English character budget from original JP line lengths.

        Scans all .ks file contents, splits dialogue on [r]/[p]/newlines,
        strips inline tags, and uses the 95th percentile of JP line lengths
        as the baseline (avoids outliers like HTML comments).  Since JP
        characters are full-width (~2x English), the budget is:

            english_budget = p95_jp_chars * 1.6

        Args:
            ks_contents: List of .ks file text contents (strings).

        Returns:
            English character budget per line (int).  Falls back to 55 if
            no dialogue is found (reasonable default for 800px window).
        """
        # Any [tag ...] or <!-- comment --> — strip for character counting
        tag_re = re.compile(r'\[[^\]]*\]|<!--.*?-->')
        # Split points: [r], [rr], [p], [l] and actual newlines
        split_re = re.compile(r'\[(?:r|rr|p|l)\]', re.IGNORECASE)

        lengths = []
        in_script = False

        for text in ks_contents:
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                # Skip iscript blocks
                if _ISCRIPT_START.match(stripped):
                    in_script = True
                    continue
                if _ISCRIPT_END.match(stripped):
                    in_script = False
                    continue
                if in_script:
                    continue
                # Skip comments (both ; and HTML)
                if stripped.startswith(";") or stripped.startswith("<!--"):
                    continue
                if _SPEAKER_RE.match(stripped):
                    continue
                if _COMMAND_LINE_RE.match(stripped):
                    continue
                # Must contain Japanese to be dialogue
                if not JAPANESE_RE.search(stripped):
                    continue

                # Split on line break tags
                segments = split_re.split(stripped)
                for seg in segments:
                    clean = tag_re.sub("", seg).strip()
                    if clean and JAPANESE_RE.search(clean):
                        lengths.append(len(clean))

        if not lengths:
            return 55  # safe default

        # Use 95th percentile to avoid outliers
        lengths.sort()
        p95_idx = int(len(lengths) * 0.95)
        p95 = lengths[min(p95_idx, len(lengths) - 1)]

        return int(p95 * 1.6)

    @staticmethod
    def wordwrap_translation(text: str, budget: int) -> str:
        """Insert [r] line break tags into translated text at word boundaries.

        Args:
            text: English translated text (may already contain [r]/[p] tags).
            budget: Maximum characters per line before wrapping.

        Returns:
            Text with [r] tags inserted at word boundaries.
        """
        if not text or budget <= 0:
            return text

        # Tag pattern — preserve but don't count toward width
        tag_re = re.compile(r'\[[^\]]*\]')

        # If text already has [r] tags from the LLM, strip them first
        # (we'll re-wrap properly)
        has_p = "[p]" in text.lower()
        # Split on [p] to preserve paragraph boundaries
        paragraphs = re.split(r'\[p\]', text, flags=re.IGNORECASE)

        wrapped_parts = []
        for para_idx, para in enumerate(paragraphs):
            # Remove existing [r] tags — we'll re-insert them
            para = re.sub(r'\[r\]', ' ', para, flags=re.IGNORECASE)
            para = re.sub(r'\[rr\]', ' ', para, flags=re.IGNORECASE)
            # Collapse multiple spaces
            para = re.sub(r'  +', ' ', para).strip()

            if not para:
                wrapped_parts.append(para)
                continue

            # Split into words and tags
            tokens = tag_re.split(para)
            tags = tag_re.findall(para)

            # Rebuild with word wrapping
            result_lines = []
            current_line = ""
            current_width = 0

            # Interleave text chunks and tags
            all_pieces = []
            for i, chunk in enumerate(tokens):
                all_pieces.append(("text", chunk))
                if i < len(tags):
                    all_pieces.append(("tag", tags[i]))

            for piece_type, piece in all_pieces:
                if piece_type == "tag":
                    current_line += piece
                    continue

                words = piece.split(" ")
                for wi, word in enumerate(words):
                    if not word:
                        continue
                    word_len = len(word)
                    # Check if adding this word exceeds budget
                    needed = word_len + (1 if current_width > 0 else 0)
                    if current_width + needed > budget and current_width > 0:
                        result_lines.append(current_line.rstrip())
                        current_line = word
                        current_width = word_len
                    else:
                        if current_width > 0:
                            current_line += " "
                            current_width += 1
                        current_line += word
                        current_width += word_len

            if current_line.strip():
                result_lines.append(current_line.rstrip())

            wrapped_parts.append("[r]".join(result_lines))

        # Re-join with [p] tags
        if has_p and len(paragraphs) > 1:
            return "[p]".join(wrapped_parts)
        return wrapped_parts[0] if wrapped_parts else text
