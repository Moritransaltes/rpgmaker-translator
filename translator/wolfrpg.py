"""Wolf RPG Editor game parser.

Handles:
  - Detection: Game.exe + Data.wolf or Data/ folder
  - DXArchive unpacking: Data.wolf → loose files (via bundled decompyler)
  - Binary parsing: .mps maps, CommonEvent.dat, DataBase.dat
  - Export: writes translated strings back into binary files

Binary format reference: WolfTL by Sinflower (MIT), Wolf_RPG_Decompyler by Daviid-P.
"""

from __future__ import annotations

import os
import re
import struct
import shutil
from pathlib import Path
from typing import Optional

from .project_model import TranslationEntry

# ── Constants ────────────────────────────────────────────────────────────

# Magic bytes (after possible encryption header)
MAGIC_COMMON = bytes([0x57, 0x00, 0x00, 0x4F, 0x4C, 0x00, 0x46, 0x43, 0x00])
MAGIC_DB     = bytes([0x57, 0x00, 0x00, 0x4F, 0x4C, 0x00, 0x46, 0x4D, 0x00])
MAGIC_MAP    = bytes([0x00] * 10 + [0x57, 0x4F, 0x4C, 0x46, 0x4D, 0x00, 0x00, 0x00, 0x00, 0x00])

# Map structure markers
MAP_EVENT_INDICATOR = 0x6F
MAP_EVENT_TERMINATOR = 0x66
MAP_EVENT_MAGIC1 = bytes([0x39, 0x30, 0x00, 0x00])
MAP_EVENT_MAGIC2 = bytes([0x00, 0x00, 0x00, 0x00])
MAP_PAGE_START = 0x79
MAP_PAGE_END = 0x7A
MAP_EVENT_END = 0x70

# CommonEvent markers
CE_HEADER = 0x8E
CE_DATA_INDICATOR = 0x8F
CE_END1 = 0x91
CE_END2 = 0x92

# Command codes (from WolfTL Command.hpp)
CMD_MESSAGE        = 101
CMD_CHOICES        = 102
CMD_COMMENT        = 103
CMD_SET_STRING     = 122
CMD_PICTURE        = 150
CMD_DB             = 250
CMD_COMMON_BY_NAME = 300
CMD_CHOICE_CASE    = 401
CMD_MOVE           = 201

# Japanese detection
JAPANESE_RE = re.compile(r'[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\uFF00-\uFFEF]')

# Filter out file paths, color codes, and non-display strings from DB
_DB_SKIP_RE = re.compile(
    r'^[\w\-/\\]+\.\w{2,4}$'   # file paths (BGM/foo.mp3)
    r'|^\d+$'                   # pure numbers
    r'|^#[0-9A-Fa-f]{3,8}$'    # color codes
    r'|^$',                     # empty
    re.MULTILINE
)


# ── Wolf RPG file decryption ─────────────────────────────────────────────

# Seed indices for decryption (from 10-byte header)
_DAT_SEED_INDICES = (0, 3, 9)        # CommonEvent.dat, DataBase.dat, etc.
_GAMEDAT_SEED_INDICES = (0, 8, 6)    # Game.dat
_DECRYPT_INTERVALS = (1, 2, 5)       # XOR step intervals per seed

# C standard library compatible LCG random (matches MSVC srand/rand)
class _CRand:
    """MSVC-compatible rand() for Wolf RPG decryption."""
    __slots__ = ('_seed',)

    def __init__(self, seed: int):
        self._seed = seed & 0xFFFFFFFF

    def rand(self) -> int:
        self._seed = (self._seed * 214013 + 2531011) & 0xFFFFFFFF
        return (self._seed >> 16) & 0x7FFF


def _decrypt_dat_v32(data: bytes, seed_indices: tuple = _DAT_SEED_INDICES
                     ) -> tuple[bytes, bytes, int]:
    """Decrypt a Wolf RPG v3.2 encrypted .dat file.

    Returns (decrypted_data, crypt_header, proj_key).
    Encrypted files have byte[1] == 0x50.
    """
    if len(data) < 11 or data[1] != 0x50:
        return data, b'', -1  # Not encrypted

    # First 10 bytes are the encryption header
    header = bytearray(data[:10])
    encrypted = bytearray(data[10:])

    # Extract seeds from header at specified indices
    seeds = [header[i] for i in seed_indices]

    # XOR decrypt using seeded random
    for pass_idx, seed in enumerate(seeds):
        rng = _CRand(seed)
        interval = _DECRYPT_INTERVALS[pass_idx]
        for j in range(0, len(encrypted), interval):
            encrypted[j] ^= (rng.rand() >> 12) & 0xFF

    # After decryption: first 5 bytes are magic-ish, then uint32 key_size
    # Then proj_key (1 byte) + remaining key bytes
    key_size = struct.unpack_from('<I', encrypted, 5)[0] if len(encrypted) > 9 else 0
    proj_key = encrypted[9] if len(encrypted) > 9 and key_size > 0 else -1

    # Skip past: 5 bytes + 4 bytes key_size + key_size bytes
    skip = 5 + 4 + key_size
    decrypted = bytes(encrypted[skip:])

    return decrypted, bytes(header), proj_key


def _decrypt_project(data: bytes, proj_key: int) -> bytes:
    """Decrypt a .project file using the project key from the .dat file."""
    if proj_key < 0:
        return data  # No encryption

    rng = _CRand(proj_key)
    result = bytearray(data)
    for i in range(len(result)):
        result[i] ^= rng.rand() & 0xFF
    return bytes(result)


# Global project key shared between .dat and .project files
_g_proj_key: int = -1


def _set_proj_key(key: int):
    global _g_proj_key
    if _g_proj_key == -1:
        _g_proj_key = key


def _get_proj_key() -> int:
    return _g_proj_key


def _reset_proj_key():
    global _g_proj_key
    _g_proj_key = -1


def _lz4_decompress(reader: 'BinaryReader') -> bytes:
    """LZ4 decompress data from current reader position.

    Format: uint32 decompressed_size + uint32 compressed_size + compressed_data.
    Returns decompressed data with the original file header prepended.
    """
    try:
        import lz4.block
    except ImportError:
        raise ImportError(
            'lz4 package required for compressed Wolf RPG files. '
            'Install with: pip install lz4'
        )

    dec_size = reader.read_uint32()
    enc_size = reader.read_uint32()
    compressed = reader.read(enc_size)

    return lz4.block.decompress(compressed, uncompressed_size=dec_size)


# ── Binary reader ────────────────────────────────────────────────────────

class BinaryReader:
    """Lightweight binary reader for Wolf RPG files."""

    __slots__ = ('data', 'pos', 'is_utf8')

    def __init__(self, data: bytes | bytearray, is_utf8: bool = False):
        self.data = data
        self.pos = 0
        self.is_utf8 = is_utf8

    def read(self, n: int) -> bytes:
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def read_byte(self) -> int:
        val = self.data[self.pos]
        self.pos += 1
        return val

    def read_uint32(self) -> int:
        val = struct.unpack_from('<I', self.data, self.pos)[0]
        self.pos += 4
        return val

    def read_string(self) -> str:
        length = self.read_uint32()
        if length == 0:
            return ''
        raw = self.read(length)
        # Strip trailing null
        if raw and raw[-1] == 0:
            raw = raw[:-1]
        if self.is_utf8:
            return raw.decode('utf-8', errors='replace')
        return raw.decode('cp932', errors='replace')

    def skip(self, n: int):
        self.pos += n

    def eof(self) -> bool:
        return self.pos >= len(self.data)

    def peek(self) -> int:
        return self.data[self.pos]


# ── Binary writer ────────────────────────────────────────────────────────

class BinaryWriter:
    """Writes Wolf RPG binary data."""

    __slots__ = ('buf', 'is_utf8')

    def __init__(self, is_utf8: bool = False):
        self.buf = bytearray()
        self.is_utf8 = is_utf8

    def write(self, data: bytes | bytearray):
        self.buf.extend(data)

    def write_byte(self, val: int):
        self.buf.append(val & 0xFF)

    def write_uint32(self, val: int):
        self.buf.extend(struct.pack('<I', val))

    def write_string(self, s: str):
        if self.is_utf8:
            encoded = s.encode('utf-8') + b'\x00'
        else:
            encoded = s.encode('cp932', errors='replace') + b'\x00'
        self.write_uint32(len(encoded))
        self.write(encoded)

    def getvalue(self) -> bytes:
        return bytes(self.buf)


# ── Command parsing ──────────────────────────────────────────────────────

class WolfCommand:
    """A single Wolf RPG event command."""

    __slots__ = ('code', 'int_args', 'string_args', 'indent',
                 'move_data', 'v35_data')

    def __init__(self, code: int, int_args: list[int],
                 string_args: list[str], indent: int):
        self.code = code
        self.int_args = int_args
        self.string_args = string_args
        self.indent = indent
        self.move_data = b''   # Extra data for Move commands
        self.v35_data = b''    # Extra data for v3.5

    @staticmethod
    def read(reader: BinaryReader) -> 'WolfCommand':
        arg_count = reader.read_byte() - 1
        code = reader.read_uint32()
        int_args = [reader.read_uint32() for _ in range(arg_count)]
        indent = reader.read_byte()
        str_count = reader.read_byte()
        string_args = [reader.read_string() for _ in range(str_count)]

        cmd = WolfCommand(code, int_args, string_args, indent)

        terminator = reader.read_byte()
        if terminator == 0x01:
            # Move command has extra data after terminator
            cmd.move_data = _read_move_data(reader)
        elif terminator != 0x00:
            raise ValueError(f'Bad command terminator: {terminator:#x}')

        return cmd

    def write(self, writer: BinaryWriter):
        writer.write_byte(len(self.int_args) + 1)
        writer.write_uint32(self.code)
        for arg in self.int_args:
            writer.write_uint32(arg)
        writer.write_byte(self.indent)
        writer.write_byte(len(self.string_args))
        for s in self.string_args:
            writer.write_string(s)
        if self.move_data:
            writer.write_byte(0x01)
            writer.write(self.move_data)
        else:
            writer.write_byte(0x00)

    def is_translatable(self) -> bool:
        """Check if this command contains translatable text."""
        if not self.string_args:
            return False
        if self.code == CMD_MESSAGE:
            return True
        if self.code == CMD_CHOICES:
            return True
        if self.code == CMD_SET_STRING:
            return any(JAPANESE_RE.search(s) for s in self.string_args)
        if self.code == CMD_PICTURE:
            # Only picture-as-text (type bits 4-6 == 2)
            if self.int_args:
                pic_type = (self.int_args[0] >> 4) & 0x07
                if pic_type == 2:  # text type
                    return any(JAPANESE_RE.search(s) for s in self.string_args)
            return False
        return False


def _read_move_data(reader: BinaryReader) -> bytes:
    """Read the extra data after a Move command (route commands)."""
    start = reader.pos
    # 5 unknown bytes + 1 flags byte
    reader.skip(6)
    route_count = reader.read_uint32()
    for _ in range(route_count):
        _id = reader.read_byte()
        arg_count = reader.read_byte()
        reader.skip(arg_count * 4)
        # Route terminator: 01 00
        reader.skip(2)
    return reader.data[start:reader.pos]


# ── Route command skip helper for writing ────────────────────────────────

def _read_route_commands(reader: BinaryReader) -> list:
    """Read route commands (for Page parsing), return raw bytes."""
    count = reader.read_uint32()
    start = reader.pos - 4
    for _ in range(count):
        reader.read_byte()   # id
        argc = reader.read_byte()
        reader.skip(argc * 4)
        reader.skip(2)       # terminator 01 00
    return reader.data[start:reader.pos]


# ── Map parsing ──────────────────────────────────────────────────────────

class WolfMap:
    """Parses a Wolf RPG .mps map file."""

    def __init__(self, path: Path):
        self.path = path
        self.name = path.stem
        self.events: list[WolfEvent] = []
        self._header_data = b''
        self._tile_data = b''
        self._raw = b''

    def load(self):
        self._raw = self.path.read_bytes()
        reader = BinaryReader(self._raw)

        # Verify magic
        magic = reader.read(20)
        if magic != MAGIC_MAP:
            raise ValueError(f'{self.path.name}: bad map magic')

        version = reader.read_uint32()
        unknown2 = reader.read_byte()
        unknown3_str = reader.read_string()

        tileset_id = reader.read_uint32()
        width = reader.read_uint32()
        height = reader.read_uint32()
        event_count = reader.read_uint32()

        # v3.5 extra fields
        layer_count = 3
        if version >= 0x67:
            reader.read_uint32()  # unknown4
            layer_count = reader.read_uint32()

        # Tile data
        tile_size = width * height * layer_count * 4
        self._tile_data = reader.read(tile_size)

        # Save everything up to events as header
        self._header_data = self._raw[:reader.pos]

        # Read events
        while reader.peek() == MAP_EVENT_INDICATOR:
            reader.read_byte()  # consume indicator
            ev = WolfEvent.read(reader)
            self.events.append(ev)

        terminator = reader.read_byte()
        if terminator != MAP_EVENT_TERMINATOR:
            raise ValueError(f'{self.path.name}: bad map terminator {terminator:#x}')

    def save(self, path: Optional[Path] = None):
        """Write map back to file with modified commands."""
        writer = BinaryWriter()
        writer.write(self._header_data)
        for ev in self.events:
            writer.write_byte(MAP_EVENT_INDICATOR)
            ev.write(writer)
        writer.write_byte(MAP_EVENT_TERMINATOR)
        target = path or self.path
        target.write_bytes(writer.getvalue())


class WolfEvent:
    """A map event containing pages of commands."""

    __slots__ = ('id', 'name', 'x', 'y', 'pages', '_magic1', '_magic2')

    @staticmethod
    def read(reader: BinaryReader) -> 'WolfEvent':
        ev = WolfEvent()
        ev._magic1 = reader.read(4)
        if ev._magic1 != MAP_EVENT_MAGIC1:
            raise ValueError(f'Bad event magic1: {ev._magic1.hex()}')
        ev.id = reader.read_uint32()
        ev.name = reader.read_string()
        ev.x = reader.read_uint32()
        ev.y = reader.read_uint32()
        page_count = reader.read_uint32()
        ev._magic2 = reader.read(4)

        ev.pages = []
        page_id = 0
        while reader.peek() == MAP_PAGE_START:
            reader.read_byte()
            page = WolfPage.read(reader, page_id)
            ev.pages.append(page)
            page_id += 1

        end = reader.read_byte()
        if end != MAP_EVENT_END:
            raise ValueError(f'Bad event end: {end:#x}')

        if len(ev.pages) != page_count:
            raise ValueError(f'Page count mismatch: expected {page_count}, got {len(ev.pages)}')

        return ev

    def write(self, writer: BinaryWriter):
        writer.write(self._magic1)
        writer.write_uint32(self.id)
        writer.write_string(self.name)
        writer.write_uint32(self.x)
        writer.write_uint32(self.y)
        writer.write_uint32(len(self.pages))
        writer.write(self._magic2)
        for page in self.pages:
            writer.write_byte(MAP_PAGE_START)
            page.write(writer)
        writer.write_byte(MAP_EVENT_END)


class WolfPage:
    """An event page containing commands."""

    __slots__ = ('id', 'commands', '_pre_command_data', '_post_command_data',
                 '_features')

    @staticmethod
    def read(reader: BinaryReader, page_id: int) -> 'WolfPage':
        page = WolfPage()
        page.id = page_id

        # Pre-command data: unknown1(4) + graphic_name(str) + 4 bytes +
        #   conditions(1+4+16+16) + movement(4) + flags(1) + route_flags(1) + routes
        pre_start = reader.pos
        reader.read_uint32()     # unknown1
        reader.read_string()     # graphic name
        reader.skip(4)           # direction, frame, opacity, render mode
        reader.skip(1 + 4 + 16 + 16)  # conditions
        reader.skip(4)           # movement
        reader.skip(1)           # flags
        reader.skip(1)           # route flags
        # Route commands
        route_count = reader.read_uint32()
        for _ in range(route_count):
            reader.read_byte()   # id
            argc = reader.read_byte()
            reader.skip(argc * 4)
            reader.skip(2)       # terminator 01 00
        page._pre_command_data = reader.data[pre_start:reader.pos]

        # Commands
        cmd_count = reader.read_uint32()
        page.commands = []
        for _ in range(cmd_count):
            cmd = WolfCommand.read(reader)
            page.commands.append(cmd)

        # Post-command data: features(4) + shadow(1) + collision_w(1) + collision_h(1)
        post_start = reader.pos
        page._features = reader.read_uint32()
        reader.skip(3)  # shadow, collision w/h
        if page._features > 3:
            reader.skip(1)  # page transfer
        page._post_command_data = reader.data[post_start:reader.pos]

        terminator = reader.read_byte()
        if terminator != MAP_PAGE_END:
            raise ValueError(f'Bad page terminator: {terminator:#x}')

        return page

    def write(self, writer: BinaryWriter):
        writer.write(self._pre_command_data)
        writer.write_uint32(len(self.commands))
        for cmd in self.commands:
            cmd.write(writer)
        writer.write(self._post_command_data)
        writer.write_byte(MAP_PAGE_END)


# ── CommonEvent parsing ──────────────────────────────────────────────────

class WolfCommonEvent:
    """A single common event."""

    __slots__ = ('id', 'int_id', 'name', 'description', 'commands',
                 '_pre_data', '_post_data')

    @staticmethod
    def read(reader: BinaryReader, idx: int) -> 'WolfCommonEvent':
        ce = WolfCommonEvent()
        ce.id = idx

        indicator = reader.read_byte()
        if indicator != CE_HEADER:
            raise ValueError(f'Bad CE header: {indicator:#x}')

        ce.int_id = reader.read_uint32()
        unknown1 = reader.read_uint32()
        unknown2 = reader.read(7)
        ce._pre_data = struct.pack('<I', unknown1) + unknown2

        ce.name = reader.read_string()

        cmd_count = reader.read_uint32()
        ce.commands = [WolfCommand.read(reader) for _ in range(cmd_count)]

        # Capture everything from unknown11 onward as _post_data
        # (unknown11 + description + all trailing structures)
        post_start = reader.pos
        unknown11 = reader.read_string()
        ce.description = reader.read_string()
        indicator = reader.read_byte()
        if indicator != CE_DATA_INDICATOR:
            raise ValueError(f'Bad CE data indicator: {indicator:#x}')

        # unknown3: string array
        count = reader.read_uint32()
        for _ in range(count):
            reader.read_string()

        # unknown4: byte array
        count = reader.read_uint32()
        reader.skip(count)

        # unknown5: array of string arrays
        count = reader.read_uint32()
        for _ in range(count):
            sub_count = reader.read_uint32()
            for _ in range(sub_count):
                reader.read_string()

        # unknown6: array of int arrays
        count = reader.read_uint32()
        for _ in range(count):
            sub_count = reader.read_uint32()
            reader.skip(sub_count * 4)

        # unknown7: 0x1D bytes
        reader.skip(0x1D)

        # unknown8: 100 strings
        for _ in range(100):
            reader.read_string()

        # End markers
        indicator = reader.read_byte()
        if indicator != CE_END1:
            raise ValueError(f'Bad CE end1: {indicator:#x}')
        reader.read_string()  # unknown9

        indicator = reader.read_byte()
        if indicator == CE_END2:
            reader.read_string()  # unknown10
            reader.read_uint32()  # unknown12
            end = reader.read_byte()
            if end != CE_END2:
                raise ValueError(f'Bad CE end2: {end:#x}')
        elif indicator != CE_END1:
            raise ValueError(f'Bad CE final: {indicator:#x}')

        ce._post_data = reader.data[post_start:reader.pos]

        return ce

    def write(self, writer: BinaryWriter):
        writer.write_byte(CE_HEADER)
        writer.write_uint32(self.int_id)
        writer.write(self._pre_data)
        writer.write_string(self.name)
        writer.write_uint32(len(self.commands))
        for cmd in self.commands:
            cmd.write(writer)
        # _post_data includes unknown11 + description + all trailing structures
        writer.write(self._post_data)


class WolfCommonEvents:
    """Container for all common events from CommonEvent.dat."""

    def __init__(self, path: Path):
        self.path = path
        self.events: list[WolfCommonEvent] = []
        self._magic = b''
        self._version = 0
        self._terminator = 0

    def load(self):
        data = self.path.read_bytes()

        # Handle encryption
        crypt_header = b''
        if len(data) > 5 and data[1] == 0x50:
            crypt_ver = data[5]
            if crypt_ver >= 0x57:
                raise ValueError('V3.5 encrypted CommonEvent.dat not supported yet')
            elif crypt_ver >= 0x55:
                raise ValueError('V3.3 encrypted CommonEvent.dat not supported yet')
            # V3.2: seeded XOR
            data, crypt_header, proj_key = _decrypt_dat_v32(data, _DAT_SEED_INDICES)
            if proj_key >= 0:
                _set_proj_key(proj_key)

        reader = BinaryReader(data)

        # Read magic (first byte is 0x00 prefix for unencrypted)
        if not crypt_header:
            first = reader.read_byte()
            magic = reader.read(len(MAGIC_COMMON))
            if magic != MAGIC_COMMON:
                raise ValueError(f'Bad CommonEvent magic: {magic.hex()}')
            self._magic = bytes([first]) + magic
        else:
            self._magic = crypt_header  # Preserve for potential re-export

        self._version = reader.read_byte()

        event_count = reader.read_uint32()
        for i in range(event_count):
            ce = WolfCommonEvent.read(reader, i)
            self.events.append(ce)

        self._terminator = reader.read_byte()

    def save(self, path: Optional[Path] = None):
        writer = BinaryWriter()
        writer.write(self._magic)
        writer.write_byte(self._version)
        writer.write_uint32(len(self.events))
        for ce in self.events:
            ce.write(writer)
        writer.write_byte(self._terminator)
        target = path or self.path
        target.write_bytes(writer.getvalue())


# ── Database parsing ─────────────────────────────────────────────────────

class WolfDatabase:
    """Parses DataBase.dat + .project file pairs for translatable strings.

    Wolf RPG databases use a two-file format:
      - .project file: type/field definitions (names, field types, structure)
      - .dat file: actual data values keyed by the project structure

    Each type has fields that are either int or string (index_info >= 0x07D0).
    Each data entry stores int values first, then string values.
    """

    # Type separator in .dat files
    DAT_TYPE_SEP = bytes([0xFE, 0xFF, 0xFF, 0xFF])
    STRING_INDICATOR = 0x0001D4C0
    FIELD_STRING_START = 0x07D0
    FIELD_INT_START = 0x03E8

    def __init__(self, dat_path: Path, project_path: Optional[Path] = None):
        self.dat_path = dat_path
        self.project_path = project_path or dat_path.with_suffix('.project')
        self.name = dat_path.stem
        self.strings: list[tuple[int, int, int, str]] = []  # (type_idx, data_idx, field_idx, text)
        self._types: list[dict] = []  # Parsed type info from .project

    def load(self):
        """Parse database files and extract translatable strings."""
        if not self.project_path.is_file():
            return  # Can't parse .dat without .project definitions

        dat_data = self.dat_path.read_bytes()
        proj_data = self.project_path.read_bytes()

        # Handle .dat encryption
        if len(dat_data) > 5 and dat_data[1] == 0x50:
            crypt_ver = dat_data[5]
            if crypt_ver >= 0x57:
                print(f'  Skipping {self.name}: V3.5 encryption not supported yet')
                return
            elif crypt_ver >= 0x55:
                print(f'  Skipping {self.name}: V3.3 encryption not supported yet')
                return
            # V3.2: seeded XOR
            dat_data, _header, proj_key = _decrypt_dat_v32(
                dat_data, _DAT_SEED_INDICES)
            if proj_key >= 0:
                _set_proj_key(proj_key)

        # Handle .project encryption (uses proj_key from .dat)
        proj_key = _get_proj_key()
        if proj_key >= 0:
            proj_data = _decrypt_project(proj_data, proj_key)

        # Parse .project file first to get type/field structure
        self._parse_project(proj_data)

        # Parse .dat file using the structure from .project
        self._parse_dat(dat_data)

    def _parse_project(self, data: bytes):
        """Parse .project file for type and field definitions."""
        reader = BinaryReader(data)
        try:
            type_count = reader.read_uint32()
            if type_count > 1000:
                return  # Probably encrypted/corrupt
        except Exception:
            return  # Encrypted or corrupt

        for _ in range(type_count):
            type_info = {}
            type_info['name'] = reader.read_string()

            # Field definitions
            field_count = reader.read_uint32()
            fields = []
            for _ in range(field_count):
                field_name = reader.read_string()
                fields.append({'name': field_name})
            type_info['fields'] = fields

            # Data names
            data_count = reader.read_uint32()
            data_names = []
            for _ in range(data_count):
                data_names.append(reader.read_string())
            type_info['data_names'] = data_names

            # Description
            type_info['description'] = reader.read_string()

            # Field type list
            field_type_list_size = reader.read_uint32()
            for i in range(min(field_count, field_type_list_size)):
                fields[i]['type'] = reader.read_byte()
            # Skip remaining
            remaining = field_type_list_size - min(field_count, field_type_list_size)
            if remaining > 0:
                reader.skip(remaining)

            # Unknown1 strings per field
            count = reader.read_uint32()
            for i in range(count):
                reader.read_string()

            # StringArgs per field
            count = reader.read_uint32()
            for i in range(count):
                sub_count = reader.read_uint32()
                for _ in range(sub_count):
                    reader.read_string()

            # IntArgs per field
            count = reader.read_uint32()
            for i in range(count):
                sub_count = reader.read_uint32()
                reader.skip(sub_count * 4)

            # Default values per field
            count = reader.read_uint32()
            reader.skip(count * 4)

            self._types.append(type_info)

    def _parse_dat(self, data: bytes):
        """Parse .dat file using structure from .project."""
        reader = BinaryReader(data)

        # Skip magic (1 byte prefix + 9 bytes magic)
        reader.skip(10)

        # Version byte — if 0xC4, the rest is LZ4 compressed
        version = reader.read_byte()
        if version == 0xC4:
            data = _lz4_decompress(reader)
            reader = BinaryReader(data)
            # Decompressed data starts at the version byte
            version = reader.read_byte()

        # Type count
        type_count = reader.read_uint32()
        if type_count != len(self._types):
            return  # Mismatch, skip

        for type_idx in range(type_count):
            try:
                self._parse_dat_type(reader, type_idx)
            except Exception:
                break  # Stop on error, keep what we have

        # Should end with version byte
        # (not critical if we miss it)

    def _parse_dat_type(self, reader: BinaryReader, type_idx: int):
        """Parse one type from the .dat file."""
        type_info = self._types[type_idx]

        # Type separator: FE FF FF FF
        sep = reader.read(4)
        if sep != self.DAT_TYPE_SEP:
            raise ValueError(f'Bad type separator: {sep.hex()}')

        unknown1 = reader.read_uint32()
        fields_size = reader.read_uint32()

        if unknown1 == self.STRING_INDICATOR:
            reader.read_string()  # unknown2

        # Read field index_info values
        field_infos = []
        for _ in range(fields_size):
            index_info = reader.read_uint32()
            is_string = index_info >= self.FIELD_STRING_START
            is_int = index_info >= self.FIELD_INT_START and not is_string
            field_infos.append({
                'index_info': index_info,
                'is_string': is_string,
                'is_valid': is_string or is_int,
            })

        # Data count
        data_count = reader.read_uint32()

        # Read data entries
        int_count = sum(1 for f in field_infos if f['is_valid'] and not f['is_string'])
        str_count = sum(1 for f in field_infos if f['is_string'])

        for data_idx in range(data_count):
            # Int values first
            reader.skip(int_count * 4)

            # String values
            for si in range(str_count):
                text = reader.read_string()
                if (text and JAPANESE_RE.search(text)
                        and not _DB_SKIP_RE.match(text.split('\n')[0])):
                    self.strings.append((type_idx, data_idx, si, text))


# ── DXArchive unpacker ───────────────────────────────────────────────────

def unpack_data_wolf(game_dir: Path) -> Path:
    """Unpack Data.wolf into a Data/ folder using Wolf_RPG_Decompyler.

    Returns the path to the data folder.
    """
    wolf_file = game_dir / 'Data.wolf'
    data_dir = game_dir / 'Data'

    if not wolf_file.is_file():
        raise FileNotFoundError(f'No Data.wolf found in {game_dir}')

    # Try importing the decompyler from _reference
    import sys
    ref_dir = Path(__file__).parent.parent / '_reference'
    if str(ref_dir) not in sys.path:
        sys.path.insert(0, str(ref_dir))

    try:
        from Wolf_RPG_Decompyler import decompiler_pairs
    except ImportError:
        raise ImportError(
            'Wolf_RPG_Decompyler not found in _reference/. '
            'Clone https://github.com/Daviid-P/Wolf_RPG_Decompyler into _reference/'
        )

    # Decompile to Data/ folder (extract everything, not just Game.dat)
    temp_dir = game_dir / 'decompiled_temp'
    if temp_dir.exists():
        shutil.rmtree(temp_dir)

    success = False
    for decompiler, key in decompiler_pairs:
        try:
            success = decompiler.decodeArchive(
                archivePath=wolf_file,
                outputPath=temp_dir,
                only_game_dat=False,
                keyString_=key,
            )
            if success:
                break
        except Exception:
            continue

    if not success:
        raise RuntimeError(f'Failed to unpack {wolf_file}')

    # If Data/ already exists (e.g. partial), merge
    if data_dir.is_dir():
        # Copy temp contents into existing Data/
        for item in temp_dir.iterdir():
            target = data_dir / item.name
            if item.is_dir():
                if target.is_dir():
                    shutil.copytree(item, target, dirs_exist_ok=True)
                else:
                    shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)
        shutil.rmtree(temp_dir)
    else:
        temp_dir.rename(data_dir)

    return data_dir


# ── Main parser ──────────────────────────────────────────────────────────

class WolfRPGParser:
    """Parser for Wolf RPG Editor games."""

    def __init__(self):
        self.game_dir: Optional[Path] = None
        self.data_dir: Optional[Path] = None
        self.maps: list[WolfMap] = []
        self.common_events: Optional[WolfCommonEvents] = None
        self.databases: list[WolfDatabase] = []
        self.context_size: int = 3

    # ── Detection ─────────────────────────────────────────────────────

    @staticmethod
    def detect(folder: str) -> bool:
        """Check if folder is a Wolf RPG game."""
        p = Path(folder)
        has_wolf = (p / 'Data.wolf').is_file()
        has_data = (p / 'Data').is_dir()

        # Check for Game.exe (case-insensitive) or any .exe with Wolf RPG markers
        has_exe = (p / 'Game.exe').is_file() or (p / 'game.exe').is_file()
        if not has_exe:
            # Accept if Data.wolf exists (exe may be renamed)
            if not has_wolf:
                return False

        # Must have Data.wolf or Data/ folder with .mps files
        if has_wolf:
            return True
        if has_data:
            mps = list((p / 'Data').glob('**/*.mps'))
            return len(mps) > 0

        return False

    def get_game_title(self, project_dir: str) -> str:
        """Read game title from Game.dat or folder name."""
        p = Path(project_dir)
        game_dat = p / 'Game.dat'
        if game_dat.is_file():
            try:
                data = game_dat.read_bytes()
                # Game.dat starts with title string in Wolf RPG format
                reader = BinaryReader(data)
                # Skip magic bytes (varies), try to read first string
                # Wolf RPG Game.dat: 4 bytes magic + title string
                if len(data) > 20:
                    reader.skip(4)
                    title = reader.read_string()
                    if title and len(title) < 200:
                        return title
            except Exception:
                pass
        return p.name

    def restore_originals(self, project_dir: str):
        """Restore original game files from backup."""
        if not self.data_dir:
            self.game_dir = Path(project_dir)
            self.data_dir = self._find_data_dir()
        if not self.data_dir:
            log.warning("No data directory found for restore")
            return

        backup_dir = self.data_dir.parent / (self.data_dir.name + '_original')
        if not backup_dir.is_dir():
            log.warning("No backup found at %s", backup_dir)
            return

        import shutil
        shutil.rmtree(self.data_dir)
        shutil.copytree(backup_dir, self.data_dir)
        log.info("Restored %s from backup", self.data_dir.name)

    # ── Loading ───────────────────────────────────────────────────────

    def load_project(self, folder: str, context_size: int | None = None) -> list[TranslationEntry]:
        """Load all translatable strings from a Wolf RPG game."""
        if context_size is not None:
            self.context_size = context_size
        self.game_dir = Path(folder)
        entries: list[TranslationEntry] = []

        # Reset global decryption state
        _reset_proj_key()

        # Find data directory
        self.data_dir = self._find_data_dir()

        # Parse maps
        map_dir = self.data_dir / 'MapData'
        if map_dir.is_dir():
            for mps_file in sorted(map_dir.glob('*.mps')):
                try:
                    wolf_map = WolfMap(mps_file)
                    wolf_map.load()
                    self.maps.append(wolf_map)
                    entries.extend(self._extract_map_entries(wolf_map, self.context_size))
                except Exception as e:
                    print(f'Warning: failed to parse {mps_file.name}: {e}')

        # Parse common events
        basic_dir = self.data_dir / 'BasicData'
        ce_path = basic_dir / 'CommonEvent.dat' if basic_dir.is_dir() else None
        if ce_path and ce_path.is_file():
            try:
                self.common_events = WolfCommonEvents(ce_path)
                self.common_events.load()
                entries.extend(self._extract_ce_entries(self.context_size))
            except Exception as e:
                print(f'Warning: failed to parse CommonEvent.dat: {e}')

        # Parse databases (skip SysDataBaseBasic — system config only, no text)
        if basic_dir and basic_dir.is_dir():
            for db_name in ('DataBase.dat', 'CDataBase.dat',
                            'SysDatabase.dat'):
                db_path = basic_dir / db_name
                if db_path.is_file():
                    try:
                        db = WolfDatabase(db_path)
                        db.load()
                        self.databases.append(db)
                        entries.extend(self._extract_db_entries(db))
                    except Exception as e:
                        print(f'Warning: failed to parse {db_name}: {e}')

        # Assign IDs as string paths (file/field) for event viewer compatibility
        for entry in entries:
            entry.id = f'{entry.file}/{entry.field}'

        return entries

    def _find_data_dir(self) -> Path:
        """Find or create the Data/ directory."""
        # Check Data/ folder with actual game content
        data_dir = self.game_dir / 'Data'
        if data_dir.is_dir():
            has_maps = (data_dir / 'MapData').is_dir()
            has_basic = (data_dir / 'BasicData').is_dir()
            if has_maps or (has_basic and any(
                    (data_dir / 'BasicData').glob('CommonEvent.dat'))):
                return data_dir

        # Check for loose BasicData/MapData in game root
        if (self.game_dir / 'BasicData').is_dir():
            if (self.game_dir / 'MapData').is_dir():
                return self.game_dir

        # Need to unpack Data.wolf
        wolf_file = self.game_dir / 'Data.wolf'
        if wolf_file.is_file():
            return unpack_data_wolf(self.game_dir)

        raise FileNotFoundError(
            f'No Data/ folder or Data.wolf in {self.game_dir}')

    # ── Entry extraction ──────────────────────────────────────────────

    def _extract_map_entries(self, wolf_map: WolfMap,
                             context_size: int) -> list[TranslationEntry]:
        """Extract translatable entries from a map."""
        entries = []
        recent_context = []
        file_name = f'MapData/{wolf_map.name}'

        for ev in wolf_map.events:
            for page in ev.pages:
                for ci, cmd in enumerate(page.commands):
                    if not cmd.is_translatable():
                        continue

                    # Build context from recent dialogue
                    context = '\n'.join(recent_context[-context_size:]) if context_size else ''

                    if cmd.code == CMD_MESSAGE:
                        text = cmd.string_args[0] if cmd.string_args else ''
                        if not text or not JAPANESE_RE.search(text):
                            continue
                        # Extract speaker from message format: @N\nSpeaker：\nText
                        speaker = self._extract_speaker(text)
                        entry = TranslationEntry(
                            id=0,
                            file=file_name,
                            field=f'Ev{ev.id}/p{page.id}/msg{ci}',
                            original=self._clean_message(text),
                            translation='',
                            status='untranslated',
                            context=f'[Speaker: {speaker}]\n{context}' if speaker else context,
                        )
                        entries.append(entry)
                        recent_context.append(text[:80])

                    elif cmd.code == CMD_CHOICES:
                        for si, choice in enumerate(cmd.string_args):
                            if choice and JAPANESE_RE.search(choice):
                                entry = TranslationEntry(
                                    id=0,
                                    file=file_name,
                                    field=f'Ev{ev.id}/p{page.id}/choice{ci}_{si}',
                                    original=choice,
                                    translation='',
                                    status='untranslated',
                                    context=context,
                                )
                                entries.append(entry)

                    elif cmd.code == CMD_SET_STRING:
                        for si, s in enumerate(cmd.string_args):
                            if s and JAPANESE_RE.search(s):
                                entry = TranslationEntry(
                                    id=0,
                                    file=file_name,
                                    field=f'Ev{ev.id}/p{page.id}/str{ci}_{si}',
                                    original=s,
                                    translation='',
                                    status='untranslated',
                                    context=context,
                                )
                                entries.append(entry)

                    elif cmd.code == CMD_PICTURE:
                        text = cmd.string_args[0] if cmd.string_args else ''
                        if text and JAPANESE_RE.search(text):
                            entry = TranslationEntry(
                                id=0,
                                file=file_name,
                                field=f'Ev{ev.id}/p{page.id}/pic{ci}',
                                original=text,
                                translation='',
                                status='untranslated',
                                context=context,
                            )
                            entries.append(entry)

        return entries

    def _extract_ce_entries(self, context_size: int) -> list[TranslationEntry]:
        """Extract translatable entries from common events."""
        entries = []

        for ce in self.common_events.events:
            recent_context = []
            file_name = 'CommonEvents'

            for ci, cmd in enumerate(ce.commands):
                if not cmd.is_translatable():
                    continue

                context = '\n'.join(recent_context[-context_size:]) if context_size else ''

                if cmd.code == CMD_MESSAGE:
                    text = cmd.string_args[0] if cmd.string_args else ''
                    if not text or not JAPANESE_RE.search(text):
                        continue
                    speaker = self._extract_speaker(text)
                    entry = TranslationEntry(
                        id=0,
                        file=file_name,
                        field=f'CE{ce.int_id}/msg{ci}',
                        original=self._clean_message(text),
                        translation='',
                        status='untranslated',
                        context=f'[Speaker: {speaker}]\n{context}' if speaker else context,
                    )
                    entries.append(entry)
                    recent_context.append(text[:80])

                elif cmd.code == CMD_CHOICES:
                    for si, choice in enumerate(cmd.string_args):
                        if choice and JAPANESE_RE.search(choice):
                            entry = TranslationEntry(
                                id=0,
                                file=file_name,
                                field=f'CE{ce.int_id}/choice{ci}_{si}',
                                original=choice,
                                translation='',
                                status='untranslated',
                                context=context,
                            )
                            entries.append(entry)

                elif cmd.code == CMD_SET_STRING:
                    for si, s in enumerate(cmd.string_args):
                        if s and JAPANESE_RE.search(s):
                            entry = TranslationEntry(
                                id=0,
                                file=file_name,
                                field=f'CE{ce.int_id}/str{ci}_{si}',
                                original=s,
                                translation='',
                                status='untranslated',
                                context=context,
                            )
                            entries.append(entry)

                elif cmd.code == CMD_PICTURE:
                    text = cmd.string_args[0] if cmd.string_args else ''
                    if text and JAPANESE_RE.search(text):
                        entry = TranslationEntry(
                            id=0,
                            file=file_name,
                            field=f'CE{ce.int_id}/pic{ci}',
                            original=text,
                            translation='',
                            status='untranslated',
                            context=context,
                        )
                        entries.append(entry)

        return entries

    def _extract_db_entries(self, db: WolfDatabase) -> list[TranslationEntry]:
        """Extract translatable entries from a database file."""
        entries = []
        file_name = f'Database/{db.name}'

        for type_idx, data_idx, field_idx, text in db.strings:
            entry = TranslationEntry(
                id=0,
                file=file_name,
                field=f'Type{type_idx}/Data{data_idx}/F{field_idx}',
                original=text,
                translation='',
                status='untranslated',
                context='',
            )
            entries.append(entry)

        return entries

    # ── Message helpers ───────────────────────────────────────────────

    @staticmethod
    def _extract_speaker(text: str) -> str:
        """Extract speaker name from Wolf RPG message format.

        Format: @N\\nSpeaker：\\nText  or  Speaker：\\nText
        """
        # Try multi-line pattern first: @N\nSpeaker：\n
        match = re.match(r'@\d+\r?\n(.+?)：', text)
        if match:
            return match.group(1).strip()
        # Try single-line pattern: Speaker：
        lines = text.split('\n')
        for line in lines:
            if line.startswith('@'):
                continue
            match = re.match(r'^(.+?)：', line)
            if match:
                return match.group(1).strip()
        return ''

    @staticmethod
    def _clean_message(text: str) -> str:
        """Clean message text for display.

        Removes @N prefixes and speaker：prefix, keeps just the dialogue.
        """
        # Try to extract just the dialogue part
        match = re.search(r'@\d+\r?\n.*?：\r?\n(.*)', text, re.DOTALL)
        if match:
            return match.group(1).strip()
        match = re.search(r'^.*?：\r?\n(.*)', text, re.DOTALL)
        if match:
            return match.group(1).strip()
        # No speaker format, return as-is but strip @N prefix
        text = re.sub(r'^@\d+\r?\n', '', text)
        return text.strip()

    # ── Export ────────────────────────────────────────────────────────

    def save_project(self, project_dir: str, entries: list[TranslationEntry]):
        """Write translations back into game files."""
        if not self.data_dir:
            return

        # Create backup on first export
        backup_dir = self.data_dir.parent / (self.data_dir.name + '_original')
        if not backup_dir.exists():
            shutil.copytree(self.data_dir, backup_dir)

        # Re-read from backup for idempotent re-export
        self._reload_from_backup(backup_dir)

        # Build lookup: field → translation
        translations = {}
        for entry in entries:
            if entry.translation and entry.status in ('translated', 'reviewed'):
                translations[f'{entry.file}/{entry.field}'] = entry.translation

        # Apply to maps
        for wolf_map in self.maps:
            file_name = f'MapData/{wolf_map.name}'
            modified = False
            for ev in wolf_map.events:
                for page in ev.pages:
                    for ci, cmd in enumerate(page.commands):
                        if cmd.code == CMD_MESSAGE and cmd.string_args:
                            key = f'{file_name}/Ev{ev.id}/p{page.id}/msg{ci}'
                            if key in translations:
                                # Rebuild full message with speaker prefix
                                original = cmd.string_args[0]
                                translated = translations[key]
                                cmd.string_args[0] = self._rebuild_message(
                                    original, translated)
                                modified = True

                        elif cmd.code == CMD_CHOICES:
                            for si in range(len(cmd.string_args)):
                                key = f'{file_name}/Ev{ev.id}/p{page.id}/choice{ci}_{si}'
                                if key in translations:
                                    cmd.string_args[si] = translations[key]
                                    modified = True

                        elif cmd.code == CMD_SET_STRING:
                            for si in range(len(cmd.string_args)):
                                key = f'{file_name}/Ev{ev.id}/p{page.id}/str{ci}_{si}'
                                if key in translations:
                                    cmd.string_args[si] = translations[key]
                                    modified = True

                        elif cmd.code == CMD_PICTURE:
                            key = f'{file_name}/Ev{ev.id}/p{page.id}/pic{ci}'
                            if key in translations and cmd.string_args:
                                cmd.string_args[0] = translations[key]
                                modified = True

            if modified:
                # Write to live data dir (map was loaded from backup)
                live_path = self.data_dir / 'MapData' / wolf_map.path.name
                wolf_map.save(live_path)

        # Apply to common events
        if self.common_events:
            file_name = 'CommonEvents'
            modified = False
            for ce in self.common_events.events:
                for ci, cmd in enumerate(ce.commands):
                    if cmd.code == CMD_MESSAGE and cmd.string_args:
                        key = f'{file_name}/CE{ce.int_id}/msg{ci}'
                        if key in translations:
                            original = cmd.string_args[0]
                            translated = translations[key]
                            cmd.string_args[0] = self._rebuild_message(
                                original, translated)
                            modified = True

                    elif cmd.code == CMD_CHOICES:
                        for si in range(len(cmd.string_args)):
                            key = f'{file_name}/CE{ce.int_id}/choice{ci}_{si}'
                            if key in translations:
                                cmd.string_args[si] = translations[key]
                                modified = True

                    elif cmd.code == CMD_SET_STRING:
                        for si in range(len(cmd.string_args)):
                            key = f'{file_name}/CE{ce.int_id}/str{ci}_{si}'
                            if key in translations:
                                cmd.string_args[si] = translations[key]
                                modified = True

                    elif cmd.code == CMD_PICTURE:
                        key = f'{file_name}/CE{ce.int_id}/pic{ci}'
                        if key in translations and cmd.string_args:
                            cmd.string_args[0] = translations[key]
                            modified = True

            if modified:
                live_ce = self.data_dir / 'CommonEvent.dat'
                self.common_events.save(live_ce)

        # Apply to databases
        for db in self.databases:
            file_name = f'Database/{db.name}'
            db_trans = {}
            for key, val in translations.items():
                if key.startswith(file_name + '/'):
                    # key = "Database/DataBase/Type0/Data1/F2"
                    field_part = key[len(file_name) + 1:]
                    db_trans[field_part] = val
            if db_trans:
                backup_dat = backup_dir / 'BasicData' / f'{db.name}.dat'
                live_dat = self.data_dir / 'BasicData' / f'{db.name}.dat'
                if backup_dat.is_file():
                    self._export_database(db, backup_dat, live_dat, db_trans)

        # Delete Data.wolf so game reads from folder
        wolf_file = self.game_dir / 'Data.wolf'
        if wolf_file.is_file():
            wolf_file.rename(self.game_dir / 'Data.wolf.bak')

    def _export_database(self, db: WolfDatabase, src_path: Path,
                         dst_path: Path, translations: dict[str, str]):
        """Patch database .dat file with translations.

        Reads from src_path, replaces strings, writes to dst_path.
        translations keys are like "Type0/Data1/F2".
        """
        data = bytearray(src_path.read_bytes())

        # Handle encryption the same way as load
        crypt_header = b''
        if len(data) > 5 and data[1] == 0x50:
            crypt_ver = data[5]
            if crypt_ver >= 0x55:
                return  # Can't handle V3.3+ encryption
            dec_data, crypt_header, _proj_key = _decrypt_dat_v32(
                bytes(data), _DAT_SEED_INDICES)
            data = bytearray(dec_data)

        reader = BinaryReader(bytes(data))
        reader.skip(10)  # magic
        version = reader.read_byte()

        if version == 0xC4:
            return  # LZ4 DB export not supported yet

        type_count = reader.read_uint32()
        if type_count != len(db._types):
            return

        # Build list of (byte_offset, old_len_bytes, new_string) patches
        patches = []
        for type_idx in range(type_count):
            type_info = db._types[type_idx]
            sep = reader.read(4)
            if sep != db.DAT_TYPE_SEP:
                return  # format error

            unknown1 = reader.read_uint32()
            fields_size = reader.read_uint32()

            if unknown1 == db.STRING_INDICATOR:
                reader.read_string()

            field_infos = []
            for _ in range(fields_size):
                index_info = reader.read_uint32()
                is_string = index_info >= db.FIELD_STRING_START
                is_int = index_info >= db.FIELD_INT_START and not is_string
                field_infos.append({
                    'is_string': is_string,
                    'is_valid': is_string or is_int,
                })

            data_count = reader.read_uint32()
            int_count = sum(1 for f in field_infos if f['is_valid'] and not f['is_string'])
            str_count = sum(1 for f in field_infos if f['is_string'])

            for data_idx in range(data_count):
                reader.skip(int_count * 4)
                for si in range(str_count):
                    str_offset = reader.pos  # offset of length prefix
                    text = reader.read_string()
                    key = f'Type{type_idx}/Data{data_idx}/F{si}'
                    if key in translations:
                        # Calculate old string byte length (4-byte length + encoded string)
                        enc = 'utf-8' if reader.is_utf8 else 'cp932'
                        old_bytes = text.encode(enc, errors='replace')
                        new_bytes = translations[key].encode(enc, errors='replace')
                        # Patch: replace length(4) + old_bytes with length(4) + new_bytes
                        patches.append((str_offset, 4 + len(old_bytes), new_bytes))

        # Apply patches in reverse order to preserve offsets
        for offset, old_total_len, new_bytes in reversed(patches):
            new_len_prefix = struct.pack('<I', len(new_bytes))
            data[offset:offset + old_total_len] = new_len_prefix + new_bytes

        # Re-encrypt if needed
        if crypt_header:
            # Wolf RPG .dat re-encryption not yet supported
            # Write unencrypted for now — game should still read it
            pass

        dst_path.parent.mkdir(parents=True, exist_ok=True)
        dst_path.write_bytes(bytes(data))
        log.info("Exported DB translations to %s", dst_path.name)

    def _reload_from_backup(self, backup_dir: Path):
        """Re-read maps and common events from backup for idempotent re-export."""
        # Reload maps
        self.maps = []
        map_dir = backup_dir / 'MapData'
        if map_dir.is_dir():
            for mps in sorted(map_dir.glob('*.mps')):
                try:
                    wolf_map = WolfMap(mps)
                    wolf_map.load()
                    self.maps.append(wolf_map)
                except Exception:
                    log.debug("Skip map %s on reload", mps.name, exc_info=True)

        # Reload common events
        ce_path = backup_dir / 'CommonEvent.dat'
        if ce_path.is_file():
            try:
                self.common_events = WolfCommonEvents(ce_path)
                self.common_events.load()
            except Exception:
                log.debug("Skip CE reload", exc_info=True)

    @staticmethod
    def _rebuild_message(original: str, translated: str) -> str:
        """Rebuild full message with original speaker prefix + translated text."""
        # Check if original had @N\nSpeaker：\n prefix
        match = re.match(r'(@\d+\r?\n.*?：\r?\n)', original)
        if match:
            return match.group(1) + translated
        match = re.match(r'(.*?：\r?\n)', original)
        if match:
            return match.group(1) + translated
        return translated

    def restore_originals(self, folder: str):
        """Restore original files from backup."""
        game_dir = Path(folder)
        data_dir = self.data_dir or self._find_data_dir()
        backup_dir = data_dir.parent / (data_dir.name + '_original')
        if backup_dir.exists():
            shutil.rmtree(data_dir)
            shutil.copytree(backup_dir, data_dir)

        # Restore Data.wolf if we renamed it
        wolf_bak = game_dir / 'Data.wolf.bak'
        if wolf_bak.is_file() and not (game_dir / 'Data.wolf').is_file():
            wolf_bak.rename(game_dir / 'Data.wolf')

    # ── Game title ────────────────────────────────────────────────────

    def get_game_title(self, folder: str) -> str:
        """Try to read game title from Game.dat."""
        game_dat = Path(folder) / 'Data' / 'BasicData' / 'Game.dat'
        if not game_dat.is_file():
            game_dat = Path(folder) / 'BasicData' / 'Game.dat'
        if not game_dat.is_file():
            # Try decompiled path
            game_dat = Path(folder) / 'decompiled_all' / 'BasicData' / 'Game.dat'
        if not game_dat.is_file():
            return Path(folder).name

        try:
            data = game_dat.read_bytes()
            # Handle encryption
            if len(data) > 5 and data[1] == 0x50:
                crypt_ver = data[5]
                if crypt_ver < 0x55:
                    data, _, proj_key = _decrypt_dat_v32(
                        data, _GAMEDAT_SEED_INDICES)
                    if proj_key >= 0:
                        _set_proj_key(proj_key)
                else:
                    return Path(folder).name  # V3.3+ not supported
            else:
                # Unencrypted: skip 10-byte magic prefix
                data = data[10:]
            reader = BinaryReader(data)
            # Game.dat format: byte_array (unknown1), uint32 (string_count),
            # then string[0] = title
            unknown1_count = reader.read_uint32()
            reader.skip(unknown1_count)  # skip unknown1 byte array
            string_count = reader.read_uint32()
            if string_count < 1:
                return Path(folder).name
            title = reader.read_string()
            return title if title else Path(folder).name
        except Exception:
            return Path(folder).name
