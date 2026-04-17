"""Shared fixtures for offline integration tests.

Exposes an in-memory app factory (``e2e_app_factory``) used by the
end-to-end reproducibility test (``test_e2e_pipeline.py``). The
factory resets the fund SQLAlchemy schema to a known-empty state
before each test run (matching the pattern used in
``tests/test_workflow_orchestrator.py``) so no test state leaks
across the integration suite.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import pytest

from coherence_engine.server.fund import models
from coherence_engine.server.fund.database import Base, SessionLocal, engine


_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _load_fixture(name: str) -> Dict[str, Any]:
    path = _FIXTURES_DIR / name
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def e2e_fixtures_dir() -> Path:
    """Directory containing offline integration-test fixtures."""
    return _FIXTURES_DIR


@pytest.fixture
def reset_fund_schema():
    """Drop + recreate all fund ORM tables for a clean integration run.

    This is functionally equivalent to running Alembic migrations to
    head against a throwaway SQLite DB: the ``Base.metadata`` object
    is populated from the same SQLAlchemy models the migrations
    target, so ``create_all`` yields the same schema as
    ``alembic upgrade head`` for schema-only purposes. Production DB
    credentials are never touched — the fund engine points at a
    SQLite file by default under test configuration.
    """
    os.environ.setdefault("COHERENCE_FUND_AUTH_MODE", "db")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


@pytest.fixture
def e2e_app_factory(reset_fund_schema) -> Callable[..., Dict[str, Any]]:
    """Return a callable that seeds an Application from the e2e fixtures.

    The factory produces a fresh (founder, application) pair per call
    with a unique ``application_id`` so the same fixtures can be used
    to run the pipeline multiple times inside one test (for
    reproducibility comparison) without colliding on primary keys.

    Returns:
        A callable with signature ``factory(*, suffix: str = "",
        app_id_override: Optional[str] = None, founder_id_override:
        Optional[str] = None) -> dict`` that returns a dict with keys
        ``application_id``, ``founder_id``, ``one_liner``,
        ``requested_check_usd``, ``domain_primary``, and
        ``compliance_status`` — the operator-readable snapshot of
        what was seeded. The Application + Founder rows are added to
        ``SessionLocal()`` and committed so downstream service calls
        (workflow orchestrator, scoring, decision policy) see them.
    """
    app_fixture = _load_fixture("e2e_application.json")
    transcript_fixture = _load_fixture("e2e_transcript.json")

    def _factory(
        *,
        suffix: str = "",
        app_id_override: Optional[str] = None,
        founder_id_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        f = app_fixture["founder"]
        a = app_fixture["application"]
        suffix_token = suffix or uuid.uuid4().hex[:8]
        app_id = app_id_override or f"app_{a['id_seed']}_{suffix_token}"
        founder_id = founder_id_override or f"{f['id']}_{suffix_token}"

        session = SessionLocal()
        try:
            founder = models.Founder(
                id=founder_id,
                full_name=str(f["full_name"]),
                email=f"{suffix_token}+{f['email']}",
                company_name=str(f["company_name"]),
                country=str(f["country"]),
            )
            app = models.Application(
                id=app_id,
                founder_id=founder_id,
                one_liner=str(a["one_liner"]),
                requested_check_usd=int(a["requested_check_usd"]),
                use_of_funds_summary=str(a["use_of_funds_summary"]),
                preferred_channel=str(a["preferred_channel"]),
                transcript_text=str(transcript_fixture["transcript_text"]),
                domain_primary=str(a["domain_primary"]),
                compliance_status=str(a["compliance_status"]),
                status="scoring_queued",
                scoring_mode=str(a.get("scoring_mode", "enforce")),
            )
            session.add_all([founder, app])
            session.commit()
            return {
                "application_id": app_id,
                "founder_id": founder_id,
                "one_liner": app.one_liner,
                "requested_check_usd": app.requested_check_usd,
                "domain_primary": app.domain_primary,
                "compliance_status": app.compliance_status,
                "transcript_text": app.transcript_text,
            }
        finally:
            session.close()

    return _factory
