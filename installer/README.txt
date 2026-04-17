COHERENCE ENGINE — INSTALLER
=============================

This folder contains everything needed to install the Coherence Engine
as a native macOS application.


QUICK START
-----------

  1. Open Terminal
  2. Navigate to this folder:
       cd /path/to/Coherence_Engine_Project/installer
  3. Run the installer:
       ./install.sh
  4. Follow the prompts

The installer will:
  - Create a self-contained virtual environment
  - Install the Coherence Engine and its dependencies
  - Build a native macOS .app bundle
  - Install it to /Applications
  - Optionally install a CLI command at /usr/local/bin/coherence-engine


WHAT GETS INSTALLED
-------------------

  /Applications/CoherenceEngine.app
    └── Contents/
        ├── Info.plist                  Application metadata
        ├── MacOS/
        │   ├── CoherenceEngine         GUI launcher (executable)
        │   └── coherence-engine-cli    CLI wrapper
        └── Resources/
            ├── CoherenceEngine.icns    Application icon
            ├── coherence_engine/       Engine source code
            └── venv/                   Self-contained Python environment

  /usr/local/bin/coherence-engine       CLI symlink (optional)


INSTALLATION OPTIONS
--------------------

During installation, you'll be asked to choose a dependency tier:

  [1] Minimal (default)
      - Zero ML dependencies (~10 MB)
      - Uses heuristic pattern matching for contradiction detection
      - Uses TF-IDF for embeddings
      - All five analysis layers functional

  [2] ML
      - Adds sentence-transformers (~500 MB)
      - High-quality SBERT embeddings (768 dimensions)
      - Heuristic contradiction detection

  [3] Full
      - Adds transformers, torch, FastAPI (~2 GB)
      - NLI-based contradiction detection (DeBERTa)
      - SBERT embeddings
      - HTTP API server


UNINSTALLING
------------

  ./install.sh --uninstall

This removes:
  - /Applications/CoherenceEngine.app
  - /usr/local/bin/coherence-engine (if installed)


REQUIREMENTS
------------

  - macOS 12.0 or later
  - Python 3.9 or later (installed from python.org or via Homebrew)
  - ~50 MB disk space (minimal) to ~3 GB (full)


FILES IN THIS FOLDER
--------------------

  install.sh            The installer script
  generate_icon.py      Generates the .icns application icon
  CoherenceEngine.icns  Pre-generated application icon
  README.txt            This file
