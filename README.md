# RPG Maker Translator — Local LLM

A desktop tool for translating RPG Maker MV/MZ games from Japanese to English using a local LLM (no cloud API needed). Runs entirely on your machine with Ollama.

![Screenshot](screenshot.png)

## Requirements

- **Windows 10/11** (tested on Windows)
- **Python 3.10+**
- **NVIDIA GPU** recommended (AMD GPUs partially supported — see below)
- **Ollama** installed and running

## Recommended Models by GPU

Pick the best model for your GPU's VRAM. Larger models produce better translations but require more memory.

| GPU (VRAM) | Recommended Model | Command | Quality | Speed |
|---|---|---|---|---|
| **RTX 3060 / 4060** (8GB) | Qwen2.5:7b | `ollama pull qwen2.5:7b` | Good | ~35 tok/s |
| **RTX 3070 / 4060 Ti** (8GB) | Qwen2.5:7b | `ollama pull qwen2.5:7b` | Good | ~45 tok/s |
| **RTX 3080 / 4070** (10-12GB) | Qwen2.5:14b | `ollama pull qwen2.5:14b` | Great | ~25 tok/s |
| **RTX 3090 / 4070 Ti** (12GB) | Qwen2.5:14b | `ollama pull qwen2.5:14b` | Great | ~30 tok/s |
| **RTX 4080** (16GB) | Qwen2.5:14b | `ollama pull qwen2.5:14b` | Great | ~40 tok/s |
| **RTX 4090** (24GB) | Qwen2.5:32b | `ollama pull qwen2.5:32b` | Excellent | ~30 tok/s |
| **2x GPUs / 48GB+** | Qwen2.5:72b | `ollama pull qwen2.5:72b` | Best | ~15 tok/s |
| **CPU only** (no GPU) | Qwen2.5:3b | `ollama pull qwen2.5:3b` | Basic | ~5 tok/s |

**Notes:**
- Speeds are approximate and vary by text length and system config
- Models use Q4_K_M quantization by default in Ollama (good quality-to-size ratio)
- If a model barely fits your VRAM, it will work but may be slower due to partial CPU offload
- You can always try the next size up — if it runs too slowly, switch back in **Settings**
- GPUs with 6GB (RTX 2060, GTX 1660) can use `qwen2.5:3b` (~3GB VRAM) but quality drops noticeably for nuanced Japanese

**Why Qwen2.5?** The Qwen2.5 model family has the best Japanese language comprehension among open-weight models at every size tier. Other models (Llama, Mistral) are weaker at Japanese and produce more translation errors.

### AMD GPU Support

Ollama supports AMD GPUs via ROCm, but with caveats:

| GPU | VRAM | OS Support | Notes |
|-----|------|------------|-------|
| **RX 7900 XTX** | 24GB | Linux (ROCm) | Best AMD option, runs 14B+ comfortably |
| **RX 7900 XT** | 20GB | Linux (ROCm) | Solid for 14B models |
| **RX 7800 XT** | 16GB | Linux (ROCm) | Good for 14B |
| **RX 7700 XT** | 12GB | Linux (ROCm) | Tight for 14B, similar to 4070 Ti |
| **RX 6000 series** | Varies | Linux (ROCm) | RDNA2 supported but slower |

**Important AMD notes:**
- **Linux is recommended** — ROCm has full support on Linux. Windows AMD support in Ollama uses a Vulkan fallback which is significantly slower.
- **~30-50% slower** than equivalent NVIDIA GPUs for LLM inference due to less mature software stack.
- AMD GPUs need more VRAM to match NVIDIA performance — a 12GB AMD card won't perform as well as a 12GB NVIDIA card.
- If you're buying new hardware specifically for local LLM work, NVIDIA is the safer choice.

## Installation

### 1. Install Ollama

Download and install Ollama from: https://ollama.com/download

After installation, open a terminal and pull the model for your GPU (see table above):

```bash
ollama pull qwen2.5:14b
```

### 2. Install Python Dependencies

```bash
pip install PyQt6 requests
```

Or using the requirements file:

```bash
pip install -r requirements.txt
```

### 3. Start Ollama

Make sure the Ollama server is running before launching the translator:

```bash
ollama serve
```

Ollama runs on `http://localhost:11434` by default. You can leave this terminal open in the background.

### 4. Launch the Translator

```bash
python main.py
```

## Getting Started

### Opening a Game Project

1. Go to **Project > Open Project** (or `Ctrl+O`)
2. Navigate to your RPG Maker MV/MZ game folder (the folder containing a `data/` or `www/data/` subfolder with JSON files)
3. The tool will scan all translatable text from database files, maps, common events, and system strings

### Actor Gender Assignment

After opening a project, a dialog will appear showing all actors found in the game. You can assign genders (male/female) so the translator uses correct pronouns (he/she). The tool auto-detects genders from profile text but you should verify.

### Translating

- **Batch Translate**: Go to **Translate > Batch Translate** (or `Ctrl+T`) to translate all untranslated entries. Progress and ETA are shown in the status bar. Auto-saves every 25 entries so you don't lose progress if something crashes.
- **Translate Selected**: Select rows in the table, right-click, and choose **Translate Selected** to translate specific entries.
- **Retranslate with Correction**: Right-click a translated entry and choose **Retranslate with Correction...** to provide a hint about what was wrong (e.g., "wrong pronoun", "too literal").
- **Show Variants**: Right-click and choose **Show Variants (3 options)...** to generate 3 different translations and pick the best one.

### Reviewing and Editing

- Click any row to view the original Japanese and English translation side-by-side in the editor panel at the bottom
- Edit the English translation directly in the right-side editor box
- Right-click the editor to insert missing control codes (`\N[1]`, `\C[2]`, etc.) from the original
- Right-click a row and choose **Mark as Reviewed** to mark it green

### Glossary

Open **Settings** and go to the **Glossary** tab to define forced translations for specific terms. The LLM will always use your glossary entries for those Japanese terms (useful for character names, locations, items).

### Renaming the Project Folder

Go to **Project > Rename Folder** to translate the Japanese folder name to English via Ollama and rename the project folder (appends " - WIP" by default). You can edit the suggested name before confirming.

### Saving and Resuming

- **Save State** (`Ctrl+S`): Saves all translations to a JSON file so you can close and resume later
- **Load State** (`Ctrl+L`): Loads a previously saved state file to continue work
- **Auto-Save**: The tool auto-saves every 2 minutes if you have entries loaded

### Exporting to the Game

- **Export to Game** (`Ctrl+E`): Writes all translated text back into the game's original JSON files in the `data/` folder (via **Game** menu)
- **Export TXT**: Saves a human-readable text patch file for reference (via **Game** menu)
- **Restore Originals**: Restores original Japanese files from backup (via **Game** menu)

### Word Wrap

Go to **Translate > Apply Word Wrap** to automatically format translated text to fit the game's message window width. The tool detects message plugins (YEP_MessageCore, VisuMZ_MessageCore, etc.) and adjusts line lengths accordingly.

## Settings

| Setting | Default | Description |
|---------|---------|-------------|
| Ollama URL | `http://localhost:11434` | Address of the Ollama server |
| Model | `qwen2.5:14b` | Which LLM model to use |
| System Prompt | (built-in) | The instruction prompt sent to the LLM |
| Context window size | `3` | Number of recent dialogue lines sent as context (higher = better coherence, more VRAM) |
| Dark mode | On | Catppuccin dark theme (toggle in Settings > Appearance) |
| Glossary | (empty) | Forced JP-to-EN term mappings |

## Supported Game Formats

| Format | Status |
|--------|--------|
| RPG Maker MV (.json) | Supported |
| RPG Maker MZ (.json) | Supported |
| RPG Maker VX Ace (.rvdata2) | Planned |
| RPG Maker VX (.rvdata) | Planned |
| RPG Maker XP (.rxdata) | Planned |

## Troubleshooting

**"Cannot connect to Ollama"**
- Make sure you ran `ollama serve` in a terminal
- Check that the URL in Settings matches (default: `http://localhost:11434`)

**Translations are slow**
- Each entry takes 2-10 seconds depending on text length and GPU speed
- The Qwen2.5:14b model on a 4070 Ti processes roughly 20-30 tokens/second
- Use a smaller model (`qwen2.5:7b`) for faster but lower-quality translations

**Control codes are missing from translations**
- The tool automatically preserves control codes (`\N[1]`, `\C[2]`, etc.) using a placeholder system
- If codes are still missing, right-click the translation editor and use **Restore Missing Code(s)**

**Wrong pronouns in translation**
- Open a project fresh and assign correct genders in the actor dialog
- Use **Retranslate with Correction** and hint "use she/her" or "use he/him"
