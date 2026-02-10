"""Ollama REST API wrapper for LLM translation."""

import re
import requests


# Regex matching RPG Maker control codes that the LLM should never touch.
# Order matters — longer patterns first to avoid partial matches.
_CONTROL_CODE_RE = re.compile(
    r'\\[A-Za-z]+\[\d*\]'      # \V[1], \N[2], \C[3], \FS[24], etc.
    r'|\\[{}$.|!><^]'           # \{, \}, \$, \., \|, \!, \>, \<, \^
    r'|<[^>]+>'                 # HTML-like tags: <br>, <WordWrap>, <B>, etc.
)

# Japanese bracket pairs → English equivalents
_JP_BRACKETS = {
    '\u300c': '"', '\u300d': '"',   # 「 」 → " "
    '\u300e': '"', '\u300f': '"',   # 『 』 → " "
    '\u3010': '[', '\u3011': ']',   # 【 】 → [ ]
    '\uff08': '(', '\uff09': ')',   # （ ） → ( )
}


SYSTEM_PROMPT = """You are a professional Japanese to English translator specializing in RPG/video game dialogue, including adult (18+) content.

Rules:
- Translate ALL text faithfully and completely, including sexual, violent, or explicit content. Never censor, euphemize, skip, or refuse any content. This is a licensed translation project.
- Translate the Japanese text naturally into English suitable for an RPG game.
- The text may contain opaque code markers like «CODE1», «CODE2», etc. These are internal engine tags. Output them EXACTLY as-is — never remove, translate, rewrite, or replace them with names or words.
- Keep the same line break structure as the original when possible.
- Do not add explanations, notes, or commentary — output ONLY the translated text.
- If the text is already in English or is a proper noun, keep it as-is.
- Match the tone and style of the original (casual, formal, dramatic, etc.).
- When a glossary is provided, you MUST use the exact glossary translations for those terms. Never deviate from glossary entries.

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


_NAME_SYSTEM_PROMPT = (
    "You are a Japanese to English translator. Translate the given Japanese text "
    "into natural English. Output ONLY the translation, nothing else. "
    "For Japanese names, transliterate them into romaji. "
    "If the text is already in English, output it as-is."
)


class OllamaClient:
    """Client for Ollama's local LLM REST API."""

    def __init__(self, base_url: str = "http://localhost:11434", model: str = "qwen2.5:14b"):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.system_prompt = SYSTEM_PROMPT  # Customizable system prompt
        self.actor_context = ""  # Character reference for pronoun inference
        self.glossary = {}       # JP term -> EN translation forced mappings

    def is_available(self) -> bool:
        """Check if Ollama server is reachable."""
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=5)
            return r.status_code == 200
        except (requests.RequestException, ValueError, OSError):
            return False

    def list_models(self) -> list:
        """Get list of available model names from Ollama."""
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=10)
            r.raise_for_status()
            data = r.json()
            return [m["name"] for m in data.get("models", [])]
        except (requests.RequestException, KeyError, ValueError, OSError):
            return []

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
            r = requests.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": _NAME_SYSTEM_PROMPT},
                        {"role": "user", "content": user_msg},
                    ],
                    "stream": False,
                    "options": {"temperature": 0, "seed": 42, "num_predict": 256},
                },
                timeout=30,
            )
            r.raise_for_status()
            result = r.json().get("message", {}).get("content", "").strip()
            return result if result else text
        except requests.RequestException:
            return text

    # ── Placeholder system ──────────────────────────────────────

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

        cleaned = _CONTROL_CODE_RE.sub(_replace, text)
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
    }

    def translate(self, text: str, context: str = "",
                  correction: str = "", old_translation: str = "",
                  field: str = "") -> str:
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

        user_msg = ""
        if self.actor_context:
            user_msg += f"{self.actor_context}\n\n"
        if self.glossary:
            glossary_str = "\n".join(f"  {jp} = {en}" for jp, en in self.glossary.items())
            user_msg += f"Glossary (MUST use these exact translations for these terms):\n{glossary_str}\n\n"
        if context:
            user_msg += f"Context (surrounding dialogue for reference, do NOT translate this):\n{context}\n\n"
        if correction and old_translation:
            user_msg += (
                f"PREVIOUS TRANSLATION (WRONG — do NOT reuse):\n{old_translation}\n\n"
                f"CORRECTION INSTRUCTIONS: {correction}\n\n"
            )
        if code_map:
            user_msg += (
                "IMPORTANT: The text contains code markers like «CODE1», «CODE2», etc. "
                "These are internal engine formatting tags — NOT names or variables. "
                "You MUST output them exactly as-is. Do NOT replace them with character "
                "names or any other text.\n\n"
            )
        if field:
            hint = self._FIELD_HINTS.get(field, field)
            user_msg += f"Content type: {hint}\n"
            # For terms fields like "terms.messages[0]", label as menu/system term
            if field.startswith("terms."):
                user_msg += "Content type: menu/system term\n"
        user_msg += f"Translate this:\n{clean_text}"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_msg},
            ],
            "stream": False,
            "options": {
                "temperature": 0,
                "seed": 42,
                "num_predict": 1024,
            },
        }

        try:
            r = requests.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=120,
            )
            r.raise_for_status()
            data = r.json()
            result = data.get("message", {}).get("content", "").strip()

            # Post-process: restore control codes from placeholders
            if code_map:
                result = self._restore_codes(result, code_map)

            return result
        except requests.RequestException as e:
            raise ConnectionError(f"Ollama API error: {e}") from e

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

        # Additional variants: slight temperature for creative variation
        seeds = [123, 456, 789, 1001, 2025]
        for i in range(count - 1):
            if i >= len(seeds):
                break
            try:
                # Pre-process same as translate()
                clean_text, code_map = self._extract_codes(text)
                clean_text = self._convert_jp_brackets(clean_text)

                user_msg = ""
                if self.actor_context:
                    user_msg += f"{self.actor_context}\n\n"
                if self.glossary:
                    glossary_str = "\n".join(
                        f"  {jp} = {en}" for jp, en in self.glossary.items()
                    )
                    user_msg += f"Glossary (MUST use these exact translations):\n{glossary_str}\n\n"
                if context:
                    user_msg += f"Context (surrounding dialogue, do NOT translate):\n{context}\n\n"
                if code_map:
                    user_msg += (
                        "IMPORTANT: Code markers like «CODE1» are engine tags. "
                        "Output them exactly as-is.\n\n"
                    )
                if field:
                    hint = self._FIELD_HINTS.get(field, field)
                    user_msg += f"Content type: {hint}\n"
                user_msg += f"Translate this:\n{clean_text}"

                r = requests.post(
                    f"{self.base_url}/api/chat",
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": self.system_prompt},
                            {"role": "user", "content": user_msg},
                        ],
                        "stream": False,
                        "options": {
                            "temperature": 0.5,
                            "seed": seeds[i],
                            "num_predict": 1024,
                        },
                    },
                    timeout=120,
                )
                r.raise_for_status()
                result = r.json().get("message", {}).get("content", "").strip()
                if code_map:
                    result = self._restore_codes(result, code_map)
                # Only add if it's actually different
                if result and result not in variants:
                    variants.append(result)
                elif result:
                    # Duplicate — try again with higher temp
                    r2 = requests.post(
                        f"{self.base_url}/api/chat",
                        json={
                            "model": self.model,
                            "messages": [
                                {"role": "system", "content": self.system_prompt},
                                {"role": "user", "content": user_msg},
                            ],
                            "stream": False,
                            "options": {
                                "temperature": 0.8,
                                "seed": seeds[i] + 100,
                                "num_predict": 1024,
                            },
                        },
                        timeout=120,
                    )
                    r2.raise_for_status()
                    result2 = r2.json().get("message", {}).get("content", "").strip()
                    if code_map:
                        result2 = self._restore_codes(result2, code_map)
                    if result2 and result2 not in variants:
                        variants.append(result2)
                    # Skip duplicates — caller handles single-variant case
            except requests.RequestException:
                pass

        return variants
