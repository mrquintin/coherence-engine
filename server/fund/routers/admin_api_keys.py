"""Admin routes for API key lifecycle management."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, Path, Request
from sqlalchemy.orm import Session

from coherence_engine.server.fund.api_utils import envelope, error_response, new_request_id
from coherence_engine.server.fund.database import get_db
from coherence_engine.server.fund.repositories.api_key_repository import ApiKeyRepository
from coherence_engine.server.fund.schemas.api import CreateApiKeyRequest, RotateApiKeyRequest
from coherence_engine.server.fund.security import enforce_roles, audit_log
from coherence_engine.server.fund.services.api_key_service import ApiKeyService
from coherence_engine.server.fund.services.secret_manager import SecretManagerError, get_secret_manager

router = APIRouter(prefix="/admin/api-keys", tags=["admin-api-keys"])


def _sync_token_to_secret_manager(secret_ref: str, token: str) -> None:
    manager = get_secret_manager()
    if manager is None:
        raise SecretManagerError("secret manager provider is not configured")
    manager.put_secret(secret_ref, token)


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

