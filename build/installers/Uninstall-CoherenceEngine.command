#!/usr/bin/env bash
# Uninstall-CoherenceEngine.command
# Double-click in Finder to remove the installed app.
set -e
APP_DIR="/Applications/CoherenceEngine.app"
BIN_LINK="/usr/local/bin/$(echo "CoherenceEngine" | tr '[:upper:]' '[:lower:]')"
echo "This will remove:"
echo "  $APP_DIR"
echo "  $BIN_LINK (if present)"
printf "Continue? (y/N) "
read -r ans
case "$ans" in
  y|Y|yes|YES)
    if [ -d "$APP_DIR" ]; then
      sudo rm -rf "$APP_DIR" && echo "Removed $APP_DIR"
    else
      echo "$APP_DIR not found (already removed?)"
    fi
    if [ -L "$BIN_LINK" ] || [ -f "$BIN_LINK" ]; then
      sudo rm -f "$BIN_LINK" && echo "Removed $BIN_LINK"
    fi
    ;;
  *) echo "Aborted." ;;
esac
