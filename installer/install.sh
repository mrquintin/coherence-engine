#!/bin/bash
# ===========================================================================
#  Coherence Engine — macOS Installer
# ===========================================================================
#  This script:
#    1. Creates a self-contained virtual environment
#    2. Installs the Coherence Engine and all dependencies
#    3. Builds a native macOS .app bundle
#    4. Installs it to /Applications
#    5. Creates a CLI command at /usr/local/bin/coherence-engine
#
#  Usage:
#    chmod +x install.sh
#    ./install.sh
#
#  To uninstall:
#    ./install.sh --uninstall
# ===========================================================================

set -e

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

APP_NAME="Coherence Engine"
APP_BUNDLE_NAME="CoherenceEngine.app"
APP_IDENTIFIER="com.coherenceengine.app"
APP_VERSION="2.0.0"

INSTALL_DIR="/Applications"
CLI_LINK="/usr/local/bin/coherence-engine"

# Resolve paths relative to this script
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ICON_FILE="$SCRIPT_DIR/CoherenceEngine.icns"

# The .app internals
APP_PATH="$INSTALL_DIR/$APP_BUNDLE_NAME"
APP_CONTENTS="$APP_PATH/Contents"
APP_MACOS="$APP_CONTENTS/MacOS"
APP_RESOURCES="$APP_CONTENTS/Resources"
APP_VENV="$APP_RESOURCES/venv"
APP_ENGINE="$APP_RESOURCES/coherence_engine"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
DIM='\033[2m'
RESET='\033[0m'

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

info()    { echo -e "${BLUE}▸${RESET} $1"; }
success() { echo -e "${GREEN}✓${RESET} $1"; }
warn()    { echo -e "${YELLOW}!${RESET} $1"; }
fail()    { echo -e "${RED}✗${RESET} $1"; exit 1; }

header() {
    echo ""
    echo -e "${BOLD}═══════════════════════════════════════════════════${RESET}"
    echo -e "${BOLD}  $1${RESET}"
    echo -e "${BOLD}═══════════════════════════════════════════════════${RESET}"
    echo ""
}

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

uninstall() {
    header "Uninstalling Coherence Engine"

    if [ -d "$APP_PATH" ]; then
        info "Removing $APP_PATH..."
        rm -rf "$APP_PATH"
        success "Application removed."
    else
        warn "Application not found at $APP_PATH."
    fi

    if [ -L "$CLI_LINK" ]; then
        info "Removing CLI link at $CLI_LINK..."
        sudo rm -f "$CLI_LINK" 2>/dev/null || rm -f "$CLI_LINK" 2>/dev/null
        success "CLI link removed."
    fi

    success "Uninstall complete."
    exit 0
}

if [ "$1" = "--uninstall" ]; then
    uninstall
fi

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

header "Coherence Engine Installer v${APP_VERSION}"

info "Checking prerequisites..."

# Check Python
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$candidate" &>/dev/null; then
        PYTHON="$(command -v "$candidate")"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    fail "Python 3.9+ is required but not found. Install from https://python.org"
fi

PYTHON_VERSION=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYTHON_MAJOR=$("$PYTHON" -c "import sys; print(sys.version_info.major)")
PYTHON_MINOR=$("$PYTHON" -c "import sys; print(sys.version_info.minor)")

if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 9 ]); then
    fail "Python 3.9+ is required. Found: Python $PYTHON_VERSION"
fi

success "Python $PYTHON_VERSION found at $PYTHON"

# Check that the project source exists
if [ ! -d "$PROJECT_DIR/coherence_engine" ]; then
    fail "Cannot find coherence_engine/ directory at $PROJECT_DIR"
fi

success "Project source found at $PROJECT_DIR"

# Check for existing installation
if [ -d "$APP_PATH" ]; then
    warn "Existing installation found at $APP_PATH"
    echo -ne "   Overwrite? [y/N] "
    read -r REPLY
    if [[ ! "$REPLY" =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi
    rm -rf "$APP_PATH"
fi

# ---------------------------------------------------------------------------
# Step 1: Create .app bundle structure
# ---------------------------------------------------------------------------

header "Building Application Bundle"

info "Creating $APP_BUNDLE_NAME structure..."

mkdir -p "$APP_MACOS"
mkdir -p "$APP_RESOURCES"

success "Bundle directory created."

# ---------------------------------------------------------------------------
# Step 2: Create virtual environment
# ---------------------------------------------------------------------------

info "Creating virtual environment (this may take a moment)..."

"$PYTHON" -m venv "$APP_VENV"
VENV_PYTHON="$APP_VENV/bin/python3"
VENV_PIP="$APP_VENV/bin/pip"

# Upgrade pip silently
"$VENV_PIP" install --upgrade pip --quiet 2>&1 | tail -1

success "Virtual environment created."

# ---------------------------------------------------------------------------
# Step 3: Install the engine and dependencies
# ---------------------------------------------------------------------------

info "Installing Coherence Engine into the virtual environment..."

# Copy the engine source into Resources
cp -R "$PROJECT_DIR/coherence_engine" "$APP_ENGINE"

# Install into the venv
"$VENV_PIP" install --quiet "$PROJECT_DIR" 2>&1 | tail -3

success "Core engine installed."

# Ask about optional ML dependencies
echo ""
echo -e "${BOLD}  Optional ML Dependencies${RESET}"
echo -e "${DIM}  These improve analysis quality but are large downloads (~2 GB).${RESET}"
echo -e "${DIM}  The engine works without them using heuristic fallbacks.${RESET}"
echo ""
echo "  [1] Minimal install (heuristic-only, fast, ~10 MB)"
echo "  [2] ML install (+ SBERT embeddings, ~500 MB)"
echo "  [3] Full install (+ NLI model + API server, ~2 GB)"
echo ""
echo -ne "  Choose [1/2/3, default=1]: "
read -r ML_CHOICE

case "$ML_CHOICE" in
    2)
        info "Installing ML dependencies (sentence-transformers)..."
        "$VENV_PIP" install --quiet "sentence-transformers>=2.2.0" "numpy>=1.21.0" 2>&1 | tail -3
        success "ML dependencies installed."
        ;;
    3)
        info "Installing full dependencies (this will take a few minutes)..."
        "$VENV_PIP" install --quiet \
            "sentence-transformers>=2.2.0" \
            "transformers>=4.30.0" \
            "torch>=2.0.0" \
            "numpy>=1.21.0" \
            "fastapi>=0.100.0" \
            "uvicorn>=0.23.0" 2>&1 | tail -3
        success "Full dependencies installed."
        ;;
    *)
        success "Minimal install selected."
        ;;
esac

# ---------------------------------------------------------------------------
# Step 4: Create the launcher executable
# ---------------------------------------------------------------------------

info "Creating application launcher..."

cat > "$APP_MACOS/CoherenceEngine" << 'LAUNCHER_SCRIPT'
#!/bin/bash
# Coherence Engine — macOS Application Launcher

# Resolve the Resources directory
DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"
VENV_PYTHON="$DIR/venv/bin/python3"

# Set up environment
export PYTHONDONTWRITEBYTECODE=1

# Add the parent of coherence_engine to Python path so imports resolve
export PYTHONPATH="$DIR:$PYTHONPATH"

# Launch the GUI
exec "$VENV_PYTHON" -c "
import sys
sys.path.insert(0, '$DIR')
from coherence_engine.gui import main
main()
" "$@" 2>/dev/null
LAUNCHER_SCRIPT

chmod +x "$APP_MACOS/CoherenceEngine"

success "Launcher created."

# ---------------------------------------------------------------------------
# Step 5: Create CLI wrapper
# ---------------------------------------------------------------------------

info "Creating CLI command..."

cat > "$APP_MACOS/coherence-engine-cli" << CLI_SCRIPT
#!/bin/bash
# Coherence Engine CLI wrapper
DIR="\$(cd "\$(dirname "\$0")/../Resources" && pwd)"
exec "\$DIR/venv/bin/python3" -m coherence_engine "\$@"
CLI_SCRIPT

chmod +x "$APP_MACOS/coherence-engine-cli"

success "CLI wrapper created."

# ---------------------------------------------------------------------------
# Step 6: Create Info.plist
# ---------------------------------------------------------------------------

info "Writing application metadata..."

cat > "$APP_CONTENTS/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>${APP_NAME}</string>
    <key>CFBundleDisplayName</key>
    <string>${APP_NAME}</string>
    <key>CFBundleIdentifier</key>
    <string>${APP_IDENTIFIER}</string>
    <key>CFBundleVersion</key>
    <string>${APP_VERSION}</string>
    <key>CFBundleShortVersionString</key>
    <string>${APP_VERSION}</string>
    <key>CFBundleExecutable</key>
    <string>CoherenceEngine</string>
    <key>CFBundleIconFile</key>
    <string>CoherenceEngine</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleSignature</key>
    <string>????</string>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSSupportsAutomaticGraphicsSwitching</key>
    <true/>
    <key>CFBundleDocumentTypes</key>
    <array>
        <dict>
            <key>CFBundleTypeName</key>
            <string>Text Document</string>
            <key>CFBundleTypeExtensions</key>
            <array>
                <string>txt</string>
            </array>
            <key>CFBundleTypeRole</key>
            <string>Viewer</string>
        </dict>
    </array>
</dict>
</plist>
PLIST

success "Info.plist written."

# ---------------------------------------------------------------------------
# Step 7: Install the icon
# ---------------------------------------------------------------------------

if [ -f "$ICON_FILE" ]; then
    cp "$ICON_FILE" "$APP_RESOURCES/CoherenceEngine.icns"
    success "Application icon installed."
else
    # Generate the icon if the pre-built one is missing
    if [ -f "$SCRIPT_DIR/generate_icon.py" ]; then
        info "Generating application icon..."
        "$VENV_PYTHON" "$SCRIPT_DIR/generate_icon.py" 2>/dev/null
        if [ -f "$SCRIPT_DIR/CoherenceEngine.icns" ]; then
            cp "$SCRIPT_DIR/CoherenceEngine.icns" "$APP_RESOURCES/CoherenceEngine.icns"
            success "Application icon generated and installed."
        else
            warn "Could not generate icon. App will use default icon."
        fi
    else
        warn "No icon file found. App will use default icon."
    fi
fi

# ---------------------------------------------------------------------------
# Step 8: Install CLI symlink
# ---------------------------------------------------------------------------

echo ""
echo -e "${BOLD}  CLI Access${RESET}"
echo -e "${DIM}  Install 'coherence-engine' command to /usr/local/bin?${RESET}"
echo -e "${DIM}  This lets you run: coherence-engine analyze \"your text\"${RESET}"
echo ""
echo -ne "  Install CLI command? [Y/n]: "
read -r CLI_REPLY

if [[ ! "$CLI_REPLY" =~ ^[Nn]$ ]]; then
    # Ensure /usr/local/bin exists
    if [ ! -d "/usr/local/bin" ]; then
        sudo mkdir -p /usr/local/bin 2>/dev/null || true
    fi

    # Create symlink (may need sudo)
    if ln -sf "$APP_MACOS/coherence-engine-cli" "$CLI_LINK" 2>/dev/null; then
        success "CLI command installed at $CLI_LINK"
    elif sudo ln -sf "$APP_MACOS/coherence-engine-cli" "$CLI_LINK" 2>/dev/null; then
        success "CLI command installed at $CLI_LINK (with sudo)"
    else
        warn "Could not install CLI command. You can run it directly from:"
        echo "    $APP_MACOS/coherence-engine-cli"
    fi
fi

# ---------------------------------------------------------------------------
# Step 9: Register with Launch Services
# ---------------------------------------------------------------------------

info "Registering application with macOS..."

# Touch the app to update modification time (triggers Finder icon refresh)
touch "$APP_PATH"

# Register with Launch Services
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP_PATH" 2>/dev/null || true

success "Application registered."

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

header "Installation Complete"

echo -e "  ${GREEN}${BOLD}$APP_NAME has been installed to:${RESET}"
echo -e "  ${BOLD}$APP_PATH${RESET}"
echo ""
echo -e "  ${BOLD}To launch:${RESET}"
echo -e "    • Open ${BOLD}$APP_NAME${RESET} from your Applications folder"
echo -e "    • Or double-click ${BOLD}$APP_BUNDLE_NAME${RESET} in Finder"
echo -e "    • Or run: ${DIM}open -a \"$APP_NAME\"${RESET}"
if [[ ! "$CLI_REPLY" =~ ^[Nn]$ ]]; then
echo ""
echo -e "  ${BOLD}CLI usage:${RESET}"
echo -e "    ${DIM}coherence-engine analyze \"Your text here\"${RESET}"
echo -e "    ${DIM}coherence-engine analyze essay.txt --format json${RESET}"
echo -e "    ${DIM}coherence-engine version${RESET}"
fi
echo ""
echo -e "  ${BOLD}To uninstall:${RESET}"
echo -e "    ${DIM}$SCRIPT_DIR/install.sh --uninstall${RESET}"
echo ""

# Offer to launch now
echo -ne "  Launch Coherence Engine now? [Y/n]: "
read -r LAUNCH_REPLY

if [[ ! "$LAUNCH_REPLY" =~ ^[Nn]$ ]]; then
    open "$APP_PATH"
    success "Launching..."
fi

echo ""
