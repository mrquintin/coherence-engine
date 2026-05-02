"""Pluggable backends for the FeatureFlags resolver.

The default runtime never imports ``launchdarkly_api_client`` or
``posthog`` — both adapters perform a *lazy* import inside their
constructor so the SDKs remain optional at install time. If a backend
is configured but its client library is not installed, construction
raises :class:`FeatureFlagBackendError` with a clear remediation
message.

A backend's responsibility is narrow: given a flag key and the
declarative type, return a value or ``None`` (meaning "I have no
opinion; fall through to the next layer"). Type coercion happens in
the resolver, not in the backend.
"""

from __future__ import annotations

import logging
import os
from typing import Optional, Protocol

_LOG = logging.getLogger(__name__)


class FeatureFlagBackendError(RuntimeError):
    """Raised when a backend cannot be constructed or fails fatally."""


class FeatureFlagBackend(Protocol):
    """Resolves a flag value or returns None to defer to the next layer."""

    name: str

    def get(self, key: str, flag_type: str) -> Optional[object]:
        ...


class NullBackend:
    """A backend that always defers. Used when no remote backend is configured."""

    name = "null"

    def get(self, key: str, flag_type: str) -> Optional[object]:
        return None


class LaunchDarklyBackend:
    """LaunchDarkly adapter (lazy import).

    Constructed only when ``COHERENCE_FUND_FEATURE_FLAGS_BACKEND=launchdarkly``.
    The ``ldclient`` package is loaded inside ``__init__`` so the
    process imports fine without LaunchDarkly installed.
    """

    name = "launchdarkly"

    def __init__(self, sdk_key: Optional[str] = None, *, user_key: str = "service-default") -> None:
        sdk_key = sdk_key or os.getenv("LAUNCHDARKLY_SDK_KEY", "").strip()
        if not sdk_key:
            raise FeatureFlagBackendError(
                "LaunchDarkly backend requires LAUNCHDARKLY_SDK_KEY"
            )
        try:  # lazy import — keep launchdarkly optional
            import ldclient  # type: ignore
            from ldclient.config import Config  # type: ignore
        except ImportError as exc:  # pragma: no cover - exercised when SDK absent
            raise FeatureFlagBackendError(
                "launchdarkly-server-sdk not installed; "
                "`pip install launchdarkly-server-sdk` to enable this backend"
            ) from exc
        ldclient.set_config(Config(sdk_key))
        self._client = ldclient.get()
        self._user_key = user_key

    def _context(self) -> object:
        try:
            from ldclient import Context  # type: ignore
        except ImportError:  # pragma: no cover
            return {"key": self._user_key}
        return Context.create(self._user_key)

    def get(self, key: str, flag_type: str) -> Optional[object]:
        ctx = self._context()
        try:
            if flag_type == "boolean":
                return self._client.variation(key, ctx, None)
            if flag_type == "string-enum":
                return self._client.variation(key, ctx, None)
            if flag_type == "int-percent":
                value = self._client.variation(key, ctx, None)
                if value is None:
                    return None
                return int(value)
        except Exception:  # pragma: no cover — defensive against SDK errors
            _LOG.exception("launchdarkly_variation_failed key=%s", key)
            return None
        return None


class PostHogBackend:
    """PostHog adapter (lazy import).

    PostHog feature flags are strings or booleans; ``int-percent`` flags
    are read as the JSON value of a multivariate flag. The ``posthog``
    Python package is imported lazily so it remains optional.
    """

    name = "posthog"

    def __init__(
        self,
        api_key: Optional[str] = None,
        host: Optional[str] = None,
        *,
        distinct_id: str = "service-default",
    ) -> None:
        api_key = api_key or os.getenv("POSTHOG_PROJECT_API_KEY", "").strip()
        if not api_key:
            raise FeatureFlagBackendError(
                "PostHog backend requires POSTHOG_PROJECT_API_KEY"
            )
        try:  # lazy import — keep posthog optional
            import posthog  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise FeatureFlagBackendError(
                "posthog package not installed; `pip install posthog` to enable this backend"
            ) from exc
        posthog.project_api_key = api_key
        if host:
            posthog.host = host
        self._client = posthog
        self._distinct_id = distinct_id

    def get(self, key: str, flag_type: str) -> Optional[object]:
        try:
            value = self._client.get_feature_flag(key, self._distinct_id)
        except Exception:  # pragma: no cover
            _LOG.exception("posthog_get_feature_flag_failed key=%s", key)
            return None
        if value is None:
            return None
        if flag_type == "boolean":
            return bool(value)
        if flag_type == "int-percent":
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
        return value


def build_backend(provider: Optional[str] = None) -> FeatureFlagBackend:
    """Construct the configured backend, defaulting to NullBackend.

    ``provider`` is read from ``COHERENCE_FUND_FEATURE_FLAGS_BACKEND``
    when omitted. Unknown / blank / ``"local"`` provider names yield
    :class:`NullBackend` so the default deployment has zero remote
    dependencies.
    """
    name = (provider or os.getenv("COHERENCE_FUND_FEATURE_FLAGS_BACKEND", "")).strip().lower()
    if not name or name in {"null", "local", "yaml", "disabled"}:
        return NullBackend()
    if name == "launchdarkly":
        return LaunchDarklyBackend()
    if name == "posthog":
        return PostHogBackend()
    raise FeatureFlagBackendError(f"unknown_feature_flags_backend:{name!r}")
