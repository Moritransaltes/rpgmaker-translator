# RPG Maker Translator — Local LLM (Ollama + PyQt6)

## Project Overview
A local LLM-powered translation tool for RPG Maker MV/MZ games, including adult (18+) content. Uses Ollama with Qwen3-14B (optimized for 4070 Ti 12GB VRAM + 64GB RAM). Features a PyQt6 desktop GUI with batch translation, pronoun-aware gender hints, two-stage workflow (DB → dialogue), actor-by-actor mode, grammar polish, plugin parameter extraction, side-by-side review/editing, two-layer glossary, translation variants, and project save/resume.

## Tech Stack
- **Python 3.14** (Windows)
- **PyQt6** — Desktop GUI framework
- **Ollama** — Local LLM inference server (REST API)
- **Qwen3:14b** — Recommended model (best JP comprehension for 12GB VRAM)
- **requests** — HTTP client for Ollama API

## Project Structure
```
e:\Hgames\Translator RPG Games\
├── main.py                          # Entry point (QApplication + Fusion style)
├── requirements.txt                 # PyQt6, requests
├── CLAUDE.md                        # This file
├── README.md                        # Install & usage guide
├── screenshot.png                   # Application screenshot for README
├── translator/
│   ├── __init__.py
│   ├── ollama_client.py             # Ollama REST API wrapper (/api/chat)
│   ├── rpgmaker_mv.py               # MV/MZ JSON parser & writer + plugin extraction
│   ├── project_model.py             # TranslationEntry dataclass + TranslationProject
│   ├── translation_engine.py        # QThread-based batch translation + polish orchestrator
│   ├── text_processor.py            # Plugin analysis + word wrap processor
│   ├── default_glossary.py          # ~100 preset JP→EN terms for common RPG vocabulary
│   └── widgets/
│       ├── __init__.py
│       ├── main_window.py           # Main window layout, menus, batch modes, signal wiring
│       ├── translation_table.py     # Table + editor panel + cross-file search
│       ├── file_tree.py             # Project file browser with progress badges
│       ├── settings_dialog.py       # Ollama config, target language, workers, glossary tabs
│       ├── actor_gender_dialog.py   # Actor gender assignment for pronoun accuracy
│       └── variant_dialog.py        # Translation variant picker (3 options)
```

## Architecture

### Data Flow
1. **Open Project** → `RPGMakerMVParser.load_project()` extracts all translatable strings from game JSON files + `plugins.js` → `TranslationEntry` list
2. **Pre-translate** → Game title + actor names/profiles translated via `OllamaClient.translate_name()` with field-type hints
3. **Folder Rename** → Offers to rename project folder to "English Title - WIP"
4. **Actor Detection** → `load_actors_raw()` + `ActorGenderDialog` → gender context stored in `client.actor_genders` + `client.actor_names`
5. **Batch Translate** → Multiple modes available:
   - **Batch DB** (`Ctrl+D`) → Translates database entries first (names, items, skills, enemies, terms)
   - **Batch Dialogue** (`Ctrl+T`) → Translates dialogue/events with auto-glossary from DB translations
   - **Batch All** (`Ctrl+Shift+T`) → Translates everything at once
   - **Batch by Actor** (`Ctrl+Shift+A`) → Groups dialogue by speaker gender (female → male → unknown → non-dialogue) for consistent pronoun usage
6. **Pronoun Hints** → `_build_code_hints()` maps `«CODEn»` placeholders back to actor names/genders; `_build_speaker_hint()` detects speaker gender from dialogue context
7. **Review/Edit** → `TranslationTable` editor panel for inline corrections, right-click retranslate with correction hint, or show 3 translation variants
8. **Grammar Polish** → English-to-English LLM pass to fix grammar/fluency without retranslating from Japanese
9. **Export** → `RPGMakerMVParser.save_project()` writes translations back into game JSON files + `plugins.js`, always reading from backup (`data_original/`, `plugins_original.js`) for idempotent re-export

### Key Modules

#### `ollama_client.py` — LLM Translation
- POST `/api/chat` with system prompt + user message
- **System prompt**: Specialized for RPG/eroge translation — faithful adult content, honorific preservation (-san, -chan, etc.), context-sensitive verbs
- **Target language**: Configurable (supports 12 languages with quality ratings); default English
- **Deterministic output**: `temperature: 0` + `seed: 42` for consistent translations across runs
- **Placeholder system**: Control codes (`\N[1]`, `\C[2]`, `<br>`, etc.) extracted before LLM sees text, replaced with `«CODE1»`, `«CODE2»` (guillemet format — LLM treats as opaque markup, won't replace with names)
- **Pronoun hint system**:
  - `actor_genders` / `actor_names` dicts set from project data
  - `_build_code_hints(code_map)` — scans placeholder→code mappings for `\N[n]` patterns, generates hints like `«CODE2» = name of Heroine (she/her)` so LLM uses correct pronouns for placeholder characters
  - `_build_speaker_hint(context)` — parses `[Speaker: name]` from dialogue context, cross-references with actor data, injects explicit gender hint (e.g., "Speaker: Sakura is FEMALE")
- **Japanese bracket conversion**: `「」` → `""`, `『』` → `""`, `【】` → `[]`, `（）` → `()`
- **Correction mode**: Accepts `correction` + `old_translation` params to fix bad translations
- **Field-type hints**: `_FIELD_HINTS` dict maps field names to human-readable labels ("dialogue line", "character name", "plugin configuration text", etc.) injected into every prompt
- **Actor context**: Character list with gender labels injected into every prompt
- **Two-layer glossary**: General glossary (shared) + project glossary (per-project) merged at translation time; project entries override general entries for same JP term
- **Translation variants**: `translate_variants()` generates 3 different translations using different seeds/temperatures for user to choose from
- **Grammar polish**: `polish()` method — English-to-English pass using `_POLISH_SYSTEM_PROMPT` to fix grammar and improve fluency without retranslating from Japanese
- **Name translation**: `translate_name()` lightweight method for short strings (names, titles) with context hints

#### `rpgmaker_mv.py` — Game File Parser
- Parses MV/MZ `data/` folder: database files, System.json, CommonEvents.json, Map###.json
- **Plugin parameter extraction**: Parses `js/plugins.js` for translatable Japanese text in plugin configuration — handles nested JSON-encoded arrays/objects recursively, filters out non-display text (file paths, color codes, asset IDs)
- **Event command codes**: 101=ShowText header (speaker info), 401=ShowText line, 102=Choices, 105/405=ScrollText
- **Dialogue grouping**: Consecutive 401 commands merged into single blocks for coherent translation
- **Speaker detection**: Reads 101 headers for face name / speaker name (MZ param[4])
- **Context building**: Configurable sliding window (`context_size`, default 3) of recent dialogue entries passed as context
- **Game title**: `get_game_title()` reads raw title from System.json regardless of language
- **Export**: Splits multi-line translations back into individual 401 commands, matching original line count
- **Backup-based export**: Creates `data_original/` backup on first export; subsequent exports always read from backup, ensuring idempotent re-export after editing translations
- **Plugin export**: Writes translated plugin parameters back to `plugins.js`, with backup as `plugins_original.js`

#### `project_model.py` — Data Model
- `TranslationEntry`: id, file, field, original, translation, status, context
- `TranslationProject`: entry list + glossary + general_glossary + actor_genders, save/load JSON state for resume
- Status values: `untranslated`, `translated`, `reviewed`, `skipped`
- Actor gender keys stored as ints (auto-converted on load for backward compatibility)

#### `translation_engine.py` — Batch Orchestration
- `TranslationWorker(QObject)` runs in `QThread`, emits `progress`, `entry_done`, `error`, `checkpoint`, `finished`
- Supports two modes: `translate` (JP→EN) and `polish` (EN→EN grammar fix)
- Configurable `num_workers` for parallel translation threads
- Skips already translated/reviewed/skipped entries
- Cancel support via `_cancelled` flag
- **Batch checkpointing**: Emits `checkpoint` signal every 25 entries for auto-save (prevents data loss on crash)

#### `text_processor.py` — Word Wrap
- `PluginAnalyzer`: Parses `js/plugins.js` for YEP_MessageCore, VisuMZ_MessageCore, etc.
- Detects message window width, font size, word wrap plugin tags (`<WordWrap>`)
- `TextProcessor`: Applies word wrapping respecting control code visual width

#### `default_glossary.py` — Preset Glossary
- ~100 common JP→EN translations for RPG terms, body parts, expressions
- Offered as defaults when opening a new project
- User can accept, modify, or skip

### GUI Widgets

#### `main_window.py`
- **Menu bar** with organized menus: Project, Translate, Game, Settings
  - **Project**: Open Project, Save State, Load State, Rename Folder
  - **Translate**: Batch DB, Batch Dialogue, Batch All, Batch by Actor, Stop, Polish Grammar, Fix Missing Codes, Apply Word Wrap
  - **Game**: Export to Game, Restore Originals, Export TXT
  - **Settings**: Opens settings dialog (includes dark mode toggle)
- Slim quick-access toolbar with Batch Translate + Stop buttons
- Catppuccin dark theme by default (toggled via Settings dialog)
- Horizontal splitter: file tree (left) + translation table (right)
- Auto-save every 2 minutes to `_translation_autosave.json` + checkpoint save every 25 entries during batch
- Translation memory: auto-fills exact duplicate strings before batch
- Auto-glossary: translated DB names automatically added to project glossary for dialogue consistency
- ETA display during batch translation
- Pre-translate: translates game title + actor info before gender dialog so user can read them
- Folder rename: translates folder name via Ollama and offers to rename to "English Title - WIP" (available anytime via Project menu)
- Retranslate with correction: background QThread for single-entry re-translation
- Translation variants: background QThread generates 3 options, shows picker dialog
- **Batch by Actor**: Groups entries by speaker gender (female → male → unknown → non-dialogue), shows breakdown dialog before starting, injects explicit gender hints for each group
- **Restore originals**: Restores both `data_original/` → `data/` and `plugins_original.js` → `plugins.js`

#### `translation_table.py`
- Columns: Status icon | File | Field | Original (JP) | Translation (EN)
- Color coding: red=untranslated, yellow=translated, green=reviewed, gray=skipped
- **Cross-file search**: Text search spans all entries regardless of current file tree selection; control codes are stripped from search terms so searching "hello" finds entries containing `\C[2]hello\C[0]`
- Filter bar: text search + status dropdown
- Right-click menu: Translate Selected, Retranslate with Correction, Show Variants (3 options), Mark Reviewed, Skip, Copy Original, Polish Grammar
- **Editor panel** (bottom 30%): side-by-side Original (read-only) + Translation (editable) QTextEdit
  - `setAcceptRichText(False)` to prevent control codes being interpreted as HTML
  - Right-click on translation editor: insert missing control codes from original
  - Edits sync back to entry + table in real-time

#### `file_tree.py`
- Groups files: Database, System, Common Events, Maps, Plugins, Other
- Shows translated/total progress badges per file and category
- Click file → filters table to that file's entries

#### `settings_dialog.py`
- Tab 1: Ollama URL, model dropdown (auto-populated), system prompt editor, test connection
- Target language dropdown (12 languages: English, Spanish, French, German, etc. with quality ratings)
- Translation options: context window size (0-20, default 3), parallel workers (1-8, default 2), word wrap override
- Appearance: dark mode toggle (Catppuccin theme)
- Tab 2: General Glossary — shared across all projects, ~100 preset defaults
- Tab 3: Project Glossary — per-project terms, auto-populated from translated DB names

#### `actor_gender_dialog.py`
- Shows all actors with auto-detected gender (from profile/note keywords)
- Displays translated names/nicknames/profiles alongside Japanese originals
- User can override: unknown/female/male
- "All Female" / "All Male" bulk buttons

#### `variant_dialog.py`
- Shows original JP text + 3 translation variants with radio buttons
- User picks preferred variant and clicks Apply
- Variants generated with different seeds/temperatures for diversity

## Important Design Decisions

1. **Placeholder format `«CODE1»`** — Earlier used `{1}` but the LLM treated it as a fillable template and replaced `{1}` with character names from actor context. Guillemet format is opaque enough that LLMs don't modify it.

2. **Code-to-character hints** — After extracting `\N[n]` codes into `«CODEn»` placeholders, `_build_code_hints()` generates explicit mapping hints (e.g., `«CODE2» = name of Heroine (she/her)`) so the LLM knows which placeholder refers to which gendered character. Without this, the LLM had no way to connect opaque placeholders to actor gender data, causing wrong pronouns.

3. **Speaker gender injection** — `_build_speaker_hint()` parses `[Speaker: name]` from dialogue context, cross-references with actor data, and injects "Speaker: Sakura is FEMALE" so the LLM uses first-person I/me for the speaker and correct third-person pronouns for others.

4. **Dialogue grouping** — Consecutive 401 commands = one translation unit. Essential for coherent multi-line dialogue. On export, translation is split back to match original line count (padded/trimmed).

5. **QTextEdit `setAcceptRichText(False)`** — Control codes like `<\N[1]>` and `<br>` were being parsed as HTML tags by QTextEdit. Plain text mode prevents this.

6. **Translation memory** — Before batch translate, scans already-translated entries and auto-fills any untranslated entries with identical original text. Saves LLM calls.

7. **Two-stage batch workflow** — Batch DB first to translate all names/terms, which auto-populates the project glossary. Then Batch Dialogue uses those terms as glossary entries for consistent naming across all dialogue.

8. **Two-layer glossary** — General glossary (shared, ~100 presets) + project glossary (per-project, auto-populated from DB translations). Both merged at translation time; project entries override general for same JP term.

9. **Backup-based export idempotency** — First export creates `data_original/` backup. All subsequent exports read from backup, not from previously-exported files. This means you can re-export after editing translations without corrupting source data.

10. **Context window** — Each entry's context includes: speaker name (from 101 header), configurable number of recent dialogue entries (default 3), actor gender info, glossary terms. All injected into the user message.

11. **Deterministic output** — `temperature: 0` + `seed: 42` ensures identical translations across runs. Variant generation uses `temperature: 0.5`/`0.8` with different seeds for diversity.

12. **Batch checkpointing** — Auto-saves every 25 entries during batch translation via `checkpoint` signal. Prevents catastrophic data loss if Ollama crashes or system interrupts.

13. **Adult content handling** — System prompt explicitly instructs faithful translation of all content including sexual/violent material. Honorifics preserved as-is (-san, -chan, etc.).

14. **Field-type hints** — Every translation request includes a "Content type" hint (e.g., "dialogue line", "character name", "plugin configuration text") so the LLM knows what it's translating and adjusts accordingly.

## RPG Maker MV/MZ Event Command Codes Reference
| Code | Name | Translatable? | Notes |
|------|------|---------------|-------|
| 101 | Show Text Header | No | Face name, speaker name (MZ param[4]) |
| 401 | Show Text Line | Yes | `parameters[0]` = text line |
| 102 | Show Choices | Yes | `parameters[0]` = string array |
| 105 | Scroll Text Header | No | Setup command |
| 405 | Scroll Text Line | Yes | `parameters[0]` = text line |
| 320 | Change Actor Name | Yes | `params[0]`=actorId, `params[1]`=name |
| 324 | Change Actor Nickname | Yes | `params[0]`=actorId, `params[1]`=nickname |
| 325 | Change Actor Profile | Yes | `params[0]`=actorId, `params[1]`=profile |
| 356 | Plugin Command (MV) | Yes | `params[0]` = command string |
| 357 | Plugin Command (MZ) | Yes | `params[3+]` may contain translatable text |
| 355 | Script (line 1) | Skipped | Plugin script calls |
| 655 | Script (continuation) | Skipped | Plugin script calls |

## Common RPG Maker Control Codes
- `\N[n]` — Actor name by ID
- `\V[n]` — Variable value
- `\C[n]` — Text color change
- `\FS[n]` — Font size
- `\{` / `\}` — Increase/decrease font size
- `\$` — Show gold window
- `\.` / `\|` — Wait 15/60 frames
- `\!` — Wait for input
- `\>` / `\<` — Instant display on/off
- `\^` — Don't wait for input
- `<br>` — Line break (plugin)
- `<WordWrap>` — Enable word wrap (plugin)

## Session Status (2026-02-13)

### Recent Changes (this session)
All pushed to master. Recent commits in order:
- `ccbad09` — Troops battle events, System type arrays, speaker filter & pronoun swap
- `f3f346b` — Single 401 mode, dark mode dialog styling, Troops export fix
- `45a1287` — Bug fix round 1: quote stripping, speaker lookup, nested value, control var, filter restore
- `8b10312` — Bug fix round 2: glossary overwrite, vocab regex, sprite pairing, bbox clamp
- `8099192` — Initialize vision_model in OllamaClient.__init__

### Bug Fix Summary (9 bugs fixed across 2 rounds)
**Round 1 (5 bugs):**
1. Quote stripping corrupted legitimate dialogue quotes (ollama_client.py) — changed `find/rfind` to only strip if text starts+ends with quote
2. Case-sensitive speaker name lookup (ollama_client.py) — `.lower()` comparison
3. Missing `isinstance(str)` check in `_set_nested_value` dict branch (rpgmaker_mv.py)
4. Control var string using f-string instead of `json.dumps` (rpgmaker_mv.py) — proper escaping
5. `_select_entry_by_id` permanently clearing file filter (translation_table.py) — restore previous filter

**Round 2 (4 bugs):**
1. `_maybe_add_to_glossary` overwrote `project.glossary` with entire merged glossary (main_window.py) — now only adds single term
2. Vocab export regex expected `[n]` but entry IDs use `/n/` format (main_window.py) — gender lookup was silently failing
3. Two-state sprite: unmatched pairs added to merged list (image_translator.py) — only confirmed matches now
4. Bbox clamping allowed zero-width/inverted geometry (image_translator.py) — discards invalid regions

**Round 3:** Clean bill of health — 21 agent findings across 4 agents, all false positives or by-design behavior. Codebase is solid.

### What's Next
- Waiting for user feedback to drive further changes
- No known bugs remaining
- Potential future work: VX Ace support, translation memory improvements

## Future Roadmap (Not Implemented)

### Priority 4 — UI Enhancements
- [ ] Event Viewer panel — groups all entries by event (CE169, Ev3/p0, etc.), lets user browse a single event's dialogue in sequence, useful for cross-referencing between game versions
- [ ] Spell checker — `pyspellchecker` + `QSyntaxHighlighter` on translation editor, red underlines on misspelled words, right-click suggestions + "Add to Dictionary", skips control codes/placeholders (English only)

### Priority 5 — Coverage Expansion
- [ ] VX Ace (.rvdata2), XP (.rxdata), VX (.rvdata) support via `rubymarshal`

### Priority 6 — Multi-Engine Support (Low Priority, Reference: DazedMTLTool MIT)
- [ ] Ren'Py (.rpy) — text-based, large visual novel user base
- [ ] Wolf RPG (.dat) — binary format, DazedMTL parser valuable as reference
- [ ] Tyrano Script (.ks) — text-based, straightforward parsing
- [ ] Kirikiri (.scn) — visual novel engine

### Completed / Superseded
- [x] Translation memory within batch (auto-fills duplicate strings before batch translate)
- [x] Plugin parameter extraction from plugins.js
- [x] Auto-retry on Japanese remnants — detects Japanese in output, retries with stronger prompt (single + batch)
- [x] Smart glossary injection — `_filter_glossary()` only injects terms found in source text + context
- [x] Manual "Fix Missing Codes" tool — menu action + right-click restore for dropped control codes
- [x] Auto-restore dropped control codes — `_restore_missing_codes()` runs at every checkpoint and batch finish, no LLM retry needed
- [x] Translation cache across sessions — already covered by project save/load (skips translated entries) + TM (deduplicates within batch) + general glossary (cross-project terms)
- [x] Translation history as assistant messages — `translate()` sends last N translations as user/assistant pairs, `max_history=10`, context window scales with history size
- [x] Speaker name translation cache — `_pre_translate_info()` translates all actor names before batch, auto-glossary ensures reuse in every subsequent call
- [x] Plugin command whitelist (356/357) — DazedMTL-based whitelist extracts only known-safe display text from 16 MZ plugins + 13 MV command patterns. Replaces scan-everything approach that broke games.

### Declined After Testing
- [~] Batch JSON translation — `translate_batch()` / `polish_batch()` code exists but quality degrades on local 14B models (Sugoi, Qwen3). Small context windows (~4K tokens) can't handle system prompt + glossary + N entries well. Single-entry (`batch_size=1`) produces better results. Code kept in case larger cloud models make it viable.
- [~] plugins.js parameter scan — `_parse_plugins()` disabled, `_scan_plugin_param()` / `_scan_parsed_value()` commented out. Over-extracted internal identifiers, config keys, and command keywords that broke games. Plugin display text now handled via event command whitelists (356/357) instead.
- [~] Plugin script text extraction (code 355/655) — too dangerous. Inline JavaScript in events; translating code strings breaks game logic. DazedMTL also has it OFF by default.
- [~] Longest-first replacement in export — not needed, export uses entry ID position in JSON tree, not substring replacement
- [~] Atomic file writes — low probability risk, `data_original/` backup already covers the failure case
