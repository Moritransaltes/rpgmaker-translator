"""Crowd visual novel engine parser (.sce encrypted script files).

Supports X-Change series and other Crowd engine games (late 1990s–2000s).

File format:
  - .sce files are XOR-encrypted with key "crowd script yeah !" (mod 18)
  - Decrypted content is cp932 (Shift-JIS) encoded
  - Script is a flat stream: sections delimited by $ label $
  - Entries delimited by "  N " (two spaces + line number + space)
  - Dialogue: voice_id@!SpeakerName@n followed by text
  - Narration: plain text without speaker prefix
  - Scene titles: SS "title"
  - Sound effects: wseNNNNN@?text
  - @n within text = in-game line break
  - Commands: CF, CB, CMA, V, G, GS, R, SS, MM, MP, CFILL, etc.
"""

import logging
import os
import re
import shutil

from .project_model import TranslationEntry
from . import JAPANESE_RE

log = logging.getLogger(__name__)

# ── Encryption ──────────────────────────────────────────────

_DEFAULT_KEY = b"crowd script yeah !"
_DEFAULT_MOD = 18  # decryption uses mod 18 (excludes the trailing '!')


def decrypt_sce(data: bytes, key: bytes = _DEFAULT_KEY,
                mod: int = _DEFAULT_MOD) -> bytearray:
    """Decrypt a Crowd .sce file."""
    out = bytearray(len(data))
    key_byte = 0
    counter = 0

    for i in range(len(data)):
        idx = (key_byte + i) % mod
        k = key[idx] | ((key_byte & counter) & 0xFF)
        out[i] = data[i] ^ (k & 0xFF)

        if idx == 0:
            new_idx = (key_byte + counter) % mod
            key_byte = key[new_idx]
            counter = (counter + 1) & 0xFFFFFFFF

    return out


def encrypt_sce(data: bytes, key: bytes = _DEFAULT_KEY,
                mod: int = _DEFAULT_MOD) -> bytearray:
    """Encrypt data back to .sce format (XOR is symmetric)."""
    return decrypt_sce(data, key, mod)


def find_key(exe_path: str, sce_path: str) -> tuple[bytes, int]:
    """Auto-extract the decryption key from the game exe.

    Scans the exe for ASCII strings and tests each as a candidate key
    against the .sce file. A correct key produces '$ ' (section marker)
    at the start of the decrypted output.

    Returns (key_bytes, mod) or falls back to the default key.
    """
    try:
        exe = open(exe_path, "rb").read()
        sce = open(sce_path, "rb").read(256)
    except OSError:
        return _DEFAULT_KEY, _DEFAULT_MOD

    # Extract null-terminated ASCII strings (length 5–60)
    candidates = []
    start = None
    for i, b in enumerate(exe):
        if 0x20 <= b < 0x7F:
            if start is None:
                start = i
        else:
            if start is not None and b == 0 and 5 <= (i - start) <= 60:
                candidates.append(exe[start:i])
            start = None

    # Test each candidate with mod = len-1 then len
    for key in candidates:
        for mod in (len(key) - 1, len(key)):
            if mod < 2:
                continue
            dec = decrypt_sce(sce[:64], key, mod)
            if dec[:2] == b"$ ":
                log.info("Auto-detected key: %r (mod=%d)", key.decode("ascii", errors="replace"), mod)
                return key, mod

    log.warning("Could not auto-detect key, using default")
    return _DEFAULT_KEY, _DEFAULT_MOD


# ── Regex patterns ──────────────────────────────────────────

# Entry delimiter: two spaces + line number + space
_LINE_DELIM = re.compile(r'  (\d+) ')

# Voice + speaker dialogue: w000001a@!Speaker@n text...
_VOICE_SPEAKER = re.compile(r'(w\d{6}\w?)@!(.+?)@n')

# Speaker without voice (bare): @!Speaker@n text...
_BARE_SPEAKER = re.compile(r'^@!(.+?)@n')

# Sound effect with text: wse00001@? text...
_SOUND_EFFECT = re.compile(r'(wse\d{5})@\?')

# Scene title: SS "title"
_SCENE_TITLE = re.compile(r'SS "([^"]+)"')

# Section label: $ label ... $  (at start or after commands)
_SECTION_LABEL = re.compile(r'\$ (\S+) ')

# Command tokens (non-translatable): CB, CF, CMA, V, G, GS, R, MM, MP, etc.
# These appear between entries and should not be translated
_COMMAND_TOKENS = re.compile(
    r'^(?:CB|CF|CMA|CM|CFILL|V|G|GS|R|RT|SS|MM|MP|MPAUSE|SD|SR|'
    r'FS|FC|FADD|FSUB|FINC|FDEC|FSET|FRND|RECE|RECS|CMD|H-\d+|'
    r'IF_\w+|jnz|#|END_\w+|SELECT_\w+)\b'
)

# In-text line break
_LINE_BREAK = re.compile(r'@n')


class CrowdParser:
    """Parser for Crowd engine .sce script files."""

    def __init__(self):
        self.context_size = 3
        self._key: bytes = _DEFAULT_KEY
        self._mod: int = _DEFAULT_MOD

    def load_project(self, project_dir: str, context_size: int = 3) -> list[TranslationEntry]:
        """Parse all .sce files in the project directory."""
        self.context_size = context_size
        sce_path = self._find_sce(project_dir)
        if not sce_path:
            log.warning("No .sce file found in %s", project_dir)
            return []

        # Auto-detect key from exe
        exe_path = self._find_exe(project_dir)
        if exe_path:
            self._key, self._mod = find_key(exe_path, sce_path)

        log.info("Loading Crowd script: %s", sce_path)
        with open(sce_path, "rb") as f:
            enc_data = f.read()

        dec_data = decrypt_sce(enc_data, self._key, self._mod)
        text = dec_data.decode("cp932", errors="replace")

        entries = self._parse_script(text, os.path.basename(sce_path))
        log.info("Parsed %d translatable entries from %s", len(entries), os.path.basename(sce_path))
        return entries

    def save_project(self, project_dir: str, entries: list[TranslationEntry]):
        """Export translations back into the .sce file."""
        sce_path = self._find_sce(project_dir)
        if not sce_path:
            log.error("No .sce file found for export")
            return

        backup_dir = os.path.join(project_dir, "sce_original")
        sce_name = os.path.basename(sce_path)

        # Create backup on first export
        backup_path = os.path.join(backup_dir, sce_name)
        if not os.path.exists(backup_dir):
            os.makedirs(backup_dir)
        if not os.path.exists(backup_path):
            shutil.copy2(sce_path, backup_path)
            log.info("Backed up %s to sce_original/", sce_name)

        # Always read from backup for idempotent re-export
        source = backup_path if os.path.exists(backup_path) else sce_path
        with open(source, "rb") as f:
            enc_data = f.read()

        dec_data = decrypt_sce(enc_data, self._key, self._mod)
        text = dec_data.decode("cp932", errors="replace")

        # Build translation map: line_number -> translated text
        trans_map = {}
        for entry in entries:
            if entry.translation and entry.status in ("translated", "reviewed"):
                # Entry ID format: "filename/type/line_num"
                parts = entry.id.split("/")
                if len(parts) >= 3:
                    line_num = int(parts[-1])
                    trans_map[line_num] = entry

        if not trans_map:
            log.warning("No translations to export")
            return

        # Reconstruct the script with translations
        translated_text = self._rebuild_script(text, trans_map)

        # Encode and encrypt
        translated_bytes = translated_text.encode("cp932", errors="replace")
        encrypted = encrypt_sce(translated_bytes, self._key, self._mod)

        with open(sce_path, "wb") as f:
            f.write(encrypted)

        log.info("Exported %d translations to %s", len(trans_map), sce_name)

    def restore_originals(self, project_dir: str):
        """Restore original .sce from backup."""
        backup_dir = os.path.join(project_dir, "sce_original")
        if not os.path.isdir(backup_dir):
            log.warning("No sce_original/ backup found")
            return

        for name in os.listdir(backup_dir):
            src = os.path.join(backup_dir, name)
            dst = os.path.join(project_dir, name)
            shutil.copy2(src, dst)
            log.info("Restored %s from backup", name)

    def get_game_title(self, project_dir: str) -> str:
        """Extract game title from the first SS command."""
        sce_path = self._find_sce(project_dir)
        if not sce_path:
            return os.path.basename(project_dir)

        with open(sce_path, "rb") as f:
            enc_data = f.read(4096)  # just the header

        # Auto-detect key if not already set
        exe_path = self._find_exe(project_dir)
        key, mod = (self._key, self._mod)
        if exe_path:
            key, mod = find_key(exe_path, sce_path)

        dec_data = decrypt_sce(enc_data, key, mod)
        text = dec_data.decode("cp932", errors="replace")

        m = _SCENE_TITLE.search(text)
        if m:
            return m.group(1)
        return os.path.basename(project_dir)

    @staticmethod
    def is_crowd_project(path: str) -> bool:
        """Check if path contains a .sce file (Crowd engine marker)."""
        if not os.path.isdir(path):
            return False
        for name in os.listdir(path):
            if name.lower().endswith(".sce"):
                return True
        return False

    # ── Private helpers ─────────────────────────────────────

    def _find_sce(self, project_dir: str) -> str | None:
        """Find the .sce file in the project directory."""
        for name in os.listdir(project_dir):
            if name.lower().endswith(".sce"):
                return os.path.join(project_dir, name)
        return None

    def _find_exe(self, project_dir: str) -> str | None:
        """Find the game .exe in the project directory."""
        for name in os.listdir(project_dir):
            if name.lower().endswith(".exe"):
                return os.path.join(project_dir, name)
        return None

    def _parse_script(self, text: str, filename: str) -> list[TranslationEntry]:
        """Parse decrypted script text into TranslationEntry list."""
        entries = []

        # Split by line number delimiters
        # Result: [preamble, "0", content0, "1", content1, ...]
        parts = _LINE_DELIM.split(text)

        if len(parts) < 3:
            log.warning("No line-number delimiters found in %s", filename)
            return entries

        # Track current section for grouping
        current_section = "main"
        recent_context: list[str] = []

        # Process each numbered entry
        for i in range(1, len(parts) - 1, 2):
            line_num = int(parts[i])
            content = parts[i + 1] if i + 1 < len(parts) else ""

            # Check for section labels in the content
            sec_m = _SECTION_LABEL.search(content)
            if sec_m:
                current_section = sec_m.group(1)

            # Extract scene titles
            for ss_m in _SCENE_TITLE.finditer(content):
                title = ss_m.group(1)
                if JAPANESE_RE.search(title):
                    entry_id = f"{filename}/scene_title/{line_num}"
                    entries.append(TranslationEntry(
                        id=entry_id,
                        file=filename,
                        field="scene_title",
                        original=title,
                        context=f"[Scene: {current_section}]",
                    ))

            # Extract translatable dialogue/narration
            entry = self._parse_entry_content(content, filename, line_num,
                                              current_section, recent_context)
            if entry:
                entries.append(entry)
                # Update context window
                speaker = entry.namebox
                ctx_line = f"[{speaker}] {entry.original}" if speaker else entry.original
                recent_context.append(ctx_line)
                if len(recent_context) > self.context_size:
                    recent_context.pop(0)

        return entries

    def _parse_entry_content(self, content: str, filename: str, line_num: int,
                             section: str, recent_context: list[str]) -> TranslationEntry | None:
        """Parse a single entry's content into a TranslationEntry or None."""
        speaker = ""
        voice_id = ""
        text = content

        # Try voice + speaker: w000001a@!Speaker@n text
        m = _VOICE_SPEAKER.search(text)
        if m:
            voice_id = m.group(1)
            speaker = m.group(2)
            text = text[m.end():]
        else:
            # Try bare speaker: @!Speaker@n text
            m = _BARE_SPEAKER.search(text)
            if m:
                speaker = m.group(1)
                text = text[m.end():]

        # Try sound effect: wse00001@? text
        if not speaker:
            m = _SOUND_EFFECT.search(text)
            if m:
                voice_id = m.group(1)
                text = text[m.end():]
                # Sound effect text is usually onomatopoeia — translatable
                speaker = "SE"

        # Strip leading/trailing command tokens
        text = self._strip_commands(text)

        # Replace @n with actual newlines for display
        display_text = _LINE_BREAK.sub('\n', text).strip()

        if not display_text:
            return None

        # Only include entries with Japanese text
        if not JAPANESE_RE.search(display_text):
            return None

        # Build field name
        if speaker and speaker != "SE":
            field = "dialogue"
        elif speaker == "SE":
            field = "sound_effect"
        else:
            field = "narration"

        entry_id = f"{filename}/{field}/{line_num}"

        # Build context
        ctx_parts = []
        if section != "main":
            ctx_parts.append(f"[Section: {section}]")
        if recent_context:
            ctx_parts.extend(recent_context[-self.context_size:])

        return TranslationEntry(
            id=entry_id,
            file=filename,
            field=field,
            original=display_text,
            context="\n".join(ctx_parts),
            namebox=speaker if speaker != "SE" else "",
        )

    def _strip_commands(self, text: str) -> str:
        """Strip non-translatable command tokens from the edges of text."""
        # Remove leading commands (CB H-010, CF 1-2, V 10, CMA xxx, etc.)
        # Commands are uppercase tokens optionally followed by arguments
        while True:
            text = text.strip()
            m = _COMMAND_TOKENS.match(text)
            if not m:
                break
            # Skip past the command and its arguments (until next Japanese or voice/speaker marker)
            # Find where the command ends: next @! or Japanese char or end
            rest_start = m.end()
            # Consume the command's arguments (non-Japanese tokens)
            while rest_start < len(text):
                ch = text[rest_start]
                if ch == '@' or ord(ch) > 0x7F:
                    break
                rest_start += 1
            text = text[rest_start:]

        return text

    def _rebuild_script(self, original_text: str, trans_map: dict) -> str:
        """Rebuild the full script with translations inserted."""
        parts = _LINE_DELIM.split(original_text)

        if len(parts) < 3:
            return original_text

        result_parts = [parts[0]]  # preamble

        for i in range(1, len(parts) - 1, 2):
            line_num = int(parts[i])
            content = parts[i + 1] if i + 1 < len(parts) else ""

            # Re-insert the delimiter
            result_parts.append(f"  {line_num} ")

            if line_num in trans_map:
                entry = trans_map[line_num]
                content = self._apply_translation(content, entry)

            # Also handle scene title translations
            title_key = line_num
            # Check for scene_title entries
            for key, entry in trans_map.items():
                if (entry.field == "scene_title" and
                        int(entry.id.split("/")[-1]) == line_num and
                        entry.translation):
                    content = self._apply_scene_title_translation(content, entry)
                    break

            result_parts.append(content)

        return "".join(result_parts)

    def _apply_translation(self, content: str, entry: TranslationEntry) -> str:
        """Replace the Japanese text in content with the translation."""
        translation = entry.translation

        # Convert newlines back to @n for the game engine
        translation = translation.replace('\n', '@n')

        speaker = entry.namebox

        if speaker:
            # Find the speaker tag and replace text after it
            # Pattern: (voice@!Speaker@n)(text) or (@!Speaker@n)(text)
            voice_m = _VOICE_SPEAKER.search(content)
            bare_m = _BARE_SPEAKER.search(content) if not voice_m else None

            if voice_m:
                prefix = content[:voice_m.end()]
                return prefix + translation
            elif bare_m:
                prefix = content[:bare_m.end()]
                return prefix + translation

        # For narration/SE — replace the Japanese text portion
        # Find where the text starts (after commands) and replace
        stripped = self._strip_commands(content)
        if stripped:
            # Find the original text position in content and replace
            original_display = _LINE_BREAK.sub('\n', stripped).strip()
            if original_display == entry.original:
                idx = content.find(stripped.strip())
                if idx >= 0:
                    return content[:idx] + translation

        # Fallback: try to replace the original text directly
        original_with_breaks = entry.original.replace('\n', '@n')
        if original_with_breaks in content:
            return content.replace(original_with_breaks, translation, 1)

        return content

    def _apply_scene_title_translation(self, content: str, entry: TranslationEntry) -> str:
        """Replace SS "original" with SS "translation" in content."""
        if entry.original and entry.translation:
            return content.replace(
                f'SS "{entry.original}"',
                f'SS "{entry.translation}"',
                1,
            )
        return content
