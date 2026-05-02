"""Admin routes for API key lifecycle management.

Two parallel surfaces live here:

* The legacy ``role``-based endpoints (``POST /``, ``GET /``,
  ``POST /{key_id}/revoke``, ``POST /{key_id}/rotate``) preserve the
  pre-prompt-28 behavior. They now produce v2 Argon2id-hashed keys
  under the hood and inflate the legacy ``role`` into the corresponding
  scope set.
* The v2 endpoints (``/service-accounts``, ``/v2``, ``/v2/{prefix}/…``)
  take an explicit list of scopes, attach the key to a service account,
  and surface ``rate_limit_per_minute``. The plaintext token is returned
  exactly once at create / rotate time.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, Path, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.api_utils import envelope, error_response, new_request_id
from coherence_engine.server.fund.database import get_db
from coherence_engine.server.fund.repositories.api_key_repository import ApiKeyRepository
from coherence_engine.server.fund.schemas.api import CreateApiKeyRequest, RotateApiKeyRequest
from coherence_engine.server.fund.security import enforce_roles, audit_log
from coherence_engine.server.fund.services.api_key_service import (
    ApiKeyService,
    KNOWN_SCOPES,
)
from coherence_engine.server.fund.services.secret_manager import SecretManagerError, get_secret_manager

router = APIRouter(prefix="/admin/api-keys", tags=["admin-api-keys"])


def _sync_token_to_secret_manager(secret_ref: str, token: str) -> None:
    manager = get_secret_manager()
    if manager is None:
        raise SecretManagerError("secret manager provider is not configured")
    manager.put_secret(secret_ref, token)


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Legacy role-based endpoints (kept for back-compat).
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
def create_api_key(
    req: CreateApiKeyRequest,
    request: Request,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    denied = enforce_roles(request, ("admin",))
    if denied:
        return denied
    request_id = x_request_id or new_request_id()
    repo = ApiKeyRepository(db)
    svc = ApiKeyService()
    actor = str(getattr(request.state, "principal", {}).get("fingerprint", "admin"))
    created = svc.create_key(
        repo=repo,
        label=req.label,
        role=req.role,
        created_by=actor,
        expires_in_days=req.expires_in_days,
    )
    if req.write_to_secret_manager:
        if not req.secret_ref:
            return error_response(request_id, 422, "VALIDATION_ERROR", "secret_ref is required when write_to_secret_manager=true")
        try:
            _sync_token_to_secret_manager(req.secret_ref, str(created["token"]))
        except SecretManagerError as exc:
            db.rollback()
            return error_response(request_id, 503, "SECRET_MANAGER_ERROR", str(exc))
    repo.add_audit_event(
        action="api_key_create",
        success=True,
        actor=actor,
        request_id=request_id,
        ip=request.client.host if request.client else "unknown",
        path=request.url.path,
        details={"key_id": created["id"], "role": created["role"], "label": created["label"]},
        api_key_id=created["id"],
    )
    if req.write_to_secret_manager and req.secret_ref:
        repo.add_audit_event(
            action="api_key_secret_synced",
            success=True,
            actor=actor,
            request_id=request_id,
            ip=request.client.host if request.client else "unknown",
            path=request.url.path,
            details={"key_id": created["id"], "secret_ref": req.secret_ref, "operation": "create"},
            api_key_id=created["id"],
        )
    db.commit()
    audit_log("api_key_create", request, "allowed", {"key_id": created["id"], "role": created["role"]})
    return envelope(request_id=request_id, data=created)


@router.get("")
def list_api_keys(
    request: Request,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    denied = enforce_roles(request, ("admin",))
    if denied:
        return denied
    request_id = x_request_id or new_request_id()
    repo = ApiKeyRepository(db)
    keys = repo.list_keys()
    data = []
    for k in keys:
        data.append(
            {
                "id": k.id,
                "label": k.label,
                "role": k.role,
                "is_active": k.is_active,
                "fingerprint": k.key_fingerprint,
                "prefix": k.prefix,
                "scopes": json.loads(k.scopes_json or "[]"),
                "service_account_id": k.service_account_id,
                "rate_limit_per_minute": k.rate_limit_per_minute,
                "created_by": k.created_by,
                "created_at": k.created_at.isoformat(),
                "expires_at": k.expires_at.isoformat() if k.expires_at else None,
                "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None,
                "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
            }
        )
    return envelope(request_id=request_id, data={"keys": data})


@router.post("/{key_id}/revoke")
def revoke_api_key(
    request: Request,
    key_id: str = Path(...),
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    denied = enforce_roles(request, ("admin",))
    if denied:
        return denied
    request_id = x_request_id or new_request_id()
    repo = ApiKeyRepository(db)
    svc = ApiKeyService()
    ok = svc.revoke_key(repo, key_id)
    if not ok:
        return error_response(request_id, 404, "NOT_FOUND", "api key not found")
    actor = str(getattr(request.state, "principal", {}).get("fingerprint", "admin"))
    repo.add_audit_event(
        action="api_key_revoke",
        success=True,
        actor=actor,
        request_id=request_id,
        ip=request.client.host if request.client else "unknown",
        path=request.url.path,
        details={"key_id": key_id},
        api_key_id=key_id,
    )
    db.commit()
    audit_log("api_key_revoke", request, "allowed", {"key_id": key_id})
    return envelope(request_id=request_id, data={"key_id": key_id, "status": "revoked"})


@router.post("/{key_id}/rotate", status_code=201)
def rotate_api_key(
    req: RotateApiKeyRequest,
    request: Request,
    key_id: str = Path(...),
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    denied = enforce_roles(request, ("admin",))
    if denied:
        return denied
    request_id = x_request_id or new_request_id()
    repo = ApiKeyRepository(db)
    svc = ApiKeyService()
    actor = str(getattr(request.state, "principal", {}).get("fingerprint", "admin"))
    rotated = svc.rotate_key(repo, key_id=key_id, actor=actor, expires_in_days=req.expires_in_days)
    if not rotated:
        return error_response(request_id, 404, "NOT_FOUND", "api key not found")
    if req.write_to_secret_manager:
        if not req.secret_ref:
            return error_response(request_id, 422, "VALIDATION_ERROR", "secret_ref is required when write_to_secret_manager=true")
        try:
            _sync_token_to_secret_manager(req.secret_ref, str(rotated["token"]))
        except SecretManagerError as exc:
            db.rollback()
            return error_response(request_id, 503, "SECRET_MANAGER_ERROR", str(exc))
    repo.add_audit_event(
        action="api_key_rotate",
        success=True,
        actor=actor,
        request_id=request_id,
        ip=request.client.host if request.client else "unknown",
        path=request.url.path,
        details={"old_key_id": key_id, "new_key_id": rotated["id"]},
        api_key_id=rotated["id"],
    )
    if req.write_to_secret_manager and req.secret_ref:
        repo.add_audit_event(
            action="api_key_secret_synced",
            success=True,
            actor=actor,
            request_id=request_id,
            ip=request.client.host if request.client else "unknown",
            path=request.url.path,
            details={"old_key_id": key_id, "new_key_id": rotated["id"], "secret_ref": req.secret_ref, "operation": "rotate"},
            api_key_id=rotated["id"],
        )
    db.commit()
    audit_log("api_key_rotate", request, "allowed", {"old_key_id": key_id, "new_key_id": rotated["id"]})
    return envelope(request_id=request_id, data=rotated)


# ---------------------------------------------------------------------------
# v2 surface — service accounts + scope-based keys (prompt 28).
# ---------------------------------------------------------------------------


class CreateServiceAccountRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    owner_email: Optional[str] = None


class CreateApiKeyV2Request(BaseModel):
    service_account_name: str = Field(min_length=1, max_length=128)
    scopes: List[str] = Field(min_length=1)
    label: str = ""
    expires_at: Optional[datetime] = None
    rate_limit_per_minute: int = 60


class RotateApiKeyV2Request(BaseModel):
    grace_seconds: int = 0
    expires_at: Optional[datetime] = None


def _serialize_account(acct: models.ServiceAccount) -> dict:
    return {
        "id": acct.id,
        "name": acct.name,
        "description": acct.description,
        "owner_email": acct.owner_email,
        "created_at": acct.created_at.isoformat() if acct.created_at else None,
    }


def _serialize_key_public(rec: models.ApiKey) -> dict:
    """Operator-facing key view; never includes plaintext."""
    return {
        "id": rec.id,
        "service_account_id": rec.service_account_id,
        "prefix": rec.prefix,
        "scopes": json.loads(rec.scopes_json or "[]"),
        "label": rec.label,
        "rate_limit_per_minute": rec.rate_limit_per_minute,
        "expires_at": rec.expires_at.isoformat() if rec.expires_at else None,
        "revoked_at": rec.revoked_at.isoformat() if rec.revoked_at else None,
        "last_used_at": rec.last_used_at.isoformat() if rec.last_used_at else None,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
    }


@router.post("/service-accounts", status_code=201)
def create_service_account(
    req: CreateServiceAccountRequest,
    request: Request,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    denied = enforce_roles(request, ("admin",))
    if denied:
        return denied
    request_id = x_request_id or new_request_id()
    existing = db.execute(
        select(models.ServiceAccount).where(models.ServiceAccount.name == req.name)
    ).scalar_one_or_none()
    if existing is not None:
        return error_response(
            request_id, 409, "CONFLICT", f"service account {req.name!r} already exists"
        )
    acct = models.ServiceAccount(
        id=_new_id("sa"),
        name=req.name,
        description=req.description or "",
        owner_email=str(req.owner_email) if req.owner_email else "",
    )
    db.add(acct)
    db.flush()
    actor = str(getattr(request.state, "principal", {}).get("fingerprint", "admin"))
    repo = ApiKeyRepository(db)
    repo.add_audit_event(
        action="service_account_create",
        success=True,
        actor=actor,
        request_id=request_id,
        ip=request.client.host if request.client else "unknown",
        path=request.url.path,
        details={"service_account_id": acct.id, "name": acct.name},
    )
    db.commit()
    audit_log(
        "service_account_create",
        request,
        "allowed",
        {"service_account_id": acct.id, "name": acct.name},
    )
    return envelope(request_id=request_id, data=_serialize_account(acct))


@router.get("/service-accounts")
def list_service_accounts(
    request: Request,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    denied = enforce_roles(request, ("admin",))
    if denied:
        return denied
    request_id = x_request_id or new_request_id()
    accounts = (
        db.execute(
            select(models.ServiceAccount).order_by(models.ServiceAccount.name)
        )
        .scalars()
        .all()
    )
    return envelope(
        request_id=request_id,
        data={"service_accounts": [_serialize_account(a) for a in accounts]},
    )


@router.post("/v2", status_code=201)
def create_api_key_v2(
    req: CreateApiKeyV2Request,
    request: Request,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    denied = enforce_roles(request, ("admin",))
    if denied:
        return denied
    request_id = x_request_id or new_request_id()
    unknown = [s for s in req.scopes if s not in KNOWN_SCOPES]
    if unknown:
        return error_response(
            request_id,
            422,
            "VALIDATION_ERROR",
            f"unknown scope(s): {', '.join(sorted(unknown))}",
        )
    acct = db.execute(
        select(models.ServiceAccount).where(
            models.ServiceAccount.name == req.service_account_name
        )
    ).scalar_one_or_none()
    if acct is None:
        return error_response(
            request_id,
            404,
            "NOT_FOUND",
            f"service account {req.service_account_name!r} not found",
        )
    actor = str(getattr(request.state, "principal", {}).get("fingerprint", "admin"))
    svc = ApiKeyService()
    created = svc.create_key_v2(
        db,
        service_account_id=acct.id,
        scopes=req.scopes,
        created_by=actor,
        label=req.label,
        expires_at=req.expires_at,
        rate_limit_per_minute=req.rate_limit_per_minute,
    )
    repo = ApiKeyRepository(db)
    repo.add_audit_event(
        action="api_key_v2_create",
        success=True,
        actor=actor,
        request_id=request_id,
        ip=request.client.host if request.client else "unknown",
        path=request.url.path,
        details={
            "key_id": created.id,
            "prefix": created.prefix,
            "service_account_id": acct.id,
            "scopes": list(created.scopes),
        },
        api_key_id=created.id,
    )
    db.commit()
    audit_log(
        "api_key_v2_create",
        request,
        "allowed",
        {"key_id": created.id, "prefix": created.prefix, "scopes": list(created.scopes)},
    )
    return envelope(
        request_id=request_id,
        data={
            "id": created.id,
            "prefix": created.prefix,
            "token": created.token,
            "service_account_id": created.service_account_id,
            "scopes": list(created.scopes),
            "expires_at": created.expires_at.isoformat() if created.expires_at else None,
            "rate_limit_per_minute": created.rate_limit_per_minute,
            "warning": "Plaintext token shown once and never again. Store it now.",
        },
    )


@router.get("/v2")
def list_api_keys_v2(
    request: Request,
    db: Session = Depends(get_db),
    service_account_name: Optional[str] = Query(default=None),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    denied = enforce_roles(request, ("admin",))
    if denied:
        return denied
    request_id = x_request_id or new_request_id()
    stmt = select(models.ApiKey).order_by(models.ApiKey.created_at.desc())
    if service_account_name:
        acct = db.execute(
            select(models.ServiceAccount).where(
                models.ServiceAccount.name == service_account_name
            )
        ).scalar_one_or_none()
        if acct is None:
            return envelope(request_id=request_id, data={"keys": []})
        stmt = stmt.where(models.ApiKey.service_account_id == acct.id)
    rows = db.execute(stmt).scalars().all()
    return envelope(
        request_id=request_id,
        data={"keys": [_serialize_key_public(r) for r in rows]},
    )


@router.post("/v2/{prefix}/revoke")
def revoke_api_key_v2(
    request: Request,
    prefix: str = Path(...),
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    denied = enforce_roles(request, ("admin",))
    if denied:
        return denied
    request_id = x_request_id or new_request_id()
    svc = ApiKeyService()
    rec = svc.revoke_key_v2(db, prefix)
    if rec is None:
        return error_response(request_id, 404, "NOT_FOUND", "api key not found")
    actor = str(getattr(request.state, "principal", {}).get("fingerprint", "admin"))
    repo = ApiKeyRepository(db)
    repo.add_audit_event(
        action="api_key_v2_revoke",
        success=True,
        actor=actor,
        request_id=request_id,
        ip=request.client.host if request.client else "unknown",
        path=request.url.path,
        details={"prefix": prefix, "key_id": rec.id},
        api_key_id=rec.id,
    )
    db.commit()
    audit_log("api_key_v2_revoke", request, "allowed", {"prefix": prefix, "key_id": rec.id})
    return envelope(
        request_id=request_id, data={"prefix": prefix, "status": "revoked"}
    )


@router.post("/v2/{prefix}/rotate", status_code=201)
def rotate_api_key_v2(
    req: RotateApiKeyV2Request,
    request: Request,
    prefix: str = Path(...),
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    denied = enforce_roles(request, ("admin",))
    if denied:
        return denied
    request_id = x_request_id or new_request_id()
    actor = str(getattr(request.state, "principal", {}).get("fingerprint", "admin"))
    svc = ApiKeyService()
    new_key = svc.rotate_key_v2(
        db,
        prefix=prefix,
        actor=actor,
        grace_seconds=int(req.grace_seconds or 0),
        expires_at=req.expires_at,
    )
    if new_key is None:
        return error_response(request_id, 404, "NOT_FOUND", "api key not found")
    repo = ApiKeyRepository(db)
    repo.add_audit_event(
        action="api_key_v2_rotate",
        success=True,
        actor=actor,
        request_id=request_id,
        ip=request.client.host if request.client else "unknown",
        path=request.url.path,
        details={
            "old_prefix": prefix,
            "new_prefix": new_key.prefix,
            "new_key_id": new_key.id,
            "grace_seconds": int(req.grace_seconds or 0),
        },
        api_key_id=new_key.id,
    )
    db.commit()
    audit_log(
        "api_key_v2_rotate",
        request,
        "allowed",
        {"old_prefix": prefix, "new_prefix": new_key.prefix},
    )
    return envelope(
        request_id=request_id,
        data={
            "id": new_key.id,
            "prefix": new_key.prefix,
            "token": new_key.token,
            "service_account_id": new_key.service_account_id,
            "scopes": list(new_key.scopes),
            "expires_at": new_key.expires_at.isoformat() if new_key.expires_at else None,
            "rate_limit_per_minute": new_key.rate_limit_per_minute,
            "old_prefix": prefix,
            "warning": "Plaintext token shown once and never again. Store it now.",
        },
    )
