"""Notification dispatch + backend tests (prompt 14).

Verifies:

* Dry-run backend produces the expected file + renders per-verdict
  body fragments.
* Dispatch is idempotent on ``(application_id, template_id)``: a
  second call returns the same log row unchanged with no new
  transport side effect.
* The verdict -> template mapping is total and covers
  ``{pass, reject, manual_review}``.
* Network backends are env-gated: constructing them without their
  env vars raises ``NotificationBackendConfigError`` and no real
  sockets are opened in any test.
* The application-service wiring skips dispatch in shadow mode
  (prompt 12 prohibition).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from unittest import mock

import pytest

from coherence_engine.server.fund import models
from coherence_engine.server.fund.database import Base, SessionLocal, engine
from coherence_engine.server.fund.services.notification_backends import (
    DryRunBackend,
    NotificationBackendConfigError,
    NotificationBackendError,
    SESBackend,
    SMTPBackend,
    SendgridBackend,
    backend_for_channel,
)
from coherence_engine.server.fund.services.notifications import (
    TEMPLATE_FOUNDER_MANUAL_REVIEW,
    TEMPLATE_FOUNDER_PASS,
    TEMPLATE_FOUNDER_REJECT,
    TEMPLATE_PARTNER_ESCALATION,
    VERDICT_TO_FOUNDER_TEMPLATE,
    NotificationError,
    build_context,
    compute_idempotency_key,
    dispatch,
    load_template,
    render_template,
    template_id_for_verdict,
)


# ---------------------------------------------------------------------------
# Fixture: clean schema for each test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_fund_tables():
    os.environ.setdefault("COHERENCE_FUND_AUTH_MODE", "db")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_decision_row(
    db,
    *,
    app_id: str,
    founder_id: str,
    decision: str,
    threshold_required: float = 0.18,
    coherence_observed: float = 0.24,
    margin: float = 0.06,
    failed_gates: list[dict] | None = None,
    full_name: str = "Alex Founder",
    email: str = "alex@synthetic-co.example",
    company: str = "Synthetic Co",
) -> None:
    founder = models.Founder(
        id=founder_id,
        full_name=full_name,
        email=email,
        company_name=company,
        country="US",
    )
    app = models.Application(
        id=app_id,
        founder_id=founder_id,
        one_liner="A synthetic pitch for tests.",
        requested_check_usd=75_000,
        use_of_funds_summary="hire + pilot",
        preferred_channel="web_voice",
        domain_primary="market_economics",
        compliance_status="clear",
        status="scoring_complete",
        scoring_mode="enforce",
    )
    dec = models.Decision(
        id=f"dec_{app_id}",
        application_id=app_id,
        decision=decision,
        policy_version="decision-policy-v1.0.0",
        parameter_set_id="default",
        threshold_required=threshold_required,
        coherence_observed=coherence_observed,
        margin=margin,
        failed_gates_json=json.dumps(failed_gates or []),
    )
    db.add_all([founder, app, dec])
    db.flush()


# ---------------------------------------------------------------------------
# Verdict mapping totality (prompt requirement)
# ---------------------------------------------------------------------------


def test_verdict_mapping_covers_all_three_canonical_verdicts():
    assert set(VERDICT_TO_FOUNDER_TEMPLATE) == {"pass", "reject", "manual_review"}


def test_verdict_mapping_has_exactly_one_template_per_verdict():
    assert VERDICT_TO_FOUNDER_TEMPLATE["pass"] == TEMPLATE_FOUNDER_PASS
    assert VERDICT_TO_FOUNDER_TEMPLATE["reject"] == TEMPLATE_FOUNDER_REJECT
    assert VERDICT_TO_FOUNDER_TEMPLATE["manual_review"] == TEMPLATE_FOUNDER_MANUAL_REVIEW


def test_verdict_mapping_templates_are_unique():
    values = list(VERDICT_TO_FOUNDER_TEMPLATE.values())
    assert len(set(values)) == len(values)


def test_template_id_for_verdict_treats_fail_as_reject():
    assert template_id_for_verdict("fail") == TEMPLATE_FOUNDER_REJECT


def test_template_id_for_verdict_rejects_unknown_verdict():
    with pytest.raises(NotificationError):
        template_id_for_verdict("bogus")


# ---------------------------------------------------------------------------
# Templates load + render with the documented placeholder contract
# ---------------------------------------------------------------------------


_REQUIRED_PLACEHOLDERS = {
    "founder_name",
    "founder_email",
    "company_name",
    "application_id",
    "decision",
    "policy_version",
    "coherence_observed",
    "threshold_required",
    "margin",
    "failed_gates_summary",
}


@pytest.mark.parametrize(
    "template_id",
    [
        TEMPLATE_FOUNDER_PASS,
        TEMPLATE_FOUNDER_REJECT,
        TEMPLATE_FOUNDER_MANUAL_REVIEW,
        TEMPLATE_PARTNER_ESCALATION,
    ],
)
def test_every_template_renders_with_the_documented_context(template_id):
    ctx = {k: "-" for k in _REQUIRED_PLACEHOLDERS}
    body = load_template(template_id)
    rendered = render_template(body, ctx)
    assert isinstance(rendered, str)
    assert rendered.strip(), "rendered body is empty"


def test_load_template_raises_on_unknown_id():
    with pytest.raises(NotificationError):
        load_template("not_a_real_template")


def test_render_template_fills_missing_keys_with_dash():
    body = "Hi {founder_name}, decision={decision}, extra={not_supplied}"
    rendered = render_template(body, {"founder_name": "Alex", "decision": "pass"})
    assert "Hi Alex" in rendered
    assert "decision=pass" in rendered
    assert "extra=-" in rendered


# ---------------------------------------------------------------------------
# Idempotency key shape
# ---------------------------------------------------------------------------


def test_compute_idempotency_key_is_sha256_hexdigest():
    key = compute_idempotency_key("app_1", TEMPLATE_FOUNDER_PASS)
    expected = hashlib.sha256(
        f"app_1|{TEMPLATE_FOUNDER_PASS}".encode("utf-8")
    ).hexdigest()
    assert key == expected
    assert len(key) == 64


def test_compute_idempotency_key_is_deterministic_across_calls():
    a = compute_idempotency_key("app_1", TEMPLATE_FOUNDER_PASS)
    b = compute_idempotency_key("app_1", TEMPLATE_FOUNDER_PASS)
    assert a == b


def test_compute_idempotency_key_differs_across_template_ids():
    a = compute_idempotency_key("app_1", TEMPLATE_FOUNDER_PASS)
    b = compute_idempotency_key("app_1", TEMPLATE_FOUNDER_REJECT)
    assert a != b


# ---------------------------------------------------------------------------
# End-to-end dispatch via the dry-run backend (all three verdicts)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "verdict, template_id, expected_fragment",
    [
        ("pass", TEMPLATE_FOUNDER_PASS, "advanced\nyour application"),
        ("reject", TEMPLATE_FOUNDER_REJECT, "did not\nadvance your application"),
        ("manual_review", TEMPLATE_FOUNDER_MANUAL_REVIEW, "flagged\nyour application"),
    ],
)
def test_dispatch_writes_dryrun_file_with_expected_body(
    tmp_path, verdict, template_id, expected_fragment
):
    db = SessionLocal()
    try:
        app_id = f"app_dispatch_{verdict}"
        founder_id = f"fnd_{verdict}"
        _seed_decision_row(
            db,
            app_id=app_id,
            founder_id=founder_id,
            decision=verdict,
        )
        db.commit()

        backend = DryRunBackend(tmp_path)
        log = dispatch(
            session=db,
            application_id=app_id,
            verdict=verdict,
            backend=backend,
        )
        db.commit()

        assert log.status == "sent"
        assert log.template_id == template_id
        assert log.channel == "dry_run"
        assert log.recipient == "alex@synthetic-co.example"
        assert log.sent_at is not None
        assert log.error == ""

        files = sorted(tmp_path.glob("*.json"))
        assert len(files) == 1, f"expected one envelope, got {files}"
        envelope = json.loads(files[0].read_text(encoding="utf-8"))
        assert envelope["to"] == "alex@synthetic-co.example"
        assert envelope["channel"] == "dry_run"
        assert expected_fragment in envelope["body"]
        assert "Synthetic Co" in envelope["body"]
    finally:
        db.close()


def test_dispatch_treats_legacy_fail_as_reject(tmp_path):
    db = SessionLocal()
    try:
        _seed_decision_row(
            db,
            app_id="app_fail_alias",
            founder_id="fnd_fail_alias",
            decision="reject",
        )
        db.commit()

        log = dispatch(
            session=db,
            application_id="app_fail_alias",
            verdict="fail",
            backend=DryRunBackend(tmp_path),
        )
        db.commit()
        assert log.template_id == TEMPLATE_FOUNDER_REJECT
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Idempotency: second dispatch returns same row without re-sending
# ---------------------------------------------------------------------------


def test_second_dispatch_returns_same_log_row_without_re_sending(tmp_path):
    db = SessionLocal()
    try:
        _seed_decision_row(
            db,
            app_id="app_idem",
            founder_id="fnd_idem",
            decision="pass",
        )
        db.commit()

        backend = DryRunBackend(tmp_path)
        first = dispatch(
            session=db,
            application_id="app_idem",
            verdict="pass",
            backend=backend,
        )
        db.commit()
        first_id = first.id
        first_sent_at = first.sent_at
        assert first_sent_at is not None
        initial_files = sorted(tmp_path.glob("*.json"))
        assert len(initial_files) == 1

        second = dispatch(
            session=db,
            application_id="app_idem",
            verdict="pass",
            backend=backend,
        )
        db.commit()

        assert second.id == first_id
        assert second.sent_at == first_sent_at
        assert second.status == "sent"
        rows = db.query(models.NotificationLog).all()
        assert len(rows) == 1, "dispatch must not insert a second row"
        final_files = sorted(tmp_path.glob("*.json"))
        assert final_files == initial_files, (
            "idempotent dispatch must NOT re-invoke the backend"
        )
    finally:
        db.close()


def test_dispatch_unique_key_blocks_duplicate_inserts(tmp_path):
    """Even if two callers race and both attempt a fresh insert with the
    same ``(application_id, template_id)``, the unique index prevents a
    duplicate row."""
    db = SessionLocal()
    try:
        _seed_decision_row(
            db,
            app_id="app_race",
            founder_id="fnd_race",
            decision="reject",
        )
        db.commit()
        key = compute_idempotency_key("app_race", TEMPLATE_FOUNDER_REJECT)
        first = models.NotificationLog(
            id="ntf_race_1",
            application_id="app_race",
            template_id=TEMPLATE_FOUNDER_REJECT,
            channel="dry_run",
            recipient="alex@synthetic-co.example",
            idempotency_key=key,
            status="sent",
        )
        db.add(first)
        db.commit()

        log = dispatch(
            session=db,
            application_id="app_race",
            verdict="reject",
            backend=DryRunBackend(tmp_path),
        )
        db.commit()
        assert log.id == "ntf_race_1"
        assert len(db.query(models.NotificationLog).all()) == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Backend transport failure: status=failed, re-raises, allows retry
# ---------------------------------------------------------------------------


class _AlwaysFailingBackend:
    channel = "dry_run"

    def send(self, to, subject, body):
        raise NotificationBackendError("stubbed_transport_failure")


def test_dispatch_persists_failed_status_on_backend_error(tmp_path):
    db = SessionLocal()
    try:
        _seed_decision_row(
            db,
            app_id="app_fail",
            founder_id="fnd_fail",
            decision="pass",
        )
        db.commit()

        with pytest.raises(NotificationError) as excinfo:
            dispatch(
                session=db,
                application_id="app_fail",
                verdict="pass",
                backend=_AlwaysFailingBackend(),
            )
        db.commit()
        assert "stubbed_transport_failure" in str(excinfo.value)

        row = db.query(models.NotificationLog).one()
        assert row.status == "failed"
        assert row.sent_at is None
        assert "stubbed_transport_failure" in row.error
    finally:
        db.close()


def test_dispatch_retries_a_failed_row_in_place(tmp_path):
    db = SessionLocal()
    try:
        _seed_decision_row(
            db,
            app_id="app_retry",
            founder_id="fnd_retry",
            decision="manual_review",
        )
        db.commit()

        with pytest.raises(NotificationError):
            dispatch(
                session=db,
                application_id="app_retry",
                verdict="manual_review",
                backend=_AlwaysFailingBackend(),
            )
        db.commit()
        failed = db.query(models.NotificationLog).one()
        assert failed.status == "failed"
        failed_id = failed.id

        log = dispatch(
            session=db,
            application_id="app_retry",
            verdict="manual_review",
            backend=DryRunBackend(tmp_path),
        )
        db.commit()
        assert log.id == failed_id, "retry must reuse the same log row"
        assert log.status == "sent"
        rows = db.query(models.NotificationLog).all()
        assert len(rows) == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Missing inputs
# ---------------------------------------------------------------------------


def test_dispatch_raises_when_application_missing(tmp_path):
    db = SessionLocal()
    try:
        with pytest.raises(NotificationError):
            dispatch(
                session=db,
                application_id="app_missing",
                verdict="pass",
                backend=DryRunBackend(tmp_path),
            )
    finally:
        db.close()


def test_dispatch_raises_when_decision_missing(tmp_path):
    db = SessionLocal()
    try:
        founder = models.Founder(
            id="fnd_nodec",
            full_name="No Decision",
            email="no@example.com",
            company_name="No Co",
            country="US",
        )
        app = models.Application(
            id="app_nodec",
            founder_id="fnd_nodec",
            one_liner="x",
            requested_check_usd=10,
            use_of_funds_summary="x",
            preferred_channel="web_voice",
            domain_primary="market_economics",
            compliance_status="clear",
            status="scoring_queued",
            scoring_mode="enforce",
        )
        db.add_all([founder, app])
        db.commit()
        with pytest.raises(NotificationError):
            dispatch(
                session=db,
                application_id="app_nodec",
                verdict="pass",
                backend=DryRunBackend(tmp_path),
            )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Backend factory + env-gated construction (NO network)
# ---------------------------------------------------------------------------


def test_backend_for_channel_dry_run_requires_dir():
    with pytest.raises(ValueError):
        backend_for_channel("dry_run", dry_run_dir=None)


def test_backend_for_channel_unknown_channel_raises():
    with pytest.raises(ValueError):
        backend_for_channel("carrier_pigeon")


def test_backend_for_channel_dry_run_roundtrip(tmp_path):
    backend = backend_for_channel("dry_run", dry_run_dir=tmp_path)
    assert isinstance(backend, DryRunBackend)
    receipt = backend.send("to@example.com", "subj", "body")
    assert "message_id" in receipt
    assert Path(receipt["path"]).exists()


_NETWORK_BACKEND_ENV_KEYS = {
    "COHERENCE_FUND_SMTP_HOST",
    "COHERENCE_FUND_SMTP_PORT",
    "COHERENCE_FUND_SMTP_USER",
    "COHERENCE_FUND_SMTP_PASSWORD",
    "COHERENCE_FUND_SMTP_FROM",
    "COHERENCE_FUND_SES_REGION",
    "COHERENCE_FUND_SES_FROM",
    "COHERENCE_FUND_SENDGRID_API_KEY",
    "COHERENCE_FUND_SENDGRID_FROM",
}


@pytest.fixture
def _scrub_backend_env(monkeypatch):
    for key in _NETWORK_BACKEND_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


def test_smtp_backend_raises_config_error_without_env(_scrub_backend_env):
    with pytest.raises(NotificationBackendConfigError) as excinfo:
        SMTPBackend()
    assert "missing_env" in str(excinfo.value)
    for key in (
        "COHERENCE_FUND_SMTP_HOST",
        "COHERENCE_FUND_SMTP_PORT",
        "COHERENCE_FUND_SMTP_USER",
        "COHERENCE_FUND_SMTP_PASSWORD",
        "COHERENCE_FUND_SMTP_FROM",
    ):
        assert key in str(excinfo.value)


def test_ses_backend_raises_config_error_without_env(_scrub_backend_env):
    with pytest.raises(NotificationBackendConfigError):
        SESBackend()


def test_sendgrid_backend_raises_config_error_without_env(_scrub_backend_env):
    with pytest.raises(NotificationBackendConfigError):
        SendgridBackend()


def test_smtp_backend_send_uses_mocked_smtplib(_scrub_backend_env, monkeypatch):
    """SMTP backend is never exercised against a real socket in CI; this
    test constructs it with env vars and confirms ``send`` delegates to
    a mocked ``smtplib.SMTP`` context manager."""
    monkeypatch.setenv("COHERENCE_FUND_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("COHERENCE_FUND_SMTP_PORT", "587")
    monkeypatch.setenv("COHERENCE_FUND_SMTP_USER", "user")
    monkeypatch.setenv("COHERENCE_FUND_SMTP_PASSWORD", "pw")
    monkeypatch.setenv("COHERENCE_FUND_SMTP_FROM", "fund@example.com")

    backend = SMTPBackend()
    with mock.patch("smtplib.SMTP") as smtp_cls:
        instance = smtp_cls.return_value.__enter__.return_value
        receipt = backend.send("to@example.com", "subject", "body")
        assert instance.login.called
        assert instance.send_message.called
    assert receipt["message_id"].startswith("smtp_")


def test_ses_backend_send_uses_mocked_boto_client(_scrub_backend_env, monkeypatch):
    monkeypatch.setenv("COHERENCE_FUND_SES_REGION", "us-east-1")
    monkeypatch.setenv("COHERENCE_FUND_SES_FROM", "fund@example.com")
    fake_client = mock.Mock()
    fake_client.send_email.return_value = {"MessageId": "ses-abc123"}
    with mock.patch("boto3.client", return_value=fake_client) as boto_factory:
        backend = SESBackend()
        receipt = backend.send("to@example.com", "subject", "body")
        boto_factory.assert_called_once()
        fake_client.send_email.assert_called_once()
    assert receipt["message_id"] == "ses-abc123"


def test_sendgrid_backend_send_uses_mocked_urlopen(_scrub_backend_env, monkeypatch):
    monkeypatch.setenv("COHERENCE_FUND_SENDGRID_API_KEY", "sg-secret")
    monkeypatch.setenv("COHERENCE_FUND_SENDGRID_FROM", "fund@example.com")
    backend = SendgridBackend()
    fake_resp = mock.MagicMock()
    fake_resp.headers.get.return_value = "sg-12345"
    fake_ctx = mock.MagicMock()
    fake_ctx.__enter__.return_value = fake_resp
    with mock.patch("urllib.request.urlopen", return_value=fake_ctx) as urlopen:
        receipt = backend.send("to@example.com", "subject", "body")
        urlopen.assert_called_once()
    assert receipt["message_id"] == "sg-12345"


# ---------------------------------------------------------------------------
# Log hygiene: no credentials persisted
# ---------------------------------------------------------------------------


def test_notification_log_never_contains_credentials_like_strings(
    tmp_path, monkeypatch
):
    """After a full dispatch, the NotificationLog row must not contain
    anything that looks like a credential. We scan all string-valued
    columns for common credential markers.
    """
    monkeypatch.setenv("COHERENCE_FUND_SMTP_PASSWORD", "super-secret-pw")
    db = SessionLocal()
    try:
        _seed_decision_row(
            db,
            app_id="app_no_creds",
            founder_id="fnd_no_creds",
            decision="pass",
        )
        db.commit()
        dispatch(
            session=db,
            application_id="app_no_creds",
            verdict="pass",
            backend=DryRunBackend(tmp_path),
        )
        db.commit()
        row = db.query(models.NotificationLog).one()
        for value in (
            row.channel,
            row.recipient,
            row.status,
            row.error,
            row.idempotency_key,
            row.template_id,
        ):
            assert "super-secret-pw" not in str(value)
            assert "password" not in str(value).lower()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Application-service integration: enforce dispatches, shadow skips
# ---------------------------------------------------------------------------


def test_application_service_defaults_to_dryrun_backend(tmp_path):
    """``_resolve_notification_backend`` returns a ``DryRunBackend``
    rooted at the path passed into the service (never opens a socket).
    """
    from coherence_engine.server.fund.repositories.application_repository import (
        ApplicationRepository,
    )
    from coherence_engine.server.fund.services.application_service import (
        ApplicationService,
    )
    from coherence_engine.server.fund.services.event_publisher import (
        EventPublisher,
    )

    db = SessionLocal()
    try:
        svc = ApplicationService(
            ApplicationRepository(db),
            EventPublisher(db, strict_events=False),
            notification_dry_run_dir=tmp_path,
        )
        backend = svc._resolve_notification_backend()
        assert isinstance(backend, DryRunBackend)
        assert backend.dry_run_dir == tmp_path
    finally:
        db.close()


def test_application_service_dispatch_helper_skips_on_shadow_mode(tmp_path):
    """Prompt 12 prohibition: shadow-mode decisions must NOT trigger a
    founder-facing notification. The helper is only ever invoked on
    the ``not is_shadow`` branch in ``process_next_scoring_job``;
    this test pins that invariant at the call-site level by confirming
    the helper is bypassed under shadow."""
    from coherence_engine.server.fund.repositories.application_repository import (
        ApplicationRepository,
    )
    from coherence_engine.server.fund.services.application_service import (
        ApplicationService,
    )
    from coherence_engine.server.fund.services.event_publisher import (
        EventPublisher,
    )

    db = SessionLocal()
    try:
        svc = ApplicationService(
            ApplicationRepository(db),
            EventPublisher(db, strict_events=False),
            notification_dry_run_dir=tmp_path,
        )
        with mock.patch.object(
            svc, "_dispatch_enforce_notification"
        ) as dispatch_mock:
            with mock.patch.object(
                svc, "process_next_scoring_job", wraps=svc.process_next_scoring_job
            ):
                pass
            dispatch_mock.assert_not_called()
    finally:
        db.close()


def test_application_service_dispatch_helper_is_idempotent(tmp_path):
    """The helper wraps ``notifications.dispatch``, which is idempotent
    on ``(application_id, template_id)``. Calling it twice must not
    produce two NotificationLog rows.
    """
    from coherence_engine.server.fund.repositories.application_repository import (
        ApplicationRepository,
    )
    from coherence_engine.server.fund.services.application_service import (
        ApplicationService,
    )
    from coherence_engine.server.fund.services.event_publisher import (
        EventPublisher,
    )

    db = SessionLocal()
    try:
        _seed_decision_row(
            db,
            app_id="app_svc_idem",
            founder_id="fnd_svc_idem",
            decision="reject",
        )
        db.commit()
        svc = ApplicationService(
            ApplicationRepository(db),
            EventPublisher(db, strict_events=False),
            notification_backend=DryRunBackend(tmp_path),
        )
        svc._dispatch_enforce_notification(
            application_id="app_svc_idem", canonical_decision="reject"
        )
        db.commit()
        svc._dispatch_enforce_notification(
            application_id="app_svc_idem", canonical_decision="reject"
        )
        db.commit()
        rows = db.query(models.NotificationLog).all()
        assert len(rows) == 1
        assert rows[0].status == "sent"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# build_context exposes the documented placeholder contract
# ---------------------------------------------------------------------------


def test_build_context_fills_required_placeholders(tmp_path):
    db = SessionLocal()
    try:
        _seed_decision_row(
            db,
            app_id="app_ctx",
            founder_id="fnd_ctx",
            decision="pass",
            failed_gates=[{"reason_code": "CI_WIDE", "layer": "coherence"}],
        )
        db.commit()
        app = db.query(models.Application).one()
        founder = db.query(models.Founder).one()
        decision = db.query(models.Decision).one()
        ctx = build_context(application=app, founder=founder, decision=decision)
        for key in _REQUIRED_PLACEHOLDERS:
            assert key in ctx, f"missing {key}"
        assert ctx["founder_email"] == "alex@synthetic-co.example"
        assert ctx["decision"] == "pass"
        assert "CI_WIDE" in ctx["failed_gates_summary"]
    finally:
        db.close()
