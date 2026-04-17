#!/usr/bin/env bash
# build-installers.command
# ----------------------------------------------------------------------------
# Produces redistributable installer artifacts into ``build/installers/`` for
# whichever macOS app stack this repo declares. Double-clickable in Finder
# (thus the ``.command`` extension) and invoked by scripts/sync-to-github.sh.
#
# Exit 0 only when at least one installer asset was emitted. On any failure
# the sync script refuses to push a release with no binaries.
# ----------------------------------------------------------------------------

set -euo pipefail

# ----------------------------------------------------------------------------
# Locate repo root (the script lives at <repo>/scripts/).
# ----------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

# Pull Homebrew + pipx + user-site bins into PATH the same way sync-to-github.sh does.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
if [ -f "$HOME/.cargo/env" ]; then
  # shellcheck disable=SC1091
  . "$HOME/.cargo/env"
fi

# ----------------------------------------------------------------------------
# App identity. The filenames here are the public contract the README links
# against; do not rename without updating README regen in the sync script.
# ----------------------------------------------------------------------------
APP_NAME="${APP_NAME:-CoherenceEngine}"
APP_IDENT="${APP_IDENT:-com.mrquintin.coherence-engine}"
APP_VERSION="${NEW_VERSION:-$(cat "$ROOT/VERSION" 2>/dev/null || echo 0.1.0)}"
APP_STACK="${APP_STACK:-}"

INSTALLERS_DIR="$ROOT/build/installers"
WORK_DIR="$ROOT/build/.installer-work"

rm -rf "$WORK_DIR"
mkdir -p "$INSTALLERS_DIR" "$WORK_DIR"

# ----------------------------------------------------------------------------
# If the parent didn't pre-detect the stack (e.g. this script was run directly
# from Finder), replicate the same FIRST-MATCH probe sync-to-github.sh uses.
# ----------------------------------------------------------------------------
if [ -z "$APP_STACK" ]; then
  if [ -f "src-tauri/tauri.conf.json" ]; then
    APP_STACK=tauri
  elif [ -f "package.json" ] && command -v jq >/dev/null 2>&1 && \
       jq -e '.build // (.main and (.dependencies.electron // .devDependencies.electron))' \
          package.json >/dev/null 2>&1; then
    APP_STACK=electron
  elif compgen -G "*.xcodeproj" >/dev/null 2>&1 || [ -f "Package.swift" ]; then
    APP_STACK=xcode
  elif [ -f "pubspec.yaml" ] && grep -q '^flutter:' pubspec.yaml; then
    APP_STACK=flutter
  elif [ -f "pyproject.toml" ] && \
       grep -qE '^\[project\.scripts\]|^\[tool\.briefcase\]' pyproject.toml; then
    APP_STACK=python
  elif [ -f "package.json" ] && command -v jq >/dev/null 2>&1 && \
       jq -e '.bin' package.json >/dev/null 2>&1; then
    APP_STACK=node-cli
  elif [ -f "Makefile" ] && grep -qE '^installer:' Makefile; then
    APP_STACK=makefile
  else
    echo "ERROR: Stack not detected — see scripts/build-installers.command and implement your build step." >&2
    exit 2
  fi
fi

echo "=========================================================="
echo " build-installers"
echo "  stack   = $APP_STACK"
echo "  app     = $APP_NAME ($APP_IDENT)"
echo "  version = $APP_VERSION"
echo "  output  = $INSTALLERS_DIR"
echo "=========================================================="

# ----------------------------------------------------------------------------
# Uninstaller. Every stack emits one with identical semantics so the README
# link shape is stable regardless of how the installer is built.
# ----------------------------------------------------------------------------
emit_uninstaller() {
  local target="$INSTALLERS_DIR/Uninstall-${APP_NAME}.command"
  cat > "$target" <<UNINST
#!/usr/bin/env bash
# Uninstall-${APP_NAME}.command
# Double-click in Finder to remove the installed app.
set -e
APP_DIR="/Applications/${APP_NAME}.app"
BIN_LINK="/usr/local/bin/\$(echo "${APP_NAME}" | tr '[:upper:]' '[:lower:]')"
echo "This will remove:"
echo "  \$APP_DIR"
echo "  \$BIN_LINK (if present)"
printf "Continue? (y/N) "
read -r ans
case "\$ans" in
  y|Y|yes|YES)
    if [ -d "\$APP_DIR" ]; then
      sudo rm -rf "\$APP_DIR" && echo "Removed \$APP_DIR"
    else
      echo "\$APP_DIR not found (already removed?)"
    fi
    if [ -L "\$BIN_LINK" ] || [ -f "\$BIN_LINK" ]; then
      sudo rm -f "\$BIN_LINK" && echo "Removed \$BIN_LINK"
    fi
    ;;
  *) echo "Aborted." ;;
esac
UNINST
  chmod +x "$target"
  echo "  wrote $target"
}

# ----------------------------------------------------------------------------
# Helpers shared by every native-ish path.
# ----------------------------------------------------------------------------
require() {
  local tool="$1"; shift
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "ERROR: required tool '$tool' not found on PATH." >&2
    if [ "$#" -gt 0 ]; then
      echo "Hint: $*" >&2
    fi
    return 1
  fi
}

wrap_as_pkg() {
  # Usage: wrap_as_pkg <source-dir-or-app-bundle> <install-location> <output.pkg>
  # <install-location> is the absolute path where <source> should land on the
  # target machine (e.g. /Applications/CoherenceEngine.app).
  local src="$1" dst="$2" out="$3"
  require pkgbuild "pkgbuild ships with macOS; re-run after installing Xcode Command Line Tools" || return 1
  require productbuild "productbuild ships with Xcode Command Line Tools" || return 1

  local component_pkg="$WORK_DIR/$(basename "$out" .pkg)-component.pkg"
  local distribution_xml="$WORK_DIR/distribution.xml"

  pkgbuild \
    --root "$src" \
    --install-location "$dst" \
    --identifier "$APP_IDENT" \
    --version "$APP_VERSION" \
    "$component_pkg"

  cat > "$distribution_xml" <<XML
<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="1">
  <title>${APP_NAME}</title>
  <organization>${APP_IDENT%.*}</organization>
  <options customize="never" require-scripts="false" hostArchitectures="arm64,x86_64" />
  <choices-outline>
    <line choice="default">
      <line choice="${APP_IDENT}" />
    </line>
  </choices-outline>
  <choice id="default" />
  <choice id="${APP_IDENT}" visible="false">
    <pkg-ref id="${APP_IDENT}" />
  </choice>
  <pkg-ref id="${APP_IDENT}" version="${APP_VERSION}" onConclusion="none">$(basename "$component_pkg")</pkg-ref>
</installer-gui-script>
XML

  productbuild \
    --distribution "$distribution_xml" \
    --package-path "$WORK_DIR" \
    "$out"
  echo "  wrote $out"
}

wrap_as_dmg() {
  # Usage: wrap_as_dmg <source-dir-or-app-bundle> <output.dmg>
  local src="$1" out="$2"
  require hdiutil "hdiutil ships with macOS" || return 1

  local staging="$WORK_DIR/dmg-staging"
  rm -rf "$staging"
  mkdir -p "$staging"
  cp -R "$src" "$staging/"
  # Friendly /Applications symlink so drag-to-install just works.
  ln -s /Applications "$staging/Applications"

  hdiutil create \
    -volname "${APP_NAME}" \
    -srcfolder "$staging" \
    -ov -format UDZO \
    "$out"
  echo "  wrote $out"
}

# ----------------------------------------------------------------------------
# Per-stack build logic. Unimplemented stacks fail loudly so the sync script
# refuses to push — no silent "empty release" path is possible.
# ----------------------------------------------------------------------------
build_python() {
  if ! command -v pyinstaller >/dev/null 2>&1; then
    cat >&2 <<'MSG'
ERROR: 'pyinstaller' not found on PATH.

This repo's detected stack is Python (pyproject.toml has [project.scripts]).
Install PyInstaller once, then rerun:

    python3 -m pip install --user pyinstaller

or if you prefer pipx:

    pipx install pyinstaller

After install, confirm with: command -v pyinstaller
MSG
    return 1
  fi

  # Generate a tiny launcher module. This is required because the repo's own
  # package (coherence_engine) resolves imports via ``pythonpath = [".."]`` in
  # pyproject.toml, which PyInstaller's analyzer cannot follow by itself.
  mkdir -p "$WORK_DIR/launcher"
  cat > "$WORK_DIR/launcher/coherence_engine_launcher.py" <<'PY'
"""PyInstaller entrypoint for CoherenceEngine."""
from __future__ import annotations
import sys


def _boot() -> None:
    # Frozen path: coherence_engine is bundled via --collect-all.
    try:
        from coherence_engine.cli import main  # type: ignore
    except ImportError:  # unfrozen / dev run fallback
        import pathlib
        here = pathlib.Path(__file__).resolve().parent
        sys.path.insert(0, str(here.parent.parent.parent))
        from coherence_engine.cli import main  # type: ignore
    raise SystemExit(main())


if __name__ == "__main__":
    _boot()
PY

  # The ``coherence_engine`` package sits at the repo root and is imported
  # relative to the repo *parent* (see pyproject.toml's pythonpath). Run
  # PyInstaller from that parent so ``--collect-all coherence_engine``
  # resolves it as a top-level package.
  local repo_parent
  repo_parent="$(cd "$ROOT/.." && pwd)"

  (
    cd "$repo_parent"
    pyinstaller \
      --noconfirm \
      --clean \
      --name "${APP_NAME}" \
      --windowed \
      --collect-all coherence_engine \
      --distpath "$WORK_DIR/dist" \
      --workpath "$WORK_DIR/work" \
      --specpath "$WORK_DIR" \
      "$WORK_DIR/launcher/coherence_engine_launcher.py"
  )

  local app_bundle="$WORK_DIR/dist/${APP_NAME}.app"
  local onedir="$WORK_DIR/dist/${APP_NAME}"
  local payload=""
  if [ -d "$app_bundle" ]; then
    payload="$app_bundle"
  elif [ -d "$onedir" ]; then
    payload="$onedir"
  else
    echo "ERROR: pyinstaller produced neither .app nor onedir output under $WORK_DIR/dist." >&2
    return 1
  fi

  wrap_as_pkg "$payload" "/Applications/${APP_NAME}.app" \
    "$INSTALLERS_DIR/${APP_NAME}-Installer.pkg"

  # DMG is best-effort: if hdiutil fails (e.g. sandbox denial) we still have
  # a .pkg, so the release is not empty.
  if wrap_as_dmg "$payload" "$INSTALLERS_DIR/${APP_NAME}-Installer.dmg"; then
    :
  else
    echo "WARNING: .dmg creation failed; continuing with .pkg only." >&2
    rm -f "$INSTALLERS_DIR/${APP_NAME}-Installer.dmg"
  fi
}

build_tauri() {
  require npm "install Node.js (https://nodejs.org) or use nvm" || return 1
  require cargo "install Rust (https://rustup.rs)" || return 1
  npm install --no-audit --no-fund
  npm run tauri -- build
  local bundle_dir="src-tauri/target/release/bundle"
  [ -d "$bundle_dir" ] || { echo "ERROR: $bundle_dir not found after tauri build." >&2; return 1; }
  local copied=0
  while IFS= read -r -d '' f; do
    case "$f" in
      *.dmg) cp "$f" "$INSTALLERS_DIR/${APP_NAME}-Installer.dmg"; copied=1 ;;
      *.pkg) cp "$f" "$INSTALLERS_DIR/${APP_NAME}-Installer.pkg"; copied=1 ;;
      *.sig) cp "$f" "$INSTALLERS_DIR/"; copied=1 ;;
    esac
  done < <(find "$bundle_dir" -type f \( -name '*.dmg' -o -name '*.pkg' -o -name '*.sig' \) -print0)
  [ "$copied" -eq 1 ] || { echo "ERROR: tauri build emitted no .dmg/.pkg." >&2; return 1; }
}

build_electron() {
  require npm "install Node.js (https://nodejs.org) or use nvm" || return 1
  npm install --no-audit --no-fund
  # electron-builder emits to ./dist/ by default.
  if npm run | grep -qE '^  dist'; then
    npm run dist
  else
    npx --yes electron-builder --mac
  fi
  local copied=0
  for ext in dmg pkg; do
    for f in dist/*."$ext"; do
      [ -e "$f" ] || continue
      cp "$f" "$INSTALLERS_DIR/${APP_NAME}-Installer.${ext}"
      copied=1
    done
  done
  [ "$copied" -eq 1 ] || { echo "ERROR: electron-builder emitted no .dmg/.pkg under dist/." >&2; return 1; }
}

build_xcode() {
  require xcodebuild "install Xcode" || return 1
  # Prefer a VERSION-file-driven number via agvtool when the project is wired for it.
  if xcrun agvtool what-marketing-version >/dev/null 2>&1; then
    xcrun agvtool new-marketing-version "$APP_VERSION" >/dev/null
  fi
  local archive="$WORK_DIR/${APP_NAME}.xcarchive"
  xcodebuild -scheme "${APP_NAME}" -configuration Release \
    -archivePath "$archive" archive
  # Export .app, then pkg/dmg it ourselves for a stable filename.
  local export_dir="$WORK_DIR/xc-export"
  mkdir -p "$export_dir"
  cat > "$WORK_DIR/exportOptions.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>method</key><string>developer-id</string>
</dict></plist>
PL
  xcodebuild -exportArchive -archivePath "$archive" \
    -exportPath "$export_dir" -exportOptionsPlist "$WORK_DIR/exportOptions.plist" || {
      echo "WARNING: exportArchive failed (missing Developer ID?); copying the xcarchive contents instead." >&2
      cp -R "$archive/Products/Applications/." "$export_dir/"
    }
  local app_bundle="$export_dir/${APP_NAME}.app"
  [ -d "$app_bundle" ] || { echo "ERROR: expected $app_bundle not found." >&2; return 1; }
  wrap_as_pkg "$app_bundle" "/Applications/${APP_NAME}.app" \
    "$INSTALLERS_DIR/${APP_NAME}-Installer.pkg"
  wrap_as_dmg "$app_bundle" "$INSTALLERS_DIR/${APP_NAME}-Installer.dmg" || true
}

build_flutter() {
  require flutter "install Flutter (https://flutter.dev)" || return 1
  flutter build macos --release
  local app_bundle
  app_bundle="$(find build/macos/Build/Products/Release -maxdepth 2 -name '*.app' -print -quit)"
  [ -n "$app_bundle" ] && [ -d "$app_bundle" ] || {
    echo "ERROR: flutter build macos produced no .app bundle." >&2; return 1
  }
  wrap_as_pkg "$app_bundle" "/Applications/$(basename "$app_bundle")" \
    "$INSTALLERS_DIR/${APP_NAME}-Installer.pkg"
  wrap_as_dmg "$app_bundle" "$INSTALLERS_DIR/${APP_NAME}-Installer.dmg" || true
}

build_node_cli() {
  require npm "install Node.js (https://nodejs.org) or use nvm" || return 1
  npm install --no-audit --no-fund
  # A node CLI ships as a tarball the user npm-installs or unpacks.
  local tarball
  tarball="$(npm pack --silent)"
  mv "$tarball" "$INSTALLERS_DIR/${APP_NAME}-${APP_VERSION}.tar.gz"
  echo "  wrote $INSTALLERS_DIR/${APP_NAME}-${APP_VERSION}.tar.gz"
}

build_makefile() {
  require make "install Xcode Command Line Tools" || return 1
  make installer
  # A Makefile-driven build is expected to put its outputs in ./build/installers
  # already. If it doesn't, nothing in $INSTALLERS_DIR and the sanity check in
  # sync-to-github.sh will abort the release.
  :
}

case "$APP_STACK" in
  tauri)    build_tauri ;;
  electron) build_electron ;;
  xcode)    build_xcode ;;
  flutter)  build_flutter ;;
  python)   build_python ;;
  node-cli) build_node_cli ;;
  makefile) build_makefile ;;
  *)
    echo "ERROR: unknown APP_STACK='$APP_STACK'." >&2
    exit 2
    ;;
esac

emit_uninstaller

echo ""
echo ">>> build-installers: done. Contents of $INSTALLERS_DIR:"
ls -la "$INSTALLERS_DIR"
