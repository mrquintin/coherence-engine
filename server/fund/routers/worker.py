"""HTTP-triggered worker endpoints for serverless cron execution."""

from __future__ import annotations

import os
import secrets
from typing import Any, Optional

from fastapi import APIRouter, Header, Query

from coherence_engine.server.fund.api_utils import envelope, error_response, new_request_id
from coherence_engine.server.fund.scoring_worker import process_once


router = APIRouter(prefix="/worker", tags=["worker"])


def _configured_cron_secret() -> str:
    return (
        os.getenv("COHERENCE_FUND_WORKER_CRON_SECRET", "").strip()
        or os.getenv("CRON_SECRET", "").strip()
    )


def _authorized(
    *,
    authorization: Optional[str],
    x_cron_secret: Optional[str],
) -> bool:
    secret = _configured_cron_secret()
    if not secret:
        return False
    bearer = f"Bearer {secret}"
    candidates = [
        authorization.strip() if authorization else "",
        x_cron_secret.strip() if x_cron_secret else "",
    ]
    return any(candidate and secrets.compare_digest(candidate, bearer) for candidate in candidates) or any(
        candidate and secrets.compare_digest(candidate, secret) for candidate in candidates
    )


def _process_scoring_once(
    *,
    max_jobs: int,
    worker_id: Optional[str],
    authorization: Optional[str],
    x_cron_secret: Optional[str],
    x_request_id: Optional[str],
) -> Any:
    request_id = x_request_id or new_request_id()
    if not _configured_cron_secret():
        return error_response(
            request_id,
            503,
            "WORKER_CRON_NOT_CONFIGURED",
            "worker cron secret is not configured",
        )
    if not _authorized(authorization=authorization, x_cron_secret=x_cron_secret):
        return error_response(request_id, 401, "UNAUTHORIZED", "invalid worker cron secret")
    result = process_once(
        max_jobs=max_jobs,
        worker_id=worker_id or "vercel-cron-scoring",
    )
    return envelope(request_id=request_id, data=result)


@router.get("/scoring/process-once")
def process_scoring_once_get(
    max_jobs: int = Query(10, ge=1, le=25),
    worker_id: Optional[str] = Query(default=None),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_cron_secret: Optional[str] = Header(default=None, alias="X-Cron-Secret"),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
) -> Any:
    """Process a bounded batch of queued scoring jobs.

    Vercel Cron invokes this with ``Authorization: Bearer $CRON_SECRET``.
    The ``X-Cron-Secret`` fallback lets platform schedulers without bearer
    support reuse the same endpoint.
    """

    return _process_scoring_once(
        max_jobs=max_jobs,
        worker_id=worker_id,
        authorization=authorization,
        x_cron_secret=x_cron_secret,
        x_request_id=x_request_id,
    )


@router.post("/scoring/process-once")
def process_scoring_once_post(
    max_jobs: int = Query(10, ge=1, le=25),
    worker_id: Optional[str] = Query(default=None),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_cron_secret: Optional[str] = Header(default=None, alias="X-Cron-Secret"),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
) -> Any:
    """Manual/operator equivalent of the GET cron endpoint."""

    return _process_scoring_once(
        max_jobs=max_jobs,
        worker_id=worker_id,
        authorization=authorization,
        x_cron_secret=x_cron_secret,
        x_request_id=x_request_id,
    )
