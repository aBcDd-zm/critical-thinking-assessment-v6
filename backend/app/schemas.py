"""Wire contracts for the V6 natural-interview flow.

The interviewer's output intentionally has only user-visible language and a
session action.  No schema field can carry a stage, target dimension, question
bank candidate, coverage value, or turn budget into runtime orchestration.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


DimensionKey = Literal[
    "problem_definition",
    "evidence_evaluation",
    "reasoning_argumentation",
    "multiple_perspectives",
    "integrative_decision",
    "dynamic_adjustment",
]


MIN_ANSWER_VISIBLE_CHARACTERS = 20

_EXPLICIT_UNCERTAINTY_PATTERN = re.compile(
    r"^(?:我)?(?:现在|暂时|目前|还|也|确实|真的){0,2}"
    r"(?:不知道(?:(?:该|要)?怎么(?:说|回答))?|"
    r"不清楚|不太清楚|不确定|不太确定|"
    r"没想好|没有想好|没想法|没有想法|"
    r"没什么想法|没有什么想法|想不到|说不上来|不会回答)"
    r"(?:了|呢|啊|吧)?$"
)


def visible_character_count(value: str) -> int:
    """Count user-visible characters while ignoring spaces and line breaks."""

    return sum(1 for character in value if not character.isspace())


def normalized_short_answer_text(value: str) -> str:
    """Normalize a short intent without treating punctuation as content."""

    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if not character.isspace()
        and not unicodedata.category(character).startswith("P")
    )


def is_explicit_uncertainty_answer(value: str) -> bool:
    """Allow only a complete, conservative expression of uncertainty.

    Full-match semantics prevent a short substantive answer containing words
    such as ``不知道`` from bypassing the normal answer-length check.
    """

    return bool(
        _EXPLICIT_UNCERTAINTY_PATTERN.fullmatch(normalized_short_answer_text(value))
    )


class StrictModelOutput(BaseModel):
    """Reject undeclared model fields instead of silently discarding control data."""

    model_config = ConfigDict(extra="forbid")


class DecisionAnchorOutput(StrictModelOutput):
    """Exact participant span that anchors the interview's decision thread."""

    turn_index: int = Field(ge=0)
    quote: str = Field(min_length=1, max_length=12000)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text_hash: Optional[str] = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def offsets_bound_the_quote(self) -> "DecisionAnchorOutput":
        if self.end <= self.start:
            raise ValueError("decision anchor end must be greater than start")
        if self.end - self.start != len(self.quote):
            raise ValueError("decision anchor offsets must exactly bound quote")
        return self


class NaturalInterviewNavigation(StrictModelOutput):
    """Private audit navigation; never a dimension or scripted-stage controller."""

    decision_anchor: DecisionAnchorOutput
    focus_kind: Literal[
        "decision_problem",
        "basis",
        "tradeoff",
        "action",
        "outcome",
        "adjustment",
        "source_ownership",
        "other",
    ]
    mainline_relation: Literal[
        "core",
        "branch",
        "return",
        "source_clarification",
        "user_switch",
    ]


class NaturalInterviewerOutput(StrictModelOutput):
    interviewer_message: str = Field(min_length=1, max_length=2000)
    session_action: Literal["continue", "finish"]
    finish_reason: Optional[Literal["enough_understanding", "natural_closure", "user_requested"]] = None
    navigation: Optional[NaturalInterviewNavigation] = None

    @model_validator(mode="before")
    @classmethod
    def normalize_json_null_string(cls, value: Any) -> Any:
        """Accept the provider's occasional JSON-string spelling of null.

        DeepSeek sometimes emits ``"finish_reason": "null"`` even while using
        JSON mode.  It is unambiguous only for this nullable protocol field, so
        normalize that exact representation before applying the strict action
        and finish-reason contract below.
        """

        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        finish_reason = normalized.get("finish_reason")
        if isinstance(finish_reason, str) and finish_reason.strip().casefold() == "null":
            normalized["finish_reason"] = None
        return normalized

    @model_validator(mode="after")
    def finish_reason_matches_action(self) -> "NaturalInterviewerOutput":
        if self.session_action == "continue" and self.finish_reason is not None:
            raise ValueError("finish_reason must be null when session_action is continue")
        if self.session_action == "finish" and self.finish_reason is None:
            raise ValueError("finish_reason is required when session_action is finish")
        return self


class EvidenceAttributionSpanOutput(StrictModelOutput):
    """One exact, model-classified span from a participant turn.

    Eligibility is deliberately absent.  The server validates the source turn,
    offsets, hash, overlap, and asset provenance before applying its own
    eligibility rules.
    """

    turn_index: int = Field(ge=0)
    quote: str = Field(min_length=1, max_length=12000)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text_hash: Optional[str] = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    owner: Literal[
        "participant_owned",
        "external_quoted",
        "external_paraphrased",
        "uncertain",
    ]
    relation: Literal[
        "own_reasoning",
        "endorses",
        "critiques",
        "rejects",
        "quotes_only",
        "asks_or_requests",
    ]
    elicitation_level: Literal[
        "spontaneous",
        "open_probe",
        "focused_probe",
        "strong_scaffold",
    ]
    source_label: Optional[str] = Field(default=None, max_length=500)
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def offsets_bound_the_quote(self) -> "EvidenceAttributionSpanOutput":
        if self.end <= self.start:
            raise ValueError("attribution span end must be greater than start")
        if self.end - self.start != len(self.quote):
            raise ValueError("attribution span offsets must exactly bound quote")
        return self


class EvidenceAttributionOutput(StrictModelOutput):
    spans: list[EvidenceAttributionSpanOutput] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def spans_do_not_overlap(self) -> "EvidenceAttributionOutput":
        previous_end_by_turn: dict[int, int] = {}
        for span in sorted(self.spans, key=lambda item: (item.turn_index, item.start, item.end)):
            previous_end = previous_end_by_turn.get(span.turn_index)
            if previous_end is not None and span.start < previous_end:
                raise ValueError("attribution spans must not overlap within a turn")
            previous_end_by_turn[span.turn_index] = span.end
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

    @model_validator(mode="before")
    @classmethod
    def bound_public_summary_lists(cls, value: Any) -> Any:
        """Normalize bounded, equivalent JSON shapes from the provider.

        The public contract remains a six-item list.  JSON-mode providers may
        instead key that list by the six dimension names; converting that
        mechanically preserves every score and quote while still letting the
        post-validator reject missing, duplicated, or unknown dimensions.
        """

        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        dimension_keys = (
            "problem_definition",
            "evidence_evaluation",
            "reasoning_argumentation",
            "multiple_perspectives",
            "integrative_decision",
            "dynamic_adjustment",
        )
        dimensions = normalized.get("dimensions")
        if dimensions is None and any(key in normalized for key in dimension_keys):
            dimensions = {key: normalized.pop(key) for key in dimension_keys if key in normalized}
        if isinstance(dimensions, dict):
            normalized_dimensions: list[Any] = []
            for key in dimension_keys:
                item = dimensions.get(key)
                if not isinstance(item, dict):
                    continue
                normalized_item = dict(item)
                normalized_item.setdefault("dimension_key", key)
                normalized_dimensions.append(normalized_item)
            normalized["dimensions"] = normalized_dimensions
        elif isinstance(dimensions, list):
            normalized_dimensions = []
            for item in dimensions:
                if not isinstance(item, dict):
                    normalized_dimensions.append(item)
                    continue
                normalized_item = dict(item)
                dimension_key = normalized_item.get("dimension_key")
                for alias in ("name", "dimension", "key"):
                    provider_key = normalized_item.get(alias)
                    if (
                        dimension_key is None
                        and isinstance(provider_key, str)
                        and provider_key in dimension_keys
                    ):
                        dimension_key = provider_key
                        normalized_item["dimension_key"] = provider_key
                        normalized_item.pop(alias, None)
                    elif provider_key == dimension_key:
                        normalized_item.pop(alias, None)
                observation = normalized_item.pop("observation", None)
                if "reason" not in normalized_item and isinstance(observation, str):
                    normalized_item["reason"] = observation
                normalized_dimensions.append(normalized_item)
            normalized["dimensions"] = normalized_dimensions
        for field_name in ("strengths", "priorities"):
            items = normalized.get(field_name)
            if isinstance(items, list) and len(items) > 2:
                normalized[field_name] = items[:2]
        return normalized

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


class EvidenceReferenceOutput(StrictModelOutput):
    """Reference to a server-validated, eligible attribution span."""

    attribution_span_id: int = Field(ge=1)


class AttributedScoringDimensionOutput(StrictModelOutput):
    dimension_key: DimensionKey
    score: Optional[int] = Field(default=None, ge=1, le=5)
    evidence_refs: list[EvidenceReferenceOutput] = Field(default_factory=list, max_length=5)
    reason: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(ge=0, le=1)
    sufficient: bool

    @model_validator(mode="after")
    def score_requires_sufficient_attributed_evidence(
        self,
    ) -> "AttributedScoringDimensionOutput":
        if self.score is not None and (not self.sufficient or not self.evidence_refs):
            raise ValueError(
                "a numeric score requires sufficient evidence and at least one attribution span"
            )
        if self.score is None and self.sufficient:
            raise ValueError("sufficient evidence must produce a numeric score")
        reference_ids = [item.attribution_span_id for item in self.evidence_refs]
        if len(reference_ids) != len(set(reference_ids)):
            raise ValueError("attribution span references must be unique within a dimension")
        return self


class SummaryWithEvidenceOutput(StrictModelOutput):
    text: str = Field(min_length=1, max_length=2000)
    attribution_span_ids: list[int] = Field(min_length=1, max_length=5)

    @field_validator("attribution_span_ids")
    @classmethod
    def evidence_ids_are_positive_and_unique(cls, value: list[int]) -> list[int]:
        if any(item < 1 for item in value):
            raise ValueError("attribution span ids must be positive")
        if len(value) != len(set(value)):
            raise ValueError("attribution span ids must be unique")
        return value


class AttributedFinalScorerOutput(StrictModelOutput):
    """V6.2.2 scoring contract that cannot introduce free-text evidence."""

    dimensions: list[AttributedScoringDimensionOutput] = Field(min_length=6, max_length=6)
    strengths: list[SummaryWithEvidenceOutput] = Field(default_factory=list, max_length=2)
    priorities: list[SummaryWithEvidenceOutput] = Field(default_factory=list, max_length=2)

    @model_validator(mode="after")
    def dimensions_are_exactly_the_contract(self) -> "AttributedFinalScorerOutput":
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

    @field_validator("content")
    @classmethod
    def normalize_and_reject_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("content must not be blank")
        return cleaned

    @property
    def anomaly_details(self) -> list[str]:
        details = [item.strip() for item in self.technical_anomalies if item.strip()]
        if self.technical_anomaly and self.technical_anomaly.strip():
            details.append(self.technical_anomaly.strip())
        return details[:10]


class AcceptClosureSuggestionRequest(BaseModel):
    expected_transcript_fingerprint: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


class FinalizeSessionRequest(BaseModel):
    evidence_check_id: Optional[int] = Field(default=None, ge=1)
    expected_transcript_fingerprint: Optional[str] = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    # Incomplete reports require an explicit participant confirmation. A
    # missing value therefore remains false even for body-less legacy retries.
    allow_incomplete: bool = False

    @model_validator(mode="after")
    def evidence_reference_is_complete(self) -> "FinalizeSessionRequest":
        if (self.evidence_check_id is None) != (
            self.expected_transcript_fingerprint is None
        ):
            raise ValueError(
                "evidence_check_id and expected_transcript_fingerprint must be supplied together"
            )
        return self


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
    reviewer: Optional[str] = Field(default=None, max_length=120)


class AdminLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def reject_blank_credentials(self) -> "AdminLoginRequest":
        if not self.username.strip() or not self.password:
            raise ValueError("username and password must not be blank")
        return self


class ExitRequest(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=1000)
