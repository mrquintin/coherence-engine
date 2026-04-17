# Coherence Engine v2.0.0 — Complete Setup Guide

This document walks through every step required to get the Coherence Engine
fully operational on a new machine. It covers the project structure, Python
environment, three installation tiers, all four interfaces (library, CLI, GUI,
HTTP API), running the test suite, and the macOS `.app` installer.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Getting the Source Code](#2-getting-the-source-code)
3. [Understanding the Project Structure](#3-understanding-the-project-structure)
4. [Creating a Python Virtual Environment](#4-creating-a-python-virtual-environment)
5. [Installing the Package](#5-installing-the-package)
6. [Choosing a Dependency Tier](#6-choosing-a-dependency-tier)
7. [Verifying the Installation](#7-verifying-the-installation)
8. [Running the Test Suite](#8-running-the-test-suite)
9. [Using the Engine as a Python Library](#9-using-the-engine-as-a-python-library)
10. [Using the CLI](#10-using-the-cli)
11. [Launching the Desktop GUI](#11-launching-the-desktop-gui)
12. [Running the HTTP API Server](#12-running-the-http-api-server)
13. [Installing as a macOS Application](#13-installing-as-a-macos-application)
14. [Configuration Reference](#14-configuration-reference)
15. [Troubleshooting](#15-troubleshooting)

---

## 1. Prerequisites

### Python 3.9 or later

The engine requires Python 3.9+. It has been tested on 3.9, 3.10, 3.11, 3.12,
and 3.13.

Check your version:

```bash
python3 --version
```

If you don't have Python 3.9+, install it:

- **macOS (Homebrew):** `brew install python@3.13`
- **macOS (official):** Download from [python.org](https://www.python.org/downloads/)
- **Ubuntu/Debian:** `sudo apt update && sudo apt install python3 python3-pip python3-venv`
- **Fedora:** `sudo dnf install python3 python3-pip`
- **Windows:** Download from [python.org](https://www.python.org/downloads/). During installation, check "Add Python to PATH".

### tkinter (for the GUI only)

The desktop GUI uses tkinter, which is included with Python on macOS and
Windows. On Linux you may need to install it separately:

```bash
# Ubuntu/Debian
sudo apt install python3-tk

# Fedora
sudo dnf install python3-tkinter
```

### pip

Ensure pip is available and up to date:

```bash
python3 -m pip install --upgrade pip
```

### Operating System

- **macOS 12+** — Full support including the `.app` installer and GUI.
- **Linux** — Full support for library, CLI, and API. GUI works if tkinter is
  installed.
- **Windows** — Full support for library, CLI, and API. GUI works out of the
  box with the official Python installer.

### Hardware

- **Minimal install:** Any machine. ~50 MB disk, negligible RAM.
- **ML install (SBERT):** ~500 MB disk, ~1 GB RAM during analysis.
- **Full install (NLI + SBERT):** ~2-3 GB disk, ~2 GB RAM during analysis. A
  GPU is supported but not required — everything runs on CPU.

---

## 2. Getting the Source Code

Clone or copy the project so you have this structure:

```
coherence_engine/               ← Project root (your workspace)
├── __init__.py
├── __main__.py
├── cli.py
├── config.py
├── gui.py
├── core/                       ← Pipeline orchestration
│   ├── types.py, parser.py, scorer.py
│   ├── report.py, explanation.py
├── layers/                     ← Five analysis layers
│   ├── contradiction.py, argumentation.py
│   ├── embedding.py, compression.py, structural.py
├── embeddings/                 ← Embedding backends
│   ├── base.py, sbert.py, tfidf.py, utils.py
├── domain/                     ← Domain detection and comparison
│   ├── detector.py, comparator.py, premises.py
├── server/                     ← Optional FastAPI server
│   └── app.py
├── data/                       ← Bundled JSON data
│   ├── societal_premises.json, negation_patterns.json
├── tests/                      ← Test suite (152 tests)
│   ├── fixtures/
│   └── test_*.py
├── installer/                  ← macOS .app installer
├── pyproject.toml              ← Package definition
├── SETUP.md                    ← This file
├── COHERENCE_ENGINE_PROJECT_STATUS.txt
└── COHERENCE_ENGINE_CONTINUATION_PROMPT.txt
```

The **project root** is the `coherence_engine/` directory — the one that
contains `pyproject.toml` and `__init__.py`. All commands in this guide should
be run from the **parent** of this directory (so that `coherence_engine/` is
a child), or ensure the package is installed via `pip install -e .`.

```bash
cd /path/to/parent_of/coherence_engine
```

---

## 3. Understanding the Project Structure

### The engine package (`coherence_engine/`)

This is the importable Python package. It contains:

| Directory | Purpose |
|-----------|---------|
| `core/` | Pipeline orchestration — parser, scorer, report generator, explanation engine, types |
| `layers/` | Five analysis layers (contradiction, argumentation, embedding, compression, structural) |
| `embeddings/` | Embedding backends: SBERT (ML) and TF-IDF (zero-dependency fallback) |
| `domain/` | Domain detection, comparison against societal premises |
| `server/` | Optional FastAPI HTTP server |
| `data/` | Bundled JSON data files (societal premises, negation patterns) |

### Six-stage pipeline

Input text flows through: parsing, five analysis layers, cross-layer signal
fusion, composite scoring, explanation generation, and report output.

### Five analysis layers

Every piece of text is analyzed by five independent layers that each produce a
0-to-1 score:

| # | Layer | Default Weight | What it measures | ML dependency |
|---|-------|---------------|------------------|---------------|
| 1 | Contradiction | 0.30 | Internal consistency (NLI or heuristic) | `transformers` + `torch` (optional) |
| 2 | Argumentation | 0.20 | Dung's framework grounded extension | None |
| 3 | Embedding | 0.20 | Semantic similarity + cosine paradox detection | `sentence-transformers` (optional) |
| 4 | Compression | 0.15 | zlib as Kolmogorov complexity proxy | None |
| 5 | Structural | 0.15 | Graph connectivity, isolation, depth, cycles | None |

The composite score is the weighted sum, clamped to [0, 1], after cross-layer
signal fusion adjusts individual layer scores when layers corroborate each
other (e.g., contradiction layer and embedding layer both flagging the same
pair boosts confidence; grounded propositions that are structurally isolated
receive a penalty).

### Calibration

- **Compression layer** uses a length-aware sigmoid calibration instead of a
  flat scaling factor, making scores more stable across different text lengths.
- **Embedding layer** uses per-embedder thresholds for suspicious-pair
  detection: SBERT thresholds (cosine > 0.70, sparsity > 0.30) are higher than
  TF-IDF thresholds (cosine > 0.50, sparsity > 0.25) because SBERT produces
  naturally higher cosine similarities.

### Graceful degradation

Every ML-dependent layer has a zero-dependency fallback:

- **Layer 1:** If `transformers`/`torch` are missing, heuristic pattern matching
  (antonyms, negation, commitment conflicts, sentiment clashes, numerical
  contradictions) is used automatically.
- **Layer 3:** If `sentence-transformers` is missing, TF-IDF embeddings are
  used automatically.

The engine **always** produces a score, even with zero optional dependencies.

---

## 4. Creating a Python Virtual Environment

A virtual environment isolates the engine's dependencies from your system
Python. This is strongly recommended.

```bash
# Create the virtual environment
python3 -m venv venv

# Activate it
# macOS / Linux:
source venv/bin/activate
# Windows (PowerShell):
venv\Scripts\Activate.ps1
# Windows (cmd):
venv\Scripts\activate.bat
```

Once activated, your shell prompt will show `(venv)`. All subsequent `pip
install` and `python` commands will use this isolated environment.

To deactivate later: `deactivate`

---

## 5. Installing the Package

From the project root (the directory containing `pyproject.toml`), with your
virtual environment activated:

```bash
pip install -e .
```

This performs an **editable install** — the package is linked into your
environment rather than copied, so changes to the source take effect
immediately without reinstalling.

What this does:
- Registers `coherence_engine` as an importable package.
- Installs the `coherence-engine` CLI command into your environment.
- Installs the `coherence-engine-gui` GUI launcher command.
- Includes bundled data files (`data/*.json`).

At this point the engine is fully functional with the **Minimal** tier (zero
ML dependencies — heuristic contradiction detection + TF-IDF embeddings).

---

## 6. Choosing a Dependency Tier

The engine supports three tiers. Choose based on your quality/resource
trade-off:

### Tier 1: Minimal (default) — ~10 MB

```bash
pip install -e .
```

- Heuristic contradiction detection (pattern matching)
- TF-IDF embeddings (500-dimensional sparse vectors)
- All five layers functional
- Zero external dependencies beyond the standard library
- Fastest startup

### Tier 2: ML — ~500 MB

```bash
pip install -e ".[ml]"
```

Adds:
- `sentence-transformers` — SBERT embeddings (`all-mpnet-base-v2`, 768-dim)
- `numpy`

Layer 3 (embedding) is significantly more accurate with dense SBERT embeddings.
Contradiction detection remains heuristic.

### Tier 3: Full — ~2 GB

```bash
pip install -e ".[full]"
```

Adds everything in ML, plus:
- `transformers` — Hugging Face transformers library
- `torch` — PyTorch
- `fastapi` + `uvicorn` — HTTP API server

Layer 1 (contradiction) uses the `cross-encoder/nli-deberta-v3-large` NLI model
for research-grade contradiction detection. Layer 3 uses SBERT. The HTTP API
server becomes available.

**First run note:** When you first use the Full tier, the NLI model
(~1.5 GB) and SBERT model (~90 MB) will be downloaded from Hugging Face and
cached locally at `~/.cache/huggingface/`. This one-time download takes a few
minutes depending on your connection. Subsequent runs load from cache instantly.

### Development dependencies

To run the test suite, also install the dev extras:

```bash
pip install -e ".[dev]"
```

This adds `pytest` and `pytest-cov`.

You can combine extras:

```bash
pip install -e ".[full,dev]"
```

---

## 7. Verifying the Installation

### Check the version and dependency status

```bash
coherence-engine version
```

This prints the engine version and the status of every optional dependency:

```
Coherence Engine v2.0.0

  sentence-transformers     v3.x.x               — SBERT embeddings (Layer 3)
  transformers              v4.x.x               — NLI contradiction detection (Layer 1)
  torch                     v2.x.x               — GPU acceleration
  networkx                  not installed (using fallback) — Graph analysis (optional)
  numpy                     v1.x.x               — Numeric computation
  fastapi                   v0.x.x               — HTTP API server
```

Any dependency showing "not installed (using fallback)" means the engine will
use its built-in fallback for that layer.

### Check which layers are active

```bash
coherence-engine layers
```

Shows each layer, its weight, and which backend is active:

```
Available analysis layers:

  1. Contradiction          weight=0.30  DeBERTa-v3-large NLI
  2. Argumentation          weight=0.20  Dung's framework (grounded extension)
  3. Embedding              weight=0.20  SBERT (all-mpnet-base-v2, 768-dim)
  4. Compression            weight=0.15  zlib (Kolmogorov proxy)
  5. Structural             weight=0.15  Graph connectivity analysis
```

If transformers is not installed, Layer 1 will show "Heuristic pattern matching".
If sentence-transformers is not installed, Layer 3 will show "TF-IDF fallback".

### Quick smoke test

```bash
coherence-engine analyze "The economy is growing. Employment is rising. Therefore we conclude that fiscal policy is working."
```

You should see an ASCII report with a composite score between 0 and 1, a
per-layer breakdown with bar charts, proposition/claim counts, and an
interpretation.

---

## 8. Running the Test Suite

Install dev dependencies if you haven't:

```bash
pip install -e ".[dev]"
```

Run the full suite:

```bash
python3 -m pytest tests/ -v
```

Expected output: **152 tests passed**. The suite takes ~5 minutes with the Full
tier (NLI model inference is the bottleneck) or ~15 seconds with the Minimal
tier.

To run a specific test file:

```bash
python3 -m pytest tests/test_parser.py -v
python3 -m pytest tests/test_explanation.py -v
```

To run with coverage:

```bash
python3 -m pytest tests/ --cov=coherence_engine --cov-report=term-missing
```

### What the test fixtures contain

| File | Purpose |
|------|---------|
| `tests/fixtures/coherent_essay.txt` | A well-structured essay; expected composite score 0.40 – 1.00 |
| `tests/fixtures/contradictory_pitch.txt` | A deliberately contradictory pitch; expected ≥2 contradictions |
| `tests/fixtures/expected_results.json` | Numeric bounds for fixture files |

---

## 9. Using the Engine as a Python Library

This is the most flexible way to use the engine. From any Python script or
REPL:

### Minimal example

```python
from coherence_engine import CoherenceScorer

scorer = CoherenceScorer()
result = scorer.score("Your text goes here. It should have multiple sentences.")

print(result.composite_score)   # e.g. 0.72
print(result.report())          # Full ASCII report
```

### With custom configuration

```python
from coherence_engine import CoherenceScorer, EngineConfig

config = EngineConfig(
    embedder="tfidf",            # Force TF-IDF even if SBERT is available
    contradiction_backend="heuristic",  # Force heuristic contradiction detection
    verbose=True,                # Print layer-by-layer progress to stderr
    weight_contradiction=0.40,   # Boost contradiction weight
    weight_argumentation=0.15,
    weight_embedding=0.15,
    weight_compression=0.15,
    weight_structural=0.15,
)
scorer = CoherenceScorer(config)
result = scorer.score(open("essay.txt").read())
```

### Accessing detailed results

```python
# Per-layer scores
for layer in result.layer_results:
    print(f"{layer.name}: {layer.score:.3f} (weight {layer.weight})")
    print(f"  Details: {layer.details}")
    if layer.warnings:
        print(f"  Warnings: {layer.warnings}")

# Contradictions
for c in result.contradictions:
    print(f"  '{c.prop_a_text}' vs '{c.prop_b_text}' ({c.confidence:.0%})")

# Argument structure
print(f"Propositions: {result.argument_structure.n_propositions}")
print(f"Claims: {len(result.argument_structure.claims)}")
for p in result.argument_structure.propositions:
    print(f"  {p.id}: [{p.prop_type}] {p.text[:80]}")

# Metadata
print(f"Time: {result.metadata['elapsed_seconds']}s")
print(f"Embedder: {result.metadata['embedder']}")
print(f"Layer timings: {result.metadata['layer_timings']}")

# Explanations (human-readable diagnosis)
from coherence_engine.core.explanation import ExplanationGenerator
explainer = ExplanationGenerator()
for item in explainer.explain(result):
    print(f"  - {item}")
```

### Output formats

```python
# ASCII text report
print(result.report(fmt="text"))

# JSON (for programmatic consumption)
print(result.report(fmt="json"))

# Markdown (for documentation or rendering)
print(result.report(fmt="markdown"))

# Raw dict
data = result.to_dict()
```

### Scoring a file directly

```python
result = scorer.score_file("path/to/document.txt")
```

### Domain comparison

```python
from coherence_engine.domain.comparator import DomainComparator

result = scorer.score(text)
comparator = DomainComparator()
comparison = comparator.compare(result)  # Auto-detects relevant domains
# or
comparison = comparator.compare(result, domains=["market_economics", "social_contract"])
```

---

## 10. Using the CLI

The CLI is available after installation as `coherence-engine` (or
`python3 -m coherence_engine` if the command isn't on your PATH).

### Analyze text

```bash
# Inline text
coherence-engine analyze "Your text here. It has multiple sentences."

# From a file
coherence-engine analyze essay.txt

# From stdin (piping)
cat essay.txt | coherence-engine analyze

# Paste interactively (Ctrl+D to end)
coherence-engine analyze
```

### Output formats

```bash
coherence-engine analyze essay.txt --format text      # Default: ASCII report
coherence-engine analyze essay.txt --format json      # Machine-readable JSON
coherence-engine analyze essay.txt --format markdown  # Markdown with tables
```

### Custom layer weights

Weights are five comma-separated floats
(contradiction, argumentation, embedding, compression, structural) that must
sum to 1.0:

```bash
coherence-engine analyze essay.txt --weights 0.40,0.15,0.15,0.15,0.15
```

### Verbose mode

```bash
coherence-engine analyze essay.txt --verbose
```

Prints layer-by-layer progress, timing, and scores to stderr during analysis.
The timing information also appears in the output report.

### Domain comparison

```bash
coherence-engine compare essay.txt
coherence-engine compare essay.txt --domain market_economics
coherence-engine compare essay.txt --format json
```

### Other commands

```bash
coherence-engine version    # Version + dependency status
coherence-engine layers     # Active layers and backends
coherence-engine gui        # Launch the desktop GUI
coherence-engine serve      # Start the HTTP API server (requires [full])
coherence-engine serve --port 9000 --host 127.0.0.1
```

---

## 11. Launching the Desktop GUI

The GUI is a native-feeling dark-themed tkinter application.

### Launch it

```bash
coherence-engine gui
# or
coherence-engine-gui
# or
python3 -m coherence_engine gui
```

### Features

- **Text input pane:** Paste or type text, or load a `.txt` file via the
  toolbar button.
- **Analyze button:** Runs the full five-layer pipeline in a background thread
  (the GUI stays responsive).
- **Results tab:** Shows the composite score with an interpretation badge,
  per-layer bar charts, structure statistics (proposition/claim/premise counts),
  a list of detected contradictions with confidence percentages, and
  human-readable explanations.
- **Export:** Copy the report as JSON or text to the clipboard, or save to a
  file.
- **Keyboard shortcuts:** Cmd+Enter (macOS) or Ctrl+Enter to analyze,
  Cmd+O/Ctrl+O to open a file.

### Requirements

The GUI needs tkinter, which is included with Python on macOS and Windows.
On Linux, install it with your package manager (see Prerequisites above).

---

## 12. Running the HTTP API Server

The API server requires the Full dependency tier.

### Start the server

```bash
coherence-engine serve
# Starts on 0.0.0.0:8000

coherence-engine serve --port 9000 --host 127.0.0.1
```

### Endpoints

**`GET /health`** — Health check

```bash
curl http://localhost:8000/health
# {"status": "ok", "version": "2.0.0"}
```

**`GET /layers`** — List available layers

```bash
curl http://localhost:8000/layers
# {"layers": ["contradiction", "argumentation", "embedding", "compression", "structural"]}
```

**`POST /analyze`** — Analyze text

```bash
curl -X POST http://localhost:8000/analyze \
  -H "Content-Type: application/json" \
  -d '{"text": "The economy is growing. Employment is rising. Therefore fiscal policy works."}'
```

Returns a JSON object with `composite_score`, `layers`, `n_propositions`,
`n_contradictions`, `contradictions`, and `metadata`.

### Interactive docs

With the server running, open `http://localhost:8000/docs` in a browser for
the auto-generated Swagger UI (provided by FastAPI).

---

## 13. Installing as a macOS Application

The `installer/` directory contains a script that builds a self-contained
macOS `.app` bundle and installs it to `/Applications`.

### Run the installer

```bash
cd installer
chmod +x install.sh
./install.sh
```

The installer will:

1. **Detect Python** — Finds the best Python 3.9+ on your system.
2. **Create a virtual environment** — Self-contained inside the `.app` bundle.
3. **Install the engine** — Copies the source and installs into the venv.
4. **Ask your dependency tier** — Minimal (10 MB), ML (500 MB), or Full (2 GB).
5. **Build the `.app` bundle** — Creates `CoherenceEngine.app` with:
   - An executable launcher for the GUI.
   - A CLI wrapper.
   - An Info.plist with file type associations (`.txt`).
   - An application icon.
6. **Install to `/Applications`** — Copies the bundle.
7. **Optionally install the CLI** — Symlinks `coherence-engine` to
   `/usr/local/bin` so you can use it from any terminal.
8. **Register with Launch Services** — Makes the app findable in Spotlight.

### After installation

- **Launch from Finder:** Open `/Applications/Coherence Engine`.
- **Launch from Spotlight:** Type "Coherence Engine".
- **Launch from Terminal:** `open -a "Coherence Engine"` or `coherence-engine gui`.
- **CLI (if installed):** `coherence-engine analyze "your text"` from any terminal.

### Uninstall

```bash
cd installer
./install.sh --uninstall
```

Removes `/Applications/CoherenceEngine.app` and the CLI symlink.

---

## 14. Configuration Reference

The `EngineConfig` dataclass controls all engine behavior. Every field has a
default — zero configuration is needed for basic usage.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `weight_contradiction` | float | 0.30 | Layer 1 weight |
| `weight_argumentation` | float | 0.20 | Layer 2 weight |
| `weight_embedding` | float | 0.20 | Layer 3 weight |
| `weight_compression` | float | 0.15 | Layer 4 weight |
| `weight_structural` | float | 0.15 | Layer 5 weight |
| `embedder` | str | `"auto"` | `"auto"`, `"sbert"`, or `"tfidf"` |
| `sbert_model` | str | `"all-mpnet-base-v2"` | Hugging Face model name for SBERT |
| `device` | str | `"auto"` | `"auto"`, `"cpu"`, `"cuda"`, `"mps"` |
| `contradiction_backend` | str | `"auto"` | `"auto"`, `"nli"`, `"heuristic"` |
| `nli_model` | str | `"cross-encoder/nli-deberta-v3-large"` | Hugging Face model name for NLI |
| `enable_domain_comparison` | bool | `False` | Enable domain-relative comparison |
| `output_format` | str | `"text"` | `"text"`, `"json"`, `"markdown"` |
| `verbose` | bool | `False` | Print progress to stderr |
| `max_propositions` | int | 200 | Maximum sentences to analyze |
| `batch_size` | int | 32 | Batch size for embedding inference |

The five weights **must** sum to 1.0 (within 0.01 tolerance).

`"auto"` for `embedder` means: try SBERT first, fall back to TF-IDF.
`"auto"` for `contradiction_backend` means: try NLI first, fall back to heuristic.
`"auto"` for `device` means: try CUDA/MPS, fall back to CPU.

---

## 15. Troubleshooting

### `ModuleNotFoundError: No module named 'coherence_engine'`

You haven't installed the package. Make sure your virtual environment is
activated and run `pip install -e .` from the project root.

### `ModuleNotFoundError: No module named 'tkinter'`

On Linux, tkinter must be installed separately:

```bash
sudo apt install python3-tk    # Debian/Ubuntu
sudo dnf install python3-tkinter  # Fedora
```

On macOS and Windows, reinstall Python from [python.org](https://www.python.org)
(Homebrew Python sometimes omits tkinter).

### `command not found: coherence-engine`

The CLI command is installed into your virtual environment's `bin/` directory.
Either:
- Activate the virtual environment: `source venv/bin/activate`
- Or use the module directly: `python3 -m coherence_engine analyze "text"`

### NLI model downloads are slow or fail

The first time you use the Full tier, ~1.5 GB of model weights are downloaded
from Hugging Face. If this fails:

1. Check your internet connection.
2. Try setting a Hugging Face mirror: `export HF_ENDPOINT=https://hf-mirror.com`
3. Download models manually: `python3 -c "from transformers import AutoModelForSequenceClassification; AutoModelForSequenceClassification.from_pretrained('cross-encoder/nli-deberta-v3-large')"`

Models are cached at `~/.cache/huggingface/hub/`. To clear the cache:
`rm -rf ~/.cache/huggingface/hub/`

### Analysis is slow

- **Full tier:** NLI inference on CPU is the bottleneck (~1-2 minutes for long
  texts). Use `--verbose` to see where time is spent.
- **Force TF-IDF for speed:** `EngineConfig(embedder="tfidf")` or use the
  Minimal tier.
- **Force heuristic contradiction:** `EngineConfig(contradiction_backend="heuristic")`
  skips the large NLI model entirely.
- **Reduce scope:** `EngineConfig(max_propositions=50)` limits the number of
  sentences processed.

### Weights don't sum to 1.0

The engine raises `ValueError` at initialization if weights deviate from 1.0
by more than 0.01. The CLI validates this at parse time and prints a clear
error. Adjust your five weights to sum exactly to 1.0.

### Test failures after editing

Run the full suite after any change:

```bash
python3 -m pytest tests/ -v
```

The suite should report 152 passed. If a test fails, the error message will
indicate which layer or component is broken and what the expected vs. actual
values were.

---

## Fund Backend Quick Start (Addendum)

The repo includes a fund orchestration backend in `server/fund/`.

Run API:

```bash
python -m coherence_engine serve-fund --host 0.0.0.0 --port 8010
```

Run migrations:

```bash
alembic upgrade head
```

Process queued scoring jobs (async path):

```bash
python -m coherence_engine process-scoring-jobs --run-mode loop
```

Dispatch outbox events:

```bash
python -m coherence_engine dispatch-outbox --backend redis --run-mode loop --redis-url redis://localhost:6379/0
```


### macOS installer fails

- Ensure you have write access to `/Applications` (the installer will prompt
  for sudo if needed).
- If the icon doesn't appear, the installer auto-generates one — this requires
  no additional dependencies.
- If the `.app` won't open, right-click it → Open → Open (to bypass
  Gatekeeper, since the app isn't signed).
