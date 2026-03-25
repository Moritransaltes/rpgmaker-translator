"""Post-processing fixes for translated entries.

Runs automatically after batch translate and on-demand via menu.
All fixes are pure string operations — no LLM needed.
"""

import re
from dataclasses import dataclass

import wordninja

from . import CONTROL_CODE_RE, TYRANO_CODE_RE, JAPANESE_RE


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
    corrupt_speaker: int = 0
    tyrano_tag_leaks: int = 0
    broken_emb_tags: int = 0
    broken_code_placeholders: int = 0
    hallucinated_tags: int = 0
    extra_tyrano_tags: int = 0
    llm_refusals: int = 0
    quote_mismatches: int = 0
    emb_spacing: int = 0
    dialogue_quotes: int = 0
    missing_spaces: int = 0
    restored_line_breaks: int = 0
    split_words: int = 0
    compound_words: int = 0
    mid_sentence_caps: int = 0
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
        if self.corrupt_speaker:
            parts.append(f"{self.corrupt_speaker} corrupt speaker names")
        if self.tyrano_tag_leaks:
            parts.append(f"{self.tyrano_tag_leaks} TyranoScript tag leaks")
        if self.broken_emb_tags:
            parts.append(f"{self.broken_emb_tags} broken [emb] tags")
        if self.broken_code_placeholders:
            parts.append(f"{self.broken_code_placeholders} broken «CODE» placeholders")
        if self.hallucinated_tags:
            parts.append(f"{self.hallucinated_tags} hallucinated tags")
        if self.extra_tyrano_tags:
            parts.append(f"{self.extra_tyrano_tags} extra [r]/[rr]/[heart] tags")
        if self.llm_refusals:
            parts.append(f"{self.llm_refusals} LLM refusals")
        if self.quote_mismatches:
            parts.append(f"{self.quote_mismatches} quote mismatches (line shift)")
        if self.emb_spacing:
            parts.append(f"{self.emb_spacing} [emb] spacing fixes")
        if self.dialogue_quotes:
            parts.append(f"{self.dialogue_quotes} dialogue quotes stripped")
        if self.missing_spaces:
            parts.append(f"{self.missing_spaces} missing spaces fixed")
        if self.split_words:
            parts.append(f"{self.split_words} split words rejoined")
        if self.restored_line_breaks:
            parts.append(f"{self.restored_line_breaks} line breaks restored")
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

# [CODE1], [CODE2] — square bracket placeholder leaks (hallucinated by LLM)
_CODE_LEAK_SQUARE_RE = re.compile(r'\[CODE\d+\]')

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

# Mid-word space patterns (LLM splits words with spaces: "Dan cer", "Pos it ion")
# Pattern A: word + short fragment(s): "Sque eze", "Pos it ion", "Act ive"
_SPLIT_WORD_3 = re.compile(r"(?<![a-zA-Z'])([A-Za-z]{2,})\s+([a-z]{1,3})\s+([a-z]{1,3})(?=\s|[^a-zA-Z]|$)")
_SPLIT_WORD_2 = re.compile(r"(?<![a-zA-Z'])([A-Za-z]{2,})\s+([a-z]{1,3})(?=\s|[^a-zA-Z]|$)")
# Pattern B: short prefix + long word: "Dis appeared", "Per vert"
_SPLIT_WORD_PREFIX = re.compile(r"(?<![a-zA-Z'])([A-Z][a-z]{1,3})\s+([a-z]{4,})(?=\s|[^a-zA-Z]|$)")

# Common English words that should NOT be merged with adjacent words
_COMMON_WORDS = {
    'a', 'an', 'the', 'is', 'it', 'in', 'on', 'to', 'do', 'no', 'or',
    'at', 'if', 'by', 'he', 'me', 'we', 'my', 'up', 'as', 'so', 'be',
    'of', 'am', 'go', 'us', 'oh', 'ok', 'hi',
    'you', 'for', 'are', 'not', 'but', 'all', 'can', 'her', 'was', 'one',
    'our', 'out', 'day', 'get', 'has', 'him', 'his', 'how', 'its', 'may',
    'new', 'now', 'old', 'see', 'way', 'who', 'did', 'let', 'say', 'she',
    'too', 'use', 'big', 'off', 'try', 'ask', 'men', 'run', 'top', 'yes',
    'yet', 'red', 'set', 'put', 'end', 'why', 'far', 'eye', 'own', 'job',
    'sir', 'and', 'got', 'lot', 'don', 'cum', 'sex', 'hot',
    'turn', 'take', 'look', 'hold', 'pull', 'drop', 'stand', 'show',
    'hang', 'work', 'fall', 'shoot', 'thank', 'every', 'some', 'even',
    'hand', 'blow',
}

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
    new = _CODE_LEAK_SQUARE_RE.sub('', new)
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


def _fix_tyrano_tag_leaks(entry) -> bool:
    """Strip literal TyranoScript tags [r], [p], [l], etc. from translations.

    These tags should have been extracted as «CODE» placeholders before
    translation.  If they appear literally, they were either echoed by
    the LLM or translated before the placeholder system was active.
    """
    trans = entry.translation
    if not trans:
        return False
    new = TYRANO_CODE_RE.sub(' ', trans)
    if new != trans:
        new = re.sub(r'  +', ' ', new).strip()
        entry.translation = new
        return True
    return False


# Broken [emb exp="f.mea] — missing closing quote before ]
# Variable names are short identifiers like f.mea, f.penis, f.you
_BROKEN_EMB_RE = re.compile(r'\[emb\s+exp="([\w.]+)\]')

# Broken [emb exp=f.mea"] — missing opening quote after =
_BROKEN_EMB_RE2 = re.compile(r'\[emb\s+exp=([\w.]+)"\]')

# Broken «CODEn without closing » — followed by anything except »
_BROKEN_CODE_RE = re.compile(r'\u00abCODE(\d+)(?!\u00bb)(?=[\s\[.,;:!?\'")\-\u00ab\]]|$)')

# Hallucinated game tags — LLM invents [pussy], [breasts], etc.
_HALLUCINATED_TAG_RE = re.compile(
    r'\[(?:pussy|breasts|cock|dick|penis|vagina|ass|anus|nipple|cum)\]',
    re.IGNORECASE,
)


def _fix_broken_emb_tags(entry) -> bool:
    """Repair broken [emb exp="...] tags with missing quotes."""
    trans = entry.translation
    if not trans or '[emb' not in trans.lower():
        return False
    new = trans
    # Fix [emb exp="f.mea] → [emb exp="f.mea"]
    new = _BROKEN_EMB_RE.sub(r'[emb exp="\1"]', new)
    # Fix [emb exp=f.mea"] → [emb exp="f.mea"]
    new = _BROKEN_EMB_RE2.sub(r'[emb exp="\1"]', new)
    if new != trans:
        entry.translation = new
        return True
    return False


def _fix_broken_code_placeholders(entry) -> bool:
    """Repair «CODEn missing closing » guillemet."""
    trans = entry.translation
    if not trans or '\u00abCODE' not in trans:
        return False
    new = _BROKEN_CODE_RE.sub('\u00abCODE\\1\u00bb', trans)
    if new != trans:
        entry.translation = new
        return True
    return False


def _fix_hallucinated_tags(entry) -> bool:
    """Strip hallucinated game tags like [pussy], [breasts], etc."""
    trans = entry.translation
    if not trans or '[' not in trans:
        return False
    new = _HALLUCINATED_TAG_RE.sub('', trans)
    if new != trans:
        new = re.sub(r'  +', ' ', new).strip()
        entry.translation = new
        return True
    return False


def _fix_extra_tyrano_tags(entry) -> bool:
    """Remove [r]/[rr]/[heart] tags that weren't in the original.

    LLM sometimes adds extra line break or decoration tags.
    Only strips tags whose count exceeds the original.
    """
    trans = entry.translation
    orig = entry.original
    if not trans or '[' not in trans:
        return False
    changed = False
    for tag in ('[r]', '[rr]', '[heart]'):
        orig_count = orig.lower().count(tag)
        trans_count = trans.lower().count(tag)
        if trans_count > orig_count:
            # Remove excess occurrences from the end
            excess = trans_count - orig_count
            for _ in range(excess):
                # Find last occurrence (case insensitive)
                idx = trans.lower().rfind(tag)
                if idx >= 0:
                    trans = trans[:idx] + trans[idx + len(tag):]
                    changed = True
    if changed:
        trans = re.sub(r'  +', ' ', trans).strip()
        entry.translation = trans
    return changed


# LLM refusal patterns — model refused to translate the content
_REFUSAL_RE = re.compile(
    r"I can'?t translate|I cannot translate|I'?m unable to translate"
    r"|As an AI|sexually explicit|I cannot assist",
    re.IGNORECASE,
)


def _fix_llm_refusal(entry, retranslate_ids: list) -> bool:
    """Detect and clear LLM refusal responses, marking for retranslation."""
    trans = entry.translation
    if not trans:
        return False
    if _REFUSAL_RE.search(trans):
        entry.translation = ""
        entry.status = "untranslated"
        retranslate_ids.append(entry.id)
        return True
    return False


def _fix_quote_mismatch(entry, retranslate_ids: list) -> bool:
    """Detect line-shifted translations where narration got dialogue quotes.

    If original is narration (no Japanese quotes) but translation starts
    with English quotes, the batch JSON likely shifted lines. Clears the
    translation and marks for retranslation.
    """
    if entry.field != "dialog":
        return False
    orig = entry.original
    trans = entry.translation
    if not trans:
        return False
    # Original is dialogue (has Japanese quotes) — skip
    if '「' in orig or '『' in orig:
        return False
    # Translation starts with quote marks — suspicious for narration
    stripped = trans.lstrip()
    if stripped and stripped[0] in ('"', '\u201c', "'"):
        entry.translation = ""
        entry.status = "untranslated"
        retranslate_ids.append(entry.id)
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


# Speaker names should never be this long — if they are, the LLM
# hallucinated a full sentence into the name field.
_MAX_SPEAKER_NAME_LEN = 40


def _fix_corrupt_speaker(entry, retranslate_ids: list) -> bool:
    """Reset speaker_name translations that are obviously corrupt.

    If a speaker name translation is longer than ~40 chars or contains
    newlines, it's almost certainly a hallucinated sentence, not a name.
    Clear it and mark for retranslation.
    """
    if entry.field != "speaker_name":
        return False
    trans = entry.translation
    if not trans:
        return False
    if len(trans) > _MAX_SPEAKER_NAME_LEN or '\n' in trans:
        entry.translation = ""
        entry.status = "untranslated"
        retranslate_ids.append(entry.id)
        return True
    return False


def _fix_dialogue_quotes(entry) -> bool:
    """Strip dialogue quotes from TyranoScript translations.

    Japanese 「」『』 brackets get converted to "" by the LLM, but
    TyranoScript already displays text in speech boxes — quotes are
    redundant visual noise.  Strip all double quotes from translations.
    """
    trans = entry.translation
    if not trans or '"' not in trans:
        return False
    new = trans.replace('"', '')
    # Clean up: collapse double spaces, strip leading/trailing whitespace
    new = re.sub(r'  +', ' ', new).strip()
    if new != trans:
        entry.translation = new
        return True
    return False


def _fix_missing_spaces(entry) -> bool:
    """Fix concatenated words (missing spaces) using wordninja segmentation.

    LLMs sometimes drop spaces between words, producing runs like
    "usingher" or "itseems". Uses wordninja's word frequency model
    to detect and split these.
    """
    trans = entry.translation
    if not trans:
        return False

    try:
        import wordninja
    except ImportError:
        return False

    tag_re = re.compile(r'\[[^\]]+\]')

    def try_split(match):
        word = match.group()
        parts = wordninja.split(word)
        if len(parts) > 1 and all(len(p) >= 2 for p in parts):
            if ''.join(parts).lower() == word.lower():
                return ' '.join(parts)
        return word

    segments = tag_re.split(trans)
    tags = tag_re.findall(trans)

    new_segments = []
    for seg in segments:
        seg = re.sub(r'[a-z]{5,}', try_split, seg)
        new_segments.append(seg)

    result = ''
    for i, seg in enumerate(new_segments):
        result += seg
        if i < len(tags):
            result += tags[i]

    # Also fix camelCase joins (lowercase immediately before uppercase)
    # e.g. "Poweris" -> "Power is", "timeMea" -> "time Mea"
    new_segments2 = []
    for seg in tag_re.split(result):
        seg = re.sub(r'([a-z])([A-Z])', r'\1 \2', seg)
        # Fix comma without space after (but not in numbers like 1,000)
        seg = re.sub(r',([a-zA-Z])', r', \1', seg)
        new_segments2.append(seg)
    tags2 = tag_re.findall(result)
    result = ''
    for i, seg in enumerate(new_segments2):
        result += seg
        if i < len(tags2):
            result += tags2[i]

    result = re.sub(r'  +', ' ', result)
    if result != trans:
        entry.translation = result
        return True
    return False


def _fix_missing_line_breaks(entry) -> bool:
    """Restore [r]/[rr] tags dropped by the LLM during translation.

    TyranoScript uses [r] for line breaks and [rr] for blank lines within
    text blocks. When the LLM drops these tags, consecutive lines in the
    exported .ks file get concatenated without any break, causing text to
    run together on screen.

    Restores trailing [r]/[rr] from the original when completely absent
    from the translation.
    """
    orig = entry.original
    trans = entry.translation
    if not trans or not orig:
        return False

    changed = False

    # Restore missing [rr] — blank line breaks
    orig_rr = orig.lower().count('[rr]')
    trans_rr = trans.lower().count('[rr]')
    if orig_rr > 0 and trans_rr == 0:
        # Stat display lines: replace spaces between stat entries with [rr]
        if re.search(r'[+-]\d', orig):
            new = re.sub(r'\s+(?=[A-Za-z]+ [+-]\d)', '[rr]', trans)
            if orig.rstrip().endswith('[rr]') and not new.rstrip().endswith('[rr]'):
                new = new.rstrip() + '[rr]'
            if new != trans:
                trans = new
                changed = True
        elif orig.rstrip().endswith('[rr]'):
            trans = trans.rstrip() + '[rr]'
            changed = True

    # Restore missing [r] — line breaks
    orig_r = len(re.findall(r'\[r\]', orig, re.IGNORECASE))
    trans_r = len(re.findall(r'\[r\]', trans, re.IGNORECASE))
    if orig_r > 0 and trans_r == 0:
        if re.search(r'\[r\]\s*$', orig, re.IGNORECASE):
            trans = trans.rstrip() + '[r]'
            changed = True

    if changed:
        entry.translation = trans
    return changed


def _fix_emb_spacing(entry) -> bool:
    """Fix spacing around [emb exp="..."] inline variable tags.

    TyranoScript [emb] tags render as inline text (e.g. "Papa", "I").
    LLMs often produce broken spacing around «CODE» placeholders that
    become [emb] tags after restoration:
      - "the[emb ...]" → "the [emb ...]" (missing space before)
      - "[emb ...] 's" → "[emb ...]'s" (unwanted space before possessive)
      - "[emb ...] ," → "[emb ...]," (unwanted space before punctuation)
      - "[emb ...][emb ...]word" → "[emb ...] [emb ...] word" (missing spaces)
    """
    trans = entry.translation
    if not trans or '[emb' not in trans.lower():
        return False

    new = trans
    emb_pat = r'\[emb\s+exp=[^\]]+\]'

    # 1. Remove space between [emb] and possessive 's
    new = re.sub(rf'({emb_pat})\s+(\'s\b)', r'\1\2', new)

    # 2. Remove space between [emb] and punctuation
    new = re.sub(rf'({emb_pat})\s+([.,;:!?\)])', r'\1\2', new)

    # 3. Add space before [emb] when preceded by a word character
    new = re.sub(rf'(\w)(\[emb\s)', r'\1 \2', new)

    # 4. Add space after [emb] when followed by a word character (not 's)
    new = re.sub(rf'({emb_pat})([A-Za-z])', r'\1 \2', new)

    # 5. Add space between consecutive [emb] tags
    new = re.sub(rf'({emb_pat})(\[emb\s)', r'\1 \2', new)

    # 6. Collapse double spaces from above fixes
    new = re.sub(r'  +', ' ', new)

    if new != trans:
        entry.translation = new
        return True
    return False


def _try_merge_fragments(groups: list[str]) -> str | None:
    """Try merging word fragments into a single word using wordninja.

    Returns the merged word if wordninja confirms it's a single word
    and at least one fragment is not a common standalone English word.
    Returns None if the fragments are likely separate real words.
    """
    combined = ''.join(groups)
    if len(combined) < 4:
        return None
    # If all fragments are common standalone words, it's likely a real phrase
    if all(g.lower() in _COMMON_WORDS for g in groups):
        return None
    # Ask wordninja: is this one word?
    parts = wordninja.split(combined.lower())
    if len(parts) == 1:
        # Preserve original capitalization
        if groups[0][0].isupper():
            return combined[0].upper() + combined[1:]
        return combined
    return None


# Common LLM compound word errors — both directions:
# Joined: "eventhough" → "even though"
# Split: "under stood" → "understood", "every one" → "everyone"
_COMPOUND_FIXES = {
    # Wrongly joined
    "eventhough": "even though",
    "evenif": "even if",
    "evenso": "even so",
    "alot": "a lot",
    "abit": "a bit",
    "infact": "in fact",
    "aswell": "as well",
    "ofcourse": "of course",
    "atleast": "at least",
    "infront": "in front",
    "eachother": "each other",
    "noone": "no one",
    "inspite": "in spite",
    "inorder": "in order",
    "asif": "as if",
    # Wrongly split
    "under stood": "understood",
    "with out": "without",
    "some thing": "something",
    "every thing": "everything",
    "any thing": "anything",
    "no thing": "nothing",
    "some one": "someone",
    "every one": "everyone",
    "any one": "anyone",
    "my self": "myself",
    "your self": "yourself",
    "him self": "himself",
    "her self": "herself",
    "them selves": "themselves",
    "our selves": "ourselves",
    "mean while": "meanwhile",
    "al ready": "already",
    "to gether": "together",
    "over come": "overcome",
    "be come": "become",
    "be cause": "because",
    "how ever": "however",
    "al though": "although",
    "break fast": "breakfast",
    "some times": "sometimes",
    "every where": "everywhere",
    "any where": "anywhere",
    "no where": "nowhere",
    "some where": "somewhere",
    "about us": "about us",  # keep — but "aboutus" needs fixing below
    "aboutus": "about us",
    "topicon": "topic on",
    "sen pai": "senpai",
    "sei za": "seiza",
    "under standing": "understanding",
}

# Multi-fragment tokenization fixes — LLM outputs like "Des per at ely"
# These are regex patterns that rejoin fragments into proper words.
_FRAGMENT_FIXES = [
    (re.compile(r'\bDes\s+per\s+at\s+ely\b', re.IGNORECASE), "desperately"),
    (re.compile(r'\bFor\s+tun\s+at\s+ely\b', re.IGNORECASE), "fortunately"),
    (re.compile(r'\bUn\s*for\s+tun\s+at\s+ely\b', re.IGNORECASE), "unfortunately"),
    (re.compile(r'\bRes\s+is\s+t(?:ing|ance|ed|s)?\b', re.IGNORECASE), lambda m: "resist" + m.group(0).split()[-1][len("t"):] if len(m.group(0).split()) > 1 else m.group(0)),
    (re.compile(r'\bRes\s+is\s+ting\b', re.IGNORECASE), "resisting"),
    (re.compile(r'\bRes\s+is\s+tance\b', re.IGNORECASE), "resistance"),
    (re.compile(r'\bAb\s+sol\s+ut\s+ely\b', re.IGNORECASE), "absolutely"),
    (re.compile(r'\bDef\s+in\s+it\s+ely\b', re.IGNORECASE), "definitely"),
    (re.compile(r'\bSep\s+ar\s+at\s+ely\b', re.IGNORECASE), "separately"),
    (re.compile(r'\bIm\s+med\s+i\s+at\s+ely\b', re.IGNORECASE), "immediately"),
    (re.compile(r'\bAp\s+par\s+ent\s+ly\b', re.IGNORECASE), "apparently"),
    (re.compile(r'\bAc\s+ci\s+dent\s+ally?\b', re.IGNORECASE), "accidentally"),
    (re.compile(r'\bCom\s+plet\s+ely\b', re.IGNORECASE), "completely"),
    (re.compile(r'\bIn\s+cred\s+ib\s+ly\b', re.IGNORECASE), "incredibly"),
]

# Build regex: match each key as a whole word (case-insensitive)
_COMPOUND_RE = re.compile(
    r'\b(' + '|'.join(re.escape(k) for k in _COMPOUND_FIXES) + r')\b',
    re.IGNORECASE,
)


def _fix_compound_words(entry) -> bool:
    """Fix LLM compound word errors (joined or split)."""
    trans = entry.translation
    if not trans:
        return False

    def _replace(m):
        word = m.group(0)
        key = word.lower()
        fix = _COMPOUND_FIXES.get(key, word)
        # Preserve original capitalization
        if word[0].isupper() and fix[0].islower():
            fix = fix[0].upper() + fix[1:]
        return fix

    new = _COMPOUND_RE.sub(_replace, trans)

    # Apply multi-fragment tokenization fixes
    for pattern, replacement in _FRAGMENT_FIXES:
        if isinstance(replacement, str):
            new = pattern.sub(lambda m, r=replacement: r[0].upper() + r[1:] if m.group(0)[0].isupper() else r, new)
        else:
            new = pattern.sub(replacement, new)

    # Context-aware: "satin the/a/my/his/her/an" = "sat in the/a/..."
    new = re.sub(
        r'\bsatin\b(?=\s+(?:the|a|an|my|his|her|our|their|this|that|one|it))\b',
        'sat in', new, flags=re.IGNORECASE)

    if new != trans:
        entry.translation = new
        return True
    return False


def _fix_split_words(entry) -> bool:
    """Rejoin words that the LLM split with spaces.

    LLMs sometimes insert spaces mid-word (e.g. "Danc ing", "Vill age",
    "Dis appeared"). Detects fragments, merges them, and verifies with
    wordninja that the result is a real word.
    """
    trans = entry.translation
    if not trans:
        return False

    new = trans
    for _ in range(3):  # Multiple passes for overlapping splits
        prev = new
        # 3-fragment: "Pos it ion", "Caut io us"
        new = _SPLIT_WORD_3.sub(
            lambda m: _try_merge_fragments([m.group(1), m.group(2), m.group(3)]) or m.group(0),
            new)
        # 2-fragment: "Dan cer", "Sque eze", "Act ive"
        new = _SPLIT_WORD_2.sub(
            lambda m: _try_merge_fragments([m.group(1), m.group(2)]) or m.group(0),
            new)
        # Prefix split: "Dis appeared", "Per vert"
        new = _SPLIT_WORD_PREFIX.sub(
            lambda m: _try_merge_fragments([m.group(1), m.group(2)]) or m.group(0),
            new)
        if new == prev:
            break

    if new != trans:
        entry.translation = new
        return True
    return False


def _fix_mid_sentence_caps(entry, known_names: set | None = None) -> bool:
    """Lowercase words that are incorrectly capitalized mid-sentence.

    Only applies to dialogue/scroll_text fields. Skips words after
    sentence-ending punctuation, known names, and common abbreviations.
    """
    if entry.field not in ("dialog", "scroll_text"):
        return False
    trans = entry.translation
    if not trans:
        return False

    # Words that should stay capitalized (common proper nouns / game terms)
    _ALWAYS_CAPS = {
        "I", "I'm", "I'll", "I've", "I'd",
        "OK", "HP", "MP", "SP", "EXP", "ATK", "DEF", "AGI", "LUK",
        "NPC", "RPG", "SNS", "CEO", "VIP",
    }
    safe = _ALWAYS_CAPS | (known_names or set())

    # Pattern: lowercase letter + space + Capitalized word + space + lowercase
    # This catches "the Garden was" but not "Mr. Garden" or "...Garden"
    def _fix(m):
        word = m.group(2)
        if word in safe:
            return m.group(0)
        return m.group(1) + word.lower() + m.group(3)

    new = re.sub(
        r'([a-z] )([A-Z][a-z]{2,})( [a-z])',
        _fix, trans)

    if new != trans:
        entry.translation = new
        return True
    return False


def run_post_processing(entries: list, verbose: bool = False,
                        glossary: dict | None = None,
                        project_type: str = "rpgmaker",
                        fix_capitals: bool = False) -> PostProcessResult:
    """Run all post-processing fixes on a list of TranslationEntry objects.

    Modifies entries in-place. Returns a summary of fixes applied.
    Only processes entries with status 'translated' or 'reviewed'.

    Args:
        glossary: Optional JP→EN glossary for reconstructing collapsed
                  color codes (character names the LLM dropped).
        project_type: "rpgmaker" or "tyranoscript" — controls which fixes run.
    """
    result = PostProcessResult()
    fixed_ids = set()

    # Build known names from glossary (should stay capitalized)
    _known_names = set()
    if glossary:
        for en_term in glossary.values():
            for word in en_term.split():
                if word and word[0].isupper() and len(word) > 1:
                    _known_names.add(word)

    for entry in entries:
        if entry.status not in ("translated", "reviewed"):
            continue
        if not entry.translation:
            continue

        entry_fixed = False

        # Tier 0: LLM refusals (clear and queue for retranslation)
        if _fix_llm_refusal(entry, result.retranslate_ids):
            result.llm_refusals += 1
            entry_fixed = True
            fixed_ids.add(entry.id)
            continue  # Translation cleared, skip remaining fixes

        # Tier 0: Line-shift detection (narration got dialogue quotes)
        if _fix_quote_mismatch(entry, result.retranslate_ids):
            result.quote_mismatches += 1
            entry_fixed = True
            fixed_ids.add(entry.id)
            continue  # Translation cleared, skip remaining fixes

        # Tier 0: Corrupt speaker names (must run first — poisons context)
        if _fix_corrupt_speaker(entry, result.retranslate_ids):
            result.corrupt_speaker += 1
            entry_fixed = True

        # Tier 0: Name cleanups
        if _fix_name_dupes(entry):
            result.name_dupes += 1
            entry_fixed = True

        # Tier 1: Zero-risk string cleanups
        if _fix_code_leaks(entry, result.retranslate_ids):
            result.code_leaks += 1
            entry_fixed = True

        if project_type == "tyranoscript":
            if _fix_broken_emb_tags(entry):
                result.broken_emb_tags += 1
                entry_fixed = True

            if _fix_broken_code_placeholders(entry):
                result.broken_code_placeholders += 1
                entry_fixed = True

            if _fix_hallucinated_tags(entry):
                result.hallucinated_tags += 1
                entry_fixed = True

            if _fix_extra_tyrano_tags(entry):
                result.extra_tyrano_tags += 1
                entry_fixed = True

            if _fix_emb_spacing(entry):
                result.emb_spacing += 1
                entry_fixed = True

            if _fix_dialogue_quotes(entry):
                result.dialogue_quotes += 1
                entry_fixed = True

            if _fix_missing_line_breaks(entry):
                result.restored_line_breaks += 1
                entry_fixed = True

            # Note: _fix_tyrano_tag_leaks (blanket strip) is replaced by the
            # targeted fixes above.  Tags in translations are legitimate —
            # restored from «CODE» placeholders after LLM call.

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

        if _fix_missing_spaces(entry):
            result.missing_spaces += 1
            entry_fixed = True

        if _fix_compound_words(entry):
            result.compound_words += 1
            entry_fixed = True

        if _fix_split_words(entry):
            result.split_words += 1
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

        if fix_capitals and _fix_mid_sentence_caps(entry, _known_names):
            result.mid_sentence_caps += 1
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
