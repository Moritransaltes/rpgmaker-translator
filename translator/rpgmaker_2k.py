"""RPG Maker 2000/2003 LCF binary parser — load, extract, export."""

import logging
import os
import re
import shutil
from dataclasses import dataclass

from translator import JAPANESE_RE
from translator.project_model import TranslationEntry

log = logging.getLogger(__name__)

# ── BER VLQ (big-endian variable-length quantity) ─────────────────────

def _read_ber(data: bytes, pos: int) -> tuple[int, int]:
    """Read a BER-encoded integer. Returns (value, new_pos)."""
    value = 0
    while pos < len(data):
        b = data[pos]; pos += 1
        value = (value << 7) | (b & 0x7F)
        if not (b & 0x80):
            break
    return value, pos


def _write_ber(value: int) -> bytes:
    """Encode an integer as BER VLQ bytes."""
    if value == 0:
        return b'\x00'
    parts = []
    while value > 0:
        parts.append(value & 0x7F)
        value >>= 7
    # Reverse so high bits come first (big-endian)
    result = bytearray()
    for i, p in enumerate(reversed(parts)):
        if i < len(parts) - 1:
            result.append(p | 0x80)
        else:
            result.append(p)
    return bytes(result)


# ── LCF chunk parsing ─────────────────────────────────────────────────

def _read_header(data: bytes) -> tuple[str, int]:
    """Read LCF file header string. Returns (header, pos_after)."""
    length, pos = _read_ber(data, 0)
    header = data[pos:pos + length].decode('ascii')
    return header, pos + length


def _write_header(header: str) -> bytes:
    """Write LCF file header."""
    encoded = header.encode('ascii')
    return _write_ber(len(encoded)) + encoded


def _parse_chunks(data: bytes, start: int, end: int) -> tuple[dict, int]:
    """Parse chunk-based object. Returns ({id: payload_bytes}, pos_after)."""
    chunks = {}
    pos = start
    while pos < end:
        cid, pos = _read_ber(data, pos)
        if cid == 0:
            break
        clen, pos = _read_ber(data, pos)
        chunks[cid] = data[pos:pos + clen]
        pos += clen
    return chunks, pos


def _write_chunks(chunks: dict, terminate: bool = True) -> bytes:
    """Serialize chunks dict back to binary.

    Args:
        terminate: If True, append 0x00 end-of-object marker.
            Use False for top-level file output (LDB/LMU end at EOF).
    """
    out = bytearray()
    for cid in sorted(chunks):
        payload = chunks[cid]
        out += _write_ber(cid)
        out += _write_ber(len(payload))
        out += payload
    if terminate:
        out.append(0x00)
    return bytes(out)


def _parse_array(data: bytes) -> list[tuple[int, dict]]:
    """Parse LCF array: count + indexed chunk objects."""
    pos = 0
    count, pos = _read_ber(data, pos)
    elements = []
    for _ in range(count):
        idx, pos = _read_ber(data, pos)
        elem_chunks, pos = _parse_chunks(data, pos, len(data))
        elements.append((idx, elem_chunks))
    return elements


def _write_array(elements: list[tuple[int, dict]]) -> bytes:
    """Serialize array back to binary."""
    out = bytearray()
    out += _write_ber(len(elements))
    for idx, chunks in elements:
        out += _write_ber(idx)
        out += _write_chunks(chunks)
    return bytes(out)


# ── String helpers ────────────────────────────────────────────────────

def _decode_str(raw: bytes) -> str:
    """Decode Shift-JIS bytes to str."""
    if not raw:
        return ""
    try:
        return raw.decode('shift_jis')
    except (UnicodeDecodeError, ValueError):
        try:
            return raw.decode('cp932')
        except (UnicodeDecodeError, ValueError):
            return raw.decode('latin-1')


def _encode_str(text: str) -> bytes:
    """Encode str to Shift-JIS bytes."""
    try:
        return text.encode('shift_jis')
    except (UnicodeEncodeError, ValueError):
        return text.encode('cp932', errors='replace')


def _has_japanese(text: str) -> bool:
    """Check if text contains Japanese characters."""
    return bool(JAPANESE_RE.search(text))


# ── Event command parsing ─────────────────────────────────────────────

@dataclass
class EventCommand:
    """A single event command in RM2K/2K3."""
    code: int
    indent: int
    string: str
    string_raw: bytes
    params: list[int]


# RM2K/2K3 event command codes
CODE_SHOW_MESSAGE      = 10110   # First line of message (up to 4 lines)
CODE_SHOW_MESSAGE_LINE = 20110   # Continuation lines (2nd-4th)
CODE_SHOW_CHOICE       = 10140   # Show Choice
CODE_SHOW_CHOICE_OPT   = 20140   # Choice branch
CODE_SET_FACE          = 10130   # Change Face Graphic
CODE_INPUT_NUMBER      = 10150   # Input Number
CODE_CHANGE_HERO_NAME  = 10610   # Change Hero Name
CODE_CHANGE_HERO_TITLE = 10620   # Change Hero Title


def _parse_commands(data: bytes) -> list[EventCommand]:
    """Parse flat command stream from event page field 0x34."""
    commands = []
    pos = 0
    while pos < len(data):
        code, pos = _read_ber(data, pos)
        indent, pos = _read_ber(data, pos)
        slen, pos = _read_ber(data, pos)
        string_raw = data[pos:pos + slen] if slen > 0 else b''
        pos += slen
        string = _decode_str(string_raw)
        pcount, pos = _read_ber(data, pos)
        params = []
        for _ in range(pcount):
            v, pos = _read_ber(data, pos)
            params.append(v)
        commands.append(EventCommand(code, indent, string, string_raw, params))
    return commands


def _write_commands(commands: list[EventCommand]) -> bytes:
    """Serialize command list back to binary."""
    out = bytearray()
    for cmd in commands:
        out += _write_ber(cmd.code)
        out += _write_ber(cmd.indent)
        raw = cmd.string_raw
        out += _write_ber(len(raw))
        out += raw
        out += _write_ber(len(cmd.params))
        for p in cmd.params:
            out += _write_ber(p)
    return bytes(out)


# ── Database field definitions ────────────────────────────────────────

# Top-level LDB chunk IDs
DB_ACTORS      = 0x0B
DB_SKILLS      = 0x0C
DB_ITEMS       = 0x0D
DB_ENEMIES     = 0x0E
DB_STATES      = 0x12
DB_VOCABULARY  = 0x15
DB_SYSTEM      = 0x16
DB_COMMONEVENTS = 0x19

# Fields to extract per database type: {chunk_id: [(field_id, field_name), ...]}
DATABASE_FIELDS = {
    DB_ACTORS:  [(0x01, "name"), (0x02, "title")],
    DB_SKILLS:  [(0x01, "name"), (0x02, "description"),
                 (0x03, "message1"), (0x04, "message2")],
    DB_ITEMS:   [(0x01, "name"), (0x02, "description")],
    DB_ENEMIES: [(0x01, "name")],
    DB_STATES:  [(0x01, "name")],
}

DB_NAMES = {
    DB_ACTORS: "Actors", DB_SKILLS: "Skills", DB_ITEMS: "Items",
    DB_ENEMIES: "Enemies", DB_STATES: "States",
}

# Map chunk IDs
MAP_EVENTS = 0x51
EVENT_NAME = 0x01
EVENT_PAGES = 0x05
PAGE_COMMANDS = 0x34

# Common event fields
CE_NAME = 0x01
CE_COMMANDS = 0x16


# ── Main parser class ─────────────────────────────────────────────────

class RPGMaker2KParser:
    """Parser for RPG Maker 2000/2003 LCF binary files."""

    # ── Detection ─────────────────────────────────────────────────────

    @staticmethod
    def is_2k_project(path: str) -> bool:
        """Return True if path looks like an RM2K/2K3 project."""
        ldb = os.path.join(path, "RPG_RT.ldb")
        lmt = os.path.join(path, "RPG_RT.lmt")
        exe = os.path.join(path, "RPG_RT.exe")
        has_lmu = any(f.endswith(".lmu") for f in os.listdir(path)
                      if os.path.isfile(os.path.join(path, f)))
        return os.path.isfile(ldb) and has_lmu and (
            os.path.isfile(lmt) or os.path.isfile(exe))

    # ── Load project ──────────────────────────────────────────────────

    def load_project(self, project_dir: str,
                     context_size: int = 3) -> list[TranslationEntry]:
        """Extract all translatable strings from an RM2K/2K3 project."""
        entries: list[TranslationEntry] = []

        ldb_path = os.path.join(project_dir, "RPG_RT.ldb")
        if not os.path.isfile(ldb_path):
            raise FileNotFoundError(f"RPG_RT.ldb not found in {project_dir}")

        # Parse database
        with open(ldb_path, 'rb') as f:
            ldb_data = f.read()
        header, pos = _read_header(ldb_data)
        if header != "LcfDataBase":
            raise ValueError(f"Not an LDB file: header={header}")
        db_chunks, _ = _parse_chunks(ldb_data, pos, len(ldb_data))

        # Extract database fields
        entries.extend(self._extract_database(db_chunks))

        # Extract vocabulary
        if DB_VOCABULARY in db_chunks:
            entries.extend(self._extract_vocabulary(db_chunks[DB_VOCABULARY]))

        # Extract common events
        if DB_COMMONEVENTS in db_chunks:
            entries.extend(self._extract_common_events(
                db_chunks[DB_COMMONEVENTS], context_size))

        # Extract map events
        for fname in sorted(os.listdir(project_dir)):
            if not fname.lower().endswith('.lmu'):
                continue
            fpath = os.path.join(project_dir, fname)
            try:
                entries.extend(self._extract_map(fpath, fname, context_size))
            except Exception as e:
                log.error("Failed to parse %s: %s", fname, e)

        log.info("Loaded %d entries from RM2K project", len(entries))
        return entries

    def _extract_database(self, db_chunks: dict) -> list[TranslationEntry]:
        """Extract translatable strings from database arrays."""
        entries = []
        for db_id, fields in DATABASE_FIELDS.items():
            if db_id not in db_chunks:
                continue
            db_name = DB_NAMES.get(db_id, f"DB_0x{db_id:02X}")
            elements = _parse_array(db_chunks[db_id])
            for idx, elem in elements:
                for field_id, field_name in fields:
                    if field_id not in elem:
                        continue
                    text = _decode_str(elem[field_id])
                    if not text.strip() or not _has_japanese(text):
                        continue
                    entry_id = f"RPG_RT.ldb/{db_name}/{idx}/{field_name}"
                    entries.append(TranslationEntry(
                        id=entry_id,
                        file="RPG_RT.ldb",
                        field=field_name,
                        original=text,
                        translation="",
                        status="untranslated",
                        context=f"[{db_name} #{idx}]",
                    ))
        return entries

    def _extract_vocabulary(self, vocab_data: bytes) -> list[TranslationEntry]:
        """Extract vocabulary/system terms."""
        entries = []
        chunks, _ = _parse_chunks(vocab_data, 0, len(vocab_data))
        for fid in sorted(chunks):
            text = _decode_str(chunks[fid])
            if not text.strip() or not _has_japanese(text):
                continue
            entry_id = f"RPG_RT.ldb/Vocabulary/0x{fid:02X}"
            entries.append(TranslationEntry(
                id=entry_id,
                file="RPG_RT.ldb",
                field="vocabulary",
                original=text,
                translation="",
                status="untranslated",
                context="[System Vocabulary]",
            ))
        return entries

    def _extract_common_events(self, ce_data: bytes,
                                context_size: int) -> list[TranslationEntry]:
        """Extract dialogue from common events."""
        entries = []
        ces = _parse_array(ce_data)
        for idx, fields in ces:
            if CE_COMMANDS not in fields:
                continue
            commands = _parse_commands(fields[CE_COMMANDS])
            name = _decode_str(fields.get(CE_NAME, b''))
            prefix = f"RPG_RT.ldb/CE{idx}"
            if name:
                prefix = f"RPG_RT.ldb/CE{idx}({name})"
            entries.extend(self._extract_dialogue_commands(
                commands, prefix, "RPG_RT.ldb", context_size))
        return entries

    def _extract_map(self, fpath: str, fname: str,
                     context_size: int) -> list[TranslationEntry]:
        """Extract dialogue from a map file."""
        entries = []
        with open(fpath, 'rb') as f:
            data = f.read()
        header, pos = _read_header(data)
        if header != "LcfMapUnit":
            return entries
        map_chunks, _ = _parse_chunks(data, pos, len(data))

        if MAP_EVENTS not in map_chunks:
            return entries

        events = _parse_array(map_chunks[MAP_EVENTS])
        map_name = fname.replace('.lmu', '')

        for ev_idx, ev_fields in events:
            if EVENT_PAGES not in ev_fields:
                continue
            ev_name = _decode_str(ev_fields.get(EVENT_NAME, b''))
            pages = _parse_array(ev_fields[EVENT_PAGES])

            for page_idx, page_fields in pages:
                if PAGE_COMMANDS not in page_fields:
                    continue
                commands = _parse_commands(page_fields[PAGE_COMMANDS])
                prefix = f"{fname}/Ev{ev_idx}"
                if ev_name:
                    prefix = f"{fname}/Ev{ev_idx}({ev_name})"
                prefix += f"/p{page_idx}"
                entries.extend(self._extract_dialogue_commands(
                    commands, prefix, fname, context_size))
        return entries

    def _extract_dialogue_commands(
        self, commands: list[EventCommand], prefix: str,
        file: str, context_size: int
    ) -> list[TranslationEntry]:
        """Extract translatable text from a command list."""
        entries = []
        recent_context: list[str] = []
        dialogue_index = 0
        i = 0

        while i < len(commands):
            cmd = commands[i]

            if cmd.code == CODE_SHOW_MESSAGE:
                # Gather message block: 10110 + following 20110s
                lines = []
                if cmd.string.strip():
                    lines.append(cmd.string)
                j = i + 1
                while j < len(commands) and commands[j].code == CODE_SHOW_MESSAGE_LINE:
                    if commands[j].string.strip():
                        lines.append(commands[j].string)
                    j += 1

                text = "\n".join(lines)
                if text.strip() and _has_japanese(text):
                    context = "\n".join(recent_context[-context_size:])
                    entry_id = f"{prefix}/dialog_{dialogue_index}"
                    entries.append(TranslationEntry(
                        id=entry_id,
                        file=file,
                        field="dialog",
                        original=text,
                        translation="",
                        status="untranslated",
                        context=context,
                    ))
                    recent_context.append(text[:60])

                dialogue_index += 1
                i = j
                continue

            elif cmd.code == CODE_SHOW_CHOICE:
                # Choice options are in the string, separated by something
                # or individual 20140 branches follow
                # Actually in RM2K, choices are separate 20140 commands
                # with string = choice text
                choices = []
                j = i + 1
                while j < len(commands):
                    if commands[j].code == CODE_SHOW_CHOICE_OPT:
                        if commands[j].string.strip():
                            choices.append(commands[j].string)
                    elif commands[j].code not in (CODE_SHOW_CHOICE_OPT, 0):
                        # Check if it's still within the choice block (by indent)
                        if commands[j].indent <= cmd.indent and commands[j].code != 20141:
                            break
                    j += 1

                for ci, choice_text in enumerate(choices):
                    if _has_japanese(choice_text):
                        entry_id = f"{prefix}/choice_{dialogue_index}_{ci}"
                        entries.append(TranslationEntry(
                            id=entry_id,
                            file=file,
                            field="choice",
                            original=choice_text,
                            translation="",
                            status="untranslated",
                            context="",
                        ))
                dialogue_index += 1
                i += 1
                continue

            elif cmd.code == CODE_CHANGE_HERO_NAME:
                if cmd.string.strip() and _has_japanese(cmd.string):
                    entry_id = f"{prefix}/hero_name_{dialogue_index}"
                    entries.append(TranslationEntry(
                        id=entry_id, file=file, field="name",
                        original=cmd.string, translation="",
                        status="untranslated", context="",
                    ))
                dialogue_index += 1

            elif cmd.code == CODE_CHANGE_HERO_TITLE:
                if cmd.string.strip() and _has_japanese(cmd.string):
                    entry_id = f"{prefix}/hero_title_{dialogue_index}"
                    entries.append(TranslationEntry(
                        id=entry_id, file=file, field="name",
                        original=cmd.string, translation="",
                        status="untranslated", context="",
                    ))
                dialogue_index += 1

            i += 1

        return entries

    # ── Actors ────────────────────────────────────────────────────────

    def load_actors_raw(self, project_dir: str) -> list[dict]:
        """Load actor list for gender dialog."""
        ldb_path = os.path.join(project_dir, "RPG_RT.ldb")
        with open(ldb_path, 'rb') as f:
            ldb_data = f.read()
        _, pos = _read_header(ldb_data)
        db_chunks, _ = _parse_chunks(ldb_data, pos, len(ldb_data))

        actors = []
        if DB_ACTORS not in db_chunks:
            return actors
        for idx, fields in _parse_array(db_chunks[DB_ACTORS]):
            name = _decode_str(fields.get(0x01, b''))
            title = _decode_str(fields.get(0x02, b''))
            if not name.strip():
                continue
            actors.append({
                "id": idx,
                "name": name,
                "nickname": title,
                "profile": "",
            })
        return actors

    # ── Game title ────────────────────────────────────────────────────

    def get_game_title(self, project_dir: str) -> str:
        """Read game title from RPG_RT.ini."""
        ini_path = os.path.join(project_dir, "RPG_RT.ini")
        if not os.path.isfile(ini_path):
            return ""
        try:
            with open(ini_path, 'r', encoding='shift_jis', errors='replace') as f:
                for line in f:
                    if line.strip().lower().startswith("gametitle="):
                        return line.strip().split("=", 1)[1]
        except Exception:
            pass
        return ""

    # ── Export ─────────────────────────────────────────────────────────

    def save_project(self, project_dir: str,
                     entries: list[TranslationEntry]):
        """Write translations back into LCF binary files."""
        # Create backups
        backup_suffix = "_original"
        ldb_path = os.path.join(project_dir, "RPG_RT.ldb")
        ldb_backup = ldb_path.replace(".ldb", f"{backup_suffix}.ldb")
        if not os.path.exists(ldb_backup):
            shutil.copy2(ldb_path, ldb_backup)

        # Build translation map from entries
        trans_map = {}
        for e in entries:
            if e.translation and e.status in ("translated", "reviewed"):
                trans_map[e.id] = e.translation

        if not trans_map:
            log.warning("No translations to export")
            return

        # Export database
        source_ldb = ldb_backup if os.path.isfile(ldb_backup) else ldb_path
        self._export_ldb(source_ldb, ldb_path, trans_map)

        # Export maps
        for fname in sorted(os.listdir(project_dir)):
            if not fname.lower().endswith('.lmu'):
                continue
            fpath = os.path.join(project_dir, fname)
            backup = fpath.replace(".lmu", f"{backup_suffix}.lmu")
            if not os.path.exists(backup):
                shutil.copy2(fpath, backup)
            source = backup if os.path.isfile(backup) else fpath
            try:
                self._export_map(source, fpath, fname, trans_map)
            except Exception as e:
                log.error("Failed to export %s: %s", fname, e)

    def _export_ldb(self, source_path: str, target_path: str,
                    trans_map: dict):
        """Apply translations to database file."""
        with open(source_path, 'rb') as f:
            data = f.read()
        header, pos = _read_header(data)
        db_chunks, _ = _parse_chunks(data, pos, len(data))

        changed = False

        # Apply database field translations
        for db_id, fields_def in DATABASE_FIELDS.items():
            if db_id not in db_chunks:
                continue
            db_name = DB_NAMES.get(db_id, f"DB_0x{db_id:02X}")
            elements = _parse_array(db_chunks[db_id])
            elem_changed = False
            for ei, (idx, elem) in enumerate(elements):
                for field_id, field_name in fields_def:
                    entry_id = f"RPG_RT.ldb/{db_name}/{idx}/{field_name}"
                    if entry_id in trans_map:
                        elem[field_id] = _encode_str(trans_map[entry_id])
                        elem_changed = True
                        elements[ei] = (idx, elem)
            if elem_changed:
                db_chunks[db_id] = _write_array(elements)
                changed = True

        # Apply vocabulary translations
        if DB_VOCABULARY in db_chunks:
            vocab_chunks, _ = _parse_chunks(
                db_chunks[DB_VOCABULARY], 0, len(db_chunks[DB_VOCABULARY]))
            vocab_changed = False
            for fid in list(vocab_chunks):
                entry_id = f"RPG_RT.ldb/Vocabulary/0x{fid:02X}"
                if entry_id in trans_map:
                    vocab_chunks[fid] = _encode_str(trans_map[entry_id])
                    vocab_changed = True
            if vocab_changed:
                db_chunks[DB_VOCABULARY] = _write_chunks(vocab_chunks)
                changed = True

        # Apply common event translations
        if DB_COMMONEVENTS in db_chunks:
            ces = _parse_array(db_chunks[DB_COMMONEVENTS])
            ce_changed = False
            for ci, (idx, fields) in enumerate(ces):
                if CE_COMMANDS not in fields:
                    continue
                name = _decode_str(fields.get(CE_NAME, b''))
                prefix = f"RPG_RT.ldb/CE{idx}"
                if name:
                    prefix = f"RPG_RT.ldb/CE{idx}({name})"
                commands = _parse_commands(fields[CE_COMMANDS])
                if self._apply_command_translations(commands, prefix, trans_map):
                    fields[CE_COMMANDS] = _write_commands(commands)
                    ces[ci] = (idx, fields)
                    ce_changed = True
            if ce_changed:
                db_chunks[DB_COMMONEVENTS] = _write_array(ces)
                changed = True

        if changed:
            out = bytearray()
            out += _write_header(header)
            out += _write_chunks(db_chunks, terminate=False)
            with open(target_path, 'wb') as f:
                f.write(out)
            log.info("Exported translations to %s", target_path)

    def _export_map(self, source_path: str, target_path: str,
                    fname: str, trans_map: dict):
        """Apply translations to a map file."""
        with open(source_path, 'rb') as f:
            data = f.read()
        header, pos = _read_header(data)
        map_chunks, _ = _parse_chunks(data, pos, len(data))

        if MAP_EVENTS not in map_chunks:
            return

        events = _parse_array(map_chunks[MAP_EVENTS])
        any_changed = False

        for ei, (ev_idx, ev_fields) in enumerate(events):
            if EVENT_PAGES not in ev_fields:
                continue
            ev_name = _decode_str(ev_fields.get(EVENT_NAME, b''))
            pages = _parse_array(ev_fields[EVENT_PAGES])

            pages_changed = False
            for pi, (page_idx, page_fields) in enumerate(pages):
                if PAGE_COMMANDS not in page_fields:
                    continue
                commands = _parse_commands(page_fields[PAGE_COMMANDS])
                prefix = f"{fname}/Ev{ev_idx}"
                if ev_name:
                    prefix = f"{fname}/Ev{ev_idx}({ev_name})"
                prefix += f"/p{page_idx}"

                if self._apply_command_translations(commands, prefix, trans_map):
                    page_fields[PAGE_COMMANDS] = _write_commands(commands)
                    pages[pi] = (page_idx, page_fields)
                    pages_changed = True

            if pages_changed:
                ev_fields[EVENT_PAGES] = _write_array(pages)
                events[ei] = (ev_idx, ev_fields)
                any_changed = True

        if any_changed:
            map_chunks[MAP_EVENTS] = _write_array(events)
            out = bytearray()
            out += _write_header(header)
            out += _write_chunks(map_chunks, terminate=False)
            with open(target_path, 'wb') as f:
                f.write(out)

    def _apply_command_translations(
        self, commands: list[EventCommand], prefix: str,
        trans_map: dict
    ) -> bool:
        """Apply translations to event commands in-place. Returns True if changed."""
        changed = False
        dialogue_index = 0
        i = 0

        while i < len(commands):
            cmd = commands[i]

            if cmd.code == CODE_SHOW_MESSAGE:
                # Gather the message block
                j = i + 1
                while j < len(commands) and commands[j].code == CODE_SHOW_MESSAGE_LINE:
                    j += 1
                msg_cmds = commands[i:j]

                entry_id = f"{prefix}/dialog_{dialogue_index}"
                if entry_id in trans_map:
                    translation = trans_map[entry_id]
                    trans_lines = translation.split("\n")

                    # Distribute translation across the command slots
                    for ci, mc in enumerate(msg_cmds):
                        if ci < len(trans_lines):
                            mc.string = trans_lines[ci]
                            mc.string_raw = _encode_str(trans_lines[ci])
                        else:
                            mc.string = ""
                            mc.string_raw = b''
                    changed = True

                dialogue_index += 1
                i = j
                continue

            elif cmd.code == CODE_SHOW_CHOICE:
                j = i + 1
                choice_ci = 0
                while j < len(commands):
                    if commands[j].code == CODE_SHOW_CHOICE_OPT:
                        entry_id = f"{prefix}/choice_{dialogue_index}_{choice_ci}"
                        if entry_id in trans_map:
                            commands[j].string = trans_map[entry_id]
                            commands[j].string_raw = _encode_str(trans_map[entry_id])
                            changed = True
                        choice_ci += 1
                    elif commands[j].indent <= cmd.indent and commands[j].code not in (CODE_SHOW_CHOICE_OPT, 20141, 0):
                        break
                    j += 1
                dialogue_index += 1
                i += 1
                continue

            elif cmd.code == CODE_CHANGE_HERO_NAME:
                entry_id = f"{prefix}/hero_name_{dialogue_index}"
                if entry_id in trans_map:
                    cmd.string = trans_map[entry_id]
                    cmd.string_raw = _encode_str(trans_map[entry_id])
                    changed = True
                dialogue_index += 1

            elif cmd.code == CODE_CHANGE_HERO_TITLE:
                entry_id = f"{prefix}/hero_title_{dialogue_index}"
                if entry_id in trans_map:
                    cmd.string = trans_map[entry_id]
                    cmd.string_raw = _encode_str(trans_map[entry_id])
                    changed = True
                dialogue_index += 1

            i += 1

        return changed

    # ── Restore originals ─────────────────────────────────────────────

    def restore_originals(self, project_dir: str):
        """Restore original LCF files from backups."""
        suffix = "_original"
        restored = 0
        for fname in os.listdir(project_dir):
            if suffix in fname:
                continue
            base, ext = os.path.splitext(fname)
            if ext.lower() not in ('.ldb', '.lmu', '.lmt'):
                continue
            backup = os.path.join(project_dir, base + suffix + ext)
            if os.path.isfile(backup):
                target = os.path.join(project_dir, fname)
                shutil.copy2(backup, target)
                restored += 1
        log.info("Restored %d original files", restored)
