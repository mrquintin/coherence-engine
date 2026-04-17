"""Versioned prompt registry with SHA-256 pinning.

Each prompt has a stable ID, semver version, human owner, a pointer to its body
file on disk, and the SHA-256 of that body's raw bytes. The registry lives at
``data/prompts/registry.json`` and is consumed by the decision artifact
(``pins.prompt_registry_digest``) and by the ``prompt-registry`` CLI verbs.

Design invariants:
    * Content hashing is over *raw on-disk bytes* (no normalization, no stripping).
    * ``registry_digest`` is deterministic across calls and Python runs.
    * The registry JSON never embeds prompt bodies inline.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional

__all__ = [
    "PromptEntry",
    "Registry",
    "Mismatch",
    "VerificationReport",
    "PromptRegistryError",
    "load_registry",
    "verify_registry",
    "registry_digest",
    "resolve",
    "default_registry_path",
]


SCHEMA_VERSION = "prompt-registry-v1"
_VALID_STATUSES = frozenset({"draft", "shadow", "prod"})


class PromptRegistryError(RuntimeError):
    """Raised when the registry file is missing, malformed, or a lookup fails."""


@dataclass(frozen=True)
class PromptEntry:
    id: str
    version: str
    status: str
    body_path: str
    content_sha256: str
    owner: str

    def as_tuple(self) -> tuple:
        return (self.id, self.version, self.content_sha256)


@dataclass(frozen=True)
class Registry:
    schema_version: str
    prompts: tuple
    source_path: Optional[Path] = None

    def by_id(self, prompt_id: str, version: Optional[str] = None) -> Optional[PromptEntry]:
        for entry in self.prompts:
            if entry.id == prompt_id and (version is None or entry.version == version):
                return entry
        return None


@dataclass(frozen=True)
class Mismatch:
    prompt_id: str
    version: str
    body_path: str
    expected_sha256: str
    actual_sha256: str


@dataclass
class VerificationReport:
    ok: bool = True
    mismatches: List[Mismatch] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "mismatches": [
                {
                    "prompt_id": m.prompt_id,
                    "version": m.version,
                    "body_path": m.body_path,
                    "expected_sha256": m.expected_sha256,
                    "actual_sha256": m.actual_sha256,
                }
                for m in self.mismatches
            ],
            "missing": list(self.missing),
        }


def default_registry_path() -> Path:
    """Return the path to the canonical registry shipped with the repo."""
    return _repo_root() / "data" / "prompts" / "registry.json"


def _repo_root() -> Path:
    """Return the repository root (``Coherence_Engine_Project/coherence_engine``)."""
    return Path(__file__).resolve().parents[3]


def _sha256_file(path: Path) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def load_registry(path: Optional[Path] = None) -> Registry:
    """Load and validate the prompt registry JSON file.

    Raises:
        PromptRegistryError: if the file is missing, malformed, or violates
            structural constraints (schema_version, required keys, duplicate
            ``(id, version)`` pairs, invalid status values, etc.).
    """
    registry_path = Path(path) if path is not None else default_registry_path()
    if not registry_path.is_file():
        raise PromptRegistryError(f"registry file not found: {registry_path}")

    try:
        with open(registry_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except json.JSONDecodeError as exc:
        raise PromptRegistryError(f"registry is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise PromptRegistryError("registry root must be a JSON object")

    schema_version = raw.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise PromptRegistryError(
            f"unsupported schema_version {schema_version!r}; expected {SCHEMA_VERSION!r}"
        )

    prompts_raw = raw.get("prompts")
    if not isinstance(prompts_raw, list):
        raise PromptRegistryError("registry.prompts must be a list")

    entries: List[PromptEntry] = []
    seen: set = set()
    required_keys = {"id", "version", "status", "body_path", "content_sha256", "owner"}
    for idx, item in enumerate(prompts_raw):
        if not isinstance(item, dict):
            raise PromptRegistryError(f"prompts[{idx}] must be an object")
        missing = required_keys - set(item.keys())
        if missing:
            raise PromptRegistryError(
                f"prompts[{idx}] missing required keys: {sorted(missing)}"
            )
        status = str(item["status"])
        if status not in _VALID_STATUSES:
            raise PromptRegistryError(
                f"prompts[{idx}] invalid status {status!r}; expected one of "
                f"{sorted(_VALID_STATUSES)}"
            )
        entry = PromptEntry(
            id=str(item["id"]),
            version=str(item["version"]),
            status=status,
            body_path=str(item["body_path"]),
            content_sha256=str(item["content_sha256"]).lower(),
            owner=str(item["owner"]),
        )
        key = (entry.id, entry.version)
        if key in seen:
            raise PromptRegistryError(
                f"duplicate prompt (id, version) = {key!r}"
            )
        seen.add(key)
        entries.append(entry)

    return Registry(
        schema_version=schema_version,
        prompts=tuple(entries),
        source_path=registry_path,
    )


def verify_registry(
    registry: Registry,
    repo_root: Optional[Path] = None,
) -> VerificationReport:
    """Recompute SHA-256 for each body file and compare with the registry.

    ``repo_root`` is the directory that ``body_path`` entries are resolved
    against. If omitted, it defaults to the repository root when the registry
    was loaded from the shipped ``data/prompts/registry.json`` and otherwise
    falls back to the registry file's parent directory (useful for tests and
    for ad-hoc registry trees created outside the repo).

    Returns a :class:`VerificationReport` describing any mismatches or missing
    body files. ``ok`` is True only when every entry hashes identically.
    """
    if repo_root is not None:
        root = Path(repo_root)
    else:
        root = _infer_root_for(registry)
    report = VerificationReport()
    for entry in registry.prompts:
        body = (root / entry.body_path).resolve()
        if not body.is_file():
            report.missing.append(entry.body_path)
            report.ok = False
            continue
        actual = _sha256_file(body)
        if actual != entry.content_sha256:
            report.mismatches.append(
                Mismatch(
                    prompt_id=entry.id,
                    version=entry.version,
                    body_path=entry.body_path,
                    expected_sha256=entry.content_sha256,
                    actual_sha256=actual,
                )
            )
            report.ok = False
    return report


def _infer_root_for(registry: Registry) -> Path:
    """Best-effort default for ``repo_root`` given where the registry lives.

    If the registry was loaded from ``<repo>/data/prompts/registry.json`` we
    resolve body paths against ``<repo>`` so entries like
    ``"data/prompts/bodies/...md"`` point at the shipped files. Otherwise we
    resolve relative to the registry file's parent directory.
    """
    src = registry.source_path
    if src is None:
        return _repo_root()
    src = Path(src).resolve()
    repo_root = _repo_root()
    shipped = (repo_root / "data" / "prompts" / "registry.json").resolve()
    if src == shipped:
        return repo_root
    return src.parent


def registry_digest(registry: Registry) -> str:
    """Return a stable SHA-256 over sorted ``(id, version, content_sha256)`` tuples.

    This digest is embedded in the decision artifact's ``pins`` block so the
    exact prompt set in force at decision time is auditable.
    """
    tuples = sorted(entry.as_tuple() for entry in registry.prompts)
    payload = json.dumps(tuples, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def resolve(
    prompt_id: str,
    version: str,
    registry: Optional[Registry] = None,
) -> PromptEntry:
    """Look up a prompt entry by ``id`` and ``version``.

    Raises:
        PromptRegistryError: if the ``(id, version)`` pair is not found.
    """
    reg = registry if registry is not None else load_registry()
    entry = reg.by_id(prompt_id, version=version)
    if entry is None:
        raise PromptRegistryError(
            f"prompt (id={prompt_id!r}, version={version!r}) not found in registry"
        )
    return entry


def iter_entries(registry: Registry) -> Iterable[PromptEntry]:
    """Iterate entries in registry-declared order (stable)."""
    return iter(registry.prompts)
