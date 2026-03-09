<p align="center">
  <img src="logo.png" alt="RPG Maker Translator" width="400">
</p>

<h1 align="center">RPG Maker Translator</h1>

<p align="center">
Translate RPG Maker MV/MZ and TyranoScript* games from Japanese to English.<br>
<b>Local LLM</b> (Ollama + Qwen 3.5 — free, private, no content filters) or <b>Cloud API</b> (OpenAI, Gemini, DeepSeek, Anthropic — experimental, pay-per-token).<br>
Auto-tuned to maximize GPU speed. Pronoun-aware. Glossary-driven. Batch translation with resume.<br>
Designed by a human, coded with <a href="https://claude.ai/code">Claude Code</a>.
Cloud API engine ported from <a href="https://github.com/dazedanon/DazedMTLTool">DazedMTLTool</a> (MIT).
</p>

<p align="center">
  <i>Built solo with local LLMs and Claude Code. If this saved you hours of work, consider supporting development:</i><br><br>
  <a href="https://ko-fi.com/moritranslates"><img src="https://ko-fi.com/img/githubbutton_sm.svg" alt="Support on Ko-fi"></a>
</p>

![Screenshot](screenshot.png)

## At a Glance

Open a game folder. Hit Batch Translate. Get a playable English translation. RPG Maker games open directly in the editor for QA — no copying files, no manual setup.

| | |
|---|---|
| **Local LLM (free)** | Ollama + [Qwen 3.5:9b](https://ollama.com/library/qwen3.5) on your GPU — auto-tuned for your hardware, no API keys, no content filters, no account bans. Your data never leaves your PC |
| **Cloud API (experimental)** | OpenAI, Gemini, DeepSeek, Anthropic — DazedMTL-compatible batch mode with live cost tracking |
| **Pronoun system** | Actor genders, speaker detection, `\N[n]` character mapping — the LLM knows who's "he" and who's "she" |
| **Glossary-driven** | Two-layer glossary auto-built from translated DB names — "Potion" stays "Potion" everywhere |
| **Open in RPG Maker** | One-click workspace with directory junctions — edit and playtest translations in RPG Maker's visual editor |
| **Crash-proof** | Auto-saves every 25 entries, project resume, translation memory deduplication |

---

## Table of Contents

- [Key Features](#key-features)
- [Quick Start](#quick-start)
- [Recommended Models](#recommended-models)
- [Cloud API Providers](#cloud-api-providers)
- [Translation Workflow](#translation-workflow)
- [Glossary System](#glossary-system)
- [Open in RPG Maker](#open-in-rpg-maker)
- [Settings Reference](#settings-reference)
- [Supported Formats & Languages](#supported-formats--languages)
- [Troubleshooting](#troubleshooting)
- [Acknowledgments](#acknowledgments)

---

## Key Features

- **Pronoun-aware gender system** — Assigns actor genders, detects speakers from dialogue headers, maps `\N[n]` codes to characters so the LLM uses correct pronouns.
- **Two-stage workflow** — Translate database names first (items, skills, enemies), then dialogue. DB names auto-populate the glossary.
- **Two-layer glossary** — General glossary (shared, ~100 presets) + project glossary (auto-built from DB translations). Smart injection — only matching terms sent per request.
- **Batch by Actor** — Groups dialogue by speaker gender (female > male > unknown) for maximum pronoun accuracy.
- **Translation memory** — Deduplicates identical strings before batch. If 50 NPCs say the same line, the LLM translates it once.
- **Translation history** — Last 10 translations sent as context so the LLM maintains consistent tone and pronouns across sequential dialogue.
- **Auto-retry** — Detects leftover Japanese in output and retries with a stronger prompt.
- **Auto-save & checkpointing** — Saves every 25 entries during batch. Crash-proof.
- **Auto-tune** — Tournament-style calibration finds your GPU's optimal batch size automatically. Just hit translate and it figures out the fastest settings.
- **DazedMTL Mode** — One-click toggle: batch 30, 4 workers, DazedMTL prompt. Works with both local Sugoi and cloud APIs.
- **Open in RPG Maker** — Creates a workspace with directory junctions so you can QA and playtest translations in RPG Maker's visual editor. Auto-detects MV vs MZ.
- **Cloud cost tracking** — Real-time token count and USD cost during batch translation.
- **Translation variants** — Generate 3 different translations per entry and pick the best one.
- **Word wrap plugin** — Auto-injected JS plugin wraps dialogue at render time using pixel measurements. Also hooks Window_Help for skill/item descriptions. No manual line break guessing.
- **Post-processor** — Automated fixes after batch translate: placeholder leaks, collapsed color codes, missing spaces, spurious newlines, skill message spacing, and more.
- **Grammar polish** — English-to-English LLM pass to fix awkward phrasing without retranslating from Japanese.
- **Prompt presets** — Default, Sugoi (DazedMTL Full), DazedMTL Simple, or Custom. Reset Default and Clear buttons.

---

## Quick Start

### 1. Install Ollama + Qwen 3.5

```bash
# Install Ollama: https://ollama.com/download
# Then grab Qwen 3.5 (best JP→EN model):
ollama pull qwen3.5:9b
```

### 2. Install & Run

```bash
pip install -r requirements.txt
python main.py
```

### 3. Translate

1. **Project > Open Project** — point to your game folder
2. **Assign actor genders** in the popup dialog
3. **Batch DB** (`Ctrl+D`) — translate names/items/skills first
4. **Batch Dialogue** (`Ctrl+T`) — translate dialogue with auto-glossary
5. **Game > Apply Translation** (`Ctrl+E`) — write back to game files
6. **Game > Open in RPG Maker** (`Ctrl+R`) — QA in the visual editor

---

## Recommended Model — Qwen 3.5

Best JP→EN model available locally. 262K native context window, multimodal (also used for image OCR), handles honorifics, adult content, and RPG Maker control codes. Works for all 24 supported target languages.

| GPU VRAM | Model | Command |
|---|---|---|
| **6GB** | Qwen 3.5:9b Q4_K_M | `ollama pull qwen3.5:9b` |
| **10GB** | Qwen 3.5:9b Q8_0 | `ollama pull qwen3.5:9b-q8_0` |
| **12GB** | Qwen 3.5:14b Q4_K_M | `ollama pull qwen3.5:14b` |
| **16GB** | Qwen 3.5:14b Q8_0 | `ollama pull qwen3.5:14b-q8_0` |
| **24GB** | Qwen 3.5:30b Q4_K_M | `ollama pull qwen3.5:30b` |

The 9b model is the sweet spot — fast, fits in 6GB VRAM, and produces excellent translations.

### No GPU? Use Cloud APIs

In Settings, switch Provider to OpenAI/Gemini/DeepSeek/Anthropic, enter your API key, and go. Same workflow, same features, pay per token. Cheapest option: **Gemini 2.0 Flash** at $0.10/$0.40 per 1M tokens.

> **Note:** Cloud APIs may refuse or filter adult content and can ban accounts for repeated NSFW requests. For unrestricted translation of all content types, use Local LLM — everything runs on your GPU, nothing is sent to the cloud.

---

## Cloud API Providers (Experimental)

> **Experimental:** Cloud API support is functional but less battle-tested than local Ollama. Expect occasional edge cases. Cloud APIs may also refuse or filter adult content — see [Local LLM note above](#no-gpu-use-cloud-apis).

| Provider | Models | Pricing (per 1M tokens) |
|---|---|---|
| **Google Gemini** | gemini-2.0-flash, 2.5-flash, 2.5-pro | $0.10–$1.25 in / $0.40–$10.00 out |
| **OpenAI** | gpt-4.1-mini, gpt-4.1, gpt-5 | $0.40–$2.00 in / $1.60–$10.00 out |
| **DeepSeek** | deepseek-chat | $0.27 in / $1.10 out |
| **Anthropic** | claude-sonnet-4.5 | $3.00 in / $15.00 out |

All providers use the OpenAI SDK as a universal abstraction. Switch in Settings > Provider, enter your API key, and the rest is automatic — batch size, workers, and prompt presets auto-configure.

---

## Translation Workflow

### Two-Stage Batch (Recommended)

1. **Batch DB** (`Ctrl+D`) — Translates database entries: item names, skill names, enemy names, system terms. These become glossary entries.
2. **Review DB names** — Fix any mistranslations in the table. Corrections auto-update the glossary.
3. **Batch Dialogue** (`Ctrl+T`) — Translates dialogue, events, and plugin text using the glossary built in step 1.

This prevents inconsistencies like an NPC saying "Take the Holy Sword" when the inventory calls it "Sacred Blade".

### Other Batch Modes

- **Batch All** (`Ctrl+Shift+T`) — DB + dialogue in one pass (skips manual review step)
- **Batch by Actor** (`Ctrl+Shift+A`) — Groups by speaker gender for best pronoun accuracy. Shows a breakdown before starting.

### Per-Entry Tools (Right-Click Menu)

- **Translate Selected** — Translate specific rows
- **Retranslate with Correction** — Hint what was wrong ("use she/her", "too literal")
- **Show Variants (3 options)** — Pick from 3 different translations
- **Mark Reviewed / Skip** — Track QA progress
- **Polish Grammar** — English-to-English cleanup pass

---

## Glossary System

The glossary forces the LLM to use specific translations for Japanese terms. Only terms that appear in the current text are injected — a 200-entry glossary doesn't bloat prompts.

**Two layers:**
- **General Glossary** (Settings) — Shared across all projects. ~100 presets for common RPG terms.
- **Project Glossary** (Settings) — Per-project. Auto-populated from translated DB names.

Project entries override general entries for the same JP term.

**Glossary menu:**
- Import/Export vocab files (DazedMTL-compatible format)
- Scan a translated game folder for JP→EN pairs
- Build glossary from this project's translations
- Apply glossary to fix inconsistent translations

---

## Open in RPG Maker

**Game > Open in RPG Maker** (`Ctrl+R`) creates a workspace folder with directory junctions pointing to the game's data and assets. RPG Maker opens it as a project — you can playtest, inspect events, and verify translations in the visual editor.

- Auto-detects MV vs MZ engine
- Zero disk space (junctions, not copies)
- Edits in RPG Maker write directly to game files
- Works with RPG Maker MV ($6 on sale) or MZ

---

## Settings Reference

| Setting | Default | Description |
|---------|---------|-------------|
| Provider | Ollama (Local) | Translation engine — Ollama, OpenAI, Gemini, DeepSeek, Anthropic, Custom |
| Model | (auto-detected) | LLM model. Qwen 3.5:9b recommended for JP→EN |
| Prompt Preset | Default / Sugoi | Preset prompt or Custom. Reset Default / Clear buttons |
| DazedMTL Mode | Off | One-click: batch 30, 4 workers, DazedMTL prompt |
| Target Language | English | 24 languages with quality ratings |
| Context window | 3 | Recent dialogue lines as context (higher = better coherence) |
| Workers | 2 | Parallel translation threads (auto-set by provider) |
| Batch size | 1 | Lines per request (auto-set: 1 local, 30 cloud) |
| Auto-tune batch size | Off | Tournament calibration tests batch sizes 5-30 and picks the fastest for your GPU |
| Translation history | 10 | Recent translations sent as assistant messages |
| Dark mode | On | Catppuccin dark theme |

---

## Supported Formats & Languages

### Game Formats

| Format | Status |
|---|---|
| RPG Maker MV (.json) | Supported |
| RPG Maker MZ (.json) | Supported |
| TyranoScript (.ks) | Testing* |
| RPG Maker VX Ace (.rvdata2) | Planned |

> **\*TyranoScript support** is functional but still being tested. Features include: auto-extraction from NW.js executables, `[r]`/`[p]`/`[emb]` tag preservation via `«CODE»` placeholders, self-calibrating word wrap from original JP line lengths, VN-specific LLM prompt, and a dedicated post-processor for tag leak cleanup. Open a `.exe` or extracted game folder and it auto-detects the engine.

### Target Languages

24 languages supported. English is the primary target (use Sugoi for best quality). Other languages use Qwen3 with quality rated 2-5 stars in Settings.

**Top tier:** English, Chinese (Simplified/Traditional), Korean, Spanish, Portuguese, French, German
**Good:** Russian, Italian, Polish, Dutch, Turkish, Indonesian, Vietnamese, Thai, Malay
**Fair:** Arabic, Hindi, Ukrainian, Czech, Romanian, Hungarian, Tagalog

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "Cannot connect to Ollama" | Run `ollama serve` in a terminal first |
| Translations are slow | Use Qwen 3.5:9b with batch size 5+ or enable DazedMTL Mode (batch 30, 4 workers). Auto-tune finds your GPU's sweet spot |
| Wrong pronouns | Assign correct genders in the actor dialog, or use Batch by Actor mode |
| Missing control codes | Right-click > Restore Missing Codes, or they auto-restore at checkpoints |
| Cloud API errors | Check your API key in Settings. Test Connection button verifies connectivity |
| Plugin translations break game | Plugin entries are skipped by default. Only unskip display text (menu labels, descriptions) |

---

## Acknowledgments

- **[DazedMTLTool](https://github.com/dazedanon/DazedMTLTool)** (MIT) — Cloud API engine, batch translation approach, prompt presets, and pricing config ported from DazedMTL. DazedMTL Mode mirrors their exact translation pipeline.
- **[Sugoi Toolkit](https://huggingface.co/sugoitoolkit)** — Fine-tuned JP→EN translation models optimized for visual novels and RPGs.
- **[Ollama](https://ollama.com/)** — Local LLM inference server that makes GPU translation free and private.

---

## License

MIT
