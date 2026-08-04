"""Wire contracts for the V6 natural-interview flow.

The interviewer's output intentionally has only user-visible language and a
session action.  No schema field can carry a stage, target dimension, question
bank candidate, coverage value, or turn budget into runtime orchestration.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


DimensionKey = Literal[
    "problem_definition",
    "evidence_evaluation",
    "reasoning_argumentation",
    "multiple_perspectives",
    "integrative_decision",
    "dynamic_adjustment",
]


class StrictModelOutput(BaseModel):
    """Reject undeclared model fields instead of silently discarding control data."""

    model_config = ConfigDict(extra="forbid")


class NaturalInterviewerOutput(StrictModelOutput):
    interviewer_message: str = Field(min_length=1, max_length=2000)
    session_action: Literal["continue", "finish"]
    finish_reason: Optional[Literal["enough_understanding", "natural_closure", "user_requested"]] = None

    @model_validator(mode="after")
    def finish_reason_matches_action(self) -> "NaturalInterviewerOutput":
        if self.session_action == "continue" and self.finish_reason is not None:
            raise ValueError("finish_reason must be null when session_action is continue")
        if self.session_action == "finish" and self.finish_reason is None:
            raise ValueError("finish_reason is required when session_action is finish")
        return self


class ScoringQuoteOutput(StrictModelOutput):
    turn_index: int = Field(ge=0)
    quote: str = Field(min_length=1, max_length=12000)


class ScoringDimensionOutput(StrictModelOutput):
    dimension_key: DimensionKey
    score: Optional[int] = Field(default=None, ge=1, le=5)
    quotes: list[ScoringQuoteOutput] = Field(default_factory=list, max_length=5)
    reason: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(ge=0, le=1)
    sufficient: bool

    @model_validator(mode="after")
    def score_requires_sufficient_user_evidence(self) -> "ScoringDimensionOutput":
        if self.score is not None and (not self.sufficient or not self.quotes):
            raise ValueError("a numeric score requires sufficient evidence and at least one quote")
        if self.score is None and self.sufficient:
            raise ValueError("sufficient evidence must produce a numeric score")
        return self


class FinalScorerOutput(StrictModelOutput):
    dimensions: list[ScoringDimensionOutput] = Field(min_length=6, max_length=6)
    strengths: list[str] = Field(default_factory=list, max_length=2)
    priorities: list[str] = Field(default_factory=list, max_length=2)

    @model_validator(mode="after")
    def dimensions_are_exactly_the_contract(self) -> "FinalScorerOutput":
        expected = {
            "problem_definition",
            "evidence_evaluation",
            "reasoning_argumentation",
            "multiple_perspectives",
            "integrative_decision",
            "dynamic_adjustment",
        }
        seen = {item.dimension_key for item in self.dimensions}
        if seen != expected or len(seen) != len(self.dimensions):
            raise ValueError("exactly one result is required for each of the six dimensions")
        return self


class ParticipantInput(BaseModel):
    display_name: str = Field(default="", max_length=100)
    identity_type: Literal["student", "professional", "informal", "other"] = "other"
    occupation: str = Field(default="", max_length=200)
    experience_level: str = Field(default="", max_length=100)
    collaboration_role: str = Field(default="", max_length=200)


class CreateSessionRequest(BaseModel):
    consent_version: str = Field(min_length=1, max_length=40)
    consent_given: bool = False
    participant: ParticipantInput = Field(default_factory=ParticipantInput)

    @model_validator(mode="before")
    @classmethod
    def accept_flat_legacy_shape(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "participant" in value:
            return value
        data = dict(value)
        data["consent_given"] = data.get("consent_given", data.get("consent_accepted", False))
        data["participant"] = {
            "display_name": data.get("participant_name", ""),
            "identity_type": data.get("identity_type", "other"),
            "occupation": data.get("occupation", ""),
            "experience_level": data.get("experience_level", ""),
            "collaboration_role": data.get("collaboration_role", ""),
        }
        return data


class SubmitTurnRequest(BaseModel):
    content: str = Field(min_length=1, max_length=12000)
    client_turn_id: str = Field(min_length=8, max_length=80)
    input_mode: Literal["text", "voice", "voice_edited"] = "text"
    answer_duration_ms: int = Field(ge=0, le=86_400_000)
    technical_anomaly: Optional[str] = Field(default=None, max_length=1000)
    # Accept the previous client shape during the isolated migration.
    # Internally it is diagnostic data and never affects interview routing.
    technical_anomalies: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def reject_blank(self) -> "SubmitTurnRequest":
        if not self.content.strip():
            raise ValueError("content must not be blank")
        return self

    @property
    def anomaly_details(self) -> list[str]:
        details = [item.strip() for item in self.technical_anomalies if item.strip()]
        if self.technical_anomaly and self.technical_anomaly.strip():
            details.append(self.technical_anomaly.strip())
        return details[:10]


class ReviewRequest(BaseModel):
    status: Literal["pending", "in_review", "approved", "needs_followup"] = "pending"
    decision: Optional[str] = Field(default=None, max_length=80)
    notes: Optional[str] = Field(default=None, max_length=8000)
    reviewer: Optional[str] = Field(default=None, max_length=120)

    @model_validator(mode="before")
    @classmethod
    def accept_frontend_aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        data["status"] = data.get("status", data.get("review_status", "pending"))
        data["notes"] = data.get("notes", data.get("review_notes"))
        return data


class ExpertScoreInput(BaseModel):
    dimension_key: DimensionKey
    score: Optional[int] = Field(default=None, ge=1, le=5)
    comment: Optional[str] = Field(default=None, max_length=4000)


class ExpertScoresRequest(BaseModel):
    scores: list[ExpertScoreInput] = Field(min_length=1, max_length=6)
    reviewer: str = Field(default="expert", min_length=1, max_length=120)


class ExitRequest(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=1000)
