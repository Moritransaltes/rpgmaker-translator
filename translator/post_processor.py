"""Post-processing fixes for translated entries.

Runs automatically after batch translate and on-demand via menu.
All fixes are pure string operations — no LLM needed.
"""

import re
from dataclasses import dataclass

from . import CONTROL_CODE_RE, JAPANESE_RE


@dataclass
class PostProcessResult:
    """Summary of what was fixed."""
    name_dupes: int = 0
    word_per_line: int = 0
    code_leaks: int = 0
    wordwrap_tags: int = 0
    hallucinated_br: int = 0
    double_spaces: int = 0
    trailing_whitespace: int = 0
    capitalize_terms: int = 0
    space_after_name_code: int = 0
    collapsed_color_codes: int = 0
    skill_message_space: int = 0
    spurious_newlines: int = 0
    total_entries_fixed: int = 0
    retranslate_ids: list = None  # Entry IDs that need LLM retranslation

    def __post_init__(self):
        if self.retranslate_ids is None:
            self.retranslate_ids = []

    def __str__(self) -> str:
        parts = []
        if self.name_dupes:
            parts.append(f"{self.name_dupes} name(name) dupes")
        if self.word_per_line:
            parts.append(f"{self.word_per_line} word-per-line")
        if self.code_leaks:
            parts.append(f"{self.code_leaks} placeholder leaks")
        if self.wordwrap_tags:
            parts.append(f"{self.wordwrap_tags} WordWrap tags")
        if self.hallucinated_br:
            parts.append(f"{self.hallucinated_br} hallucinated <br> tags")
        if self.double_spaces:
            parts.append(f"{self.double_spaces} double spaces")
        if self.trailing_whitespace:
            parts.append(f"{self.trailing_whitespace} trailing whitespace")
        if self.capitalize_terms:
            parts.append(f"{self.capitalize_terms} capitalization")
        if self.space_after_name_code:
            parts.append(f"{self.space_after_name_code} missing space after \\n[N]")
        if self.collapsed_color_codes:
            parts.append(f"{self.collapsed_color_codes} collapsed color codes")
        if self.skill_message_space:
            parts.append(f"{self.skill_message_space} skill message leading spaces")
        if self.spurious_newlines:
            parts.append(f"{self.spurious_newlines} spurious newlines in non-dialog")
        if self.retranslate_ids:
            parts.append(f"{len(self.retranslate_ids)} queued for retranslation")
        if not parts:
            return "No issues found"
        return f"{self.total_entries_fixed} entries fixed: " + ", ".join(parts)


# ---------- Regexes ----------

# Name(name) or Name (Name) — LLM appends romanization in parens
_NAME_DUPE_RE = re.compile(r'(\b\w{2,})\s*\(\1\)', re.IGNORECASE)

# <<CODE1>>, <<CODE2>>, etc. — guillemet placeholder leaks
_CODE_LEAK_RE = re.compile(r'\u00abCODE\d+\u00bb')

# <<CODE1>>, <<CODE2>> — angle bracket placeholder leaks
_CODE_LEAK_ANGLE_RE = re.compile(r'<<CODE\d+>>')

# <WordWrap> tag (case insensitive)
_WORDWRAP_TAG_RE = re.compile(r'<WordWrap>', re.IGNORECASE)

# <br> tag (case insensitive) — hallucinated by LLM as line break
_BR_TAG_RE = re.compile(r'<br\s*/?>', re.IGNORECASE)

# Multiple consecutive spaces
_DOUBLE_SPACE_RE = re.compile(r'  +')

# \n[N] NOT followed by a space, newline, punctuation, or end of string
# This catches "\\n[1]She" but not "\\n[1] She" or "\\n[1]\n"
# Only \N[n] needs this fix — it expands to an actor name inline,
# so "\\N[1]She" would render as "HeroShe" in-game.
_NAME_CODE_NO_SPACE_RE = re.compile(r'(\\n\[\d+\])(?=[A-Za-z])')

# Collapsed color codes: \c[N]\c[0] with nothing meaningful between them
_COLLAPSED_COLOR_RE = re.compile(r'(\\c\[\d+\])(\\c\[0\])')

# System term fields that should be title-cased
_SYSTEM_TERM_FIELDS = {
    "terms/commands", "terms/params", "terms/basic",
    "elements", "skillTypes", "weaponTypes", "armorTypes", "equipTypes",
}


def _is_system_term(entry) -> bool:
    """Check if an entry is a System.json term that should be capitalized."""
    if entry.file != "System.json":
        return False
    field = entry.field
    # Direct match: "terms/commands/5", "elements/2", etc.
    for prefix in _SYSTEM_TERM_FIELDS:
        if field.startswith(prefix):
            return True
    return False


def _is_db_short_field(entry) -> bool:
    """Check if entry is a short DB field (name, label) vs dialogue."""
    # Dialogue fields contain "dialog" or are from Map/CommonEvents/Troops
    field = entry.field
    if "dialog" in field or "choice" in field:
        return False
    # DB files: names, descriptions, terms, etc.
    db_files = {
        "Actors.json", "Classes.json", "Skills.json", "Items.json",
        "Weapons.json", "Armors.json", "Enemies.json", "States.json",
        "System.json", "Tilesets.json", "MapInfos.json", "Types.json",
    }
    return entry.file in db_files


def _count_newlines(text: str) -> int:
    """Count actual newline characters in text."""
    return text.count('\n')


def _fix_name_dupes(entry) -> bool:
    """Strip duplicate romanization in parentheses: Ria(ria) → Ria.

    LLMs sometimes append the original reading in parens after
    transliterating a Japanese name, e.g. "Karen (karen)" or "Ria(Ria)".
    """
    trans = entry.translation
    if not trans:
        return False
    new = _NAME_DUPE_RE.sub(r'\1', trans)
    if new != trans:
        entry.translation = new
        return True
    return False


def _fix_word_per_line(entry) -> bool:
    """Fix word-per-line artifacts by comparing newline counts.

    If translation has significantly more newlines than original,
    and most 'lines' are single words, rejoin with spaces.
    """
    orig = entry.original
    trans = entry.translation
    if not trans or '\n' not in trans:
        return False

    orig_newlines = _count_newlines(orig)
    trans_newlines = _count_newlines(trans)

    # Only fix if translation has way more newlines than original
    if trans_newlines <= orig_newlines:
        return False

    # Check if it looks like word-per-line: most lines are single words
    lines = trans.split('\n')
    single_word_lines = sum(1 for line in lines if ' ' not in line.strip() and line.strip())
    total_nonempty = sum(1 for line in lines if line.strip())

    if total_nonempty == 0:
        return False

    word_per_line_ratio = single_word_lines / total_nonempty

    # If 70%+ of lines are single words and we have way more \n than original,
    # this is almost certainly a word-per-line artifact
    if word_per_line_ratio >= 0.7 and trans_newlines >= orig_newlines + 3:
        # Rejoin: replace all \n with spaces, then restore original line breaks
        # For DB/short fields: just join everything with spaces
        if _is_db_short_field(entry):
            fixed = ' '.join(line.strip() for line in lines if line.strip())
            # Collapse multiple spaces
            fixed = _DOUBLE_SPACE_RE.sub(' ', fixed).strip()
            entry.translation = fixed
            return True
        else:
            # For dialogue: join everything, then we'll let word wrap handle it
            fixed = ' '.join(line.strip() for line in lines if line.strip())
            fixed = _DOUBLE_SPACE_RE.sub(' ', fixed).strip()
            entry.translation = fixed
            return True

    return False


def _fix_code_leaks(entry, retranslate_ids: list) -> bool:
    """Strip <<CODE1>>, «CODE1», etc. from translations.

    If stripping leaves orphaned text (e.g. "'s hand" with no subject),
    marks for retranslation instead of just stripping.
    """
    trans = entry.translation
    if not trans:
        return False
    new = _CODE_LEAK_RE.sub('', trans)
    new = _CODE_LEAK_ANGLE_RE.sub('', new)
    if new != trans:
        # Clean up artifacts: double spaces, space before punctuation
        new = _DOUBLE_SPACE_RE.sub(' ', new)
        new = re.sub(r'\s+([.,!?;:])', r'\1', new)
        new = new.strip()
        # Check if stripping left orphaned text (starts with 's, possessive, etc.)
        # or if a line starts with lowercase after removal (missing subject)
        lines = new.split('\n')
        needs_retranslation = False
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("'s ") or stripped.startswith("'s\n"):
                needs_retranslation = True
                break
        if needs_retranslation:
            # Mark for retranslation — clear translation so LLM redo from scratch
            entry.translation = ""
            entry.status = "untranslated"
            retranslate_ids.append(entry.id)
        else:
            entry.translation = new
        return True
    return False


def _fix_wordwrap_tags(entry) -> bool:
    """Strip <WordWrap> from stored translations (injected at export only)."""
    trans = entry.translation
    if not trans:
        return False
    new = _WORDWRAP_TAG_RE.sub('', trans)
    if new != trans:
        entry.translation = new.lstrip()  # Remove leading space left by tag removal
        return True
    return False


def _fix_hallucinated_br(entry) -> bool:
    """Replace <br> with newline when original doesn't contain <br>.

    LLMs sometimes hallucinate <br> tags as line breaks. If the original
    text doesn't have them, they'll show as literal text in-game.
    """
    trans = entry.translation
    if not trans or '<br' not in trans.lower():
        return False
    # Only strip if original doesn't have <br>
    if '<br' in entry.original.lower():
        return False
    new = _BR_TAG_RE.sub('\n', trans)
    if new != trans:
        # Clean up: collapse double newlines from replacement
        new = re.sub(r'\n{3,}', '\n\n', new)
        # Clean up spaces around the replaced newline
        new = re.sub(r' *\n *', '\n', new)
        entry.translation = new
        return True
    return False


def _fix_double_spaces(entry) -> bool:
    """Collapse multiple spaces to single."""
    trans = entry.translation
    if not trans:
        return False
    new = _DOUBLE_SPACE_RE.sub(' ', trans)
    if new != trans:
        entry.translation = new
        return True
    return False


def _fix_trailing_whitespace(entry) -> bool:
    """Strip trailing whitespace and newlines."""
    trans = entry.translation
    if not trans:
        return False
    new = trans.rstrip()
    if new != trans:
        entry.translation = new
        return True
    return False


def _fix_capitalize_terms(entry) -> bool:
    """Title-case System.json menu terms and DB name fields."""
    if not _is_system_term(entry):
        return False
    trans = entry.translation
    if not trans:
        return False

    # Strip control codes for analysis, but preserve them in output
    # Split by \n, title-case each part
    lines = trans.split('\n')
    new_lines = []
    changed = False
    for line in lines:
        if not line:
            new_lines.append(line)
            continue
        # Title case: capitalize first letter of each word
        # But preserve control codes
        words = line.split(' ')
        new_words = []
        for word in words:
            if word and word[0].islower() and not word.startswith('\\'):
                new_words.append(word[0].upper() + word[1:])
                changed = True
            else:
                new_words.append(word)
        new_lines.append(' '.join(new_words))

    if changed:
        entry.translation = '\n'.join(new_lines)
        return True
    return False


def _fix_space_after_name_code(entry) -> bool:
    r"""Insert space after \n[N] when followed directly by a letter.

    Fixes: "\\n[1]She went" → "\\n[1] She went"
    Only \N[n] needs this — it expands to an actor name inline.
    Other codes like \C[n] are invisible, so spacing around them
    is preserved by _extract_codes capturing adjacent whitespace.
    """
    trans = entry.translation
    if not trans:
        return False
    new = _NAME_CODE_NO_SPACE_RE.sub(r'\1 ', trans)
    if new != trans:
        entry.translation = new
        return True
    return False


def _fix_skill_message_space(entry) -> bool:
    """Add leading space to skill/state message fields.

    RPG Maker concatenates ActorName + message directly, so
    "released Curse!" becomes "Novalreleased Curse!" without a space.
    Applies to Skills.json and States.json message1-4 fields.
    """
    if entry.file not in ("Skills.json", "States.json"):
        return False
    if entry.field not in ("message1", "message2", "message3", "message4"):
        return False
    trans = entry.translation
    if not trans or trans.startswith(' '):
        return False
    entry.translation = ' ' + trans
    return True


def _fix_collapsed_color_codes(entry, retranslate_ids: list,
                               glossary: dict | None = None) -> bool:
    r"""Fix \c[N]\c[0] with nothing between them (lost highlight).

    Tries to reconstruct from the original: finds the JP text between
    \c[N]...\c[0] in the original, looks it up in the glossary, and
    inserts the EN name.  Falls back to inserting the raw JP text.
    Only marks for retranslation if reconstruction fails entirely.
    """
    trans = entry.translation
    if not trans:
        return False
    if not _COLLAPSED_COLOR_RE.search(trans):
        return False

    # Extract color-wrapped words from original: \c[N]word\c[0]
    orig = entry.original
    orig_words = re.findall(
        r'\\[cC]\[\d+\]([^\\]+?)\\[cC]\[0\]', orig
    )
    if not orig_words:
        # Can't reconstruct — mark for retranslation
        entry.translation = ""
        entry.status = "untranslated"
        retranslate_ids.append(entry.id)
        return True

    # Build lookup: JP word → EN translation (from glossary or raw JP)
    word_iter = iter(orig_words)

    def _replace_collapsed(m):
        jp_word = next(word_iter, None)
        if jp_word is None:
            return m.group(0)  # no more words to fill
        # Try glossary lookup
        en_word = None
        if glossary:
            en_word = glossary.get(jp_word)
        # Use JP word as fallback (still better than empty)
        if not en_word:
            en_word = jp_word
        return m.group(1) + en_word + m.group(2)

    new = _COLLAPSED_COLOR_RE.sub(_replace_collapsed, trans)
    if new != trans:
        entry.translation = new
        return True
    return False


def _fix_spurious_newlines(entry) -> bool:
    """Strip newlines from non-dialog fields when original had none.

    LLMs sometimes word-wrap short fields like choices, skill messages,
    and item names.  These fields don't go through the message window
    word wrapper, so newlines render as literal line breaks in menus,
    choice windows, and battle log — breaking layout.

    Exempts description fields: RPG Maker's Help Window (Window_Help)
    renders \\n as line breaks, so word wrap newlines are needed there.
    """
    if entry.field in ("dialog", "scroll_text", "description"):
        return False
    trans = entry.translation
    orig = entry.original
    if not trans or '\n' not in trans:
        return False
    # Only strip if original had no newlines
    if '\n' in orig:
        return False
    entry.translation = trans.replace('\n', ' ')
    return True


def run_post_processing(entries: list, verbose: bool = False,
                        glossary: dict | None = None) -> PostProcessResult:
    """Run all post-processing fixes on a list of TranslationEntry objects.

    Modifies entries in-place. Returns a summary of fixes applied.
    Only processes entries with status 'translated' or 'reviewed'.

    Args:
        glossary: Optional JP→EN glossary for reconstructing collapsed
                  color codes (character names the LLM dropped).
    """
    result = PostProcessResult()
    fixed_ids = set()

    for entry in entries:
        if entry.status not in ("translated", "reviewed"):
            continue
        if not entry.translation:
            continue

        entry_fixed = False

        # Tier 0: Name cleanups
        if _fix_name_dupes(entry):
            result.name_dupes += 1
            entry_fixed = True

        # Tier 1: Zero-risk string cleanups
        if _fix_code_leaks(entry, result.retranslate_ids):
            result.code_leaks += 1
            entry_fixed = True

        if _fix_wordwrap_tags(entry):
            result.wordwrap_tags += 1
            entry_fixed = True

        if _fix_hallucinated_br(entry):
            result.hallucinated_br += 1
            entry_fixed = True

        if _fix_spurious_newlines(entry):
            result.spurious_newlines += 1
            entry_fixed = True

        if _fix_word_per_line(entry):
            result.word_per_line += 1
            entry_fixed = True

        if _fix_double_spaces(entry):
            result.double_spaces += 1
            entry_fixed = True

        if _fix_skill_message_space(entry):
            result.skill_message_space += 1
            entry_fixed = True

        if _fix_trailing_whitespace(entry):
            result.trailing_whitespace += 1
            entry_fixed = True

        if _fix_capitalize_terms(entry):
            result.capitalize_terms += 1
            entry_fixed = True

        # Tier 2: Compare-based fixes
        if _fix_space_after_name_code(entry):
            result.space_after_name_code += 1
            entry_fixed = True

        if _fix_collapsed_color_codes(entry, result.retranslate_ids, glossary):
            result.collapsed_color_codes += 1
            entry_fixed = True

        if entry_fixed:
            fixed_ids.add(entry.id)

    result.total_entries_fixed = len(fixed_ids)
    return result
