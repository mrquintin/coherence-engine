"""Secret manifest — declarative inventory of every named secret.

The manifest is a YAML file at
``data/governed/secret_manifest.yaml`` that enumerates every secret the
backend may read along with a *policy* string. Three policies are
recognized:

* ``prod_required``  — must resolve when the runtime env is
  ``production``; missing values abort startup.
* ``prod_optional``  — looked up if present; absence is logged.
* ``dev_optional``   — never required.

The manifest is the single source of truth for the
``secrets manifest`` CLI verb and the startup ``verify_manifest``
gate. See :mod:`secret_backends` for the per-backend resolution
machinery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import yaml


SCHEMA_VERSION = "secret-manifest-v1"

POLICY_PROD_REQUIRED = "prod_required"
POLICY_PROD_OPTIONAL = "prod_optional"
POLICY_DEV_OPTIONAL = "dev_optional"
ALLOWED_POLICIES = {
    POLICY_PROD_REQUIRED,
    POLICY_PROD_OPTIONAL,
    POLICY_DEV_OPTIONAL,
}


class ManifestError(ValueError):
    """Raised when the manifest YAML is malformed or schema-invalid."""


class MissingRequiredSecret(RuntimeError):
    """Raised by ``SecretManager.verify_manifest`` in production env when
    a ``prod_required`` secret could not be resolved."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = list(missing)
        super().__init__(
            "missing required secrets: " + ", ".join(self.missing)
        )


@dataclass(frozen=True)
class ManifestEntry:
    name: str
    category: str
    policy: str
    owner: Optional[str] = None
    description: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.name or not isinstance(self.name, str):
            raise ManifestError("manifest entry missing 'name'")
        if not self.category or not isinstance(self.category, str):
            raise ManifestError(f"manifest entry {self.name} missing 'category'")
        if self.policy not in ALLOWED_POLICIES:
            raise ManifestError(
                f"manifest entry {self.name} has invalid policy {self.policy!r}; "
                f"allowed: {sorted(ALLOWED_POLICIES)}"
            )


@dataclass(frozen=True)
class ResolvedEntry:
    """One row of a :class:`ManifestReport`. Never holds a secret value."""

    name: str
    category: str
    policy: str
    status: str  # "resolved" | "missing"
    backend: Optional[str]  # which backend resolved it (None if missing)
    owner: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "category": self.category,
            "policy": self.policy,
            "status": self.status,
            "backend": self.backend,
            "owner": self.owner,
        }


@dataclass(frozen=True)
class ManifestReport:
    env: str
    schema_version: str
    entries: tuple[ResolvedEntry, ...] = field(default_factory=tuple)

    @property
    def missing_required(self) -> list[str]:
        return [
            e.name
            for e in self.entries
            if e.status == "missing" and e.policy == POLICY_PROD_REQUIRED
        ]

    @property
    def resolved_count(self) -> int:
        return sum(1 for e in self.entries if e.status == "resolved")

    @property
    def missing_count(self) -> int:
        return sum(1 for e in self.entries if e.status == "missing")

    def to_dict(self) -> dict:
        return {
            "env": self.env,
            "schema_version": self.schema_version,
            "missing_required": list(self.missing_required),
            "resolved_count": self.resolved_count,
            "missing_count": self.missing_count,
            "entries": [e.to_dict() for e in self.entries],
        }


@dataclass(frozen=True)
class SecretManifest:
    schema_version: str
    entries: tuple[ManifestEntry, ...]

    @classmethod
    def from_dict(cls, payload: object) -> "SecretManifest":
        if not isinstance(payload, dict):
            raise ManifestError("manifest root must be a mapping")
        version = payload.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ManifestError(
                f"unsupported schema_version {version!r}; expected {SCHEMA_VERSION!r}"
            )
        raw_secrets = payload.get("secrets")
        if not isinstance(raw_secrets, list) or not raw_secrets:
            raise ManifestError("manifest 'secrets' must be a non-empty list")
        seen: set[str] = set()
        entries: list[ManifestEntry] = []
        for raw in raw_secrets:
            if not isinstance(raw, dict):
                raise ManifestError("each manifest secret must be a mapping")
            entry = ManifestEntry(
                name=str(raw.get("name", "")).strip(),
                category=str(raw.get("category", "")).strip(),
                policy=str(raw.get("policy", "")).strip(),
                owner=raw.get("owner"),
                description=raw.get("description"),
            )
            if entry.name in seen:
                raise ManifestError(f"duplicate manifest entry {entry.name!r}")
            seen.add(entry.name)
            entries.append(entry)
        return cls(schema_version=version, entries=tuple(entries))

    @classmethod
    def load(cls, path: Path | str) -> "SecretManifest":
        p = Path(path)
        if not p.is_file():
            raise ManifestError(f"manifest file not found: {p}")
        with p.open("r", encoding="utf-8") as f:
            payload = yaml.safe_load(f)
        return cls.from_dict(payload)

    @classmethod
    def default(cls) -> "SecretManifest":
        return cls.load(default_manifest_path())

    def names(self) -> Iterable[str]:
        return (e.name for e in self.entries)

    def required_names(self) -> Iterable[str]:
        return (e.name for e in self.entries if e.policy == POLICY_PROD_REQUIRED)

    def find(self, name: str) -> Optional[ManifestEntry]:
        for entry in self.entries:
            if entry.name == name:
                return entry
        return None


def default_manifest_path() -> Path:
    """Path to the canonical manifest shipped in-repo."""
    return (
        Path(__file__).resolve().parents[3]
        / "data"
        / "governed"
        / "secret_manifest.yaml"
    )
