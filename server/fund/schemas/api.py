"""API schemas for fund endpoints."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class Channel(str, Enum):
    phone = "phone"
    web_voice = "web_voice"
    async_voice = "async_voice"


class DecisionStatus(str, Enum):
    pass_ = "pass"
    fail = "fail"
    manual_review = "manual_review"
    pending = "pending"


class FounderInput(BaseModel):
    full_name: str
    email: str
    company_name: str
    country: str = Field(min_length=2)


class StartupInput(BaseModel):
    one_liner: str
    requested_check_usd: int = Field(gt=0)
    use_of_funds_summary: str
    preferred_channel: Channel


class ConsentInput(BaseModel):
    ai_assessment: bool
    recording: bool
    data_processing: bool


class CreateApplicationRequest(BaseModel):
    founder: FounderInput
    startup: StartupInput
    consent: ConsentInput


class CreateInterviewSessionRequest(BaseModel):
    channel: Channel
    locale: str


class TriggerScoringRequest(BaseModel):
    mode: str = Field(pattern="^(standard|priority)$")
    dry_run: bool = False
    transcript_text: str | None = None
    transcript_uri: str | None = None


class CreateEscalationPacketRequest(BaseModel):
    partner_email: str
    include_calendar_link: bool = True


class FailedGate(BaseModel):
    gate: str
    reason_code: str


class ErrorObject(BaseModel):
    code: str
    message: str
    details: List[Dict[str, Any]] = Field(default_factory=list)


class MetaObject(BaseModel):
    request_id: str


class Envelope(BaseModel):
    data: Optional[Dict[str, Any]]
    error: Optional[ErrorObject]
    meta: MetaObject


class DecisionArtifactResponse(BaseModel):
    application_id: str
    decision_id: str
    decision: str
    policy_version: str
    threshold_required: float
    coherence_observed: float
    margin: float
    failed_gates: List[FailedGate]
    updated_at: datetime


class CreateApiKeyRequest(BaseModel):
    label: str
    role: str = Field(pattern="^(viewer|analyst|admin)$")
    expires_in_days: int | None = None
    write_to_secret_manager: bool = False
    secret_ref: str | None = None


class RotateApiKeyRequest(BaseModel):
    expires_in_days: int | None = None
    write_to_secret_manager: bool = False
    secret_ref: str | None = None

