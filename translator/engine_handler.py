"""Engine handler registry — centralizes all engine-specific behavior.

Each game engine (RPG Maker MV/MZ, VX Ace, 2000/2003, TyranoScript, SRPG Studio)
gets a handler subclass that declares its capabilities, parser, display name,
pipeline steps, etc.  The main app dispatches through the active handler instead
of scattering if/elif chains everywhere.
"""

import logging
import os

log = logging.getLogger(__name__)


class EngineHandler:
    """Base class — each engine subclass declares its capabilities."""

    # ── Identity ─────────────────────────────────────────────
    key: str = ""                       # e.g. "rpgmaker", "rpgmaker_2k"
    display_name: str = "Unknown"       # e.g. "RPG Maker MV/MZ"
    backup_description: str = ""        # e.g. "data_original/"

    # ── Capabilities ─────────────────────────────────────────
    has_db_split: bool = False          # Separate DB vs dialogue batch steps
    has_actors: bool = False            # Actor gender system
    has_plugin_system: bool = False     # JS plugins (word wrap injection, etc.)
    has_patch_zip: bool = False         # JSON-based patch zip export
    has_wordwrap: bool = False          # Word wrap step in pipeline
    has_speaker_processing: bool = False  # Namebox / speaker name detection

    # ── Pipeline steps: list of (key, label) ─────────────────
    pipeline_steps: list[tuple[str, str]] = []

    # ── DB files for batch split ─────────────────────────────
    db_files: set[str] = set()
    auto_glossary_fields: dict[str, tuple] = {}

    # ── System prompt key — maps to prompt constants in ai_client.py ──
    system_prompt_key: str | None = None  # matches handler key or None for default

    # ── Default translation settings (per-engine defaults) ────
    default_context_size: int = 10      # dialogue lines sent as context
    default_batch_size: int = 5         # entries per LLM request
    default_workers: int = 2            # parallel translation threads
    default_wordwrap_chars: int = 0     # 0 = auto-detect

    def __init__(self, parser=None):
        self.parser = parser

    # ── Detection ────────────────────────────────────────────

    @staticmethod
    def detect(path: str) -> bool:
        """Return True if path is this engine's project."""
        return False

    # ── Standard interface ───────────────────────────────────

    def load_project(self, path: str, context_size: int = 3) -> list:
        """Parse project, return list[TranslationEntry]."""
        # Set context_size on parser instance (all parsers store it as attribute)
        if hasattr(self.parser, 'context_size'):
            self.parser.context_size = context_size
        # Call load_project without context_size kwarg — not all parsers accept it
        return self.parser.load_project(path)

    def load_actors(self, path: str) -> list[dict]:
        """Load actor list for gender dialog. Returns [] if no actor system."""
        if not self.has_actors:
            return []
        return self.parser.load_actors_raw(path)

    def get_game_title(self, path: str) -> str:
        """Read game title from project files."""
        return self.parser.get_game_title(path)

    def save_project(self, path: str, entries: list):
        """Export translations back to game files."""
        self.parser.save_project(path, entries)

    def restore_originals(self, path: str):
        """Restore original files from backup."""
        self.parser.restore_originals(path)

    def is_valid_project_dir(self, path: str) -> bool:
        """Check if path is still a valid project for this engine."""
        return self.detect(path)

    def get_status_message(self, entries: list) -> str:
        """Status bar message after loading project."""
        file_count = len(set(e.file for e in entries))
        return f"{self.display_name}: {len(entries)} entries from {file_count} files"

    def get_export_message(self, count: int) -> str:
        """Success message after export."""
        return (f"Exported {count} translations.\n"
                f"Original files backed up as {self.backup_description}.")

    def get_restore_message(self) -> str:
        """Success message after restore."""
        return f"Original files restored from {self.backup_description}."

    # ── Wizard labels ────────────────────────────────────────

    def get_export_label(self) -> str | None:
        """Custom export checkbox label, or None for default."""
        return None

    def get_wordwrap_label(self) -> str | None:
        """Custom word wrap checkbox label, or None for default."""
        return None


# ── RPG Maker MV/MZ (shared base) ────────────────────────────

class _RPGMakerJSONBase(EngineHandler):
    """Shared base for RPG Maker MV and MZ — both use JSON data files."""

    backup_description = "data_original/"

    has_db_split = True
    has_actors = True
    has_plugin_system = True
    has_patch_zip = True
    has_wordwrap = True
    has_speaker_processing = True

    pipeline_steps = [
        ("db", "Translate DB"),
        ("dialogue", "Translate Dialogue"),
        ("cleanup", "Clean Up"),
        ("wordwrap", "Word Wrap"),
        ("export", "Export"),
    ]

    db_files = {
        "Actors.json", "Classes.json", "Items.json", "Weapons.json",
        "Armors.json", "Skills.json", "States.json", "Enemies.json",
        "System.json",
    }

    auto_glossary_fields = {
        "Actors.json": ("name", "nickname"),
        "Classes.json": ("name",),
        "Items.json": ("name",),
        "Weapons.json": ("name",),
        "Armors.json": ("name",),
        "Skills.json": ("name",),
        "Enemies.json": ("name",),
        "States.json": ("name",),
    }

    def get_status_message(self, entries: list) -> str:
        plugin_count = sum(1 for e in entries if e.file == "plugins.js")
        base = super().get_status_message(entries)
        if plugin_count:
            base += f" ({plugin_count} plugin entries)"
        return base

    def get_export_message(self, count: int) -> str:
        return (f"Exported {count} translations to game JSON files.\n"
                f"Original files backed up in data_original/.")


class RPGMakerMVHandler(_RPGMakerJSONBase):
    key = "rpgmaker_mv"
    display_name = "RPG Maker MV"
    system_prompt_key = "rpgmaker_mv"

    @staticmethod
    def detect(path: str) -> bool:
        from .rpgmaker_mv import RPGMakerMVParser
        engine = RPGMakerMVParser.detect_engine(path)
        return engine == "mv"


class RPGMakerMZHandler(_RPGMakerJSONBase):
    key = "rpgmaker_mz"
    display_name = "RPG Maker MZ"
    system_prompt_key = "rpgmaker_mz"

    @staticmethod
    def detect(path: str) -> bool:
        from .rpgmaker_mv import RPGMakerMVParser
        engine = RPGMakerMVParser.detect_engine(path)
        return engine == "mz"


# ── RPG Maker VX Ace ─────────────────────────────────────────

class RPGMakerAceHandler(EngineHandler):
    key = "rpgmaker_ace"
    display_name = "RPG Maker VX Ace"
    backup_description = "Data_original/"
    system_prompt_key = "rpgmaker_ace"

    has_db_split = True
    has_actors = True
    has_wordwrap = True
    has_speaker_processing = True

    pipeline_steps = [
        ("db", "Translate DB"),
        ("dialogue", "Translate Dialogue"),
        ("cleanup", "Clean Up"),
        ("wordwrap", "Word Wrap"),
        ("export", "Export"),
    ]

    db_files = {
        "Actors.rvdata2", "Classes.rvdata2", "Items.rvdata2", "Weapons.rvdata2",
        "Armors.rvdata2", "Skills.rvdata2", "States.rvdata2", "Enemies.rvdata2",
        "System.rvdata2",
    }

    auto_glossary_fields = {
        "Actors.rvdata2": ("name", "nickname"),
        "Classes.rvdata2": ("name",),
        "Items.rvdata2": ("name",),
        "Weapons.rvdata2": ("name",),
        "Armors.rvdata2": ("name",),
        "Skills.rvdata2": ("name",),
        "Enemies.rvdata2": ("name",),
        "States.rvdata2": ("name",),
    }

    @staticmethod
    def detect(path: str) -> bool:
        from .rpgmaker_ace import RPGMakerAceParser
        return RPGMakerAceParser.is_ace_project(path)

    def get_export_label(self):
        return "Export translations to .rvdata2 files"

    def get_export_message(self, count: int) -> str:
        return (f"Exported {count} translations to .rvdata2 files.\n"
                f"Original files backed up in Data_original/.")


# ── RPG Maker 2000/2003 ─────────────────────────────────────

class RPGMaker2KHandler(EngineHandler):
    key = "rpgmaker_2k"
    display_name = "RPG Maker 2000/2003"
    backup_description = "RPG_RT_original.ldb + *_original.lmu"
    system_prompt_key = "rpgmaker_2k"

    has_db_split = True
    has_actors = True
    has_wordwrap = True

    default_wordwrap_chars = 50     # ~50 chars/line in RM2K message boxes

    pipeline_steps = [
        ("db", "Translate DB"),
        ("dialogue", "Translate Dialogue"),
        ("cleanup", "Clean Up"),
        ("wordwrap", "Word Wrap"),
        ("export", "Export"),
    ]

    db_files = {"RPG_RT.ldb"}

    auto_glossary_fields = {
        "RPG_RT.ldb": ("name", "nickname"),
    }

    @staticmethod
    def detect(path: str) -> bool:
        from .rpgmaker_2k import RPGMaker2KParser
        return RPGMaker2KParser.is_2k_project(path)

    def get_export_label(self):
        return "Export translations to LCF files"

    def get_export_message(self, count: int) -> str:
        return (f"Exported {count} translations to LCF files.\n"
                f"Original files backed up with _original suffix.")


# ── TyranoScript ─────────────────────────────────────────────

class TyranoScriptHandler(EngineHandler):
    key = "tyranoscript"
    display_name = "TyranoScript"
    backup_description = "scenario_original/"

    has_wordwrap = True
    system_prompt_key = "tyranoscript"

    pipeline_steps = [
        ("dialogue", "Translate"),
        ("cleanup", "Clean Up"),
        ("wordwrap", "Word Wrap"),
        ("export", "Export"),
    ]

    @staticmethod
    def detect(path: str) -> bool:
        from .tyranoscript import TyranoScriptParser
        return TyranoScriptParser.is_tyranoscript_project(path)

    def get_export_label(self):
        return "Export translations to .ks files"

    def get_wordwrap_label(self):
        return "Apply word wrap (visual novel lines)"

    def get_export_message(self, count: int) -> str:
        return (f"Exported {count} translations to .ks files.\n"
                f"Original files backed up in scenario_original/.")


# ── SRPG Studio ──────────────────────────────────────────────

class SRPGStudioHandler(EngineHandler):
    key = "srpgstudio"
    display_name = "SRPG Studio"
    backup_description = "data_original.dts"

    has_wordwrap = True
    system_prompt_key = "srpgstudio"

    default_wordwrap_chars = 50     # ~50 chars/line in SRPG text boxes

    pipeline_steps = [
        ("dialogue", "Translate"),
        ("cleanup", "Clean Up"),
        ("wordwrap", "Word Wrap"),
        ("export", "Export"),
    ]

    @staticmethod
    def detect(path: str) -> bool:
        from .srpgstudio import SRPGStudioParser
        return SRPGStudioParser.is_srpgstudio_project(path)

    def get_export_label(self):
        return "Export translations to data.dts"

    def get_wordwrap_label(self):
        return "Apply word wrap (~50 chars/line)"

    def get_export_message(self, count: int) -> str:
        return (f"Exported {count} translations to data.dts.\n"
                f"Original file backed up as data_original.dts.")


# ── Kirikiri / KAG ──────────────────────────────────────────

class KirikiriHandler(EngineHandler):
    key = "kirikiri"
    display_name = "Kirikiri"
    backup_description = "scenario_original/"

    has_speaker_processing = True
    system_prompt_key = "kirikiri"

    pipeline_steps = [
        ("dialogue", "Translate"),
        ("cleanup", "Clean Up"),
        ("export", "Export"),
    ]

    @staticmethod
    def detect(path: str) -> bool:
        from .kirikiri import KirikiriParser
        return KirikiriParser.is_kirikiri_project(path)

    def get_export_label(self):
        return "Export translations to .ks files"

    def get_export_message(self, count: int) -> str:
        return (f"Exported {count} translations to .ks files.\n"
                f"Original files backed up in scenario_original/.")


# ── Crowd ───────────────────────────────────────────────────

class CrowdHandler(EngineHandler):
    key = "crowd"
    display_name = "Crowd (Experimental)"
    backup_description = "sce_original/"

    has_speaker_processing = True
    system_prompt_key = "crowd"

    pipeline_steps = [
        ("dialogue", "Translate"),
        ("cleanup", "Clean Up"),
        ("export", "Export"),
    ]

    @staticmethod
    def detect(path: str) -> bool:
        from .crowd import CrowdParser
        return CrowdParser.is_crowd_project(path)

    def get_export_label(self):
        return "Export translations to .sce file"

    def get_export_message(self, count: int) -> str:
        return (f"Exported {count} translations to .sce file.\n"
                f"Original file backed up in sce_original/.")


# ── Ren'Py ──────────────────────────────────────────────────

class RenPyHandler(EngineHandler):
    key = "renpy"
    display_name = "Ren'Py"
    backup_description = "game_original/"

    has_wordwrap = True
    system_prompt_key = "renpy"

    pipeline_steps = [
        ("dialogue", "Translate"),
        ("cleanup", "Clean Up"),
        ("wordwrap", "Word Wrap"),
        ("export", "Export"),
    ]

    @staticmethod
    def detect(path: str) -> bool:
        from .renpy import RenPyParser
        return RenPyParser.is_renpy_project(path)

    def get_export_label(self):
        return "Export translations to .rpy files"

    def get_wordwrap_label(self):
        return "Apply word wrap (visual novel lines)"

    def get_export_message(self, count: int) -> str:
        return (f"Exported {count} translations to .rpy files.\n"
                f"Original files backed up in game_original/.")


# ── Wolf RPG ──────────────────────────────────────────────────

class WolfRPGHandler(EngineHandler):
    key = "wolfrpg"
    display_name = "Wolf RPG Editor"
    backup_description = "Data_original/ (or BasicData_original/)"

    has_db_split = True
    has_wordwrap = True
    system_prompt_key = "wolfrpg"

    default_wordwrap_chars = 50     # ~50 chars/line in Wolf RPG message boxes

    pipeline_steps = [
        ("db", "Translate DB"),
        ("dialogue", "Translate Dialogue"),
        ("cleanup", "Clean Up"),
        ("wordwrap", "Word Wrap"),
        ("export", "Export"),
    ]

    db_files = {"Database/DataBase", "Database/CDataBase", "Database/SysDatabase"}

    auto_glossary_fields = {
        "Database/DataBase": ("name",),
        "Database/CDataBase": ("name",),
    }

    @staticmethod
    def detect(path: str) -> bool:
        from .wolfrpg import WolfRPGParser
        return WolfRPGParser.detect(path)

    def get_export_label(self):
        return "Export translations to Wolf RPG data files"

    def get_export_message(self, count: int) -> str:
        return (f"Exported {count} translations to Wolf RPG data files.\n"
                f"Data.wolf renamed to .bak — game reads from Data/ folder.")


# ── Registry ─────────────────────────────────────────────────
# Detection order matters: more specific engines first (RM2K before MV/MZ)

ENGINE_REGISTRY: list[type[EngineHandler]] = [
    CrowdHandler,        # Crowd engine (.sce files)
    KirikiriHandler,     # Kirikiri/KAG (.ks + startup.tjs)
    RenPyHandler,
    TyranoScriptHandler,  # Must be after Kirikiri (both use .ks)
    SRPGStudioHandler,
    WolfRPGHandler,
    RPGMakerAceHandler,
    RPGMaker2KHandler,
    RPGMakerMZHandler,   # MZ before MV (MZ detection is more specific)
    RPGMakerMVHandler,   # Last — fallback for any data/ folder with rpg_core.js
]

# Backward compatibility: old saves used "rpgmaker" for both MV and MZ
_KEY_ALIASES = {"rpgmaker": "rpgmaker_mv"}


def detect_engine(path: str) -> type[EngineHandler] | None:
    """Detect which engine a project folder belongs to.

    Returns the handler CLASS (not instance) or None if unrecognized.
    """
    for handler_cls in ENGINE_REGISTRY:
        try:
            if handler_cls.detect(path):
                return handler_cls
        except Exception:
            log.debug("Detection failed for %s", handler_cls.key, exc_info=True)
    return None


def get_handler_by_key(key: str) -> type[EngineHandler] | None:
    """Look up a handler class by its key string (e.g. from saved state)."""
    key = _KEY_ALIASES.get(key, key)
    for handler_cls in ENGINE_REGISTRY:
        if handler_cls.key == key:
            return handler_cls
    return None
