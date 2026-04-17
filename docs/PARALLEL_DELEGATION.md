## Parallel Prompt Delegation

The CLI and GUI now support parallel "subagent" delegation for large prompts.

```bash
python3 -m coherence_engine delegate "<your prompt>"
```

```bash
python3 -m coherence_engine analyze "<your prompt>"
```

### What It Does

- Automatically delegates very large prompts (threshold-based).
- Supports forced parallel splitting across `1..4` agents.
- Supports selecting agents from an `agent list`.
- Generates delegate-ready prompts for each chunk plus a final synthesis prompt.

### Core Options

- `--force-parallel N`  
  Force split any prompt across `N` parallel agents (`1` to `4`).

- `--agent-list planner,builder,...`  
  Enable only specific agents from available profiles.

- `--agent-list-file path/to/agents.json`  
  Load custom agent profiles from JSON.

- `--no-auto-delegate`  
  Disable automatic large-prompt delegation.

- `analyze` uses `--no-delegate-large`  
  Disable auto-fan-out in `analyze` while keeping standard single-run behavior.

- `--auto-threshold-words INT` / `--auto-threshold-chars INT`  
  Tune auto-delegation trigger.

### Output Modes

- `--format text` (default): delegation summary + synthesis prompt
- `--format json`: structured full output (`runs`, delegate prompts, aggregate score)
- `--format markdown`: chunk reports in markdown

### Example: Force 4 Parallel Agents

```bash
python3 -m coherence_engine delegate "Massive task prompt..." \
  --force-parallel 4 \
  --agent-list planner,critic,builder,synthesizer \
  --format json
```

Or directly through `analyze`:

```bash
python3 -m coherence_engine analyze "Massive task prompt..." \
  --force-parallel 4 \
  --agent-list planner,critic,builder,synthesizer \
  --format json
```

### Agent List File Format

`agents.json`

```json
[
  {
    "name": "planner",
    "role": "Task decomposition lead",
    "objective": "Break the task into non-overlapping executable workstreams."
  },
  {
    "name": "builder",
    "role": "Implementation specialist",
    "objective": "Turn chunk requirements into actionable implementation steps."
  }
]
```

### GUI Behavior

- Analyze tab includes delegation controls:
  - auto-fan-out toggle
  - force parallel value (`0..4`)
  - agent list text field
  - word/char threshold controls
- When delegated, the Results tab shows:
  - aggregate delegation score
  - per-chunk agent/word/score cards
  - generated synthesis prompt
