"""Pydantic models for the starter fund orchestrator API."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class Channel(str, Enum):
    PHONE = "phone"
    WEB_VOICE = "web_voice"
    ASYNC_VOICE = "async_voice"


class DecisionStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    MANUAL_REVIEW = "manual_review"
    PENDING = "pending"


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
    mode: str
    dry_run: bool


class CreateEscalationPacketRequest(BaseModel):
    partner_email: str
    include_calendar_link: bool


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


class DecisionArtifact(BaseModel):
    application_id: str
    decision_id: str
    decision: DecisionStatus
    policy_version: str
    threshold_required: float
    coherence_observed: float
    margin: float
    failed_gates: List[FailedGate]
    updated_at: datetime

