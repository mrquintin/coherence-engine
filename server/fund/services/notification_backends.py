"""Pluggable notification backends (prompt 14).

The notifications service ``server/fund/services/notifications.py``
delegates the actual transport step to a ``NotificationBackend``
implementation. Four backends ship in-tree:

* :class:`DryRunBackend` — the **default** backend used in CI and
  local development. Renders to a file under ``dry_run_dir`` and
  performs zero network I/O. Tests assert the file content.
* :class:`SMTPBackend` — ``smtplib``-based; reads
  ``COHERENCE_FUND_SMTP_HOST``, ``COHERENCE_FUND_SMTP_PORT``,
  ``COHERENCE_FUND_SMTP_USER``, ``COHERENCE_FUND_SMTP_PASSWORD``,
  ``COHERENCE_FUND_SMTP_FROM`` from the environment. Raises
  :class:`NotificationBackendConfigError` if any required env var
  is missing. **Not exercised in default CI.**
* :class:`SESBackend` — boto3 / AWS SES; reads
  ``COHERENCE_FUND_SES_REGION`` and ``COHERENCE_FUND_SES_FROM`` from
  the environment. Credentials are sourced from the standard boto3
  credential chain (env, profile, IAM role); the backend never
  reads or stores them directly. **Not exercised in default CI.**
* :class:`SendgridBackend` — Sendgrid HTTPS API; reads
  ``COHERENCE_FUND_SENDGRID_API_KEY`` and
  ``COHERENCE_FUND_SENDGRID_FROM`` from the environment.
  **Not exercised in default CI.**

Prohibitions (prompt 14):

* No backend writes credentials into the ``NotificationLog`` table
  or into a dry-run file. The log row only carries the resolved
  recipient address, the channel name, and operator-readable status
  / error strings.
* Tests MUST NOT exercise real network sockets — the SMTP / SES /
  Sendgrid backends are constructed but never ``send``-invoked
  outside mocked or env-guarded paths.

Public surface
--------------

The module exposes:

* :class:`NotificationBackend` — :class:`typing.Protocol` with a
  single ``send(to, subject, body) -> dict`` method and a
  ``channel`` property.
* :class:`NotificationBackendError` — base exception raised by any
  backend on transport failure.
* :class:`NotificationBackendConfigError` — raised by the
  optional backends when required env vars are missing.
* :class:`DryRunBackend` (default).
* :class:`SMTPBackend`, :class:`SESBackend`,
  :class:`SendgridBackend` (optional).
* :func:`backend_for_channel` — factory that maps a channel string
  to a backend instance, used by the CLI / dispatcher.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Protocol, runtime_checkable


__all__ = [
    "NotificationBackendError",
    "NotificationBackendConfigError",
    "NotificationBackend",
    "DryRunBackend",
    "SMTPBackend",
    "SESBackend",
    "SendgridBackend",
    "backend_for_channel",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class NotificationBackendError(Exception):
    """Raised by a backend when transport fails.

    The string form of the exception is what gets persisted into
    ``NotificationLog.error``; callers are expected to keep messages
    short and operator-readable (no credentials, no full stack traces
    embedded inline).
    """


class NotificationBackendConfigError(NotificationBackendError):
    """Raised by the non-default backends when required env vars are missing.

    Subclassing :class:`NotificationBackendError` lets the dispatch
    surface a uniform failure path while still letting callers
    distinguish "config missing" from "remote rejected the message".
    """


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class NotificationBackend(Protocol):
    """Backend transport contract used by ``notifications.dispatch``.

    Implementations MUST:

    * expose a ``channel`` string identifying the transport
      (``"dry_run"``, ``"smtp"``, ``"ses"``, ``"sendgrid"``);
    * implement ``send(to, subject, body) -> dict`` returning a
      JSON-serializable receipt (e.g. ``{"message_id": "..."}``);
    * raise :class:`NotificationBackendError` on transport failure
      with an operator-readable message that does NOT contain
      credentials.
    """

    channel: str

    def send(self, to: str, subject: str, body: str) -> Dict[str, object]:
        ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FILESAFE_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _filesafe(value: str) -> str:
    """Return a filesystem-safe slug for use in dry-run filenames."""
    cleaned = _FILESAFE_RE.sub("_", value or "").strip("_")
    return cleaned or "anonymous"


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Default backend: dry-run filesystem writer
# ---------------------------------------------------------------------------


class DryRunBackend:
    """Default in-process backend that writes a JSON-wrapped envelope to disk.

    Each ``send`` call writes a single ``.json`` file under
    ``dry_run_dir`` whose name encodes the recipient, the wall-clock
    timestamp, and a short uuid suffix to keep two near-simultaneous
    sends from colliding. The file content is the full envelope
    (``to``, ``subject``, ``body``) so tests can assert per-template
    body fragments.

    The backend is intentionally side-effect-free outside the
    configured directory: it never opens sockets, never reads
    credentials, never touches the OS mailer. This is the only
    backend used by default tests and by ``red-team-run`` /
    ``backtest-run`` style replays.
    """

    channel: str = "dry_run"

    def __init__(self, dry_run_dir: Path):
        self.dry_run_dir = Path(dry_run_dir)

    def send(self, to: str, subject: str, body: str) -> Dict[str, object]:
        self.dry_run_dir.mkdir(parents=True, exist_ok=True)
        ts = _utc_now().strftime("%Y%m%dT%H%M%S")
        suffix = uuid.uuid4().hex[:8]
        fname = f"{ts}_{_filesafe(to)}_{suffix}.json"
        path = self.dry_run_dir / fname
        envelope = {
            "channel": self.channel,
            "to": to,
            "subject": subject,
            "body": body,
            "written_at": _utc_now().isoformat(),
        }
        path.write_text(
            json.dumps(envelope, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return {
            "message_id": f"dryrun_{suffix}",
            "path": str(path),
        }


# ---------------------------------------------------------------------------
# Optional backends (env-gated, never exercised in default CI)
# ---------------------------------------------------------------------------


def _require_env(*names: str) -> Dict[str, str]:
    """Read env vars, raising :class:`NotificationBackendConfigError` if any
    are missing. Never echoes the value back in the error message.
    """
    out: Dict[str, str] = {}
    missing = []
    for name in names:
        value = os.environ.get(name, "")
        if not value:
            missing.append(name)
        else:
            out[name] = value
    if missing:
        raise NotificationBackendConfigError(
            "missing_env:" + ",".join(sorted(missing))
        )
    return out


class SMTPBackend:
    """``smtplib``-based SMTP backend.

    Reads SMTP host / port / user / password / from-address from
    env vars at construction time and raises
    :class:`NotificationBackendConfigError` if any are missing.
    Credentials are NEVER returned from ``send`` and NEVER written
    to the notification log.

    Not exercised in default CI; integration tests against a real
    SMTP host are gated on the env vars being set.
    """

    channel: str = "smtp"

    def __init__(self) -> None:
        env = _require_env(
            "COHERENCE_FUND_SMTP_HOST",
            "COHERENCE_FUND_SMTP_PORT",
            "COHERENCE_FUND_SMTP_USER",
            "COHERENCE_FUND_SMTP_PASSWORD",
            "COHERENCE_FUND_SMTP_FROM",
        )
        self._host = env["COHERENCE_FUND_SMTP_HOST"]
        try:
            self._port = int(env["COHERENCE_FUND_SMTP_PORT"])
        except ValueError as exc:
            raise NotificationBackendConfigError(
                "invalid_env:COHERENCE_FUND_SMTP_PORT"
            ) from exc
        self._user = env["COHERENCE_FUND_SMTP_USER"]
        self._password = env["COHERENCE_FUND_SMTP_PASSWORD"]
        self._from = env["COHERENCE_FUND_SMTP_FROM"]

    def send(self, to: str, subject: str, body: str) -> Dict[str, object]:
        import smtplib
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["From"] = self._from
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        try:
            with smtplib.SMTP(self._host, self._port, timeout=15) as smtp:
                smtp.starttls()
                smtp.login(self._user, self._password)
                smtp.send_message(msg)
        except Exception as exc:  # pragma: no cover - exercised via mocks
            raise NotificationBackendError(
                f"smtp_send_failed:{type(exc).__name__}"
            ) from exc
        return {"message_id": f"smtp_{uuid.uuid4().hex[:12]}"}


class SESBackend:
    """boto3 / AWS SES backend.

    Credentials are sourced from the standard boto3 credential chain
    (env, profile, IAM role); the backend itself never reads
    AWS access keys directly. ``COHERENCE_FUND_SES_REGION`` and
    ``COHERENCE_FUND_SES_FROM`` are required.

    Not exercised in default CI.
    """

    channel: str = "ses"

    def __init__(self) -> None:
        env = _require_env(
            "COHERENCE_FUND_SES_REGION",
            "COHERENCE_FUND_SES_FROM",
        )
        self._region = env["COHERENCE_FUND_SES_REGION"]
        self._from = env["COHERENCE_FUND_SES_FROM"]
        try:
            import boto3
        except ImportError as exc:
            raise NotificationBackendConfigError(
                "missing_dependency:boto3"
            ) from exc
        self._client = boto3.client("ses", region_name=self._region)

    def send(self, to: str, subject: str, body: str) -> Dict[str, object]:
        try:
            response = self._client.send_email(
                Source=self._from,
                Destination={"ToAddresses": [to]},
                Message={
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
                },
            )
        except Exception as exc:  # pragma: no cover - exercised via mocks
            raise NotificationBackendError(
                f"ses_send_failed:{type(exc).__name__}"
            ) from exc
        return {"message_id": str(response.get("MessageId", ""))}


class SendgridBackend:
    """Sendgrid HTTPS API backend.

    Uses the official ``sendgrid`` SDK if available and falls back to
    a thin ``urllib.request`` POST otherwise. The API key is read
    once at construction time from
    ``COHERENCE_FUND_SENDGRID_API_KEY`` and is never echoed back from
    ``send`` or persisted into the notification log.

    Not exercised in default CI.
    """

    channel: str = "sendgrid"

    def __init__(self) -> None:
        env = _require_env(
            "COHERENCE_FUND_SENDGRID_API_KEY",
            "COHERENCE_FUND_SENDGRID_FROM",
        )
        self._api_key = env["COHERENCE_FUND_SENDGRID_API_KEY"]
        self._from = env["COHERENCE_FUND_SENDGRID_FROM"]

    def send(self, to: str, subject: str, body: str) -> Dict[str, object]:
        payload = {
            "personalizations": [{"to": [{"email": to}], "subject": subject}],
            "from": {"email": self._from},
            "content": [{"type": "text/plain", "value": body}],
        }
        try:
            import urllib.error
            import urllib.request

            req = urllib.request.Request(
                "https://api.sendgrid.com/v3/mail/send",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                msg_id = resp.headers.get("X-Message-Id", "")
        except Exception as exc:  # pragma: no cover - exercised via mocks
            raise NotificationBackendError(
                f"sendgrid_send_failed:{type(exc).__name__}"
            ) from exc
        return {"message_id": str(msg_id) or f"sendgrid_{uuid.uuid4().hex[:12]}"}


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def backend_for_channel(
    channel: str,
    *,
    dry_run_dir: Optional[Path] = None,
) -> NotificationBackend:
    """Return a backend instance for the requested channel.

    ``dry_run`` is the default and the only backend that does not
    require any environment configuration; the caller MUST provide
    ``dry_run_dir`` when requesting it. The other backends construct
    themselves from env vars and raise
    :class:`NotificationBackendConfigError` if any are missing.

    Raises ``ValueError`` for unknown channel strings (so a typo at
    the call site fails loudly rather than silently falling through
    to a default).
    """
    norm = (channel or "").strip().lower()
    if norm == "dry_run":
        if dry_run_dir is None:
            raise ValueError("dry_run_backend_requires_dry_run_dir")
        return DryRunBackend(Path(dry_run_dir))
    if norm == "smtp":
        return SMTPBackend()
    if norm == "ses":
        return SESBackend()
    if norm == "sendgrid":
        return SendgridBackend()
    raise ValueError(f"unknown_notification_channel:{channel!r}")
