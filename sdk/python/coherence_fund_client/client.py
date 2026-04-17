"""Auto-generated Coherence Fund Python SDK client.

DO NOT EDIT BY HAND. Regenerate via::

    python scripts/generate_sdk_stubs.py

Source spec : docs/specs/openapi_v1.yaml
Spec SHA-256: 99c7ccb145bfadf48e7b2f63b5241182933d4c9103333e8adc104ef1e8abdd24
Generator   : 1.0
Operations  : 18

The client depends only on the Python standard library so it can be
vendored into environments without ``requests``. Each endpoint method
is named ``<tag>_<operationId_snake>`` and returns the parsed JSON
response body. Non-2xx responses raise :class:`CoherenceFundClientError`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Mapping, Optional


class CoherenceFundClientError(Exception):
    """Raised on non-2xx responses from the Coherence Fund API."""

    def __init__(self, status: int, body: Any, message: str = "") -> None:
        super().__init__(message or f"HTTP {status}")
        self.status = status
        self.body = body


class CoherenceFundClient:
    """Typed stdlib-only client for the Coherence Fund Orchestrator API.

    The ``base_url`` should include the API server prefix (e.g.
    ``https://fund.example.com/api/v1``). All methods return the
    decoded JSON envelope produced by the server.
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: Optional[str] = None,
        bearer_token: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.bearer_token = bearer_token
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, Any]] = None,
        json_body: Any = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        if query:
            cleaned = {k: v for k, v in query.items() if v is not None}
            if cleaned:
                url = f"{url}?{urllib.parse.urlencode(cleaned, doseq=True)}"
        merged: Dict[str, str] = {"Accept": "application/json"}
        if self.api_key:
            merged["X-API-Key"] = self.api_key
        if self.bearer_token:
            merged["Authorization"] = f"Bearer {self.bearer_token}"
        if headers:
            for k, v in headers.items():
                if v is None:
                    continue
                merged[str(k)] = str(v)
        data_bytes: Optional[bytes] = None
        if json_body is not None:
            merged.setdefault("Content-Type", "application/json")
            data_bytes = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data_bytes, method=method, headers=merged)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if not raw:
                    return {}
                try:
                    return json.loads(raw.decode("utf-8"))
                except ValueError:
                    return {"raw": raw.decode("utf-8", errors="replace")}
        except urllib.error.HTTPError as exc:
            body_bytes = b""
            try:
                body_bytes = exc.read()
            except Exception:
                body_bytes = b""
            try:
                body = json.loads(body_bytes.decode("utf-8")) if body_bytes else None
            except ValueError:
                body = {"raw": body_bytes.decode("utf-8", errors="replace")}
            raise CoherenceFundClientError(exc.code, body, f"HTTP {exc.code}") from exc


    def admin_create_api_key(self, *, body: Any) -> Dict[str, Any]:
        """Create API key

        HTTP POST /admin/api-keys

        operationId: createApiKey

        required roles: admin
        """
        path = "/admin/api-keys"
        return self._request("POST", path, query=None, headers=None, json_body=body)

    def applications_create_application(self, *, body: Any, idempotency_key: Any) -> Dict[str, Any]:
        """Create application intake

        HTTP POST /applications

        operationId: createApplication

        required roles: analyst, admin
        """
        path = "/applications"
        headers: Dict[str, Any] = {
            "Idempotency-Key": idempotency_key,
        }
        return self._request("POST", path, query=None, headers=headers, json_body=body)

    def applications_create_escalation_packet(self, application_id: str, *, body: Any, idempotency_key: Any) -> Dict[str, Any]:
        """Create escalation packet

        HTTP POST /applications/{application_id}/escalation-packet

        operationId: createEscalationPacket

        required roles: admin
        """
        path = "/applications/{application_id}/escalation-packet".format(application_id=application_id)
        headers: Dict[str, Any] = {
            "Idempotency-Key": idempotency_key,
        }
        return self._request("POST", path, query=None, headers=headers, json_body=body)

    def applications_create_interview_session(self, application_id: str, *, body: Any, idempotency_key: Any) -> Dict[str, Any]:
        """Start interview session

        HTTP POST /applications/{application_id}/interview-sessions

        operationId: createInterviewSession

        required roles: analyst, admin
        """
        path = "/applications/{application_id}/interview-sessions".format(application_id=application_id)
        headers: Dict[str, Any] = {
            "Idempotency-Key": idempotency_key,
        }
        return self._request("POST", path, query=None, headers=headers, json_body=body)

    def applications_get_decision(self, application_id: str) -> Dict[str, Any]:
        """Get latest decision artifact

        HTTP GET /applications/{application_id}/decision

        operationId: getDecision

        required roles: viewer, analyst, admin
        """
        path = "/applications/{application_id}/decision".format(application_id=application_id)
        return self._request("GET", path, query=None, headers=None, json_body=None)

    def applications_get_decision_artifact(self, application_id: str) -> Dict[str, Any]:
        """Fetch the persisted decision_artifact.v1 bundle

        HTTP GET /applications/{application_id}/decision_artifact

        operationId: getDecisionArtifact

        required roles: viewer, analyst, admin
        """
        path = "/applications/{application_id}/decision_artifact".format(application_id=application_id)
        return self._request("GET", path, query=None, headers=None, json_body=None)

    def health_get_health(self) -> Dict[str, Any]:
        """Health check

        HTTP GET /health

        operationId: getHealth

        required roles: public
        """
        path = "/health"
        return self._request("GET", path, query=None, headers=None, json_body=None)

    def live_get_live(self) -> Dict[str, Any]:
        """Liveness check

        HTTP GET /live

        operationId: getLive

        required roles: public
        """
        path = "/live"
        return self._request("GET", path, query=None, headers=None, json_body=None)

    def ready_get_ready(self) -> Dict[str, Any]:
        """Readiness check

        HTTP GET /ready

        operationId: getReady

        required roles: public
        """
        path = "/ready"
        return self._request("GET", path, query=None, headers=None, json_body=None)

    def secret_manager_get_secret_manager_ready(self) -> Dict[str, Any]:
        """Secret manager startup and reachability status

        HTTP GET /secret-manager/ready

        operationId: getSecretManagerReady

        required roles: public
        """
        path = "/secret-manager/ready"
        return self._request("GET", path, query=None, headers=None, json_body=None)

    def admin_list_api_keys(self) -> Dict[str, Any]:
        """List API keys

        HTTP GET /admin/api-keys

        operationId: listApiKeys

        required roles: admin
        """
        path = "/admin/api-keys"
        return self._request("GET", path, query=None, headers=None, json_body=None)

    def notifications_list_notifications(self, *, application_id: Any, limit: Optional[Any] = None, offset: Optional[Any] = None) -> Dict[str, Any]:
        """List notification log entries for an application

        HTTP GET /notifications

        operationId: listNotifications

        required roles: viewer, analyst, admin
        """
        path = "/notifications"
        query: Dict[str, Any] = {
            "application_id": application_id,
            "limit": limit,
            "offset": offset,
        }
        return self._request("GET", path, query=query, headers=None, json_body=None)

    def workflow_resume_workflow(self, application_id: str, *, body: Any = None) -> Dict[str, Any]:
        """Resume the most recent non-succeeded workflow run

        HTTP POST /workflow/{application_id}/resume

        operationId: resumeWorkflow

        required roles: analyst, admin
        """
        path = "/workflow/{application_id}/resume".format(application_id=application_id)
        return self._request("POST", path, query=None, headers=None, json_body=body)

    def admin_revoke_api_key(self, key_id: str) -> Dict[str, Any]:
        """Revoke API key

        HTTP POST /admin/api-keys/{key_id}/revoke

        operationId: revokeApiKey

        required roles: admin
        """
        path = "/admin/api-keys/{key_id}/revoke".format(key_id=key_id)
        return self._request("POST", path, query=None, headers=None, json_body=None)

    def admin_rotate_api_key(self, key_id: str, *, body: Any) -> Dict[str, Any]:
        """Rotate API key

        HTTP POST /admin/api-keys/{key_id}/rotate

        operationId: rotateApiKey

        required roles: admin
        """
        path = "/admin/api-keys/{key_id}/rotate".format(key_id=key_id)
        return self._request("POST", path, query=None, headers=None, json_body=body)

    def workflow_run_workflow(self, application_id: str) -> Dict[str, Any]:
        """Start a fresh workflow orchestrator run

        HTTP POST /workflow/{application_id}/run

        operationId: runWorkflow

        required roles: analyst, admin
        """
        path = "/workflow/{application_id}/run".format(application_id=application_id)
        return self._request("POST", path, query=None, headers=None, json_body=None)

    def applications_set_scoring_mode(self, application_id: str, *, body: Any) -> Dict[str, Any]:
        """Toggle scoring mode (enforce <-> shadow)

        HTTP POST /applications/{application_id}/mode

        operationId: setScoringMode

        required roles: admin
        """
        path = "/applications/{application_id}/mode".format(application_id=application_id)
        return self._request("POST", path, query=None, headers=None, json_body=body)

    def applications_trigger_scoring(self, application_id: str, *, body: Any, idempotency_key: Any) -> Dict[str, Any]:
        """Trigger scoring pipeline

        HTTP POST /applications/{application_id}/score

        operationId: triggerScoring

        required roles: analyst, admin
        """
        path = "/applications/{application_id}/score".format(application_id=application_id)
        headers: Dict[str, Any] = {
            "Idempotency-Key": idempotency_key,
        }
        return self._request("POST", path, query=None, headers=headers, json_body=body)
