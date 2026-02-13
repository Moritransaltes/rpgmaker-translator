"""Post-translation text processor — word wrapping and cleanup.

Analyzes RPG Maker MV/MZ plugins to detect message window settings,
then applies proper word wrapping to translated text.
"""

import json
import os
import re


# Default RPG Maker MV/MZ message window: 816px wide, 28px font
# Roughly 4 lines of ~55 chars at default settings
DEFAULT_CHARS_PER_LINE = 55
DEFAULT_MAX_LINES = 4

# Minimal word wrap plugin for RPG Maker MV/MZ.
# Injected into games that lack a word wrap plugin (YEP/VisuMZ).
# Recognises <WordWrap> tag and wraps text at word boundaries using
# the game's actual font metrics.
WORDWRAP_PLUGIN_JS = r"""/*:
 * @plugindesc Word wrap for translated text. Add <WordWrap> at the start of a message to enable.
 * @author RPG Maker Translator
 *
 * @help
 * This plugin enables automatic word wrapping for translated text.
 * Place <WordWrap> at the beginning of a message to activate it.
 * Text will wrap at word boundaries to fit the message window.
 *
 * Auto-injected by RPG Maker Translator during export.
 */
(function() {
    'use strict';

    // --- Detect <WordWrap> tag and pre-wrap text ---
    var _Window_Base_convertEscapeCharacters =
        Window_Base.prototype.convertEscapeCharacters;
    Window_Base.prototype.convertEscapeCharacters = function(text) {
        text = _Window_Base_convertEscapeCharacters.call(this, text);
        this._twrWordWrap = false;
        if (/<wordwrap>/i.test(text)) {
            this._twrWordWrap = true;
            text = text.replace(/<wordwrap>/gi, '');
            text = this._twrApplyWordWrap(text);
        }
        return text;
    };

    // --- Pre-process: insert \n at word boundaries ---
    Window_Base.prototype._twrApplyWordWrap = function(text) {
        var maxWidth = this.contentsWidth ? this.contentsWidth() :
                       (this.contents ? this.contents.width : 408);
        var lines = text.split('\n');
        var result = [];
        for (var i = 0; i < lines.length; i++) {
            result.push(this._twrWrapLine(lines[i], maxWidth));
        }
        return result.join('\n');
    };

    Window_Base.prototype._twrWrapLine = function(line, maxWidth) {
        if (!line) return line;
        var words = line.split(' ');
        var currentLine = '';
        var currentWidth = 0;
        var resultLines = [];
        for (var j = 0; j < words.length; j++) {
            var word = words[j];
            var cleanWord = this._twrStripCodes(word);
            var wordWidth = this.textWidth(cleanWord);
            var spaceWidth = currentLine ? this.textWidth(' ') : 0;
            if (currentWidth + spaceWidth + wordWidth > maxWidth && currentLine) {
                resultLines.push(currentLine);
                currentLine = word;
                currentWidth = wordWidth;
            } else {
                currentLine += (currentLine ? ' ' : '') + word;
                currentWidth += spaceWidth + wordWidth;
            }
        }
        if (currentLine) resultLines.push(currentLine);
        return resultLines.join('\n');
    };

    // --- Strip escape codes for width measurement ---
    Window_Base.prototype._twrStripCodes = function(text) {
        return text.replace(/\x1b[A-Za-z](?:\[\d*\])?/g, '')
                   .replace(/\x1b[{}$.|!><^]/g, '')
                   .replace(/<[^>]+>/g, '');
    };
})();
"""

# Known message plugins and their settings
MESSAGE_PLUGINS = {
    "YEP_MessageCore": {
        "width_param": "Default Width",
        "rows_param": "Message Rows",
        "wordwrap_param": "Word Wrapping",
        "default_width": 816,
    },
    "MessageWindowPopup": {"default_width": 816},
    "Galv_MessageStyles": {"default_width": 816},
    "SRD_MessageBacklog": {},
    "CGMZ_MessageSystem": {"width_param": "Window Width"},
    "VisuMZ_MessageCore": {
        "width_param": "General:MessageWindow:MessageWidth",
        "rows_param": "General:MessageWindow:MessageRows",
        "wordwrap_param": "Word Wrap:EnableWordWrap",
        "default_width": 816,
    },
}

from . import CONTROL_CODE_RE
CONTROL_CODE_REGEX = CONTROL_CODE_RE  # local alias for backward compat


class PluginAnalyzer:
    """Analyzes RPG Maker MV/MZ plugins to determine text formatting settings."""

    def __init__(self):
        self.message_width = 816
        self.font_size = 28
        self.chars_per_line = DEFAULT_CHARS_PER_LINE
        self.max_lines = DEFAULT_MAX_LINES
        self.has_wordwrap_plugin = False
        self.wordwrap_tag = ""  # e.g. "<WordWrap>" if plugin supports it
        self.detected_plugins = []
        self.inject_wordwrap = False  # True → inject our plugin during export

    def analyze_project(self, project_dir: str):
        """Analyze a project's plugins to detect message settings."""
        plugins_path = self._find_plugins_file(project_dir)
        if not plugins_path:
            return

        plugins = self._load_plugins(plugins_path)
        if not plugins:
            return

        for plugin in plugins:
            name = plugin.get("name", "")
            status = plugin.get("status", False)
            params = plugin.get("parameters", {})

            if not status:
                continue

            # Check against known message plugins
            for known_name, config in MESSAGE_PLUGINS.items():
                if known_name.lower() in name.lower():
                    self.detected_plugins.append(name)
                    self._apply_plugin_settings(name, params, config)

        # Also check System.json for font size
        self._check_system_settings(project_dir)

        # Recalculate chars per line based on detected settings
        self._recalculate()

    def _find_plugins_file(self, project_dir: str) -> str:
        """Find the plugins.js file."""
        candidates = [
            os.path.join(project_dir, "js", "plugins.js"),
            os.path.join(project_dir, "www", "js", "plugins.js"),
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
        return ""

    def _load_plugins(self, path: str) -> list:
        """Parse plugins.js which is a JS file with var $plugins = [...]."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()

            # Extract the JSON array from: var $plugins = [...];
            match = re.search(r'\[.*\]', content, re.DOTALL)
            if match:
                return json.loads(match.group())
        except (json.JSONDecodeError, OSError):
            pass
        return []

    def _apply_plugin_settings(self, name: str, params: dict, config: dict):
        """Apply detected plugin settings."""
        # Check for message width parameter
        width_param = config.get("width_param", "")
        if width_param and width_param in params:
            try:
                self.message_width = int(params[width_param])
            except (ValueError, TypeError):
                pass

        # Check for message rows parameter
        rows_param = config.get("rows_param", "")
        if rows_param and rows_param in params:
            try:
                rows = int(params[rows_param])
                if rows > 0:
                    self.max_lines = rows
            except (ValueError, TypeError):
                pass

        # Check for word wrap support
        wordwrap_param = config.get("wordwrap_param", "")
        if wordwrap_param:
            self.has_wordwrap_plugin = True
            wp_value = params.get(wordwrap_param, "")
            if str(wp_value).lower() in ("true", "1", "yes"):
                self.wordwrap_tag = "<WordWrap>"

        # YEP / VisuMZ specific
        if "yep" in name.lower() or "visumz" in name.lower():
            self.has_wordwrap_plugin = True
            if not self.wordwrap_tag:
                self.wordwrap_tag = "<WordWrap>"

    def _check_system_settings(self, project_dir: str):
        """Check System.json for any font size overrides."""
        data_dirs = [
            os.path.join(project_dir, "data"),
            os.path.join(project_dir, "Data"),
            os.path.join(project_dir, "www", "data"),
        ]
        for data_dir in data_dirs:
            system_path = os.path.join(data_dir, "System.json")
            if os.path.exists(system_path):
                try:
                    with open(system_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    fs = data.get("advanced", {}).get("fontSize", 0)
                    if fs > 0:
                        self.font_size = fs
                except (json.JSONDecodeError, OSError):
                    pass
                return

    def _recalculate(self):
        """Recalculate chars per line from detected settings."""
        # Rough formula: message window has ~24px padding on each side
        usable_width = self.message_width - 48
        # Each character is roughly font_size * 0.55 wide for English text
        char_width = self.font_size * 0.55
        if char_width > 0:
            self.chars_per_line = max(20, int(usable_width / char_width))

    def get_summary(self) -> str:
        """Return a human-readable summary of detected settings."""
        lines = [f"Message width: {self.message_width}px"]
        lines.append(f"Font size: {self.font_size}px")
        lines.append(f"Chars per line: ~{self.chars_per_line}")
        lines.append(f"Max lines per box: {self.max_lines}")
        if self.detected_plugins:
            lines.append(f"Message plugins: {', '.join(self.detected_plugins)}")
        if self.has_wordwrap_plugin:
            lines.append(f"Word wrap plugin detected (tag: {self.wordwrap_tag or 'auto'})")
        else:
            lines.append("No word wrap plugin — manual line breaks needed")
        return "\n".join(lines)

    def should_inject_plugin(self) -> bool:
        """True if no existing word wrap plugin and injection was requested."""
        return self.inject_wordwrap and not self.has_wordwrap_plugin


class TextProcessor:
    """Applies word wrapping and text cleanup to translated entries."""

    def __init__(self, analyzer: PluginAnalyzer):
        self.analyzer = analyzer
        self.overflow_entries: list[tuple[str, str]] = []  # (id, file) of overflows

    def process_entry(self, original: str, translation: str,
                      *, use_tag: bool = True) -> str:
        """Process a single translated text — apply word wrapping.

        Args:
            original: The original Japanese text (for reference on line count).
            translation: The English translation to wrap.
            use_tag: If True and a wordwrap plugin exists, add <WordWrap> tag.
                     If False, always use manual line breaks (for DB fields).

        Returns:
            The processed translation with proper line breaks.
        """
        if not translation or not translation.strip():
            return translation

        # Count original lines to know how many text boxes we have
        orig_line_count = len(original.split("\n"))

        # If the game has a word wrap plugin, let it handle wrapping
        if use_tag and self.analyzer.has_wordwrap_plugin and self.analyzer.wordwrap_tag:
            return self._apply_plugin_wordwrap(translation, orig_line_count)

        # Otherwise — manually redistribute text across lines
        return self._apply_manual_wordwrap(translation, orig_line_count)

    def _apply_plugin_wordwrap(self, text: str, orig_line_count: int) -> str:
        """For games with word wrap plugins: add tag, keep within line count.

        The in-game word wrap plugin handles visual line breaking, so we
        just need to fit text into the correct number of 401 commands
        (= orig_line_count).  If the translation has more newlines than
        the original, merge overflow into the last slot — the plugin
        re-wraps it at the message window width.
        """
        tag = self.analyzer.wordwrap_tag

        # Split by existing newlines (which map to 401 command boundaries)
        lines = text.split("\n")

        # Merge overflow lines into the last slot so we stay within
        # orig_line_count 401 commands.  The word wrap plugin handles
        # the visual breaking of long lines.
        if len(lines) > orig_line_count:
            keep = lines[:orig_line_count - 1] if orig_line_count > 1 else []
            merged = " ".join(
                seg.strip() for seg in lines[len(keep):] if seg.strip()
            )
            keep.append(merged)
            lines = keep

        # Pad with empty lines if fewer
        while len(lines) < orig_line_count:
            lines.append("")

        # Add word wrap tag to first line if not already present
        if lines and tag and not lines[0].startswith(tag):
            lines[0] = tag + lines[0]

        return "\n".join(lines)

    def _apply_manual_wordwrap(self, text: str, orig_line_count: int) -> str:
        """Redistribute text across lines to fit message window width.

        Joins all text, re-wraps to chars_per_line, and expands to as
        many lines as needed.  The export code inserts extra 401/405
        commands when the translation needs more lines than the original.
        RPG Maker auto-paginates when text exceeds the message box height.

        Sets self._last_overflow if the wrapped text exceeds a single
        message box (analyzer.max_lines).
        """
        max_chars = self.analyzer.chars_per_line
        self._last_overflow = False

        # Strip any leftover <WordWrap> tags
        text = re.sub(r'<[Ww]ord[Ww]rap>', '', text)

        # Join all lines into one blob, then re-wrap properly
        all_text = " ".join(
            seg.strip() for seg in text.split("\n") if seg.strip()
        )
        if not all_text:
            return "\n".join([""] * orig_line_count)

        # Word-wrap into a flat list of lines
        wrapped = self._wrap_to_lines(all_text, max_chars)

        # Flag overflow if text exceeds one message box (needs pagination)
        if len(wrapped) > self.analyzer.max_lines:
            self._last_overflow = True

        # Pad with empty lines if fewer lines than original
        while len(wrapped) < orig_line_count:
            wrapped.append("")

        return "\n".join(wrapped)

    def _wrap_to_lines(self, text: str, max_chars: int) -> list[str]:
        """Word-wrap text into a flat list of lines, each <= max_chars.

        Respects control codes (which don't take visual space).
        """
        if not text:
            return [""]

        words = text.split(" ")
        lines: list[str] = []
        current = ""

        for word in words:
            if not word:
                continue
            test = f"{current} {word}" if current else word
            if self._visual_length(test) <= max_chars:
                current = test
            else:
                if current:
                    lines.append(current)
                current = word

        if current:
            lines.append(current)

        return lines if lines else [""]

    def _visual_length(self, text: str) -> int:
        """Calculate the visual character count, ignoring control codes."""
        cleaned = CONTROL_CODE_REGEX.sub("", text)
        return len(cleaned)

    # Only these field types go through the message window where
    # <WordWrap> tags are processed.  DB fields (name, description,
    # terms, etc.) are shown in menus that don't handle the tag.
    _WORDWRAP_FIELDS = {"dialog", "scroll_text"}

    def process_all(self, entries: list) -> int:
        """Process all translated entries. Returns count of modified entries.

        After calling, check:
          self.overflow_entries — entries that exceed one message box
          self.expanded_count  — entries that needed extra 401 lines
          self.extra_lines     — total extra 401 commands that will be inserted
        """
        self.overflow_entries = []
        self.expanded_count = 0
        self.extra_lines = 0
        self._last_overflow = False
        count = 0
        for entry in entries:
            if entry.status not in ("translated", "reviewed"):
                continue
            if not entry.translation:
                continue

            use_tag = entry.field in self._WORDWRAP_FIELDS
            self._last_overflow = False
            orig_line_count = len(entry.original.split("\n"))
            processed = self.process_entry(
                entry.original, entry.translation, use_tag=use_tag)
            if processed != entry.translation:
                entry.translation = processed
                count += 1
            # Track entries that expanded beyond original line count
            new_line_count = len(processed.split("\n"))
            if new_line_count > orig_line_count:
                self.expanded_count += 1
                self.extra_lines += new_line_count - orig_line_count
            if self._last_overflow:
                self.overflow_entries.append((entry.id, entry.file))
        return count
