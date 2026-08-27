"""Expose the existing V6 evidence, scoring, and report chain over MCP.

This module is deliberately a protocol adapter.  It does not contain a rubric,
prompt, eligibility rule, score rule, or report rule.  Every substantive result
comes from :class:`InterviewOrchestrator`; the in-memory registry only prevents
an MCP caller from replacing server-validated evidence IDs with text or stale
IDs between tool calls.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid as uuid_lib
from collections import OrderedDict
from dataclasses import dataclass
from typing import Annotated, Any, Literal

import anyio
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.catalog import DIMENSIONS
from app.models import AssessmentSession, DialogueTurn, EvidenceAttributionSpan
from app.schemas import AttributedFinalScorerOutput, DimensionKey
from app.services.orchestrator import (
    EvidenceAttributionAssessment,
    InterviewOrchestrator,
    ReportReadinessAssessment,
    _attribution_user_turns,
    transcript_fingerprint,
)
from app.services.session_service import _report_readiness_asset_fingerprint


CandidateId = Annotated[
    str,
    Field(
        min_length=69,
        max_length=69,
        pattern=r"^span_[0-9a-f]{64}$",
    ),
]


class ToolModel(BaseModel):
    """Strict JSON boundary shared by all three public tools."""

    model_config = ConfigDict(extra="forbid")


class TranscriptTurn(ToolModel):
    turn_no: int = Field(ge=0)
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=12000)

    @field_validator("text")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("transcript turn text must not be blank")
        # Preserve the exact submitted text because span offsets and hashes are
        # bound to it by the existing evidence-attribution implementation.
        return value


class EvidenceSpan(ToolModel):
    candidate_id: CandidateId
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
    eligibility: Literal["eligible", "context_only", "manual_review"]
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def end_must_follow_start(self) -> "EvidenceSpan":
        if self.end <= self.start:
            raise ValueError("evidence span end must be greater than start")
        return self


class AttributeEvidenceResult(ToolModel):
    spans: list[EvidenceSpan] = Field(default_factory=list, max_length=100)


class SixDimensionScore(ToolModel):
    dimension_key: DimensionKey
    status: Literal["SCORED", "IE", "ERROR"]
    score: int | None = Field(default=None, ge=1, le=5)
    evidence_span_ids: list[CandidateId] = Field(default_factory=list, max_length=5)
    reason: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(ge=0, le=1)

    @field_validator("evidence_span_ids")
    @classmethod
    def span_ids_must_be_unique_and_well_formed(
        cls, value: list[CandidateId]
    ) -> list[CandidateId]:
        if len(value) != len(set(value)):
            raise ValueError("evidence span IDs must be unique within a dimension")
        if any(
            len(item) != 69
            or not item.startswith("span_")
            or any(character not in "0123456789abcdef" for character in item[5:])
            for item in value
        ):
            raise ValueError("evidence span ID is malformed")
        return value

    @model_validator(mode="after")
    def status_must_match_payload(self) -> "SixDimensionScore":
        if self.status == "SCORED" and (
            self.score is None or not self.evidence_span_ids
        ):
            raise ValueError("SCORED requires a score and verified evidence span IDs")
        if self.status in {"IE", "ERROR"} and self.score is not None:
            raise ValueError(f"{self.status} must not carry a numeric score")
        if self.status == "IE" and self.evidence_span_ids:
            raise ValueError("IE must not carry evidence span IDs")
        return self


class ScoreSixDimensionsResult(ToolModel):
    dimensions: list[SixDimensionScore] = Field(min_length=6, max_length=6)

    @model_validator(mode="after")
    def dimensions_are_exactly_the_v6_contract(self) -> "ScoreSixDimensionsResult":
        expected = {dimension.key for dimension in DIMENSIONS}
        seen = [dimension.dimension_key for dimension in self.dimensions]
        if set(seen) != expected or len(set(seen)) != len(seen):
            raise ValueError("exactly one result is required for each V6 dimension")
        return self


class ReportEvidence(ToolModel):
    turn_index: int = Field(ge=0)
    quote: str = Field(min_length=1, max_length=12000)
    source_type: Literal["user"]


class ReportDimension(ToolModel):
    dimension_key: DimensionKey
    dimension_name: str = Field(min_length=1)
    score: int | None = Field(default=None, ge=1, le=5)
    status: Literal["sufficient", "limited", "unmeasured"]
    reason: str = Field(min_length=1)
    strength: str = Field(min_length=1)
    suggestion: str = Field(min_length=1)
    evidences: list[ReportEvidence]
    observable_behaviors: list[str]


class StructuredReport(ToolModel):
    session_uuid: str = Field(min_length=36, max_length=36)
    experimental_notice: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    dimensions: list[ReportDimension] = Field(min_length=6, max_length=6)
    strengths: list[str] = Field(default_factory=list, max_length=2)
    priorities: list[str] = Field(default_factory=list, max_length=2)
    manual_review_recommended: bool
    disclaimer: str = Field(min_length=1)


class QualificationGateError(ValueError):
    """An MCP request failed the existing server-owned evidence boundary."""


@dataclass
class ToolAudit:
    tool: str
    duration_ms: int
    gateway_calls: int
    model_calls: int
    provider: str
    model: str


@dataclass
class _ChainRecord:
    transcript: list[dict[str, Any]]
    transcript_fingerprint: str
    asset_fingerprint: str
    readiness_check_id: int
    session: AssessmentSession
    spans: list[EvidenceAttributionSpan]
    public_spans: list[EvidenceSpan]
    attribution: EvidenceAttributionAssessment
    score_assessment: ReportReadinessAssessment | None = None
    public_scores: ScoreSixDimensionsResult | None = None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _evidence_signature(spans: list[EvidenceSpan]) -> str:
    payload = sorted(
        (span.model_dump(mode="json") for span in spans),
        key=lambda item: item["candidate_id"],
    )
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _score_signature(scores: ScoreSixDimensionsResult) -> str:
    payload = sorted(
        (dimension.model_dump(mode="json") for dimension in scores.dimensions),
        key=lambda item: item["dimension_key"],
    )
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


class MCPExposureService:
    """Thin, fail-closed adapter around the existing V6 service methods."""

    def __init__(
        self,
        *,
        orchestrator: InterviewOrchestrator | None = None,
        registry_capacity: int = 128,
    ) -> None:
        self.orchestrator = orchestrator or InterviewOrchestrator()
        self.registry_capacity = registry_capacity
        self._records: OrderedDict[str, _ChainRecord] = OrderedDict()
        self._records_by_evidence: dict[str, _ChainRecord] = {}
        self._lock = threading.RLock()
        self._last_audit: ToolAudit | None = None

    @property
    def last_audit(self) -> ToolAudit | None:
        with self._lock:
            return self._last_audit

    def _set_last_audit(self, audit: ToolAudit) -> None:
        with self._lock:
            self._last_audit = audit

    def clear(self) -> None:
        """Reset ephemeral protocol state; used by isolated tests only."""

        with self._lock:
            self._records.clear()
            self._records_by_evidence.clear()
            self._last_audit = None

    @staticmethod
    def _normalize_transcript(
        transcript_turns: list[TranscriptTurn],
    ) -> tuple[list[dict[str, Any]], AssessmentSession, str]:
        turn_numbers = [turn.turn_no for turn in transcript_turns]
        if len(turn_numbers) != len(set(turn_numbers)):
            raise ValueError("transcript turn_no values must be unique")
        transcript = [
            {
                "turn_index": turn.turn_no,
                "role": turn.role,
                "content": turn.text,
            }
            for turn in sorted(transcript_turns, key=lambda item: item.turn_no)
        ]
        session = AssessmentSession(
            turns=[
                DialogueTurn(
                    turn_index=item["turn_index"],
                    role=item["role"],
                    content=item["content"],
                )
                for item in transcript
            ],
            user_answer_count=sum(1 for item in transcript if item["role"] == "user"),
            ended_early=False,
        )
        fingerprint = transcript_fingerprint(session)
        session.uuid = str(
            uuid_lib.uuid5(uuid_lib.NAMESPACE_URL, f"siheng-v6-mcp:{fingerprint}")
        )
        return transcript, session, fingerprint

    @staticmethod
    def _readiness_check_id(fingerprint: str) -> int:
        # The existing scorer validates a positive check identity.  This ID is
        # an ephemeral protocol-registry identity, never persisted as a DB row.
        return int(fingerprint[:15], 16) % 2_000_000_000 + 1

    def _store_record(self, record: _ChainRecord) -> None:
        with self._lock:
            previous = self._records.pop(record.transcript_fingerprint, None)
            if previous is not None:
                self._records_by_evidence.pop(
                    _evidence_signature(previous.public_spans), None
                )
            self._records[record.transcript_fingerprint] = record
            self._records_by_evidence[_evidence_signature(record.public_spans)] = record
            while len(self._records) > self.registry_capacity:
                _fingerprint, evicted = self._records.popitem(last=False)
                self._records_by_evidence.pop(
                    _evidence_signature(evicted.public_spans), None
                )

    def _record_for_transcript(self, fingerprint: str) -> _ChainRecord:
        with self._lock:
            record = self._records.get(fingerprint)
            if record is None:
                raise QualificationGateError(
                    "evidence_not_registered_for_transcript; call attribute_evidence first"
                )
            self._records.move_to_end(fingerprint)
            return record

    def attribute_evidence(
        self, transcript_turns: list[TranscriptTurn]
    ) -> AttributeEvidenceResult:
        started = time.perf_counter()
        transcript, session, fingerprint = self._normalize_transcript(transcript_turns)
        assessment = self.orchestrator.assess_evidence_attribution(
            transcript=transcript
        )
        candidates = {
            (
                int(candidate["turn_index"]),
                int(candidate["start"]),
                int(candidate["end"]),
                str(candidate["quote_hash"]),
            ): candidate
            for turn in _attribution_user_turns(transcript)
            for candidate in turn["span_candidates"]
        }
        asset_fingerprint = _report_readiness_asset_fingerprint("enforce")
        check_id = self._readiness_check_id(fingerprint)
        internal_spans: list[EvidenceAttributionSpan] = []
        public_spans: list[EvidenceSpan] = []
        for numeric_id, validated in enumerate(assessment.spans, start=1):
            output = validated.output
            candidate = candidates.get(
                (output.turn_index, output.start, output.end, validated.text_hash)
            )
            if candidate is None:
                raise QualificationGateError(
                    "validated_attribution_span_missing_server_candidate"
                )
            candidate_id = str(candidate["candidate_id"])
            internal_spans.append(
                EvidenceAttributionSpan(
                    id=numeric_id,
                    session_id=0,
                    user_turn_id=numeric_id,
                    readiness_check_id=check_id,
                    turn_index=output.turn_index,
                    quote=output.quote,
                    start=output.start,
                    end=output.end,
                    text_hash=validated.text_hash,
                    owner=output.owner,
                    relation=output.relation,
                    elicitation_level=output.elicitation_level,
                    source_label=output.source_label,
                    confidence=output.confidence,
                    reason=output.reason,
                    eligibility=validated.eligibility,
                    validation_status=validated.validation_status,
                    validation_reason=validated.validation_reason,
                    transcript_fingerprint=fingerprint,
                    asset_fingerprint=asset_fingerprint,
                    prompt_template_id=assessment.prompt_template_id,
                    prompt_version=assessment.prompt_version,
                    schema_version=assessment.schema_version,
                )
            )
            public_spans.append(
                EvidenceSpan(
                    candidate_id=candidate_id,
                    owner=output.owner,
                    relation=output.relation,
                    eligibility=validated.eligibility,
                    start=output.start,
                    end=output.end,
                    hash=validated.text_hash,
                )
            )
        record = _ChainRecord(
            transcript=transcript,
            transcript_fingerprint=fingerprint,
            asset_fingerprint=asset_fingerprint,
            readiness_check_id=check_id,
            session=session,
            spans=internal_spans,
            public_spans=public_spans,
            attribution=assessment,
        )
        self._store_record(record)
        result = AttributeEvidenceResult(spans=public_spans)
        self._set_last_audit(ToolAudit(
            tool="attribute_evidence",
            duration_ms=round((time.perf_counter() - started) * 1000),
            gateway_calls=assessment.attempt_count,
            model_calls=(
                0
                if assessment.provider in {"mock", "system"}
                else assessment.attempt_count
            ),
            provider=assessment.provider,
            model=assessment.model,
        ))
        return result

    def score_six_dimensions(
        self,
        transcript_turns: list[TranscriptTurn],
        eligible_span_ids: list[CandidateId],
    ) -> ScoreSixDimensionsResult:
        started = time.perf_counter()
        if len(eligible_span_ids) != len(set(eligible_span_ids)):
            raise QualificationGateError("eligible_span_ids_must_be_unique")
        transcript, _session, fingerprint = self._normalize_transcript(transcript_turns)
        record = self._record_for_transcript(fingerprint)
        public_by_id = {span.candidate_id: span for span in record.public_spans}
        expected_ids = {
            span.candidate_id
            for span in record.public_spans
            if span.eligibility == "eligible"
        }
        submitted_ids = set(eligible_span_ids)
        unknown_ids = submitted_ids - set(public_by_id)
        ineligible_ids = {
            candidate_id
            for candidate_id in submitted_ids
            if candidate_id in public_by_id
            and public_by_id[candidate_id].eligibility != "eligible"
        }
        if unknown_ids:
            raise QualificationGateError("unknown_or_stale_evidence_span_id")
        if ineligible_ids:
            raise QualificationGateError("ineligible_evidence_span_id")
        # The existing REST flow sends the complete validated eligible set.
        # Requiring the same set prevents MCP callers from manufacturing IE by
        # omitting valid evidence, while still never accepting evidence text.
        if submitted_ids != expected_ids:
            raise QualificationGateError(
                "eligible_span_ids_must_equal_server_validated_eligible_set"
            )
        internal_by_candidate = {
            public.candidate_id: internal
            for public, internal in zip(record.public_spans, record.spans)
        }
        eligible_spans = [
            internal_by_candidate[public.candidate_id]
            for public in record.public_spans
            if public.eligibility == "eligible"
        ]
        assessment = self.orchestrator.assess_attributed_evidence(
            eligible_spans=eligible_spans,
            transcript=transcript,
            expected_check_id=record.readiness_check_id,
            expected_transcript_fingerprint=record.transcript_fingerprint,
            expected_asset_fingerprint=record.asset_fingerprint,
        )
        if not isinstance(assessment.output, AttributedFinalScorerOutput):
            raise QualificationGateError("attributed_scorer_contract_not_returned")
        candidate_by_numeric_id = {
            internal.id: public.candidate_id
            for public, internal in zip(record.public_spans, record.spans)
        }
        result = ScoreSixDimensionsResult(
            dimensions=[
                SixDimensionScore(
                    dimension_key=dimension.dimension_key,
                    status="SCORED" if dimension.score is not None else "IE",
                    score=dimension.score,
                    evidence_span_ids=[
                        candidate_by_numeric_id[reference.attribution_span_id]
                        for reference in dimension.evidence_refs
                    ],
                    reason=dimension.reason,
                    confidence=dimension.confidence,
                )
                for dimension in assessment.output.dimensions
            ]
        )
        with self._lock:
            record.score_assessment = assessment
            record.public_scores = result
        self._set_last_audit(ToolAudit(
            tool="score_six_dimensions",
            duration_ms=round((time.perf_counter() - started) * 1000),
            gateway_calls=assessment.attempt_count,
            model_calls=(
                0
                if assessment.provider in {"mock", "system"}
                else assessment.attempt_count
            ),
            provider=assessment.provider,
            model=assessment.model,
        ))
        return result

    def generate_report(
        self,
        six_dimension_scores: list[SixDimensionScore],
        evidence_spans: list[EvidenceSpan],
    ) -> StructuredReport:
        started = time.perf_counter()
        scores = ScoreSixDimensionsResult(dimensions=six_dimension_scores)
        if any(dimension.status == "ERROR" for dimension in scores.dimensions):
            # The existing report builder has no contract that converts a
            # scorer failure into insufficient evidence.  Fail at the protocol
            # boundary rather than rewriting ERROR as IE.
            raise QualificationGateError("ERROR_cannot_be_rewritten_as_IE")
        with self._lock:
            record = self._records_by_evidence.get(_evidence_signature(evidence_spans))
        if record is None:
            raise QualificationGateError(
                "evidence_spans_not_from_server_validated_registry"
            )
        if record.score_assessment is None or record.public_scores is None:
            raise QualificationGateError(
                "score_not_registered_for_evidence; call score_six_dimensions first"
            )
        if _score_signature(scores) != _score_signature(record.public_scores):
            raise QualificationGateError(
                "six_dimension_scores_do_not_match_registered_scorer_output"
            )
        scorer_output = record.score_assessment.output
        if not isinstance(scorer_output, AttributedFinalScorerOutput):
            raise QualificationGateError("attributed_scorer_contract_not_registered")
        legacy_output = self.orchestrator._legacy_output_from_attributed(
            scorer_output, record.spans
        )
        report = self.orchestrator._build_report(record.session, legacy_output)
        result = StructuredReport.model_validate(report)
        self._set_last_audit(ToolAudit(
            tool="generate_report",
            duration_ms=round((time.perf_counter() - started) * 1000),
            gateway_calls=0,
            model_calls=0,
            provider="system",
            model="existing-v6-report-builder",
        ))
        return result


mcp_service = MCPExposureService()
mcp = FastMCP("siheng-v6-scoring")


@mcp.tool()
async def attribute_evidence(
    transcript_turns: list[TranscriptTurn],
) -> AttributeEvidenceResult:
    """Attribute transcript evidence through the existing server-side gate."""

    return await anyio.to_thread.run_sync(
        mcp_service.attribute_evidence, transcript_turns
    )


@mcp.tool()
async def score_six_dimensions(
    transcript_turns: list[TranscriptTurn],
    eligible_span_ids: list[CandidateId],
) -> ScoreSixDimensionsResult:
    """Score six dimensions from verified span IDs; evidence text is forbidden."""

    return await anyio.to_thread.run_sync(
        mcp_service.score_six_dimensions,
        transcript_turns,
        eligible_span_ids,
    )


@mcp.tool()
async def generate_report(
    six_dimension_scores: list[SixDimensionScore],
    evidence_spans: list[EvidenceSpan],
) -> StructuredReport:
    """Generate the existing structured report from the registered score chain."""

    return await anyio.to_thread.run_sync(
        mcp_service.generate_report,
        six_dimension_scores,
        evidence_spans,
    )


if __name__ == "__main__":
    mcp.run()
