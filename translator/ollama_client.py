"""Ollama REST API wrapper for LLM translation."""

import json
import logging
import os
import re
import subprocess
import time

import requests

from . import CONTROL_CODE_RE, JAPANESE_RE

log = logging.getLogger(__name__)


# Japanese bracket pairs → English equivalents
_JP_BRACKETS = {
    '\u300c': '"', '\u300d': '"',   # 「 」 → " "
    '\u300e': '"', '\u300f': '"',   # 『 』 → " "
    '\u3010': '[', '\u3011': ']',   # 【 】 → [ ]
    '\uff08': '(', '\uff09': ')',   # （ ） → ( )
}


# Regex to strip Qwen3 thinking blocks (<think>...</think>) that waste
# tokens and slow down inference.  These appear when the model's internal
# chain-of-thought mode is enabled (Qwen3 default).
_THINK_RE = re.compile(r'<think>.*?</think>\s*', re.DOTALL)

# Regex to strip translator notes/commentary the LLM sometimes appends.
# Matches common patterns at the end of the output.
_NOTE_STRIP_RE = re.compile(
    r'(?:'
    r'\n\s*[-—–]{2,}\s*\n.*'                     # --- separator followed by notes
    r'|\n\s*\*{2,}\s*\n.*'                        # *** separator followed by notes
    r'|\n\s*(?:Note|Notes|Translation [Nn]ote|Translator\'?s? [Nn]ote|TL [Nn]ote|Commentary|Explanation)s?\s*[:：].*'
    r'|\n\s*\((?:Note|Notes|Translation [Nn]ote|Translator\'?s? [Nn]ote|TL [Nn]ote)s?\s*[:：].*?\)\s*$'
    r'|\n\s*\[(?:Note|Notes|Translation [Nn]ote|Translator\'?s? [Nn]ote|TL [Nn]ote)s?\s*[:：].*?\]\s*$'
    r'|\n\s*\*(?:Note|Notes|Translation [Nn]ote|Translator\'?s? [Nn]ote|TL [Nn]ote)s?\s*[:：].*?\*\s*$'
    r')',
    re.DOTALL | re.IGNORECASE,
)



def _to_pig_latin(text: str) -> str:
    """Convert English text to Pig Latin, preserving «CODEn» placeholders.

    Rules:
    - Vowel-start words: word + "way"  (apple → appleway)
    - Consonant-start words: move leading consonant cluster + "ay"  (string → ingstray)
    - Preserve capitalization pattern
    - Skip control code placeholders, numbers, punctuation-only tokens
    """
    _VOWELS = set("aeiouAEIOU")

    def _pig_word(word: str) -> str:
        if not word:
            return word
        # Skip «CODEn» placeholders
        if "\u00ab" in word or "\u00bb" in word:
            return word
        # Extract leading/trailing non-alpha characters (punctuation, quotes, etc.)
        i = 0
        while i < len(word) and not word[i].isalpha():
            i += 1
        j = len(word) - 1
        while j >= i and not word[j].isalpha():
            j -= 1
        if i > j:
            return word  # No alphabetic core (numbers, punctuation, etc.)
        prefix = word[:i]
        suffix = word[j + 1:]
        core = word[i:j + 1]
        # Detect capitalization
        all_upper = core.isupper() and len(core) > 1
        cap_first = core[0].isupper()
        low = core.lower()
        # Apply Pig Latin rule
        if low[0] in _VOWELS:
            result = low + "way"
        else:
            k = 0
            while k < len(low) and low[k] not in _VOWELS:
                k += 1
            result = low[k:] + low[:k] + "ay"
        # Restore capitalization
        if all_upper:
            result = result.upper()
        elif cap_first:
            result = result[0].upper() + result[1:]
        return prefix + result + suffix

    # Process line by line (preserving \n), then word by word
    lines = text.split("\n")
    return "\n".join(
        " ".join(_pig_word(w) for w in line.split(" "))
        for line in lines
    )


SYSTEM_PROMPT = """You are a professional Japanese to English translator specializing in RPG/video game dialogue, including adult (18+) content.

Rules:
- Translate ALL text faithfully and completely, including sexual, violent, or explicit content. Never censor, euphemize, skip, or refuse any content. This is a licensed translation project.
- Translate the Japanese text naturally into English suitable for an RPG game.
- The text may contain opaque code markers like «CODE1», «CODE2», etc. These are internal engine tags. Output them EXACTLY as-is — never remove, translate, rewrite, or replace them with names or words.
- Keep the same line break structure as the original when possible.
- NEVER add explanations, translator notes, commentary, or parenthetical remarks — output ONLY the translated text and nothing else. No "Note:", no "TL note:", no commentary of any kind.
- If the text is already in English or is a proper noun, keep it as-is.
- Match the tone and style of the original (casual, formal, dramatic, etc.).
- When a glossary is provided, you MUST use the exact glossary translations for those terms. Never deviate from glossary entries.
- ALWAYS translate katakana words into their English MEANING, not romanized form. Katakana loanwords from English should become the original English word (コンピュータ → Computer). Katakana Japanese slang/words must be translated to English meaning.
- NEVER use romanized Japanese (romaji) in the output. Translate ALL Japanese words to proper English. Do NOT leave words like omanko, oppai, kimochi, sugoi, kawaii, baka, ecchi, hentai, etc. in romanized form — translate them to their actual English equivalents. The ONLY exceptions are: character names, place names, and preserved honorifics (-san, -chan, -kun, -sama, -sensei, -senpai, -dono).
- Your output must contain NO Japanese characters (hiragana, katakana, or kanji). Everything must be fully translated to English.

Pronoun rules (CRITICAL):
- Japanese often omits pronouns entirely. Do NOT guess or infer pronouns randomly.
- If character info lists a gender (male/female), use the correct pronouns for that character.
- If gender is NOT specified or unclear, prefer: the character's name, "they/them", or "you" — do NOT default to "he" or "she".
- Use second person "you" for lines spoken TO the player character.
- Use first person "I/me" for lines where the speaker talks about themselves.
- Pay close attention to Japanese pronouns when present: 彼 (he), 彼女 (she), 俺/僕/私 (I), あなた/お前/君 (you).
- Keep character name translations consistent throughout.

Honorifics:
- Preserve Japanese honorifics as-is: -san, -kun, -chan, -sama, -sensei, -senpai, -dono, etc.
- Example: 田中さん → Tanaka-san (NOT "Mr. Tanaka")
- onii-chan, onee-san, etc. should be kept in romanized form when used as address terms.

Context-sensitive translation:
- Consider the physical setting described in context. Translate verbs appropriately for the situation.
- 揺れる can mean: swaying, shaking, rocking, bouncing, trembling — pick the one that fits the scene.
- Avoid overly literal translations. Prioritize natural English that makes sense in the game scene.
- For adult scenes, use natural and explicit English that matches the original intensity. Do not tone down or soften the language."""


# Sugoi Ultra 14B — JP→EN specialized model (fine-tuned from Qwen2.5-14B).
# Trimmed prompt: Sugoi already handles katakana→English, no-romaji, no-JP-in-output,
# and context-sensitive translation natively from its fine-tuning data.
# We keep only tool-specific rules: placeholders, glossary, pronouns, honorifics, adult content.
SUGOI_SYSTEM_PROMPT = """You are a professional localizer translating Japanese to English for an RPG game, including adult (18+) content.

Rules:
- Translate ALL text faithfully and completely, including sexual, violent, or explicit content. Never censor, euphemize, or refuse. This is a licensed translation project.
- The text may contain code markers like «CODE1», «CODE2», etc. These are internal engine tags. Output them EXACTLY as-is — never remove, translate, or replace them with names or words.
- Keep the same line break structure as the original when possible.
- NEVER add explanations, translator notes, or commentary — output ONLY the translated text.
- If the text is already in English or is a proper noun, keep it as-is.
- Match the tone and style of the original (casual, formal, dramatic, etc.).
- When a glossary is provided, you MUST use the exact glossary translations for those terms.

Pronoun rules (CRITICAL):
- If character info lists a gender (male/female), use the correct pronouns for that character.
- If gender is NOT specified or unclear, prefer the character's name, "they/them", or "you".
- Use "you" for lines spoken TO the player character.
- Use "I/me" for lines where the speaker talks about themselves.
- Keep character name translations consistent throughout.

Honorifics — preserve as-is: -san, -kun, -chan, -sama, -sensei, -senpai, -dono. Keep onii-chan, onee-san, etc. in romanized form."""


def is_sugoi_model(model_name: str) -> bool:
    """Check if the given model name is a Sugoi variant (JP→EN specialized)."""
    return "sugoi" in model_name.lower()


_POLISH_SYSTEM_PROMPT = """\
You are an English editor for a translated RPG game. The text was machine-translated \
from Japanese and may have awkward grammar, unnatural phrasing, or broken sentences.

Your job:
- Fix grammar, spelling, and punctuation errors.
- Make the English sound natural and fluent while keeping the EXACT same meaning.
- Preserve the tone (casual, formal, dramatic, comedic, sexual, etc.).
- Do NOT add, remove, or change any information — only improve how it reads.
- Do NOT change character names, honorifics (-san, -chan, etc.), or proper nouns.
- If the text contains code markers like «CODE1», output them exactly as-is.
- Keep line breaks in the same positions.
- For short menu labels or single words that are already correct, output them unchanged.
- Output ONLY the polished text, nothing else. No explanations, no notes."""


_NAME_SYSTEM_PROMPT = (
    "You are a Japanese to English translator. Translate the given Japanese text "
    "into natural English. Output ONLY the translation, nothing else. "
    "For Japanese names, transliterate them into romaji. "
    "If the text is already in English, output it as-is."
)

# Supported target languages with quality ratings.
# For JP→EN: Sugoi Ultra 14B is the best choice (fine-tuned on VN/RPG JP→EN data).
# For other languages: Qwen3 supports 119 languages, trained on 36T tokens.
# Ratings reflect JP→target translation quality specifically.
# 5★/4★ = works well even on 8b models
# 3★    = better with 14b+
# 2★    = 14b+ strongly recommended, may struggle on 8b
# (name, stars, tooltip description)
TARGET_LANGUAGES = [
    ("English",               "\u2605\u2605\u2605\u2605\u2605", "Best — use Sugoi Ultra 14B for optimal JP→EN quality"),
    ("Chinese (Simplified)",  "\u2605\u2605\u2605\u2605\u2605", "Excellent — Qwen's native language, huge JP\u2194CN corpus. Works well on 8b+"),
    ("Chinese (Traditional)", "\u2605\u2605\u2605\u2605\u2606", "Excellent — close to Simplified, strong CJK support. Works well on 8b+"),
    ("Korean",                "\u2605\u2605\u2605\u2605\u2606", "Excellent — strong CJK family, large JP\u2194KR corpus. Works well on 8b+"),
    ("Spanish",               "\u2605\u2605\u2605\u2605\u2606", "Very good — major language, strong Qwen3 training. Works well on 8b+"),
    ("Portuguese",            "\u2605\u2605\u2605\u2605\u2606", "Very good — large speaker base, solid Qwen3 support. Works well on 8b+"),
    ("French",                "\u2605\u2605\u2605\u2605\u2606", "Very good — major language, strong Qwen3 training. Works well on 8b+"),
    ("German",                "\u2605\u2605\u2605\u2605\u2606", "Very good — major language, strong Qwen3 training. Works well on 8b+"),
    ("Russian",               "\u2605\u2605\u2605\u2606\u2606", "Good — improved in Qwen3, decent JP\u2192RU community. Better with 14b+"),
    ("Italian",               "\u2605\u2605\u2605\u2606\u2606", "Good — well-supported European language in Qwen3. Better with 14b+"),
    ("Polish",                "\u2605\u2605\u2605\u2606\u2606", "Good — improved EU language support in Qwen3. Better with 14b+"),
    ("Dutch",                 "\u2605\u2605\u2605\u2606\u2606", "Good — improved EU language support in Qwen3. Better with 14b+"),
    ("Turkish",               "\u2605\u2605\u2605\u2606\u2606", "Good — well-represented in Qwen3 training. Better with 14b+"),
    ("Indonesian",            "\u2605\u2605\u2605\u2606\u2606", "Good — strong Austronesian support (Alibaba SEA focus). Better with 14b+"),
    ("Vietnamese",            "\u2605\u2605\u2605\u2606\u2606", "Good — improved in Qwen3, growing JP game community. Better with 14b+"),
    ("Thai",                  "\u2605\u2605\u2605\u2606\u2606", "Good — improved in Qwen3, active JP fan-translation scene. Better with 14b+"),
    ("Malay",                 "\u2605\u2605\u2605\u2606\u2606", "Good — strong Austronesian support in Qwen3. Better with 14b+"),
    ("Arabic",                "\u2605\u2605\u2606\u2606\u2606", "Fair — less JP\u2192AR parallel data, RTL script. 14b+ strongly recommended"),
    ("Hindi",                 "\u2605\u2605\u2606\u2606\u2606", "Fair — limited JP\u2192HI parallel data. 14b+ strongly recommended"),
    ("Ukrainian",             "\u2605\u2605\u2606\u2606\u2606", "Fair — Cyrillic training, limited JP parallel data. 14b+ strongly recommended"),
    ("Czech",                 "\u2605\u2605\u2606\u2606\u2606", "Fair — supported EU language, limited JP data. 14b+ strongly recommended"),
    ("Romanian",              "\u2605\u2605\u2606\u2606\u2606", "Fair — supported EU language, limited JP data. 14b+ strongly recommended"),
    ("Hungarian",             "\u2605\u2605\u2606\u2606\u2606", "Fair — supported EU language, limited JP data. 14b+ strongly recommended"),
    ("Tagalog",               "\u2605\u2605\u2606\u2606\u2606", "Fair — Austronesian support, limited JP data. 14b+ strongly recommended"),
    ("Pig Latin",             "\u2605\u2605\u2605\u2605\u2605", "Erfectpay — anslatesTray JP\u2192English enThay igPay atinLay. Qapla'!"),
]


def build_system_prompt(target_language: str = "English", model: str = "") -> str:
    """Build the main translation system prompt for a given target language and model.

    Sugoi models get a trimmed prompt (JP→EN specialized, many rules redundant).
    Qwen3 / other models get the full prompt.
    Sugoi prompt is only used for English target — non-English falls back to
    the general prompt since Sugoi is JP→EN only.
    """
    if is_sugoi_model(model) and target_language in ("English", "Pig Latin"):
        return SUGOI_SYSTEM_PROMPT
    if target_language in ("English", "Pig Latin"):
        return SYSTEM_PROMPT
    return SYSTEM_PROMPT.replace("English", target_language)


def _build_name_prompt(target_language: str = "English") -> str:
    """Build the short name-translation prompt for a given target language."""
    if target_language in ("English", "Pig Latin"):
        return _NAME_SYSTEM_PROMPT
    return _NAME_SYSTEM_PROMPT.replace("English", target_language)


class OllamaClient:
    """Client for Ollama's local LLM REST API."""

    def __init__(self, base_url: str = "http://localhost:11434", model: str = "qwen3:14b"):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.system_prompt = SYSTEM_PROMPT  # Customizable system prompt
        self.target_language = "English"   # Target translation language
        self.actor_context = ""  # Character reference for pronoun inference
        self.actor_genders = {}  # {actor_id(int): "male"/"female"/"unknown"}
        self.actor_names = {}    # {actor_id(int): "name string"}
        self.glossary = {}       # JP term -> EN translation forced mappings
        self._managed_proc = None  # subprocess.Popen if we started Ollama

    def _chat(self, *, messages: list, stream: bool = False,
              timeout: int = 120, **kwargs) -> dict:
        """Send a chat request to Ollama with thinking mode disabled.

        Centralizes all /api/chat calls so global options (like disabling
        Qwen3 thinking) are applied consistently.  Extra kwargs are merged
        into the request payload (e.g. ``options``, ``format``).
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "think": False,   # Disable Qwen3 chain-of-thought (huge speed win)
            **kwargs,
        }
        r = requests.post(
            f"{self.base_url}/api/chat",
            json=payload,
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()

    def is_available(self) -> bool:
        """Check if Ollama server is reachable."""
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=5)
            return r.status_code == 200
        except (requests.RequestException, ValueError, OSError):
            return False

    # ── Ollama process management ─────────────────────────────────

    def stop_server(self):
        """Stop any running Ollama process (service or managed subprocess)."""
        # 1. Kill our own managed subprocess if we started one
        if self._managed_proc and self._managed_proc.poll() is None:
            log.info("Stopping managed Ollama subprocess (PID %d)", self._managed_proc.pid)
            self._managed_proc.terminate()
            try:
                self._managed_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._managed_proc.kill()
                self._managed_proc.wait(timeout=3)
            self._managed_proc = None

        # 2. Stop the Windows service (if running)
        try:
            subprocess.run(
                ["net", "stop", "OllamaService"],
                capture_output=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

        # 3. Kill any remaining ollama.exe processes
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "ollama.exe"],
                capture_output=True, timeout=5,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

        # Brief pause to let ports release
        time.sleep(0.5)

    def restart_server(self, num_parallel: int = 2) -> bool:
        """Restart Ollama with OLLAMA_NUM_PARALLEL set.

        Stops any existing Ollama process, then starts a new one as a
        managed subprocess. Returns True if the server becomes reachable.
        """
        self.stop_server()

        # Find ollama executable
        ollama_exe = self._find_ollama_exe()
        if not ollama_exe:
            log.error("Could not find ollama.exe")
            return False

        # Build environment with OLLAMA_NUM_PARALLEL
        env = os.environ.copy()
        env["OLLAMA_NUM_PARALLEL"] = str(num_parallel)

        log.info("Starting Ollama: %s serve (OLLAMA_NUM_PARALLEL=%d)", ollama_exe, num_parallel)
        try:
            self._managed_proc = subprocess.Popen(
                [ollama_exe, "serve"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (FileNotFoundError, OSError) as e:
            log.error("Failed to start Ollama: %s", e)
            return False

        # Wait for server to become reachable (up to 15s)
        for _ in range(30):
            time.sleep(0.5)
            if self.is_available():
                log.info("Ollama server ready (PID %d)", self._managed_proc.pid)
                return True
            # Check if process died
            if self._managed_proc.poll() is not None:
                log.error("Ollama process exited with code %d", self._managed_proc.returncode)
                self._managed_proc = None
                return False

        log.error("Ollama server did not become reachable within 15s")
        return False

    def cleanup(self):
        """Kill managed Ollama subprocess. Call on app exit."""
        if self._managed_proc and self._managed_proc.poll() is None:
            log.info("Cleaning up managed Ollama subprocess (PID %d)", self._managed_proc.pid)
            self._managed_proc.terminate()
            try:
                self._managed_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._managed_proc.kill()
            self._managed_proc = None

    @staticmethod
    def _find_ollama_exe() -> str | None:
        """Locate the ollama executable on the system."""
        # Check PATH first
        for path_dir in os.environ.get("PATH", "").split(os.pathsep):
            candidate = os.path.join(path_dir, "ollama.exe")
            if os.path.isfile(candidate):
                return candidate
        # Common install locations on Windows
        for base in [
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama"),
            os.path.expandvars(r"%LOCALAPPDATA%\Ollama"),
            r"C:\Program Files\Ollama",
        ]:
            candidate = os.path.join(base, "ollama.exe")
            if os.path.isfile(candidate):
                return candidate
        return None

    def list_models(self) -> list:
        """Get list of available model names from Ollama."""
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=10)
            r.raise_for_status()
            data = r.json()
            return [m["name"] for m in data.get("models", [])]
        except (requests.RequestException, KeyError, ValueError, OSError):
            return []

    def list_vision_models(self) -> list:
        """Get list of vision-capable models from Ollama.

        Filters installed models by known vision model keywords.
        """
        _VISION_KEYWORDS = ("vl", "vision", "llava", "minicpm-v", "bakllava")
        all_models = self.list_models()
        return [m for m in all_models
                if any(kw in m.lower() for kw in _VISION_KEYWORDS)]

    def translate_name(self, text: str, hint: str = "") -> str:
        """Translate a short string (name, title, profile) without the full
        placeholder/glossary pipeline.  Returns original on failure.

        Args:
            text: The Japanese text to translate.
            hint: Context hint like "character name", "game title", etc.
        """
        if not text or not text.strip():
            return text
        user_msg = f"Translate this:\n{text}"
        if hint:
            user_msg = f"Context: this is a {hint} from an RPG game.\nTranslate this:\n{text}"
        try:
            data = self._chat(
                messages=[
                    {"role": "system", "content": _build_name_prompt(self.target_language)},
                    {"role": "user", "content": user_msg},
                ],
                timeout=30,
                options={"temperature": 0, "seed": 42, "num_predict": 256, "num_ctx": 4096},
            )
            result = self._strip_thinking(data.get("message", {}).get("content", "").strip())
            if result and self.target_language == "Pig Latin":
                result = _to_pig_latin(result)
            return result if result else text
        except requests.RequestException:
            return text

    # ── Placeholder system ──────────────────────────────────────

    @staticmethod
    def _strip_thinking(text: str) -> str:
        """Remove Qwen3 <think>...</think> reasoning blocks from output."""
        return _THINK_RE.sub('', text).strip()

    @staticmethod
    def _strip_notes(text: str) -> str:
        """Remove translator notes/commentary the LLM sometimes appends."""
        return _NOTE_STRIP_RE.sub('', text).rstrip()

    @staticmethod
    def _contains_japanese(text: str) -> bool:
        """Check if text still contains Japanese characters (hiragana/katakana/kanji).

        Used to detect incomplete translations where the LLM left Japanese
        in the output. Ignores text inside «CODEn» placeholders.
        """
        # Remove code placeholders before checking
        cleaned = re.sub(r'\u00abCODE\d+\u00bb', '', text)
        return bool(JAPANESE_RE.search(cleaned))

    @staticmethod
    def _extract_codes(text: str) -> tuple:
        """Replace control codes with opaque placeholders.

        Uses «CODE1», «CODE2», etc. — a format the LLM will treat as
        untouchable markup rather than a fillable template variable.

        Returns:
            (cleaned_text, mapping) where mapping is {"«CODE1»": "\\C[2]", ...}
        """
        mapping = {}
        counter = [0]

        def _replace(m):
            counter[0] += 1
            key = f"\u00abCODE{counter[0]}\u00bb"  # «CODE1», «CODE2», ...
            mapping[key] = m.group(0)
            return key

        cleaned = CONTROL_CODE_RE.sub(_replace, text)
        return cleaned, mapping

    @staticmethod
    def _restore_codes(text: str, mapping: dict) -> str:
        """Put control codes back from placeholders."""
        for key, code in mapping.items():
            text = text.replace(key, code)
        return text

    @staticmethod
    def _convert_jp_brackets(text: str) -> str:
        """Convert Japanese brackets to English equivalents."""
        for jp, en in _JP_BRACKETS.items():
            text = text.replace(jp, en)
        return text

    # Regex to detect \N[n] actor name codes among extracted codes
    _ACTOR_NAME_CODE_RE = re.compile(r'\\[Nn]\[(\d+)\]')

    def _build_code_hints(self, code_map: dict) -> str:
        """Build hints mapping «CODEn» placeholders to actor names and genders.

        When \\N[1] gets replaced by «CODE3», the LLM has no idea that
        «CODE3» refers to a specific character. This method detects \\N[n]
        codes in the mapping and creates an explicit hint so the LLM can
        use correct pronouns for referenced characters.
        """
        if not code_map or not self.actor_genders:
            return ""
        hints = []
        for placeholder, code in code_map.items():
            m = self._ACTOR_NAME_CODE_RE.match(code)
            if not m:
                continue
            actor_id = int(m.group(1))
            gender = self.actor_genders.get(actor_id, "")
            if not gender or gender == "unknown":
                continue
            name = self.actor_names.get(actor_id, f"Actor {actor_id}")
            if gender == "female":
                pronoun = "she/her"
            elif gender == "male":
                pronoun = "he/him"
            else:
                pronoun = "they/them"
            hints.append(f"  {placeholder} = name of {name} ({pronoun})")
        if not hints:
            return ""
        return (
            "Character name codes (these will display as character names "
            "in-game — use the CORRECT pronouns for each):\n"
            + "\n".join(hints)
        )

    def _build_speaker_hint(self, context: str) -> str:
        """If context identifies a speaker, add their gender as a translation hint.

        The parser embeds ``[Speaker: name]`` in dialogue context from code 101
        headers.  Cross-referencing with actor data lets us tell the LLM
        the speaker's gender so first-person voice and pronoun references
        are accurate.
        """
        if not context or not self.actor_genders:
            return ""
        m = re.search(r'\[Speaker:\s*(.+?)\]', context)
        if not m:
            return ""
        speaker = m.group(1).strip()
        if not speaker:
            return ""
        for actor_id, name in self.actor_names.items():
            if name == speaker or speaker == name:
                gender = self.actor_genders.get(actor_id, "")
                if gender == "female":
                    return (
                        f"Speaker: {name} is FEMALE. "
                        "Lines spoken by her use first-person I/me. "
                        "Others referring to her use she/her.\n"
                    )
                elif gender == "male":
                    return (
                        f"Speaker: {name} is MALE. "
                        "Lines spoken by him use first-person I/me. "
                        "Others referring to him use he/him.\n"
                    )
        return ""

    def _filter_glossary(self, text: str, context: str = "") -> dict[str, str]:
        """Return only glossary entries whose JP term appears in text or context.

        Matches against the raw (untransformed) text so JP bracket forms
        still match.  Simple substring check — fast for typical glossaries.
        """
        if not self.glossary:
            return {}
        search_text = text + "\n" + context
        return {jp: en for jp, en in self.glossary.items() if jp in search_text}

    def _build_user_message(self, clean_text: str, raw_text: str,
                            code_map: dict, context: str = "",
                            field: str = "",
                            correction: str = "",
                            old_translation: str = "") -> str:
        """Build the user prompt prefix shared by translate/variants.

        Args:
            clean_text: Text with control codes replaced by «CODEn» placeholders.
            raw_text: Original untransformed text (for glossary matching).
            code_map: Placeholder→control-code mapping from _extract_codes().
            context: Surrounding dialogue for coherence.
            field: Entry field type (e.g. "dialog", "name").
            correction: Optional correction hint for retranslation.
            old_translation: The previous bad translation to fix.

        Returns:
            Complete user message string ending with the text to translate.
        """
        parts: list[str] = []
        if self.actor_context:
            parts.append(self.actor_context)
        if context:
            parts.append(
                f"Context (surrounding dialogue for reference, do NOT translate this):\n{context}"
            )
            speaker_hint = self._build_speaker_hint(context)
            if speaker_hint:
                parts.append(speaker_hint)
        if correction and old_translation:
            parts.append(
                f"PREVIOUS TRANSLATION (WRONG — do NOT reuse):\n{old_translation}\n\n"
                f"CORRECTION INSTRUCTIONS: {correction}"
            )
        if code_map:
            parts.append(
                "IMPORTANT: The text contains code markers like «CODE1», «CODE2», etc. "
                "These are internal engine formatting tags — NOT names or variables. "
                "You MUST output them exactly as-is. Do NOT replace them with character "
                "names or any other text."
            )
            code_hints = self._build_code_hints(code_map)
            if code_hints:
                parts.append(code_hints)
        if field:
            if field.startswith("terms."):
                parts.append("Content type: menu/system term")
            else:
                hint = self._FIELD_HINTS.get(field, field)
                parts.append(f"Content type: {hint}")
        # Glossary placed last (right before text) for maximum attention
        filtered_glossary = self._filter_glossary(raw_text, context)
        if filtered_glossary:
            glossary_str = "\n".join(f"  {jp} → {en}" for jp, en in filtered_glossary.items())
            parts.append(
                f"REQUIRED glossary — use these EXACT translations:\n{glossary_str}"
            )
        parts.append(f"Translate this:\n{clean_text}")
        return "\n\n".join(parts)

    def _postprocess_result(self, result: str, code_map: dict) -> str:
        """Strip thinking/notes, apply Pig Latin, restore control codes."""
        result = self._strip_thinking(result)
        result = self._strip_notes(result)
        if self.target_language == "Pig Latin":
            result = _to_pig_latin(result)
        if code_map:
            result = self._restore_codes(result, code_map)
        return result

    # Human-readable labels for RPG Maker entry field types
    _FIELD_HINTS = {
        "dialog": "dialogue line",
        "choice": "player choice option",
        "scroll_text": "scrolling narrative text",
        "name": "name",
        "nickname": "character nickname",
        "profile": "character profile/biography",
        "description": "item or skill description",
        "message1": "battle message",
        "message2": "battle message",
        "message3": "battle message",
        "message4": "battle message",
        "gameTitle": "game title",
        "displayName": "map location name",
        "note": "developer note",
        "plugin_command": "plugin command text",
        "plugin_param": "plugin configuration text",
    }

    def translate(self, text: str, context: str = "",
                  correction: str = "", old_translation: str = "",
                  field: str = "",
                  history: list[tuple[str, str]] | None = None) -> str:
        """Translate Japanese text to English using the configured model.

        Args:
            text: The Japanese text to translate.
            context: Optional surrounding dialogue for better coherence.
            correction: Optional user correction hint (e.g. "wrong pronoun").
            old_translation: The previous bad translation to fix.
            field: Entry field type (e.g. "dialog", "name", "choice").

        Returns:
            The English translation string.
        """
        if not text or not text.strip():
            return ""

        # Pre-process: extract control codes → placeholders, convert JP brackets
        clean_text, code_map = self._extract_codes(text)
        clean_text = self._convert_jp_brackets(clean_text)

        user_msg = self._build_user_message(
            clean_text, text, code_map,
            context=context, field=field,
            correction=correction, old_translation=old_translation,
        )

        # Build messages: system → history pairs → current request
        messages = [{"role": "system", "content": self.system_prompt}]
        if history:
            for hist_jp, hist_en in history:
                messages.append({"role": "user", "content": f"Translate this:\n{hist_jp}"})
                messages.append({"role": "assistant", "content": hist_en})
        messages.append({"role": "user", "content": user_msg})

        # Scale context window for history
        num_ctx = 4096
        if history:
            num_ctx = min(4096 + len(history) * 256, 8192)

        try:
            data = self._chat(
                messages=messages,
                timeout=120,
                options={
                    "temperature": 0,
                    "seed": 42,
                    "num_predict": 1024,
                    "num_ctx": num_ctx,
                },
            )
            result = data.get("message", {}).get("content", "").strip()

            # Guard: treat empty LLM output as a failure so we don't
            # silently mark entries as "translated" with blank text.
            if not self._strip_thinking(result):
                raise ConnectionError("Ollama returned empty translation")

            result = self._postprocess_result(result, code_map)

            # Auto-retry if the translation still contains Japanese characters
            if self._contains_japanese(result):
                log.info("Translation contains Japanese — retrying with stronger prompt")
                retry_msg = (
                    "Your translation still contains Japanese characters. "
                    "You MUST translate ALL Japanese text to English. "
                    "Do not leave any hiragana, katakana, or kanji in the output. "
                    "Do not romanize Japanese words — translate them to proper English.\n\n"
                    f"Fix this translation:\n{result}"
                )
                messages_retry = [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": user_msg},
                    {"role": "assistant", "content": result},
                    {"role": "user", "content": retry_msg},
                ]
                try:
                    data2 = self._chat(
                        messages=messages_retry,
                        timeout=120,
                        options={
                            "temperature": 0,
                            "seed": 42,
                            "num_predict": 1024,
                            "num_ctx": num_ctx,
                        },
                    )
                    retry_result = data2.get("message", {}).get("content", "").strip()
                    if retry_result:
                        result = self._postprocess_result(retry_result, code_map)
                except requests.RequestException as exc:
                    log.debug("Japanese-retry failed, keeping original: %s", exc)

            return result
        except requests.RequestException as e:
            raise ConnectionError(f"Ollama API error: {e}") from e

    def polish(self, text: str) -> str:
        """Polish an existing English translation for grammar and fluency.

        Uses the same placeholder system to protect control codes.
        Returns the polished text, or the original on failure.
        """
        if not text or not text.strip():
            return text

        clean_text, code_map = self._extract_codes(text)

        user_msg = ""
        if code_map:
            user_msg += (
                "IMPORTANT: The text contains code markers like «CODE1», «CODE2», etc. "
                "These are internal engine formatting tags. "
                "You MUST output them exactly as-is.\n\n"
            )
        user_msg += f"Polish this:\n{clean_text}"

        try:
            data = self._chat(
                messages=[
                    {"role": "system", "content": _POLISH_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                timeout=120,
                options={
                    "temperature": 0,
                    "seed": 42,
                    "num_predict": 1024,
                    "num_ctx": 4096,
                },
            )
            result = self._strip_thinking(data.get("message", {}).get("content", "").strip())
            if not result:
                return text  # Keep original on empty response

            if code_map:
                result = self._restore_codes(result, code_map)

            return result
        except requests.RequestException:
            return text  # Keep original on error

    # ── Batch JSON translation (DEPRECATED) ─────────────────────
    # Tested with Sugoi Ultra 14B and Qwen3-14B — quality degrades noticeably
    # when packing multiple lines into one request.  Local 14B models have
    # tight context windows (~4096 tokens) and lose per-line nuance when
    # sharing system prompt / glossary / actor context across N entries.
    # Single-entry translation (batch_size=1) produces consistently better
    # results.  Code kept for potential future use with larger cloud models.

    # Regex for extracting a JSON object from LLM response that may have
    # markdown fences or preamble text around it.
    _JSON_EXTRACT_RE = re.compile(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', re.DOTALL)

    @staticmethod
    def _parse_batch_response(raw: str, expected_keys: list[str]) -> dict[str, str]:
        """Parse and validate a batch JSON response from the LLM.

        Handles clean JSON, markdown-fenced JSON, and JSON embedded in text.
        Returns a dict mapping keys to translated strings.
        Raises ValueError if JSON is unparseable or no expected keys found.
        """
        # Try 1: direct parse
        result = None
        try:
            result = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass

        # Try 2: strip markdown fences
        if result is None:
            stripped = re.sub(r'```(?:json)?\s*', '', raw).strip()
            stripped = re.sub(r'\s*```\s*$', '', stripped).strip()
            try:
                result = json.loads(stripped)
            except (json.JSONDecodeError, TypeError):
                pass

        # Try 3: regex extract first JSON object
        if result is None:
            m = OllamaClient._JSON_EXTRACT_RE.search(raw)
            if m:
                try:
                    result = json.loads(m.group(0))
                except (json.JSONDecodeError, TypeError):
                    pass

        if not isinstance(result, dict):
            raise ValueError(f"Could not parse JSON from LLM response: {raw[:200]}")

        # Validate: at least one expected key must be present
        found = {k: str(v).strip() for k, v in result.items()
                 if k in expected_keys and v is not None and str(v).strip()}
        if not found:
            raise ValueError(f"No expected keys found in response. Expected {expected_keys}, got {list(result.keys())}")

        return found

    def translate_batch(self, entries: list[tuple[str, str, str, str]]) -> dict[str, str]:
        """Translate multiple entries in a single API call using JSON format.

        Args:
            entries: List of (key, original_text, context, field) tuples.

        Returns:
            Dict mapping key -> translated text (with codes restored).
        """
        if not entries:
            return {}

        # Pre-process each entry: extract codes, convert brackets
        code_maps = {}  # key -> code_map
        payload = {}    # key -> cleaned text
        any_codes = False

        for key, original, _context, _field in entries:
            clean, code_map = self._extract_codes(original)
            clean = self._convert_jp_brackets(clean)
            code_maps[key] = code_map
            payload[key] = clean
            if code_map:
                any_codes = True

        # Build user message (context/glossary/actors ONCE for entire batch)
        user_msg = ""
        if self.actor_context:
            user_msg += f"{self.actor_context}\n\n"

        # Use context from the first entry (entries are sequential)
        first_context = entries[0][2] if entries[0][2] else ""
        if first_context:
            user_msg += f"Context (surrounding dialogue for reference, do NOT translate this):\n{first_context}\n\n"
            speaker_hint = self._build_speaker_hint(first_context)
            if speaker_hint:
                user_msg += speaker_hint + "\n"

        if any_codes:
            user_msg += (
                "IMPORTANT: The text contains code markers like «CODE1», «CODE2», etc. "
                "These are internal engine formatting tags — NOT names or variables. "
                "You MUST output them exactly as-is.\n\n"
            )
            # Build combined code hints from all entries
            combined_map = {}
            for cm in code_maps.values():
                combined_map.update(cm)
            code_hints = self._build_code_hints(combined_map)
            if code_hints:
                user_msg += code_hints + "\n\n"

        # Glossary placed last (before text) for maximum attention from smaller models
        batch_search_text = "\n".join(original for _key, original, _ctx, _field in entries)
        filtered_glossary = self._filter_glossary(batch_search_text, first_context)
        if filtered_glossary:
            glossary_str = "\n".join(f"  {jp} → {en}" for jp, en in filtered_glossary.items())
            user_msg += f"REQUIRED glossary — use these EXACT translations:\n{glossary_str}\n\n"

        payload_json = json.dumps(payload, ensure_ascii=False, indent=2)
        user_msg += (
            "Translate the following JSON. Each value is a separate line of text to translate.\n"
            "Respond with ONLY a JSON object using the EXACT same keys, "
            "where each value is the translated text:\n\n"
            f"{payload_json}"
        )

        # Build system prompt with batch instruction
        batch_sys = (
            self.system_prompt + "\n\n"
            "CRITICAL: You must respond with a valid JSON object. "
            "Use the exact same keys from the input. "
            "Do not add any text outside the JSON."
        )

        num_predict = min(1024 * len(entries), 8192)

        try:
            data = self._chat(
                messages=[
                    {"role": "system", "content": batch_sys},
                    {"role": "user", "content": user_msg},
                ],
                timeout=120 + 30 * len(entries),
                format="json",
                options={
                    "temperature": 0,
                    "seed": 42,
                    "num_predict": num_predict,
                    "num_ctx": max(4096, 2048 * len(entries)),
                },
            )
            raw = self._strip_thinking(data.get("message", {}).get("content", "").strip())
            if not raw:
                raise ConnectionError("Ollama returned empty response for batch")
        except requests.RequestException as e:
            raise ConnectionError(f"Ollama API error: {e}") from e

        expected_keys = [key for key, *_ in entries]
        parsed = self._parse_batch_response(raw, expected_keys)

        # Strip notes and restore control codes per-entry
        # Collect entries that still have Japanese for individual retry
        results = {}
        retry_entries = []
        for key, translation in parsed.items():
            translation = self._strip_notes(translation)
            if self._contains_japanese(translation):
                # Find the original text for this key
                orig = next((o for k, o, _c, _f in entries if k == key), None)
                if orig:
                    retry_entries.append((key, orig, translation))
                    continue
            if self.target_language == "Pig Latin":
                translation = _to_pig_latin(translation)
            if code_maps.get(key):
                translation = self._restore_codes(translation, code_maps[key])
            results[key] = translation

        # Retry entries with Japanese via single translate() for better results
        for key, original, bad_result in retry_entries:
            log.info("Batch entry %s has Japanese — retrying individually", key)
            try:
                ctx = next((c for k, _o, c, _f in entries if k == key), "")
                fld = next((f for k, _o, _c, f in entries if k == key), "")
                result = self.translate(text=original, context=ctx, field=fld)
                results[key] = result
            except ConnectionError:
                # Fall back to the bad result with codes restored
                if code_maps.get(key):
                    bad_result = self._restore_codes(bad_result, code_maps[key])
                results[key] = bad_result

        return results

    def polish_batch(self, entries: list[tuple[str, str]]) -> dict[str, str]:
        """Polish multiple English translations in a single API call.

        Args:
            entries: List of (key, english_text) tuples.

        Returns:
            Dict mapping key -> polished text (with codes restored).
        """
        if not entries:
            return {}

        code_maps = {}
        payload = {}
        any_codes = False

        for key, text in entries:
            clean, code_map = self._extract_codes(text)
            code_maps[key] = code_map
            payload[key] = clean
            if code_map:
                any_codes = True

        user_msg = ""
        if any_codes:
            user_msg += (
                "IMPORTANT: The text contains code markers like «CODE1», «CODE2», etc. "
                "These are internal engine formatting tags. "
                "You MUST output them exactly as-is.\n\n"
            )

        payload_json = json.dumps(payload, ensure_ascii=False, indent=2)
        user_msg += (
            "Polish the following JSON. Each value is a separate English translation to improve.\n"
            "Respond with ONLY a JSON object using the EXACT same keys, "
            "where each value is the polished text:\n\n"
            f"{payload_json}"
        )

        batch_sys = (
            _POLISH_SYSTEM_PROMPT + "\n\n"
            "CRITICAL: You must respond with a valid JSON object. "
            "Use the exact same keys from the input. "
            "Do not add any text outside the JSON."
        )

        num_predict = min(1024 * len(entries), 8192)

        try:
            data = self._chat(
                messages=[
                    {"role": "system", "content": batch_sys},
                    {"role": "user", "content": user_msg},
                ],
                timeout=120 + 30 * len(entries),
                format="json",
                options={
                    "temperature": 0,
                    "seed": 42,
                    "num_predict": num_predict,
                    "num_ctx": max(4096, 2048 * len(entries)),
                },
            )
            raw = self._strip_thinking(data.get("message", {}).get("content", "").strip())
            if not raw:
                raise ConnectionError("Ollama returned empty response for batch")
        except requests.RequestException as e:
            raise ConnectionError(f"Ollama API error: {e}") from e

        expected_keys = [key for key, _ in entries]
        parsed = self._parse_batch_response(raw, expected_keys)

        results = {}
        for key, translation in parsed.items():
            if code_maps.get(key):
                translation = self._restore_codes(translation, code_maps[key])
            results[key] = translation

        return results

    def translate_variants(self, text: str, context: str = "",
                           field: str = "", count: int = 3) -> list:
        """Generate multiple translation variants using different seeds/temperatures.

        Returns:
            List of translation strings.
        """
        variants = []
        # First variant: deterministic (temp=0, seed=42) — the "standard" translation
        try:
            v = self.translate(text=text, context=context, field=field)
            variants.append(v)
        except ConnectionError:
            pass

        # Pre-process once for all creative variants
        clean_text, code_map = self._extract_codes(text)
        clean_text = self._convert_jp_brackets(clean_text)
        user_msg = self._build_user_message(clean_text, text, code_map,
                                            context=context, field=field)
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_msg},
        ]

        # Additional variants: slight temperature for creative variation
        seeds = [123, 456, 789, 1001, 2025]
        for i in range(count - 1):
            if i >= len(seeds):
                break
            try:
                data = self._chat(
                    messages=messages, timeout=120,
                    options={"temperature": 0.5, "seed": seeds[i],
                             "num_predict": 1024, "num_ctx": 4096},
                )
                result = self._postprocess_result(
                    data.get("message", {}).get("content", "").strip(), code_map)
                if result and result not in variants:
                    variants.append(result)
                elif result:
                    # Duplicate — try again with higher temp
                    data2 = self._chat(
                        messages=messages, timeout=120,
                        options={"temperature": 0.8, "seed": seeds[i] + 100,
                                 "num_predict": 1024, "num_ctx": 4096},
                    )
                    result2 = self._postprocess_result(
                        data2.get("message", {}).get("content", "").strip(), code_map)
                    if result2 and result2 not in variants:
                        variants.append(result2)
            except requests.RequestException as exc:
                log.debug("Variant %d/%d failed: %s", len(variants) + 1, 3, exc)

        return variants
