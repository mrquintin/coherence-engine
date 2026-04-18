<!-- INSTALLERS:START -->
## Download

- macOS (pkg): https://github.com/mrquintin/coherence-engine/releases/latest/download/CoherenceEngine-Installer.pkg
- macOS (dmg): https://github.com/mrquintin/coherence-engine/releases/latest/download/CoherenceEngine-Installer.dmg
- Uninstall:   https://github.com/mrquintin/coherence-engine/releases/latest/download/Uninstall-CoherenceEngine.command

Latest version: v0.1.5 — https://github.com/mrquintin/coherence-engine/releases/tag/v0.1.5
<!-- INSTALLERS:END -->

# Coherence Engine — WIP

Measure the internal logical coherence of any text on a 0–1 scale, plus the
fund-orchestrator backend that wraps it into an automated pre-seed decision
pipeline. This repo is under active development; the download links above
always resolve to the newest release's assets via GitHub's
`/releases/latest/download/` redirect.

## Sync workflow

Local development uses a one-button sync bound to `Cmd+Shift+Y` in Cursor:

- The keybinding runs `workbench.action.tasks.runTask`.
- `.vscode/tasks.json` defines exactly one task — **Sync to GitHub** — so
  Cursor runs it without showing a picker.
- The task invokes `scripts/sync-to-github.sh`, which bumps the patch
  version, builds installers via `scripts/build-installers.command`, stages
  artifacts, commits, pushes, and creates (or replaces) a GitHub Release
  named `v<VERSION>` with every installer attached.
- The Download section above is regenerated on every sync so the filenames
  and `Latest version` line stay in sync with the newest release.

## Manual rebuild

If you just want to rebuild installers without syncing:

```
./scripts/build-installers.command
```

Artifacts land in `build/installers/`.

## Repo layout highlights

- `coherence_engine/` — the Python package (this repo is the package root).
- `server/fund/` — FastAPI-based fund orchestrator backend.
- `scripts/` — sync + build automation.
- `docs/` — specs (decision policy, event schemas, backtest, red-team harness,
  release readiness) and ops runbooks.
- `build/installers/` — committed installer artifacts for offline checkouts
  and the `raw.githubusercontent.com` fallback link.
- `build/release-artifacts/` — staging dir for `gh release create`; git-ignored.

## License

MIT. See `pyproject.toml`.
