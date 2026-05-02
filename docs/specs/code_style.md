# Code style & pre-commit policy

This repo enforces formatting and lint rules at three layers:

1. **Local pre-commit hooks** (developer machines) — fast feedback before
   a commit lands.
2. **CI `lint` job** — the same hooks run again, fail-fast, on every PR.
3. **`make lint` / `make typecheck`** — same logic, runnable on demand.

If a hook fails locally, fix the underlying issue. Do **not** commit with
`--no-verify` and do **not** suppress findings without an error code.

## Tools

| Layer       | Tool                                | Config                              |
| ----------- | ----------------------------------- | ----------------------------------- |
| Python lint | **ruff** (`E`,`F`,`I`,`B`,`UP`,`S`,`RUF`) | `[tool.ruff]` in `pyproject.toml`   |
| Python fmt  | **ruff-format** (line length 100)   | `[tool.ruff.format]`                |
| Python type | **mypy --strict** (Python 3.11)     | `[tool.mypy]` in `pyproject.toml`   |
| JS/TS lint  | **eslint** (`next/core-web-vitals`) | `apps/<app>/.eslintrc.json`         |
| JS/TS fmt   | **prettier** (+ tailwind plugin)    | `apps/<app>/package.json`           |
| Whitespace  | `trailing-whitespace`, `end-of-file-fixer` | pre-commit-hooks         |
| Validators  | `check-yaml`, `check-json`, `check-toml`   | pre-commit-hooks         |
| `.env`      | **dotenv-linter**                   | runs against `.env.example`         |
| Branch gate | `no-commit-to-branch` (forbid `main`) | pre-commit-hooks                  |

## Ruff

* Line length: **100**.
* Rule sets: `E`, `F`, `I` (isort), `B`, `UP`, `S` (security), `RUF`.
* Per-file ignores:
  * `tests/**` ignores `S101` (asserts), `S105/S106/S311`, and `B011`.
  * `scripts/**` and `deploy/**` ignore `S603/S607` (subprocess use).
  * `alembic/**` ignores `S608` (literal SQL is unavoidable in migrations).
* Generated SDKs (`sdk/`, `apps/*/src/sdk/`) are excluded from both
  lint and format. Do not auto-format them.

## Mypy (strict)

* Targets: `server/`, `core/` (the backend Python packages).
* Strict mode: `disallow_untyped_defs`, `disallow_any_generics`,
  `warn_return_any`, `no_implicit_reexport`, etc.
* **Tests are typed.** `tests/` is **not** exempt from mypy.
* Suppress narrowly. Bare `# type: ignore` is forbidden — every
  ignore must include an error code, e.g. `# type: ignore[arg-type]`.
  The `test_pre_commit_runs.py::test_no_type_ignore_without_error_code`
  test enforces this for backend packages.
* Outstanding strict-mode debt is tracked in `docs/specs/mypy_todo.md`
  (or the equivalent issue tracker). New code may not add to it.

## ESLint + Prettier

* Each Next.js app extends `next/core-web-vitals`.
* The Astro `apps/site` uses `eslint:recommended` +
  `@typescript-eslint/recommended` with the `astro-eslint-parser` for
  `.astro` files.
* Prettier wraps Tailwind class sorting via
  `prettier-plugin-tailwindcss`.
* `--max-warnings=0` is enforced in CI: warnings break the build.

## Pre-commit lifecycle

```bash
# one-time
pre-commit install

# run on staged files (this happens automatically on `git commit`)
pre-commit run

# run on the entire tree
pre-commit run --all-files

# update hook versions
pre-commit autoupdate
```

## CI

The `lint` job in `.github/workflows/ci.yml` runs
`pre-commit run --all-files` with `--show-diff-on-failure` so reviewers
can copy the suggested patch directly. The job is **fail-fast** — when
lint fails, downstream jobs (tests, build) are cancelled to save
minutes.

## What the policy explicitly forbids

* `# type: ignore` without an error code.
* Exempting `tests/` from mypy.
* Auto-formatting generated SDK clients.
* `git commit --no-verify` (the hooks are the policy).
* Committing directly to `main` (`no-commit-to-branch` enforces).
