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
# When a face/portrait graphic is displayed, the text area shrinks.
# Standard RPG Maker face is 144px + 12px margin = 156px.
# Face offset in chars = 156 / (font_size * char_ratio).
FACE_OFFSET_PX = 156  # pixels consumed by face graphic + margin

# Minimal word wrap plugin for RPG Maker MV/MZ.
# Injected into games that lack a word wrap plugin (YEP/VisuMZ).
# Wraps text at word boundaries using character counting.
# Font is swapped to Consolas via gamefont.css during export,
# so character counts are exact for this monospace font.
WORDWRAP_PLUGIN_JS = r"""/*:
 * @plugindesc Pre-render word wrap for translated text.
 * @author RPG Maker Translator
 *
 * @help
 * Wraps text at word boundaries BEFORE rendering by inserting line
 * breaks into the text string at createTextState time.  Uses pixel-
 * accurate measurement via contents.measureTextWidth().
 *
 * Hooks Window_Message (dialogue) and Window_Help (descriptions).
 * Does NOT affect choice windows, battle logs, popups, or other UI.
 *
 * Works on both RPG Maker MV and MZ — the pre-processing approach
 * is engine-agnostic (no processCharacter / processNormalCharacter).
 *
 * Activated by <WordWrap> tag at the start of a text block.
 *
 * Based on McKathlin_MessageControl's proven pre-render technique.
 * Auto-injected by RPG Maker Translator during export.
 */
(function() {
    'use strict';

    // ===== Text width measurement (strips escape codes) =====
    var ICON_RE = /\x1bI\[\d{1,4}\]/g;
    var ESCAPE_RE = /\x1b[A-Za-z{}\[\]$.|!><^]/g;
    // Matches \x1b followed by optional letter and bracket content like \x1bC[2]
    var BRACKET_RE = /\x1b[A-Za-z]?\[\d{1,5}\]/g;

    function printedTextWidth(win, text) {
        // Count icons — each takes iconWidth + 4px
        var iconCount = 0;
        text = text.replace(ICON_RE, function() { iconCount++; return ''; });
        // Strip bracket escape codes like \C[2], \FS[24], \V[3]
        text = text.replace(BRACKET_RE, '');
        // Strip remaining single-char escapes like \{ \} \$ \. \|
        text = text.replace(ESCAPE_RE, '');
        var iconWidth = (typeof ImageManager !== 'undefined' &&
                         ImageManager.iconWidth) ? ImageManager.iconWidth + 4 : 36;
        var measured = win.contents ? win.contents.measureTextWidth(text) : text.length * 14;
        return measured + (iconCount * iconWidth);
    }

    // ===== Core: split paragraph into wrapped lines =====
    function splitAtWrapPoints(win, text, width) {
        var lines = [];
        var start = 0;
        while (start < text.length) {
            var end = text.length;
            var testLine = text.slice(start, end);
            // Shrink by finding last space that fits
            while (printedTextWidth(win, testLine) > width && end > start) {
                var spaceIdx = testLine.lastIndexOf(' ');
                var fwSpaceIdx = testLine.lastIndexOf('\u3000');
                var breakAt = Math.max(spaceIdx, fwSpaceIdx);
                if (breakAt <= 0) {
                    // No space found — force break at character level
                    while (end > start + 1 && printedTextWidth(win, text.slice(start, end)) > width) {
                        end--;
                    }
                    break;
                }
                end = start + breakAt;
                testLine = text.slice(start, end);
            }
            lines.push(text.slice(start, end).trimEnd());
            // Skip past the space we broke at
            start = end;
            if (start < text.length) {
                var ch = text.charCodeAt(start);
                if (ch === 0x20 || ch === 0x3000) start++;
            }
        }
        return lines.length ? lines : [''];
    }

    // ===== Wrap full text (handles existing newlines as paragraphs) =====
    function wrapText(win, text) {
        var width = win.contents ? win.contents.width : 408;
        // Account for face graphic in Window_Message
        if (win.newLineX) {
            var dummy = { y: 0, height: 0 };
            var faceX = win.newLineX(dummy);
            if (faceX > 0) width -= faceX;
        }
        var paragraphs = text.split('\n');
        var result = [];
        for (var i = 0; i < paragraphs.length; i++) {
            var p = paragraphs[i];
            if (!p.length) { result.push(''); continue; }
            var wrapped = splitAtWrapPoints(win, p, width);
            for (var j = 0; j < wrapped.length; j++) {
                result.push(wrapped[j]);
            }
        }
        return result.join('\n');
    }

    // ===== convertEscapeCharacters: detect <WordWrap>, strip newlines =====
    function hookConvertEsc(original) {
        return function(text) {
            this._twrWordWrap = false;
            if (/<wordwrap>/i.test(text)) {
                this._twrWordWrap = true;
                text = text.replace(/<wordwrap>/gi, '');
            }
            text = original.call(this, text);
            if (this._twrWordWrap) {
                // Merge all lines into one paragraph (we re-wrap later)
                text = text.replace(/[\n\r]+/g, ' ');
                // Honour explicit <br> tags as hard line breaks
                text = text.replace(/<(?:br|line break)>/gi, '\n');
            }
            return text;
        };
    }

    var _WM_convertEsc = Window_Message.prototype.convertEscapeCharacters;
    Window_Message.prototype.convertEscapeCharacters = hookConvertEsc(_WM_convertEsc);

    var _WH_convertEsc =
        Window_Help.prototype.convertEscapeCharacters ||
        Window_Base.prototype.convertEscapeCharacters;
    Window_Help.prototype.convertEscapeCharacters = hookConvertEsc(_WH_convertEsc);

    // ===== createTextState: pre-process wrap BEFORE rendering =====
    function hookCreateTextState(original) {
        return function(text, x, y, width) {
            var textState = original.call(this, text, x, y, width);
            if (this._twrWordWrap && textState && textState.text) {
                textState.text = wrapText(this, textState.text);
            }
            return textState;
        };
    }

    var _WM_createTS = Window_Message.prototype.createTextState;
    Window_Message.prototype.createTextState = hookCreateTextState(_WM_createTS);

    // Window_Help may inherit createTextState from Window_Base
    var _WH_createTS =
        Window_Help.prototype.createTextState ||
        Window_Base.prototype.createTextState;
    Window_Help.prototype.createTextState = hookCreateTextState(_WH_createTS);

    // ===== processNewLine: handle face graphic offset + page break =====
    var _WM_processNewLine = Window_Message.prototype.processNewLine;
    Window_Message.prototype.processNewLine = function(textState) {
        _WM_processNewLine.call(this, textState);
        if (this._twrWordWrap && textState) {
            if (this.newLineX) {
                textState.x = this.newLineX(textState);
            }
            if (this.needsNewPage && this.needsNewPage(textState)) {
                this.startPause();
            }
        }
    };

    if (!Window_Message.prototype.needsNewPage) {
        Window_Message.prototype.needsNewPage = function(textState) {
            if (!textState) return false;
            var lineH = this.lineHeight ? this.lineHeight() : 36;
            var maxH = this.contents ? this.contents.height : 144;
            return (textState.y + lineH > maxH);
        };
    }
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
        self.face_chars_per_line = max(15, DEFAULT_CHARS_PER_LINE - int(
            FACE_OFFSET_PX / (self.font_size * 0.55)))
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
            self.face_chars_per_line = max(15, int(
                (usable_width - FACE_OFFSET_PX) / char_width))

    def get_summary(self) -> str:
        """Return a human-readable summary of detected settings."""
        lines = [f"Message width: {self.message_width}px"]
        lines.append(f"Font size: {self.font_size}px")
        lines.append(f"Chars per line: ~{self.chars_per_line} (face: ~{self.face_chars_per_line})")
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
                      *, use_tag: bool = True,
                      has_face: bool = False) -> str:
        """Process a single translated text — apply word wrapping.

        Args:
            original: The original Japanese text (for reference on line count).
            translation: The English translation to wrap.
            use_tag: If True and a wordwrap plugin exists, add <WordWrap> tag.
                     If False, always use manual line breaks (for DB fields).
            has_face: If True, use narrower width (face graphic takes space).

        Returns:
            The processed translation with proper line breaks.
        """
        if not translation or not isinstance(translation, str) or not translation.strip():
            return translation

        # Count original lines to know how many text boxes we have
        orig_line_count = len(original.split("\n"))

        # If the game has a word wrap plugin (or we're injecting one), use tags
        has_plugin = self.analyzer.has_wordwrap_plugin or self.analyzer.inject_wordwrap
        if use_tag and has_plugin:
            return self._apply_plugin_wordwrap(translation, orig_line_count,
                                               has_face=has_face)

        # Otherwise — manually redistribute text across lines
        return self._apply_manual_wordwrap(translation, orig_line_count,
                                           has_face=has_face)

    def _apply_plugin_wordwrap(self, text: str, orig_line_count: int,
                               *, has_face: bool = False) -> str:
        """For games with word wrap plugins: add tag only if lines overflow.

        Only adds <WordWrap> when at least one line exceeds the available
        character width.  Entries where all lines already fit keep their
        original line breaks — this preserves intentional formatting
        (e.g. repair shop results, short dialogue).
        """
        tag = self.analyzer.wordwrap_tag or "<WordWrap>"
        max_chars = (self.analyzer.face_chars_per_line if has_face
                     else self.analyzer.chars_per_line)

        # Split by existing newlines (which map to 401 command boundaries)
        lines = text.split("\n")

        # Check if any line actually overflows the available width
        needs_wrap = any(
            self._visual_length(line) > max_chars
            for line in lines if line.strip()
        )

        if not needs_wrap:
            # All lines fit — preserve original formatting, no tag needed.
            # Still enforce line count to match 401 command slots.
            if len(lines) > orig_line_count:
                keep = lines[:orig_line_count - 1] if orig_line_count > 1 else []
                merged = " ".join(
                    seg.strip() for seg in lines[len(keep):] if seg.strip()
                )
                keep.append(merged)
                lines = keep
            while len(lines) < orig_line_count:
                lines.append("")
            # Strip any existing <WordWrap> tag — not needed
            result = "\n".join(lines)
            return re.sub(r'<[Ww]ord[Ww]rap>', '', result)

        # Lines overflow — merge into orig_line_count slots and add tag
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

    def _apply_manual_wordwrap(self, text: str, orig_line_count: int,
                               *, has_face: bool = False) -> str:
        """Redistribute text across lines to fit message window width.

        Joins all text, re-wraps to chars_per_line, and expands to as
        many lines as needed.  The export code inserts extra 401/405
        commands when the translation needs more lines than the original.
        RPG Maker auto-paginates when text exceeds the message box height.

        Sets self._last_overflow if the wrapped text exceeds a single
        message box (analyzer.max_lines).
        """
        max_chars = (self.analyzer.face_chars_per_line if has_face
                     else self.analyzer.chars_per_line)
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

    # Field types where the <WordWrap> tag is used for render-time wrapping.
    # dialog / scroll_text — Window_Message (dialogue boxes)
    # description — Window_Help (skill/item/weapon/armor help text)
    # Other DB fields (name, terms) use menus that don't support the tag.
    _WORDWRAP_FIELDS = {"dialog", "scroll_text", "description"}

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
            has_face = getattr(entry, 'has_face', False)
            processed = self.process_entry(
                entry.original, entry.translation, use_tag=use_tag,
                has_face=has_face)
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
