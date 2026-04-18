#!/usr/bin/env bash
# sync-to-github.sh
# ----------------------------------------------------------------------------
# One-button "Sync to GitHub":
#   (a) cd to repo root
#   (b) handle gitbutler/workspace branch if active, otherwise stay put
#   (c) gate: skip cleanly if no source changes (with user confirmation)
#   (d) load PATH for Homebrew + cargo
#   (e) detect app stack (first match wins)
#   (f) bump patch version in VERSION + stack-specific files
#   (g) build installers into build/installers via scripts/build-installers.command
#   (h) stage release artifacts into build/release-artifacts
#   (i) commit and push to current branch
#   (j) create (or replace) a GitHub Release with all artifacts attached
#   (k) regenerate the README Download block (before the final push)
#   (l) verify release assets + print OK/PARTIAL/FAILED banner
#
# Invoked from Cursor via .vscode/tasks.json ("Sync to GitHub"). Can also be
# run directly:
#     scripts/sync-to-github.sh
#
# Override flags:
#     SYNC_FORCE=1       — skip the "no changes, rebuild anyway?" prompt
#                          (always proceed with the full build + release)
#     NO_COLOR=1         — disable ANSI color output
#
# POSIX-bash compatible (no zsh-only syntax). No --force. No history rewrites.
# No secret writes.
# ----------------------------------------------------------------------------

set -euo pipefail

# ----------------------------------------------------------------------------
# Color + banner helpers.
# Color auto-disables when stdout isn't a TTY (logs, CI, redirected output).
# ----------------------------------------------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RESET=$'\033[0m'
  C_RED=$'\033[1;31m'
  C_GREEN=$'\033[1;32m'
  C_YELLOW=$'\033[1;33m'
  C_CYAN=$'\033[1;36m'
  C_DIM=$'\033[2m'
else
  C_RESET=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_CYAN=""; C_DIM=""
fi

log()  { printf "%s>>> %s%s\n" "$C_CYAN"   "$*" "$C_RESET"; }
warn() { printf "%s!!! %s%s\n" "$C_YELLOW" "$*" "$C_RESET" >&2; }
die()  { printf "%sERROR: %s%s\n" "$C_RED" "$*" "$C_RESET" >&2; exit 1; }

# Usage: print_banner <green|yellow|red> <icon> <title> [body lines ...]
# Body lines are printed literally (may contain embedded color codes).
print_banner() {
  local color="$1" icon="$2" title="$3"
  shift 3
  local c=""
  case "$color" in
    green)  c="$C_GREEN"  ;;
    yellow) c="$C_YELLOW" ;;
    red)    c="$C_RED"    ;;
    *)      c=""          ;;
  esac
  local bar="============================================================"
  printf "\n%s%s%s\n"      "$c" "$bar" "$C_RESET"
  printf "%s  %s  %s%s\n"  "$c" "$icon" "$title" "$C_RESET"
  printf "%s%s%s\n"        "$c" "$bar" "$C_RESET"
  local line
  for line in "$@"; do
    printf "  %s\n" "$line"
  done
  printf "%s%s%s\n\n"      "$c" "$bar" "$C_RESET"
}

# ----------------------------------------------------------------------------
# Failure trap: print a red banner if anything under ``set -e`` bails out.
# A success path sets SYNC_SUCCEEDED=1 before exiting so the trap stays quiet.
# CURRENT_STEP is updated at each phase to provide useful diagnostic context.
# ----------------------------------------------------------------------------
SYNC_SUCCEEDED=0
CURRENT_STEP="starting up"

on_exit() {
  local code=$?
  if [ "$SYNC_SUCCEEDED" = "1" ]; then
    return 0
  fi
  print_banner red "[X]" "SYNC FAILED" \
    "Exit code: $code" \
    "Last step: $CURRENT_STEP" \
    "" \
    "Re-run after investigating. Tip: 'git status' will show any commits" \
    "or stashes this run left behind."
}
trap on_exit EXIT

# ----------------------------------------------------------------------------
# Repo identity. Update OWNER/REPO/APP_NAME here if this script is cloned into
# another repo — the installer filenames in README and build-installers.command
# must stay in lockstep.
# ----------------------------------------------------------------------------
OWNER="mrquintin"
REPO="coherence-engine"
APP_NAME="CoherenceEngine"
APP_IDENT="com.mrquintin.coherence-engine"

# ----------------------------------------------------------------------------
# (a) Locate the repo root. Bail out if git isn't initialised yet — the sync
#     task has nothing to push until the user runs `git init` + adds a remote.
# ----------------------------------------------------------------------------
CURRENT_STEP="locating git repo root"
if ! ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  die "not inside a git repository. Run: git init && git remote add origin git@github.com:${OWNER}/${REPO}.git"
fi
cd "$ROOT"

# ----------------------------------------------------------------------------
# Clear stale git locks left behind by crashed GitButler runs, aborted merges,
# or force-killed git operations. Only touches the locks when NO git process
# is actually running; never races a live command.
# ----------------------------------------------------------------------------
CURRENT_STEP="clearing stale git locks"
clean_stale_git_locks() {
  if pgrep -x git >/dev/null 2>&1; then
    return 0
  fi
  local removed=""
  for f in .git/index.lock .git/HEAD.lock .git/config.lock .git/shallow.lock .git/packed-refs.lock; do
    if [ -f "$f" ]; then
      rm -f "$f" && removed="$removed $f"
    fi
  done
  if [ -d .git/refs ]; then
    local ref_locks
    ref_locks="$(find .git/refs -type f -name '*.lock' 2>/dev/null)"
    if [ -n "$ref_locks" ]; then
      find .git/refs -type f -name '*.lock' -delete 2>/dev/null || true
      removed="$removed (.git/refs/*.lock)"
    fi
  fi
  if [ -n "$removed" ]; then
    warn "cleared stale git locks:$removed"
  fi
}
clean_stale_git_locks

# ----------------------------------------------------------------------------
# (d) Load PATH for Homebrew (where gh + jq typically live) and Cargo, if
#     present. Doing this up front makes every later `command -v` check
#     deterministic across interactive-vs-task invocations.
# ----------------------------------------------------------------------------
CURRENT_STEP="preparing PATH and checking hard deps"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
if [ -f "$HOME/.cargo/env" ]; then
  # shellcheck disable=SC1091
  . "$HOME/.cargo/env"
fi

command -v git     >/dev/null 2>&1 || die "git not on PATH."
command -v gh      >/dev/null 2>&1 || die "gh (GitHub CLI) not on PATH — install via: brew install gh"
command -v jq      >/dev/null 2>&1 || die "jq not on PATH — install via: brew install jq"
command -v python3 >/dev/null 2>&1 || die "python3 not on PATH."

# ----------------------------------------------------------------------------
# (b) Branch resolution with detached-HEAD fallback + GitButler escape hatch.
# ----------------------------------------------------------------------------
CURRENT_STEP="resolving current branch"
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
if [ -z "$CURRENT_BRANCH" ] || [ "$CURRENT_BRANCH" = "HEAD" ]; then
  warn "detached HEAD detected — checking out main"
  if git show-ref --verify --quiet refs/heads/main; then
    git checkout main
  elif git show-ref --verify --quiet refs/remotes/origin/main; then
    git checkout -b main origin/main
  else
    git checkout -b main
  fi
  CURRENT_BRANCH="main"
fi
log "current branch: $CURRENT_BRANCH"

if [ "$CURRENT_BRANCH" = "gitbutler/workspace" ]; then
  CURRENT_STEP="migrating gitbutler/workspace to main"
  log "gitbutler/workspace detected — migrating to main"

  # GitButler drops a marker hook; remove it so our commits don't get blocked.
  if [ -f "$ROOT/.git/hooks/pre-commit" ] && \
     grep -qi "gitbutler" "$ROOT/.git/hooks/pre-commit" 2>/dev/null; then
    log "removing gitbutler pre-commit hook marker"
    rm -f "$ROOT/.git/hooks/pre-commit"
  fi

  # Stash-or-commit local work so checkout doesn't lose anything.
  if [ -n "$(git status --porcelain)" ]; then
    log "committing in-progress changes before branch switch"
    git add -A
    git commit -m "wip: sync workspace snapshot" || true
  fi

  git fetch origin --prune

  if git show-ref --verify --quiet refs/heads/main; then
    git checkout main
  elif git show-ref --verify --quiet refs/remotes/origin/main; then
    git checkout -b main origin/main
  else
    # No main yet on either side — create a fresh main rooted at the current tip.
    git checkout -b main
  fi

  if ! git merge --no-edit gitbutler/workspace; then
    die "merge conflict bringing gitbutler/workspace into main — resolve manually and rerun."
  fi

  CURRENT_BRANCH="main"
fi

# ----------------------------------------------------------------------------
# (c) Gate expensive work when the tree is clean. The gate distinguishes three
#     cases so we never silently skip an unpushed commit:
#       1. dirty tree → full pipeline (build + release).
#       2. clean tree, ahead of origin → push the existing commits and exit
#          with a "no new release" banner (no rebuild needed).
#       3. clean tree, in sync with origin → prompt y/N for a forced rebuild.
#                                            N / non-interactive → skip.
#     Submodule drift is ignored so a chronically-modified gitlink doesn't
#     force a rebuild every run.
# ----------------------------------------------------------------------------
CURRENT_STEP="checking for source changes"

has_source_changes() {
  if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
    return 0  # fresh repo, always treat as needing a build
  fi
  if ! git diff --quiet --ignore-submodules=all HEAD \
        -- . ':!build/installers' ':!build/release-artifacts' 2>/dev/null; then
    return 0
  fi
  local untracked
  untracked="$(git ls-files --others --exclude-standard \
                -- . ':!build/installers' ':!build/release-artifacts')"
  [ -n "$untracked" ]
}

count_unpushed_commits() {
  if ! git rev-parse --abbrev-ref --symbolic-full-name "@{u}" >/dev/null 2>&1; then
    # No upstream yet → the branch is implicitly "ahead" until we push it.
    if git rev-parse --verify HEAD >/dev/null 2>&1; then
      echo 1
    else
      echo 0
    fi
    return
  fi
  git rev-list --count "@{u}..HEAD" 2>/dev/null || echo 0
}

prompt_rebuild() {
  if [ -n "${SYNC_FORCE:-}" ] && [ "${SYNC_FORCE}" != "0" ]; then
    log "SYNC_FORCE=1 set — proceeding without prompt"
    return 0
  fi
  if [ ! -t 0 ]; then
    warn "non-interactive stdin and SYNC_FORCE unset — skipping rebuild"
    return 1
  fi
  printf "No source changes detected. Rebuild installers and push anyway? (y/N) "
  local ans
  read -r ans || ans=""
  case "$ans" in
    y|Y|yes|YES) return 0 ;;
    *)           return 1 ;;
  esac
}

# ----------------------------------------------------------------------------
# Pre-push size guard. GitHub's pre-receive hook rejects any single file that
# exceeds 100 MB, terminating the whole push (see error GH001). Scanning HEAD's
# tree up front lets us fail in seconds with a clear message, instead of
# discovering the limit after uploading a multi-hundred-MB pack. Applies equally
# to stuck-from-a-prior-run commits (Case 2) and freshly-built commits (Case 3).
# ----------------------------------------------------------------------------
check_head_for_oversized_files() {
  python3 - "$ROOT" <<'PY' || return 1
import pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1])
MAX = 100 * 1024 * 1024  # GitHub's per-file push limit.
try:
    out = subprocess.check_output(
        ["git", "ls-tree", "-r", "-l", "HEAD"], cwd=root, text=True
    )
except subprocess.CalledProcessError:
    # No HEAD yet (fresh repo). Nothing tracked → nothing to reject.
    sys.exit(0)
bad = []
for line in out.splitlines():
    # Format: "<mode> SP <type> SP <hash> SP <size>\t<path>"
    meta, _, path = line.partition("\t")
    parts = meta.split()
    if len(parts) < 4 or not path:
        continue
    try:
        size = int(parts[3])
    except ValueError:
        continue
    if size > MAX:
        bad.append((size, path))
if not bad:
    sys.exit(0)
bad.sort(reverse=True)
red   = "\033[1;31m" if sys.stderr.isatty() else ""
reset = "\033[0m"    if sys.stderr.isatty() else ""
print(f"\n{red}ERROR: {len(bad)} file(s) tracked in HEAD exceed GitHub's "
      f"100 MB per-file push limit:{reset}", file=sys.stderr)
for size, path in bad:
    print(f"  {size // 1024 // 1024:>4} MB  {path}", file=sys.stderr)
print("\nThese are almost certainly build artifacts. To unblock the push:", file=sys.stderr)
print("  1. Ensure the path(s) are covered by .gitignore", file=sys.stderr)
print("  2. 'git rm --cached <path>' to untrack them", file=sys.stderr)
print("  3. 'git commit --amend --no-edit' to rewrite the offending commit", file=sys.stderr)
print("  4. Rerun sync. Large binaries ship via GitHub Releases, not in commits.",
      file=sys.stderr)
sys.exit(1)
PY
}

UNPUSHED_COUNT="$(count_unpushed_commits)"

if ! has_source_changes; then
  if [ "$UNPUSHED_COUNT" -gt 0 ]; then
    # Case 2: clean tree, but local branch is ahead. Push existing commits
    # without building new installers or cutting a new release tag.
    log "clean tree with $UNPUSHED_COUNT unpushed commit(s) on $CURRENT_BRANCH — pushing without rebuild"
    CURRENT_STEP="checking HEAD for oversized files"
    check_head_for_oversized_files || exit 1
    CURRENT_STEP="pushing existing commits"
    if git rev-parse --abbrev-ref --symbolic-full-name "@{u}" >/dev/null 2>&1; then
      git push origin "$CURRENT_BRANCH"
    else
      git push -u origin "$CURRENT_BRANCH"
    fi
    SYNC_SUCCEEDED=1
    print_banner green "[OK]" "CODE PUSHED (no new release)" \
      "Branch:          $CURRENT_BRANCH" \
      "Commits pushed:  $UNPUSHED_COUNT" \
      "" \
      "No source changes, so no new installer build or release tag." \
      "To force a full rebuild + release, rerun with SYNC_FORCE=1 or" \
      "answer 'y' to the prompt on a clean tree."
    exit 0
  fi
  # Case 3: clean tree, in sync with origin. Ask.
  if ! prompt_rebuild; then
    SYNC_SUCCEEDED=1
    print_banner yellow "[-]" "SYNC SKIPPED" \
      "Branch:  $CURRENT_BRANCH" \
      "" \
      "No source changes and no unpushed commits on $CURRENT_BRANCH." \
      "Set SYNC_FORCE=1 or answer 'y' to force a rebuild + release."
    exit 0
  fi
fi

# ----------------------------------------------------------------------------
# (e) Stack detection. FIRST match wins. Mirrored in build-installers.command.
# ----------------------------------------------------------------------------
CURRENT_STEP="detecting app stack"
APP_STACK=""
if [ -f "src-tauri/tauri.conf.json" ]; then
  APP_STACK="tauri"
elif [ -f "package.json" ] && \
     jq -e '.build // (.main and (.dependencies.electron // .devDependencies.electron))' \
        package.json >/dev/null 2>&1; then
  APP_STACK="electron"
elif compgen -G "*.xcodeproj" >/dev/null 2>&1 || [ -f "Package.swift" ]; then
  APP_STACK="xcode"
elif [ -f "pubspec.yaml" ] && grep -q '^flutter:' pubspec.yaml; then
  APP_STACK="flutter"
elif [ -f "pyproject.toml" ] && \
     grep -qE '^\[project\.scripts\]|^\[tool\.briefcase\]' pyproject.toml; then
  APP_STACK="python"
elif [ -f "package.json" ] && jq -e '.bin' package.json >/dev/null 2>&1; then
  APP_STACK="node-cli"
elif [ -f "Makefile" ] && grep -qE '^installer:' Makefile; then
  APP_STACK="makefile"
else
  die "Stack not detected — see scripts/build-installers.command and implement your build step"
fi
log "detected stack: $APP_STACK"

# ----------------------------------------------------------------------------
# (f) Version bump. VERSION is the single source of truth for the README
#     generator; stack-specific files are kept in sync so editors and
#     CI tooling see consistent numbers.
# ----------------------------------------------------------------------------
CURRENT_STEP="bumping version"
if [ ! -f "$ROOT/VERSION" ]; then
  log "no VERSION file — seeding at 0.1.0"
  echo "0.1.0" > "$ROOT/VERSION"
fi

CURRENT_VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
if ! printf '%s' "$CURRENT_VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  die "VERSION file does not contain a semver-ish string (got: '$CURRENT_VERSION')"
fi

IFS='.' read -r VMAJ VMIN VPAT <<EOF
$CURRENT_VERSION
EOF
VPAT=$((VPAT + 1))
NEW_VERSION="${VMAJ}.${VMIN}.${VPAT}"
echo "$NEW_VERSION" > "$ROOT/VERSION"
log "version bump: $CURRENT_VERSION -> $NEW_VERSION"

bump_pyproject() {
  python3 - "$ROOT/pyproject.toml" "$NEW_VERSION" <<'PY'
import pathlib, re, sys
path, ver = sys.argv[1], sys.argv[2]
p = pathlib.Path(path)
if not p.is_file():
    raise SystemExit(0)
text = p.read_text(encoding="utf-8")
new = re.sub(
    r'(?m)^(\s*version\s*=\s*)"[^"]*"',
    lambda m: f'{m.group(1)}"{ver}"',
    text,
    count=1,
)
p.write_text(new, encoding="utf-8")
PY
}

bump_json_version() {
  python3 - "$1" "$NEW_VERSION" <<'PY'
import json, pathlib, sys
path, ver = sys.argv[1], sys.argv[2]
p = pathlib.Path(path)
if not p.is_file():
    raise SystemExit(0)
data = json.loads(p.read_text(encoding="utf-8"))
data["version"] = ver
p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PY
}

bump_cargo_toml() {
  python3 - "$1" "$NEW_VERSION" <<'PY'
import pathlib, re, sys
path, ver = sys.argv[1], sys.argv[2]
p = pathlib.Path(path)
if not p.is_file():
    raise SystemExit(0)
text = p.read_text(encoding="utf-8")
# Only rewrite the [package] version, not dependency pins.
out, in_package = [], False
for line in text.splitlines():
    s = line.strip()
    if s.startswith("[") and s.endswith("]"):
        in_package = (s == "[package]")
    if in_package and re.match(r'^\s*version\s*=', line):
        line = re.sub(r'"[^"]*"', f'"{ver}"', line, count=1)
    out.append(line)
p.write_text("\n".join(out) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
PY
}

bump_pubspec() {
  python3 - "$ROOT/pubspec.yaml" "$NEW_VERSION" <<'PY'
import pathlib, re, sys
path, ver = sys.argv[1], sys.argv[2]
p = pathlib.Path(path)
if not p.is_file():
    raise SystemExit(0)
text = p.read_text(encoding="utf-8")
new = re.sub(
    r'(?m)^(version:\s*)[^\s]+',
    lambda m: f"{m.group(1)}{ver}",
    text,
    count=1,
)
p.write_text(new, encoding="utf-8")
PY
}

case "$APP_STACK" in
  tauri)
    bump_json_version "$ROOT/src-tauri/tauri.conf.json"
    bump_cargo_toml   "$ROOT/src-tauri/Cargo.toml"
    bump_json_version "$ROOT/package.json"
    ;;
  electron|node-cli)
    bump_json_version "$ROOT/package.json"
    ;;
  xcode)
    if command -v agvtool >/dev/null 2>&1 && compgen -G "*.xcodeproj" >/dev/null 2>&1; then
      ( xcrun agvtool new-marketing-version "$NEW_VERSION" >/dev/null 2>&1 || true )
    fi
    # VERSION file already holds the source of truth; nothing else to do.
    ;;
  flutter)
    bump_pubspec
    ;;
  python)
    bump_pyproject
    ;;
  makefile)
    : # VERSION file alone; the Makefile target is expected to read it.
    ;;
esac

# Exported so the build script doesn't need to re-derive them.
export APP_STACK NEW_VERSION APP_NAME APP_IDENT

# ----------------------------------------------------------------------------
# (g) Build installers. Refuse to ship a release with no binaries.
# ----------------------------------------------------------------------------
CURRENT_STEP="building installers"
BUILD_SCRIPT="$ROOT/scripts/build-installers.command"
[ -x "$BUILD_SCRIPT" ] || die "$BUILD_SCRIPT missing or not executable"
mkdir -p "$ROOT/build/installers"

log "invoking $BUILD_SCRIPT"
if ! "$BUILD_SCRIPT"; then
  die "installer build failed — refusing to push a release with no binaries."
fi

# Sanity: at least one shippable artifact must exist.
SHIPPED_COUNT="$(find "$ROOT/build/installers" -maxdepth 1 -type f \( \
  -name '*.pkg' -o -name '*.dmg' -o -name '*.command' -o -name '*.tar.gz' \
  \) | wc -l | tr -d ' ')"
if [ "$SHIPPED_COUNT" -eq 0 ]; then
  die "no installer artifacts emitted to build/installers/ — aborting."
fi
log "installers: $SHIPPED_COUNT artifact(s) in build/installers/"

# ----------------------------------------------------------------------------
# (h) Stage release artifacts. build/release-artifacts/ is .gitignored — it's
#     a scratch staging dir for gh release upload, nothing more.
# ----------------------------------------------------------------------------
CURRENT_STEP="staging release artifacts"
STAGE="$ROOT/build/release-artifacts"
rm -rf "$STAGE"
mkdir -p "$STAGE"
for pat in "*.pkg" "*.dmg" "*.command" "*.tar.gz" "*.sig"; do
  # Use a loop so shopt/globfail doesn't matter; skip literal-pattern cases.
  for f in "$ROOT/build/installers/"$pat; do
    [ -e "$f" ] || continue
    cp "$f" "$STAGE/"
  done
done
STAGED_COUNT="$(find "$STAGE" -maxdepth 1 -type f | wc -l | tr -d ' ')"
[ "$STAGED_COUNT" -gt 0 ] || die "nothing staged in build/release-artifacts/ — aborting."
log "staged $STAGED_COUNT release artifact(s)"

# ----------------------------------------------------------------------------
# Release-asset size guard. GitHub's Release API rejects any individual asset
# larger than 2 GiB (HTTP 422 "size must be less than 2147483648"). A bundle
# that blows past this is nearly always the result of PyInstaller over-collection
# (``--collect-all`` on a package whose dir doubles as the repo root sweeps
# tests/artifacts/db/etc. as data). Detect that here rather than after burning
# two+ minutes on a ``gh release create`` that can't succeed.
# ----------------------------------------------------------------------------
CURRENT_STEP="checking release asset sizes"
GH_RELEASE_ASSET_MAX=2147483648  # 2 GiB — GitHub's hard per-asset cap.
OVERSIZED_ASSETS=""
OVERSIZED_ASSETS_COUNT=0
while IFS= read -r -d '' asset; do
  sz="$(wc -c < "$asset" 2>/dev/null | tr -d ' ')"
  [ -n "$sz" ] || continue
  if [ "$sz" -gt "$GH_RELEASE_ASSET_MAX" ]; then
    OVERSIZED_ASSETS="${OVERSIZED_ASSETS}  $((sz / 1024 / 1024)) MB  $(basename "$asset")
"
    OVERSIZED_ASSETS_COUNT=$((OVERSIZED_ASSETS_COUNT + 1))
  fi
done < <(find "$STAGE" -maxdepth 1 -type f -print0)

if [ "$OVERSIZED_ASSETS_COUNT" -gt 0 ]; then
  {
    printf "%sERROR: %d staged release asset(s) exceed GitHub's 2 GiB per-asset limit:%s\n" \
      "$C_RED" "$OVERSIZED_ASSETS_COUNT" "$C_RESET"
    printf "%s" "$OVERSIZED_ASSETS"
    printf "\nThis is almost always PyInstaller over-collection. Common causes:\n"
    printf "  - ``--collect-all <pkg>`` where <pkg>'s directory is the repo root,\n"
    printf "    which sweeps tests/, migrations, artifacts/, DB files, etc. as data.\n"
    printf "  - Accidentally bundling a large model file or dataset as package-data.\n"
    printf "\nInspect build/.installer-work/work/*/warn-*.txt and xref-*.html for\n"
    printf "what grew. Trim the build script and rerun.\n"
  } >&2
  exit 1
fi

# ----------------------------------------------------------------------------
# (k, pre-push) Regenerate the README Download block so the committed README
#     always reflects the new tag. Only touches content if both markers
#     already exist — the one-time seeding is the README file we ship
#     alongside this script.
# ----------------------------------------------------------------------------
CURRENT_STEP="regenerating README Download block"
regen_readme_block() {
  local readme="$ROOT/README.md"
  [ -f "$readme" ] || return 0
  grep -q '<!-- INSTALLERS:START -->' "$readme" || return 0
  grep -q '<!-- INSTALLERS:END -->'   "$readme" || return 0
  python3 - "$readme" "$NEW_VERSION" "$OWNER" "$REPO" "$APP_NAME" <<'PY'
import pathlib, re, sys
readme, version, owner, repo, app = sys.argv[1:6]
base = f"https://github.com/{owner}/{repo}/releases/latest/download"
block = (
    "<!-- INSTALLERS:START -->\n"
    "## Download\n\n"
    f"- macOS (pkg): {base}/{app}-Installer.pkg\n"
    f"- macOS (dmg): {base}/{app}-Installer.dmg\n"
    f"- Uninstall:   {base}/Uninstall-{app}.command\n\n"
    f"Latest version: v{version} — "
    f"https://github.com/{owner}/{repo}/releases/tag/v{version}\n"
    "<!-- INSTALLERS:END -->"
)
p = pathlib.Path(readme)
text = p.read_text(encoding="utf-8")
new = re.sub(
    r'<!-- INSTALLERS:START -->[\s\S]*?<!-- INSTALLERS:END -->',
    block,
    text,
    count=1,
)
p.write_text(new, encoding="utf-8")
PY
  log "regenerated README Download block for v${NEW_VERSION}"
}
regen_readme_block

# ----------------------------------------------------------------------------
# (i) Commit + push on the current branch. "Nothing to commit" is a clean exit
#     only when there also aren't any unpushed commits waiting (the gate
#     already handled the clean-tree-but-ahead case, so getting here means we
#     either just built something or the user forced a rebuild).
# ----------------------------------------------------------------------------
CURRENT_STEP="committing and pushing"
git add -A
if git diff --cached --quiet; then
  # Nothing to commit AND we reached this point only because the user either
  # had source changes (now already committed by a prior run?) or forced a
  # rebuild. Either way, push any outstanding commits to keep things clean.
  UNPUSHED_COUNT="$(count_unpushed_commits)"
  if [ "$UNPUSHED_COUNT" -gt 0 ]; then
    log "nothing new to commit but $UNPUSHED_COUNT unpushed commit(s) — pushing"
    CURRENT_STEP="checking HEAD for oversized files"
    check_head_for_oversized_files || exit 1
    CURRENT_STEP="committing and pushing"
    if git rev-parse --abbrev-ref --symbolic-full-name "@{u}" >/dev/null 2>&1; then
      git push origin "$CURRENT_BRANCH"
    else
      git push -u origin "$CURRENT_BRANCH"
    fi
  else
    log "nothing to commit and nothing unpushed — skipping push and release."
    SYNC_SUCCEEDED=1
    print_banner yellow "[-]" "SYNC SKIPPED (no-op)" \
      "Branch: $CURRENT_BRANCH" \
      "" \
      "Tree matched HEAD after the build and nothing was unpushed."
    exit 0
  fi
else
  git commit -m "v${NEW_VERSION}: Sync and build"
  CURRENT_STEP="checking HEAD for oversized files"
  check_head_for_oversized_files || exit 1
  CURRENT_STEP="committing and pushing"
  if git rev-parse --abbrev-ref --symbolic-full-name "@{u}" >/dev/null 2>&1; then
    git push origin "$CURRENT_BRANCH"
  else
    git push -u origin "$CURRENT_BRANCH"
  fi
fi

# ----------------------------------------------------------------------------
# (j) GitHub Release. Idempotent: if the tag already exists (e.g. a prior
#     sync produced the same version), delete the old release + tag first so
#     the reattached assets are the ones from this run.
# ----------------------------------------------------------------------------
CURRENT_STEP="creating GitHub release"
TAG="v${NEW_VERSION}"
if gh release view "$TAG" >/dev/null 2>&1; then
  log "release $TAG already exists — replacing it"
  gh release delete "$TAG" --yes --cleanup-tag >/dev/null 2>&1 || true
  git tag -d "$TAG" >/dev/null 2>&1 || true
  git push origin ":refs/tags/$TAG" >/dev/null 2>&1 || true
fi

# Collect staged artifacts into a bash array for gh.
ASSETS=()
while IFS= read -r -d '' f; do
  ASSETS+=("$f")
done < <(find "$STAGE" -maxdepth 1 -type f -print0 | sort -z)

[ "${#ASSETS[@]}" -gt 0 ] || die "no assets to attach — aborting release creation."

gh release create "$TAG" \
  --title "$TAG" \
  --notes "Automated release ${TAG}. Installers attached." \
  "${ASSETS[@]}"

# ----------------------------------------------------------------------------
# (l) Release verification. Compare the assets actually attached to the
#     newly-created release against the set we staged. The dynamic expected
#     list (vs. a hardcoded baseline) means this stays correct across stacks
#     and across future additions to build-installers.command.
# ----------------------------------------------------------------------------
CURRENT_STEP="verifying release assets"

# Expected = filenames we staged.
EXPECTED_NAMES="$(cd "$STAGE" && for f in *; do [ -f "$f" ] && printf '%s\n' "$f"; done | sort)"

# Actual = filenames gh reports on the release we just created.
ACTUAL_NAMES="$(gh release view "$TAG" --json assets --jq '.assets[].name' 2>/dev/null | sort || true)"

VERIFY_OK=0
VERIFY_MISSING=0
VERIFY_EXTRA=0
VERIFY_LINES=()

while IFS= read -r name; do
  [ -z "$name" ] && continue
  if printf '%s\n' "$ACTUAL_NAMES" | grep -qFx "$name"; then
    VERIFY_LINES+=("  ${C_GREEN}[OK]${C_RESET}       $name")
    VERIFY_OK=$((VERIFY_OK + 1))
  else
    VERIFY_LINES+=("  ${C_RED}[MISSING]${C_RESET}  $name")
    VERIFY_MISSING=$((VERIFY_MISSING + 1))
  fi
done <<< "$EXPECTED_NAMES"

# Surface any assets the release has that we didn't stage (harmless, but useful
# info — e.g. leftover from a prior failed run that wasn't actually cleaned up).
while IFS= read -r name; do
  [ -z "$name" ] && continue
  if ! printf '%s\n' "$EXPECTED_NAMES" | grep -qFx "$name"; then
    VERIFY_LINES+=("  ${C_DIM}[EXTRA]${C_RESET}    $name")
    VERIFY_EXTRA=$((VERIFY_EXTRA + 1))
  fi
done <<< "$ACTUAL_NAMES"

EXPECTED_COUNT=$((VERIFY_OK + VERIFY_MISSING))
RELEASE_URL="$(gh release view "$TAG" --json url --jq '.url' 2>/dev/null \
               || printf 'https://github.com/%s/%s/releases/tag/%s' "$OWNER" "$REPO" "$TAG")"

# ----------------------------------------------------------------------------
# Final banner. Three outcomes:
#   - every staged asset made it to the release  → green SYNC COMPLETE
#   - at least one made it, at least one missing → yellow SYNC PARTIAL
#   - none made it (upload wholesale failed)     → red SYNC FAILED, exit 1
# ----------------------------------------------------------------------------
CURRENT_STEP="printing final banner"
SYNC_SUCCEEDED=1

if [ "$VERIFY_MISSING" -eq 0 ] && [ "$VERIFY_OK" -gt 0 ]; then
  print_banner green "[OK]" "SYNC COMPLETE" \
    "Version:  v${NEW_VERSION}" \
    "Branch:   ${CURRENT_BRANCH}" \
    "Release:  ${RELEASE_URL}" \
    "Assets:   ${VERIFY_OK}/${EXPECTED_COUNT} installers attached" \
    "" \
    "${VERIFY_LINES[@]}"
  exit 0
elif [ "$VERIFY_OK" -gt 0 ]; then
  print_banner yellow "[!]" "SYNC PARTIAL" \
    "Version:  v${NEW_VERSION}" \
    "Branch:   ${CURRENT_BRANCH}" \
    "Release:  ${RELEASE_URL}" \
    "Assets:   ${VERIFY_OK}/${EXPECTED_COUNT} installers attached" \
    "" \
    "${VERIFY_LINES[@]}" \
    "" \
    "${VERIFY_MISSING} expected asset(s) did not land on the release." \
    "Inspect ${RELEASE_URL} and rerun to retry the missing uploads."
  exit 0
else
  # Code is pushed but nothing landed on the release. Treat as failure so CI
  # style checks / exit-code consumers notice.
  SYNC_SUCCEEDED=0
  print_banner red "[X]" "SYNC FAILED" \
    "Version:  v${NEW_VERSION}" \
    "Branch:   ${CURRENT_BRANCH}" \
    "Release:  ${RELEASE_URL}" \
    "Assets:   0/${EXPECTED_COUNT} installers attached — upload failed" \
    "" \
    "${VERIFY_LINES[@]}" \
    "" \
    "Code has been pushed to origin/${CURRENT_BRANCH}, but the release is empty." \
    "Inspect ${RELEASE_URL} and rerun once the upload path is fixed."
  # Disarm the generic trap because we've already printed a failure banner.
  SYNC_SUCCEEDED=1
  exit 1
fi
