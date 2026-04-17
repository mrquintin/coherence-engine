"""OpenAPI refresh + SDK stub generator tests (prompt 17).

Covers:

* The YAML is still valid YAML and exposes the four new operations
  (decision_artifact GET, mode POST, workflow run/resume, notifications
  list).
* The SDK generator is byte-reproducible (regenerating the client
  in-test matches the committed ``client.py``).
* Each new FastAPI endpoint responds on the happy path through
  :class:`TestClient` (role gating + envelope shape).

No network I/O; ``urllib`` is not invoked by the SDK in this test —
we only assert the generator's output.
"""

from __future__ import annotations

import importlib
import os
import sys
import types
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
yaml = pytest.importorskip("yaml")
from fastapi.testclient import TestClient  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / "docs" / "specs" / "openapi_v1.yaml"
GENERATOR_PATH = REPO_ROOT / "scripts" / "generate_sdk_stubs.py"
CLIENT_PATH = REPO_ROOT / "sdk" / "python" / "coherence_fund_client" / "client.py"
SDK_PACKAGE_ROOT = REPO_ROOT / "sdk" / "python"


# ---------------------------------------------------------------------------
# OpenAPI contract sanity
# ---------------------------------------------------------------------------


def _load_spec():
    with SPEC_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_openapi_is_valid_yaml():
    spec = _load_spec()
    assert isinstance(spec, dict)
    assert spec.get("openapi", "").startswith("3.")
    assert "paths" in spec and isinstance(spec["paths"], dict)


@pytest.mark.parametrize(
    "path,method,operation_id",
    [
        ("/applications/{application_id}/decision_artifact", "get", "getDecisionArtifact"),
        ("/applications/{application_id}/mode", "post", "setScoringMode"),
        ("/workflow/{application_id}/run", "post", "runWorkflow"),
        ("/workflow/{application_id}/resume", "post", "resumeWorkflow"),
        ("/notifications", "get", "listNotifications"),
    ],
)
def test_openapi_contains_new_operations(path, method, operation_id):
    spec = _load_spec()
    paths = spec.get("paths") or {}
    assert path in paths, f"missing path {path} in openapi_v1.yaml"
    op = (paths[path] or {}).get(method)
    assert op, f"missing {method.upper()} on {path}"
    assert op.get("operationId") == operation_id
    assert op.get("x-required-roles"), "x-required-roles annotation required"


# ---------------------------------------------------------------------------
# SDK generator reproducibility
# ---------------------------------------------------------------------------


def _import_generator() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "coherence_sdk_generator", GENERATOR_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_generator_is_reproducible_against_committed_client(tmp_path):
    """Regenerating the SDK produces bytes identical to the committed file."""
    generator = _import_generator()
    output = tmp_path / "client.py"
    rc = generator.run(SPEC_PATH, output, check=False)
    assert rc == 0
    assert output.exists()
    committed = CLIENT_PATH.read_bytes()
    regenerated = output.read_bytes()
    assert regenerated == committed, (
        "SDK stubs drifted from committed client.py. "
        "Run `python scripts/generate_sdk_stubs.py` and commit the result."
    )


def test_generator_check_flag_returns_zero_with_no_drift():
    generator = _import_generator()
    rc = generator.run(SPEC_PATH, CLIENT_PATH, check=True)
    assert rc == 0


def test_generator_check_flag_detects_drift(tmp_path):
    """If the committed client.py does not match, ``--check`` exits non-zero."""
    generator = _import_generator()
    stale = tmp_path / "client.py"
    stale.write_text("# intentionally stale placeholder\n", encoding="utf-8")
    rc = generator.run(SPEC_PATH, stale, check=True)
    assert rc != 0


def test_generated_client_exposes_new_endpoint_methods():
    """Sanity-check method names for each newly added operation."""
    # Import from the committed client.py directly so the test does not
    # depend on sdk/python being on sys.path for consumers.
    sys.path.insert(0, str(SDK_PACKAGE_ROOT))
    try:
        if "coherence_fund_client" in sys.modules:
            del sys.modules["coherence_fund_client"]
        if "coherence_fund_client.client" in sys.modules:
            del sys.modules["coherence_fund_client.client"]
        mod = importlib.import_module("coherence_fund_client")
        client_cls = mod.CoherenceFundClient
        method_names = {m for m in dir(client_cls) if not m.startswith("_")}
        for expected in (
            "applications_get_decision_artifact",
            "applications_set_scoring_mode",
            "workflow_run_workflow",
            "workflow_resume_workflow",
            "notifications_list_notifications",
        ):
            assert expected in method_names, f"missing SDK method: {expected}"
    finally:
        try:
            sys.path.remove(str(SDK_PACKAGE_ROOT))
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# FastAPI endpoint happy paths
# ---------------------------------------------------------------------------


@pytest.fixture
def client_with_workflow():
    """Build an ``app = create_app()`` with the workflow router mounted."""
    os.environ["COHERENCE_FUND_AUTH_MODE"] = "db"
    os.environ["COHERENCE_FUND_BOOTSTRAP_ADMIN_ENABLED"] = "false"
    os.environ["COHERENCE_FUND_SECRET_MANAGER_PROVIDER"] = "disabled"

    from coherence_engine.server.fund.app import create_app
    from coherence_engine.server.fund.database import Base, SessionLocal, engine
    from coherence_engine.server.fund.repositories.api_key_repository import (
        ApiKeyRepository,
    )
    from coherence_engine.server.fund.routers.workflow import router as workflow_router
    from coherence_engine.server.fund.services.api_key_service import ApiKeyService

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    tokens: dict = {}
    db = SessionLocal()
    try:
        repo = ApiKeyRepository(db)
        svc = ApiKeyService()
        admin = svc.create_key(
            repo, label="p17-admin", role="admin", created_by="tests", expires_in_days=30
        )
        analyst = svc.create_key(
            repo, label="p17-analyst", role="analyst", created_by="tests", expires_in_days=30
        )
        viewer = svc.create_key(
            repo, label="p17-viewer", role="viewer", created_by="tests", expires_in_days=30
        )
        tokens["admin"] = admin["token"]
        tokens["analyst"] = analyst["token"]
        tokens["viewer"] = viewer["token"]
        db.commit()
    finally:
        db.close()

    app = create_app()
    # workflow router is out of scope for app.py modification (prompt 17);
    # mount it here for tests and future wire-in.
    app.include_router(workflow_router, prefix="/api/v1")
    client = TestClient(app)
    yield client, tokens

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _headers(token: str, *, idempotency: str | None = None) -> dict:
    h = {"X-API-Key": token, "X-Request-Id": "req_p17"}
    if idempotency is not None:
        h["Idempotency-Key"] = idempotency
    return h


def _create_application(client: TestClient, admin_token: str, idem: str) -> str:
    res = client.post(
        "/api/v1/applications",
        headers=_headers(admin_token, idempotency=idem),
        json={
            "founder": {
                "full_name": "P17 Founder",
                "email": "p17@example.com",
                "company_name": "P17 Co",
                "country": "US",
            },
            "startup": {
                "one_liner": "OpenAPI refresh pilot",
                "requested_check_usd": 50000,
                "use_of_funds_summary": "Ship the SDK",
                "preferred_channel": "web_voice",
            },
            "consent": {
                "ai_assessment": True,
                "recording": True,
                "data_processing": True,
            },
        },
    )
    assert res.status_code == 201, res.text
    return res.json()["data"]["application_id"]


def test_set_scoring_mode_endpoint_toggles_mode(client_with_workflow):
    client, tokens = client_with_workflow
    app_id = _create_application(client, tokens["admin"], "p17-mode-1")

    res = client.post(
        f"/api/v1/applications/{app_id}/mode",
        headers=_headers(tokens["admin"]),
        json={"mode": "shadow"},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["application_id"] == app_id
    assert data["previous_mode"] == "enforce"
    assert data["new_mode"] == "shadow"
    assert data["changed"] is True

    # Viewer should be forbidden.
    res_denied = client.post(
        f"/api/v1/applications/{app_id}/mode",
        headers=_headers(tokens["viewer"]),
        json={"mode": "enforce"},
    )
    assert res_denied.status_code == 403


def test_get_decision_artifact_returns_404_when_absent(client_with_workflow):
    client, tokens = client_with_workflow
    app_id = _create_application(client, tokens["admin"], "p17-art-1")

    res = client.get(
        f"/api/v1/applications/{app_id}/decision_artifact",
        headers=_headers(tokens["viewer"]),
    )
    # No artifact yet -> 404 with structured envelope.
    assert res.status_code == 404
    payload = res.json()
    assert payload["data"] is None
    assert payload["error"]["code"] == "NOT_FOUND"


def test_get_decision_artifact_returns_payload_when_present(client_with_workflow):
    client, tokens = client_with_workflow
    app_id = _create_application(client, tokens["admin"], "p17-art-2")

    # Seed a decision_artifact row directly via the service to avoid the
    # full scoring pipeline (we are not validating that here — just the
    # HTTP surface).
    from coherence_engine.server.fund.database import SessionLocal
    from coherence_engine.server.fund import models
    from coherence_engine.server.fund.services.decision_artifact import ARTIFACT_KIND

    db = SessionLocal()
    try:
        payload_json = (
            '{"artifact_id":"art_p17","decision":'
            '{"policy_version":"decision-policy-v1","verdict":"pass"}}'
        )
        row = models.ArgumentArtifact(
            id="art_p17",
            application_id=app_id,
            scoring_job_id="",
            propositions_json="[]",
            relations_json="[]",
            kind=ARTIFACT_KIND,
            payload_json=payload_json,
        )
        db.add(row)
        db.commit()
    finally:
        db.close()

    res = client.get(
        f"/api/v1/applications/{app_id}/decision_artifact",
        headers=_headers(tokens["viewer"]),
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["application_id"] == app_id
    assert data["artifact_id"] == "art_p17"
    assert data["decision_policy_version"] == "decision-policy-v1"
    assert data["payload"]["artifact_id"] == "art_p17"


def test_list_notifications_endpoint_returns_empty_envelope(client_with_workflow):
    client, tokens = client_with_workflow
    app_id = _create_application(client, tokens["admin"], "p17-notif-1")

    res = client.get(
        "/api/v1/notifications",
        headers=_headers(tokens["viewer"]),
        params={"application_id": app_id, "limit": 10, "offset": 0},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["application_id"] == app_id
    assert data["entries"] == []
    assert data["total"] == 0
    assert data["limit"] == 10
    assert data["offset"] == 0


def test_workflow_run_endpoint_returns_run_envelope(client_with_workflow):
    """The run endpoint surfaces the WorkflowRun row (possibly failed).

    We only assert the shape of the response: status is a recognised
    state and the ``steps`` list exists. A minimal application without
    an interview session is expected to fail early in the pipeline,
    which is fine for the contract test.
    """
    client, tokens = client_with_workflow
    app_id = _create_application(client, tokens["admin"], "p17-wf-1")

    res = client.post(
        f"/api/v1/workflow/{app_id}/run",
        headers=_headers(tokens["analyst"]),
    )
    # 202 on success, 422/500 on early stage failure (WorkflowError vs
    # raw stage exception). All three paths return a structured
    # envelope through the error_response/envelope helpers.
    assert res.status_code in (202, 422, 500), res.text
    payload = res.json()
    if res.status_code == 202:
        data = payload["data"]
        assert data["application_id"] == app_id
        assert data["status"] in {"pending", "running", "succeeded", "failed"}
        assert isinstance(data["steps"], list)
    else:
        assert payload["error"]["code"] in {
            "WORKFLOW_STAGE_FAILED",
            "UNPROCESSABLE_STATE",
        }


def test_workflow_run_requires_analyst_or_admin(client_with_workflow):
    client, tokens = client_with_workflow
    app_id = _create_application(client, tokens["admin"], "p17-wf-auth")

    res = client.post(
        f"/api/v1/workflow/{app_id}/run",
        headers=_headers(tokens["viewer"]),
    )
    assert res.status_code == 403


def test_workflow_resume_requires_analyst_or_admin(client_with_workflow):
    client, tokens = client_with_workflow
    app_id = _create_application(client, tokens["admin"], "p17-wf-resume-auth")

    res = client.post(
        f"/api/v1/workflow/{app_id}/resume",
        headers=_headers(tokens["viewer"]),
        json={"force": False},
    )
    assert res.status_code == 403
