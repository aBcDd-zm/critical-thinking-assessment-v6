"""Natural V6 interview orchestration.

The orchestrator observes a transcript and delegates conversational choice to the
interviewer model. It never selects a dimension, a question, a stage, a
coverage target, or an expected number of turns.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.domain.catalog import DIMENSIONS, DIMENSION_BY_KEY
from app.models import (
    AssessmentReport,
    AssessmentSession,
    DialogueTurn,
    EvidenceAttributionSpan,
    EvidenceItem,
    ScoringRun,
    utcnow,
)
from app.schemas import (
    AttributedFinalScorerOutput,
    EvidenceAttributionOutput,
    EvidenceAttributionSpanOutput,
    FinalScorerOutput,
    NaturalInterviewerOutput,
)
from app.services.model_gateway import (
    ATTRIBUTED_EVIDENCE_PROMPT_ID,
    ATTRIBUTED_EVIDENCE_PROMPT_VERSION,
    EVIDENCE_ATTRIBUTION_PROMPT_ID,
    EVIDENCE_ATTRIBUTION_PROMPT_VERSION,
    EVIDENCE_ATTRIBUTION_SCHEMA_VERSION,
    EVIDENCE_CANDIDATE_RULE_VERSION,
    INCREMENTAL_EVIDENCE_PROMPT_ID,
    INCREMENTAL_EVIDENCE_PROMPT_VERSION,
    NATURAL_FINAL_SCORER_PROMPT_ID,
    NATURAL_FINAL_SCORER_PROMPT_VERSION,
    NATURAL_INTERVIEWER_PROMPT_VERSION,
    ModelGatewayError,
    ModelGatewayService,
    StructuredCallResult,
    EvidenceAttributionSelectionOutput,
    attribution_span_candidate_id,
    attach_attribution_span_candidates,
    build_interview_anchor_candidates,
    payload_fingerprint,
    resolve_natural_interviewer_prompt,
    source_clarification_required,
)


FINALIZING_MESSAGE = "这段访谈已被冻结，正在仅依据你的原话整理报告。"
SUGGEST_FINISH_ACTION = "suggest_finish"
MODEL_NATURAL_CLOSE_REASONS = frozenset({"enough_understanding", "natural_closure"})
CLOSURE_CONFIRMATION_CONSENT_VERSIONS = frozenset(
    {"v6-natural-interview-guidance-2026-08"}
)
SAFE_CLOSURE_SUGGESTION_MESSAGE = (
    "这件事已经梳理得比较完整，可以考虑在这里结束；"
    "如果还有重要内容，你仍可以继续补充。"
)
EVIDENCE_GATE_CONTINUATION_MESSAGE = (
    "我们再把这次经历往深处看一点：还有哪条重要依据、权衡或变化，"
    "是你觉得没有说清的？"
)
USER_FINISH_INTENT_MESSAGE = (
    "好的。如果你想现在提交，可以点击“结束并生成报告”；"
    "证据有限的部分会如实说明。"
)
AMBIGUOUS_USER_FINISH_INTENT_MESSAGE = (
    "我还不能确认你是否在要求结束本次访谈。如果想结束，可选择“结束并生成报告”"
    "或页面上方的“退出不生成报告”。"
    "若继续，刚才这件事里还有哪条重要依据、权衡或变化没有说清？"
)
_FINISH_INTENT_TRADITIONAL_TRANSLATION = str.maketrans(
    {
        "訪": "访",
        "談": "谈",
        "對": "对",
        "話": "话",
        "這": "这",
        "請": "请",
        "繼": "继",
        "續": "续",
        "問": "问",
        "報": "报",
        "產": "产",
        "現": "现",
        "裡": "里",
        "願": "愿",
        "說": "说",
        "幫": "帮",
    }
)
_EXPLICIT_USER_FINISH_INTENT_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        # Keep this deliberately conservative. The permanent end button remains
        # available when a participant uses wording that is not matched here.
        r"^(?:好的)?(?:我|本人)(?:现在|这次|本次|已经|就|真的|确实)*"
        r"(?:想|要|希望|决定|准备|打算)(?:现在|立即|就)?"
        r"(?:结束|停止)(?:这次|本次|当前)?(?:访谈|对话|问答)"
        r"(?:了|吧|啦)?(?:请?(?:就)?停在这里|谢谢|多谢)?$",
        r"^(?:(?:请|麻烦)(?:帮我)?(?:现在|立即|就)?)?(?:结束|停止|退出)"
        r"(?:这次|本次|当前)?(?:访谈|对话|问答)(?:吧|谢谢|多谢)?$",
        r"^(?:我)?(?:已经|都)?(?:说完|回答完)(?:了)?(?:请|麻烦)(?:帮我)?"
        r"(?:结束|停止)(?:这次|本次|当前)?(?:访谈|对话|问答)(?:吧)?$",
        r"^(?:我|本人)(?:现在|已经|真的|确实|有点累了|累了|有事了)*"
        r"(?:不想|不愿意|不愿|不准备|不打算)(?:再|继续)"
        r"(?:回答|参与|进行|聊|说)(?:这次|本次|当前)?"
        r"(?:访谈|对话|问答)?(?:了|啦|吧)?$",
        r"^(?:我|本人)(?:现在|已经)?(?:不想|不愿意|不愿|不准备|不打算)"
        r"(?:再|继续)(?:了|啦|吧)$",
        r"^(?:我|本人)(?:现在|已经|真的|确实|有点累了|累了|有事了)*"
        r"(?:不想|不愿意|不愿|不准备|不打算)"
        r"(?:(?:参与|进行)(?:这次|本次|当前)?(?:访谈|对话|问答)|"
        r"(?:回答|答|聊|说)(?:了|啦|吧))$",
        r"^(?:(?:请|麻烦)(?:你)?|我(?:希望|想让)你)?(?:别|不要|不用)"
        r"(?:再|继续)?(?:问|提问|追问)(?:我)?(?:了|啦|吧)?$",
        r"^(?:请|麻烦)(?:你)?(?:停止|结束)(?:向我)?(?:提问|追问)(?:吧)?$",
        r"^(?:我|本人)?(?:现在|已经|真的|就)*(?:想|要|希望|决定|准备|打算)?"
        r"退出(?:这次|本次|当前)?(?:访谈|对话|问答)?(?:了|吧|啦)?$",
        r"^(?:我|本人)(?:现在|真的)?(?:想|要|希望)停(?:了|下|下来)?$",
        r"^(?:我)?(?:不聊|不说|不答|不回答|不继续)(?:了|啦|吧)$",
        r"^(?:这次|本次|当前)(?:访谈|对话|问答)(?:就)?到这里(?:吧|了)?$",
        r"^(?:(?:我|本人)(?:现在|这次|本次)?(?:想|要|希望|决定|准备|打算)|"
        r"(?:请|麻烦)(?:帮我)?(?:现在|立即|就)?)"
        r"(?:结束(?:这次|本次|当前)?(?:访谈|对话|问答)(?:并|然后)?)?"
        r"(?:生成|产生|输出|提交)(?:这次|本次|当前)?报告"
        r"(?:吧|谢谢|多谢)?$",
        r"^(?:i(?:want|wouldlike|dlike|need|havedecided)to|please)"
        r"(?:end|stop)(?:this|the)?(?:interview|conversation|assessment)(?:now)?$",
        r"^i(?:do(?:not|nt)|dont)wanttocontinue"
        r"(?:answering|thisinterview|theinterview|thisconversation|theconversation)?$",
        r"^i(?:want|need)to(?:leave|quit)(?:this|the)?"
        r"(?:interview|conversation|assessment)?$",
        r"^please(?:stop|quit)(?:asking|theinterview|thisinterview)?$",
        r"^(?:stop|quit)$",
        r"^(?:i(?:want|wouldlike|dlike|need)to|please)"
        r"(?:generate|create|produce|submit)(?:my|the)?report$",
    )
)
_CLEAR_NON_FINISH_INTENT_MARKERS = (
    "还有什么想问",
    "继续问",
    "接着问",
    "请继续",
    "想继续",
    "要继续",
    "希望继续",
    "还想继续",
    "还不准备结束",
    "还不想结束",
    "不是要结束",
    "并非要结束",
    "不想结束",
    "不要结束",
    "不提交",
)
_FINISH_INTENT_CUES = (
    "结束",
    "停止",
    "退出",
    "不聊",
    "不说",
    "不答",
    "不想回答",
    "不愿回答",
    "不回答",
    "不继续",
    "别问",
    "不要问",
    "说完",
    "到这里",
    "到这儿",
    "到此为止",
    "够了",
    "就这样",
    "先这样",
    "停一下",
    "不想继续",
    "不愿继续",
    "要走了",
    "先走了",
    "生成报告",
    "产生报告",
    "输出报告",
    "提交报告",
    "end",
    "stop",
    "quit",
    "report",
)
SAFETY_STOP_MESSAGE = (
    "你刚才提到的内容可能涉及当下的人身安全。此刻比继续访谈更重要的是先获得"
    "现实中的即时支持；如果你或他人有立即危险，请联系当地紧急服务、身边可信的人，"
    "或尽快到安全的地方。我们先在这里停下。"
)

_PUBLIC_DIMENSION_SUGGESTIONS = {
    "problem_definition": "继续明确目标、范围和需要核实的边界。",
    "evidence_evaluation": "继续核实来源、样本范围和仍不确定的信息。",
    "reasoning_argumentation": "继续区分结论、依据与可能改变结论的假设。",
    "multiple_perspectives": "继续补充不同相关方和选择可能带来的影响。",
    "integrative_decision": "继续把目标、约束、风险和回退条件放在一起权衡。",
    "dynamic_adjustment": "继续提前写下会触发调整的信号和下一步行动。",
}

# Safety matching treats the transcript as untrusted text and only controls
# whether the interview should stop. It does not diagnose, score, or infer a
# user's mental state.
_TRANSLATION = str.maketrans(
    {
        "殺": "杀",
        "傷": "伤",
        "殘": "残",
        "輕": "轻",
        "結": "结",
        "離": "离",
        "開": "开",
        "藥": "药",
        "綁": "绑",
        "槍": "枪",
        "樓": "楼",
        "尋": "寻",
        "見": "见",
        "歸": "归",
        "於": "于",
        "盡": "尽",
        "緊": "紧",
        "醫": "医",
        "報": "报",
        "復": "复",
        "і": "i",
        "ѕ": "s",
        "с": "c",
        "е": "e",
        "а": "a",
        "о": "o",
    }
)
_RISK_MARKERS = (
    "自杀",
    "自残",
    "自伤",
    "轻生",
    "寻死",
    "不想活",
    "活不下去",
    "我想死",
    "我要死",
    "结束生命",
    "割腕",
    "跳楼",
    "吞药",
    "杀了他",
    "杀了她",
    "杀人",
    "捅人",
    "砍人",
    "开枪",
    "正在打我",
    "家暴",
    "被绑架",
    "suicide",
    "selfharm",
    "killmyself",
    "iwanttodie",
    "endmylife",
    "overdose",
    "killsomeone",
    "stabsomeone",
    "immediatedanger",
)
_SAFETY_CONTEXT_EXEMPTIONS = (
    "自杀预防",
    "自伤预防",
    "suicideprevention",
    "selfharmprevention",
    "过去",
    "曾经",
    "历史上",
    "朋友说",
    "新闻里",
)

# An interviewer never needs to encourage dangerous, violent, coercive, or
# retaliatory action. This catches positive directives without turning an
# ordinary discussion *about* harm into a false safety stop.
_HARMFUL_INTERVIEWER_DIRECTIVE = re.compile(
    r"(?:你|您|对方|他|她|他们|任何人).{0,12}"
    r"(?:可以|应该|必须|得|不妨|最好|值得|赶紧|马上|去|试着).{0,24}"
    r"(?:自杀|自残|自伤|轻生|吞药|割腕|跳楼|伤害|杀人|杀了|打人|打他|打她|捅|砍|开枪|报复|威胁|胁迫|逼迫)"
)
_HARMFUL_DIRECTIVE_WITHOUT_SUBJECT = re.compile(
    r"(?:请|去|赶紧|马上|不妨|最好|应该|必须).{0,14}"
    r"(?:自杀|自残|自伤|轻生|吞药|割腕|跳楼|伤害|杀人|杀了|打人|打他|打她|捅|砍|开枪|报复|威胁|胁迫|逼迫)"
)

# The participant report may contain evidence-grounded observations, but must
# never become a personality label, career recommendation, diagnosis, or
# comparison with other people. Filtering is deterministic because these
# strings are rendered directly to the participant.
_UNSAFE_PUBLIC_REPORT_TEXT = re.compile(
    r"(?:人格|性格|天生|注定|本质|心理(?:疾病|问题|诊断)?|精神(?:疾病|问题)?|"
    r"抑郁|焦虑|诊断|(?:适合|不适合|应该从事|推荐从事).{0,20}(?:职业|行业|专业|岗位|工作)|"
    r"(?:职业|行业|专业|岗位|工作).{0,20}(?:排名|推荐|选择)|"
    r"(?:排名|胜过|超过|优于|不如).{0,20}(?:他人|别人|同龄|其他人|其他用户)|"
    r"(?:你应(?:该)?|建议你|请你|务必|必须|第一步|第二步|正确答案)|系统提示|prompt|评分标准)"
)


class FinalizationError(RuntimeError):
    """The frozen transcript remains retryable after a scoring failure."""


class InterviewContractError(RuntimeError):
    """A model response violated a hard V6 visibility or safety boundary."""


@dataclass(frozen=True)
class InterviewResult:
    content: str
    session_action: str
    finish_reason: str | None
    provider: str
    model: str
    prompt_template_id: str
    prompt_version: str
    repair_used: bool
    latency_ms: int
    attempt_count: int
    quality_flags: list[str]
    input_fingerprint: str
    model_session_action: str | None
    model_finish_reason: str | None
    navigation: dict[str, Any] | None


@dataclass(frozen=True)
class ValidatedAttributionSpan:
    output: EvidenceAttributionSpanOutput
    text_hash: str
    eligibility: str
    validation_status: str
    validation_reason: str


@dataclass(frozen=True)
class EvidenceAttributionAssessment:
    spans: list[ValidatedAttributionSpan]
    provider: str
    model: str
    prompt_template_id: str
    prompt_version: str
    schema_version: str
    repair_used: bool
    latency_ms: int
    attempt_count: int
    input_fingerprint: str


@dataclass(frozen=True)
class ReportReadinessAssessment:
    """Private result of applying the existing final evidence rules.

    Participant APIs expose only the aggregate decision.  The validated output
    may be cached privately and promoted only if this exact transcript is later
    frozen, avoiding a second scorer call with a potentially different result.
    """

    ready: bool
    evidence_ready: bool
    sufficient_dimension_count: int
    minimum_turns_required: int
    minimum_turns_met: bool
    provider: str
    model: str
    prompt_template_id: str
    prompt_version: str
    repair_used: bool
    latency_ms: int
    attempt_count: int
    output: FinalScorerOutput | AttributedFinalScorerOutput


def normalized_text(value: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKC", value).casefold().translate(_TRANSLATION)
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )


def classify_user_finish_request(
    value: str,
) -> Literal["confirmed", "not_intent", "ambiguous"]:
    """Classify whether the participant directly asks to end this interview.

    A model-provided ``user_requested`` label is not evidence by itself. This
    intentionally narrow guard preserves hard stop requests, rejects clearly
    unrelated or negated cases, and routes uncertain wording to a neutral choice.
    """

    raw = unicodedata.normalize("NFKC", value).strip().casefold()
    normalized = normalized_text(value).translate(
        _FINISH_INTENT_TRADITIONAL_TRANSLATION
    )
    if not normalized:
        return "ambiguous"
    quote_pairs = (
        ("“", "”"),
        ('"', '"'),
        ("‘", "’"),
        ("'", "'"),
        ("「", "」"),
        ("『", "』"),
    )
    if any(
        raw.startswith(left) and raw.endswith(right)
        for left, right in quote_pairs
    ):
        return "ambiguous"
    if re.search(
        r"(?:如果|假如|要是|假设).{0,24}"
        r"(?:结束|停止|退出|生成报告|提交报告)",
        normalized,
    ):
        return "ambiguous"
    if re.search(
        r"(?:朋友|同学|别人|他|她).{0,16}(?:说|问|要求).{0,16}"
        r"(?:结束|停止|退出|报告)",
        normalized,
    ):
        return "ambiguous"
    has_question_form = any(marker in raw for marker in ("?", "？"))
    if not has_question_form and any(
        pattern.fullmatch(normalized)
        for pattern in _EXPLICIT_USER_FINISH_INTENT_PATTERNS
    ):
        return "confirmed"
    # Longer mixed sentences can contain a real interview-level stop request
    # plus another clause. Keep those uncertain instead of letting a nearby
    # topic/report negation override the participant's boundary.
    if re.search(
        r"(?:(?:我|本人).{0,8}(?:想|要|希望|决定|准备|打算)|"
        r"(?:请|麻烦)(?:帮我)?).{0,8}(?:结束|停止|退出).{0,8}"
        r"(?:访谈|对话|问答)",
        normalized,
    ):
        return "ambiguous"
    if re.search(
        r"(?:(?:我|本人).{0,8}(?:想|要|希望|决定|准备|打算)|"
        r"(?:请|麻烦)(?:帮我)?).{0,8}(?:生成|产生|输出|提交)报告",
        normalized,
    ):
        return "ambiguous"
    single_question_refusal = "这个问题" in normalized and any(
        marker in normalized for marker in ("不想回答", "不回答", "换一个")
    )
    if single_question_refusal:
        return "not_intent"
    if re.search(
        r"(?:不想|不愿|不要|不打算|不准备|无法|不能)(?:再)?继续",
        normalized,
    ):
        return "ambiguous"
    if any(marker in normalized for marker in _CLEAR_NON_FINISH_INTENT_MARKERS):
        return "not_intent"
    if re.search(
        r"(?:项目|事情|方案|会议|任务|关系|课程|工作).{0,10}(?:结束|停止|提交)",
        normalized,
    ):
        return "not_intent"
    if has_question_form:
        return "ambiguous"
    if any(marker in normalized for marker in _FINISH_INTENT_CUES):
        return "ambiguous"
    return "not_intent"


def is_explicit_user_finish_request(value: str) -> bool:
    return classify_user_finish_request(value) == "confirmed"


def is_immediate_high_risk(value: str) -> bool:
    normalized = normalized_text(value)
    if not any(marker in normalized for marker in _RISK_MARKERS):
        return False
    # A prevention course or clearly historical/third-party report should not
    # trigger a forced stop without stronger present-tense language.
    if any(marker in normalized for marker in _SAFETY_CONTEXT_EXEMPTIONS):
        present_markers = ("现在", "今晚", "马上", "准备", "计划", "rightnow", "tonight", "imgoingto")
        return any(marker in normalized for marker in present_markers)
    return True


def is_harmful_interviewer_output(value: str) -> bool:
    """Reject unsafe advice generated by the interviewer before it is shown."""

    normalized = normalized_text(value)
    return (
        is_immediate_high_risk(value)
        or bool(_HARMFUL_INTERVIEWER_DIRECTIVE.search(normalized))
        or bool(_HARMFUL_DIRECTIVE_WITHOUT_SUBJECT.search(normalized))
    )


def _public_report_text_is_safe(value: str) -> bool:
    """Whether model-authored text is safe to render in the participant report."""

    compact = normalized_text(value)
    return bool(compact) and not bool(_UNSAFE_PUBLIC_REPORT_TEXT.search(compact))


def _neutral_reason_for(score: int | None) -> str:
    if score is None:
        return "证据有限，未充分测得该视角。"
    return "该视角仅依据本次用户原话整理，不作额外定性。"


def _transcript_rows(session: AssessmentSession) -> list[dict[str, Any]]:
    return [
        {
            "turn_index": turn.turn_index,
            "role": turn.role,
            "content": turn.content,
        }
        for turn in sorted(session.turns, key=lambda item: item.turn_index)
    ]


def transcript_fingerprint(session: AssessmentSession) -> str:
    canonical = json.dumps(
        _transcript_rows(session), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def attribution_text_hash(value: str) -> str:
    """Canonical hash for an exact Unicode span (UTF-8 bytes)."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _attribution_user_turns(
    transcript: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    previous_assistant: str | None = None
    rows: list[dict[str, Any]] = []
    for item in sorted(transcript, key=lambda row: int(row["turn_index"])):
        if item["role"] == "assistant":
            previous_assistant = str(item["content"])
            continue
        if item["role"] != "user":
            continue
        rows.append(
            {
                "turn_index": int(item["turn_index"]),
                "content": str(item["content"]),
                "preceding_question": previous_assistant,
            }
        )
    return attach_attribution_span_candidates(rows)


def classify_span_eligibility(
    span: EvidenceAttributionSpanOutput,
) -> tuple[str, str, str]:
    """Compute the hard eligibility gate without trusting model self-report."""

    if span.owner == "uncertain":
        return (
            "manual_review",
            "manual_review",
            "source_ownership_uncertain",
        )
    if span.owner != "participant_owned":
        return (
            "context_only",
            "validated",
            "external_material_is_context_only",
        )
    if span.relation in {"quotes_only", "asks_or_requests"}:
        return (
            "context_only",
            "validated",
            "quote_or_request_cannot_support_score",
        )
    if span.relation == "endorses":
        return (
            "context_only",
            "validated",
            "endorsement_requires_separate_participant_reasoning_span",
        )
    if span.relation in {"own_reasoning", "critiques", "rejects"}:
        return "eligible", "validated", "participant_reasoning_eligible"
    return "manual_review", "manual_review", "unsupported_attribution_combination"


def materialize_attribution_output(
    output: EvidenceAttributionSelectionOutput,
    span_candidates: list[dict[str, Any]],
) -> EvidenceAttributionOutput:
    """Resolve compact model classifications through the server registry.

    The model never controls quote text, offsets, occurrence, or hashes.  This
    function validates the registry identity and then restores the established
    span output shape for the existing hard gate and persistence layer.
    """

    registry: dict[str, dict[str, Any]] = {}
    occurrence_by_turn_hash: dict[tuple[int, str], int] = {}
    ordered_candidates = sorted(
        span_candidates,
        key=lambda item: (
            int(item.get("turn_index", -1)),
            int(item.get("start", -1)),
            int(item.get("end", -1)),
        ),
    )
    for candidate in ordered_candidates:
        try:
            candidate_id = str(candidate["candidate_id"])
            turn_index = int(candidate["turn_index"])
            quote = str(candidate["quote"])
            start = int(candidate["start"])
            end = int(candidate["end"])
            quote_hash = str(candidate["quote_hash"])
            occurrence = int(candidate["occurrence"])
        except (KeyError, TypeError, ValueError) as exc:
            raise FinalizationError(
                "attribution_candidate_registry_invalid"
            ) from exc
        authoritative_hash = attribution_text_hash(quote)
        if quote_hash != authoritative_hash:
            raise FinalizationError(
                "attribution_candidate_registry_quote_hash_mismatch"
            )
        occurrence_key = (turn_index, quote_hash)
        expected_occurrence = occurrence_by_turn_hash.get(occurrence_key, 0) + 1
        occurrence_by_turn_hash[occurrence_key] = expected_occurrence
        if occurrence != expected_occurrence:
            raise FinalizationError(
                "attribution_candidate_registry_occurrence_mismatch"
            )
        expected_id = attribution_span_candidate_id(
            turn_index=turn_index,
            start=start,
            end=end,
            quote_hash=quote_hash,
            occurrence=occurrence,
        )
        if candidate_id != expected_id:
            raise FinalizationError(
                "attribution_candidate_registry_identity_mismatch"
            )
        if candidate_id in registry:
            raise FinalizationError(
                "attribution_candidate_registry_duplicate_id"
            )
        registry[candidate_id] = candidate

    classifications: dict[str, Any] = {}
    for selection in output.spans:
        candidate_id = selection.candidate_id
        if candidate_id not in registry:
            raise FinalizationError(
                "attribution_output_unknown_candidate_id"
            )
        if candidate_id in classifications:
            raise FinalizationError(
                "attribution_output_duplicate_candidate_id"
            )
        classifications[candidate_id] = selection
    if set(registry) - set(classifications):
        raise FinalizationError("attribution_output_missing_candidate_id")

    materialized: list[dict[str, Any]] = []
    for candidate in span_candidates:
        candidate_id = str(candidate["candidate_id"])
        selection = classifications[candidate_id]
        forced_uncertain = candidate.get("force_uncertain") is True
        materialized.append(
            {
                "turn_index": int(candidate["turn_index"]),
                "quote": str(candidate["quote"]),
                "start": int(candidate["start"]),
                "end": int(candidate["end"]),
                "text_hash": None,
                "owner": "uncertain" if forced_uncertain else selection.owner,
                "relation": (
                    "quotes_only" if forced_uncertain else selection.relation
                ),
                "elicitation_level": selection.elicitation_level,
                "source_label": (
                    None if forced_uncertain else selection.source_label
                ),
                "confidence": (
                    min(selection.confidence, 0.45)
                    if forced_uncertain
                    else selection.confidence
                ),
                "reason": (
                    "服务端候选标记：整轮来源混合且无可靠切分点。"
                    if forced_uncertain
                    else selection.reason
                ),
            }
        )
    return EvidenceAttributionOutput.model_validate({"spans": materialized})


def validate_attribution_output(
    output: EvidenceAttributionOutput,
    transcript: list[dict[str, Any]],
    *,
    span_candidates: list[dict[str, Any]] | None = None,
) -> list[ValidatedAttributionSpan]:
    """Verify role, occurrence, offsets, hash, and non-overlap before storage."""

    user_turns = {
        int(item["turn_index"]): str(item["content"])
        for item in transcript
        if item.get("role") == "user" and str(item.get("content", "")).strip()
    }
    previous_end_by_turn: dict[int, int] = {}
    represented_user_turns: set[int] = set()
    validated: list[ValidatedAttributionSpan] = []
    for span in sorted(output.spans, key=lambda item: (item.turn_index, item.start, item.end)):
        source = user_turns.get(span.turn_index)
        if source is None:
            raise FinalizationError("attribution_span_must_reference_user_turn")
        if span.start < 0 or span.end > len(source) or span.end <= span.start:
            raise FinalizationError("attribution_span_offsets_out_of_bounds")
        if source[span.start:span.end] != span.quote:
            raise FinalizationError("attribution_span_offsets_do_not_match_quote")
        previous_end = previous_end_by_turn.get(span.turn_index)
        if previous_end is not None and span.start < previous_end:
            raise FinalizationError("attribution_spans_overlap")
        previous_end_by_turn[span.turn_index] = span.end
        represented_user_turns.add(span.turn_index)
        authoritative_hash = attribution_text_hash(span.quote)
        if span.text_hash is not None and span.text_hash != authoritative_hash:
            raise FinalizationError("attribution_span_text_hash_mismatch")
        eligibility, validation_status, validation_reason = (
            classify_span_eligibility(span)
        )
        validated.append(
            ValidatedAttributionSpan(
                output=span,
                text_hash=authoritative_hash,
                eligibility=eligibility,
                validation_status=validation_status,
                validation_reason=validation_reason,
            )
        )
    if not validated:
        raise FinalizationError("attribution_output_has_no_valid_spans")
    if set(user_turns) - represented_user_turns:
        raise FinalizationError("attribution_output_missing_user_turn")
    if span_candidates is not None:
        expected = {
            (
                int(candidate["turn_index"]),
                int(candidate["start"]),
                int(candidate["end"]),
                str(candidate["quote"]),
            )
            for candidate in span_candidates
        }
        actual = {
            (
                item.output.turn_index,
                item.output.start,
                item.output.end,
                item.output.quote,
            )
            for item in validated
        }
        if actual - expected:
            raise FinalizationError(
                "attribution_output_has_unregistered_span_candidate"
            )
        if expected - actual or len(validated) != len(span_candidates):
            raise FinalizationError("attribution_output_missing_span_candidate")
    return validated


def _is_self_label_or_prompted_claim(quote: str) -> bool:
    """Return true when a quote is only a capability claim, not an observation.

    A user may agree with an interviewer, describe themselves as "good at
    reasoning", or repeat a flattering label.  Those statements are still
    part of the transcript, but they are not evidence of the claimed ability.
    We deliberately use a conservative heuristic here: a scorer can always
    quote a concrete behaviour from the same turn instead, while a label must
    never be enough on its own for a numeric result.
    """

    compact = normalized_text(quote)
    if len(compact) > 160:
        return False
    patterns = (
        r"(?:我|自己|本人)(?:觉得|认为|相信|感觉)?(?:很|非常|比较)?"
        r"(?:擅长|有|具备|缺乏|没有|不具备|拥有).{0,36}"
        r"(?:能力|思维|判断力|素养|逻辑|批判|分析|决策|评估)",
        r"(?:我|自己|本人).{0,14}(?:是|算是|属于).{0,30}"
        r"(?:优秀|理性|有逻辑|会思考|善于分析|善于判断)",
        r"(?:如你所说|正如你所说|像你说的|按照你的说法).{0,70}",
    )
    return any(re.search(pattern, compact) for pattern in patterns)


def _source_turn_is_only_self_label(source: str) -> bool:
    """Detect a label-only source even if a scorer quotes a short substring.

    A scorer must not evade the self-label guard by selecting only words such
    as “证据评估” from “我觉得自己很擅长证据评估”.  At the same time, a
    longer answer that contains a concrete account (for example “我会核实
    数据”) may still yield that concrete part as evidence.  This is therefore
    intentionally limited to label-like turns without a behavioural context.
    """

    compact = normalized_text(source)
    if not _is_self_label_or_prompted_claim(source):
        return False
    behavioural_context = (
        "我会",
        "我先",
        "我通常",
        "我曾",
        "我总会",
        "每次",
        "例如",
        "当时",
        "因为",
        "通过",
        "之后",
        "此前",
        "核实",
        "检验",
        "比较",
        "权衡",
        "记录",
        "调整",
        "复盘",
    )
    return not any(marker in compact for marker in behavioural_context)


def _repeats_latest_user_wording(message: str, latest_user_text: str | None) -> bool:
    """Detect substantial verbatim echoing without changing the model's wording.

    V6 deliberately records ordinary style issues rather than replacing a
    natural question after the fact.  A continuous 12-character overlap is a
    useful review signal while avoiding flags for short, unavoidable words.
    """

    if not latest_user_text:
        return False
    response = normalized_text(message)
    source = normalized_text(latest_user_text)
    minimum_span = 12
    if len(response) < minimum_span or len(source) < minimum_span:
        return False
    return any(
        source[index:index + minimum_span] in response
        for index in range(len(source) - minimum_span + 1)
    )


def _quality_flags(message: str, latest_user_text: str | None = None) -> list[str]:
    flags: list[str] = []
    if message.count("？") + message.count("?") > 1:
        flags.append("multiple_primary_questions")
    if re.search(r"(?:A[、.]|B[、.]|二选一|选择[AB])", message, flags=re.IGNORECASE):
        flags.append("option_format")
    if re.search(r"(?:是|会|想|要|更(?:像|倾向于)?).{1,24}(?:还是|或者).{1,24}[？?]", message):
        flags.append("binary_choice_question")
    if re.search(r"(?:你应该|建议你|第一步|第二步|正确答案)", message):
        flags.append("instructional_tone")
    if _repeats_latest_user_wording(message, latest_user_text):
        flags.append("repeated_user_wording")
    return flags


def _validate_interviewer_output(
    output: NaturalInterviewerOutput, latest_user_text: str | None = None
) -> list[str]:
    message = output.interviewer_message.strip()
    if not message:
        raise InterviewContractError("empty_interviewer_message")
    if is_harmful_interviewer_output(message):
        raise InterviewContractError("unsafe_interviewer_output")
    leaked_terms = (
        "系统提示",
        "prompt",
        "评分",
        "测评维度",
        "覆盖率",
        "target_dimension",
        "考试",
        "测验",
    )
    if any(term.casefold() in message.casefold() for term in leaked_terms):
        raise InterviewContractError("internal_or_scoring_leak")
    return _quality_flags(message, latest_user_text)


def _validated_navigation(
    output: NaturalInterviewerOutput,
    *,
    prompt_version: str,
    transcript: list[dict[str, Any]],
) -> dict[str, Any] | None:
    navigation = output.navigation
    user_turns = {
        int(item["turn_index"]): str(item["content"])
        for item in transcript
        if item.get("role") == "user"
    }
    if not user_turns:
        if prompt_version == "v6.2.1" and navigation is not None:
            raise InterviewContractError("opening_navigation_must_be_null")
        return None
    if prompt_version == "v6.2.1" and navigation is None:
        raise InterviewContractError("v621_navigation_required")
    if navigation is None:
        return None
    anchor = navigation.decision_anchor
    source = user_turns.get(anchor.turn_index)
    if source is None:
        raise InterviewContractError("navigation_anchor_must_reference_user_turn")
    if anchor.start < 0 or anchor.end > len(source) or anchor.end <= anchor.start:
        raise InterviewContractError("navigation_anchor_offsets_out_of_bounds")
    if source[anchor.start:anchor.end] != anchor.quote:
        raise InterviewContractError("navigation_anchor_offsets_do_not_match_quote")
    if prompt_version == "v6.2.1":
        candidates = build_interview_anchor_candidates(transcript)
        anchor_key = (anchor.turn_index, anchor.start, anchor.end, anchor.quote)
        candidate_keys = {
            (
                int(candidate["turn_index"]),
                int(candidate["start"]),
                int(candidate["end"]),
                str(candidate["quote"]),
            )
            for candidate in candidates
        }
        if anchor_key not in candidate_keys:
            raise InterviewContractError(
                "navigation_anchor_not_in_server_candidates"
            )
    authoritative_hash = attribution_text_hash(anchor.quote)
    if anchor.text_hash is not None and anchor.text_hash != authoritative_hash:
        raise InterviewContractError("navigation_anchor_text_hash_mismatch")
    result = navigation.model_dump(mode="json")
    result["decision_anchor"]["text_hash"] = authoritative_hash
    return result


class InterviewOrchestrator:
    def __init__(self, gateway: ModelGatewayService | None = None) -> None:
        self.gateway = gateway or ModelGatewayService()

    def opening(
        self,
        participant: dict[str, str],
        *,
        prompt_version: str = NATURAL_INTERVIEWER_PROMPT_VERSION,
    ) -> InterviewResult:
        payload = {"participant": participant, "transcript": []}
        call = self.gateway.generate_opening(
            participant,
            prompt_version=prompt_version,
        )
        return self._result_from_call(
            call,
            input_fingerprint=payload_fingerprint(payload),
            prompt_version=prompt_version,
            transcript=[],
        )

    def process(
        self,
        session: AssessmentSession,
        user_turn: DialogueTurn,
        *,
        prompt_version: str = NATURAL_INTERVIEWER_PROMPT_VERSION,
    ) -> InterviewResult:
        if is_immediate_high_risk(user_turn.content):
            session.phase = "safety_stopped"
            session.finalization_state = "safety_stopped"
            session.manual_review_recommended = True
            return InterviewResult(
                content=SAFETY_STOP_MESSAGE,
                session_action="finish",
                finish_reason="safety_stopped",
                provider="safety_gate",
                model="none",
                prompt_template_id="v6_safety_gate",
                prompt_version="v6.0.0",
                repair_used=False,
                latency_ms=0,
                attempt_count=0,
                quality_flags=["safety_stopped"],
                input_fingerprint=hashlib.sha256(user_turn.content.encode("utf-8")).hexdigest(),
                model_session_action=None,
                model_finish_reason=None,
                navigation=None,
            )
        payload = {
            "participant": {
                "display_name": session.display_name or "",
                "identity_type": (session.profile_data or {}).get("identity_type", "other"),
                "occupation": session.occupation or "",
                "experience_level": session.experience_level or "",
                "collaboration_role": session.collaboration_role or "",
            },
            "transcript": _transcript_rows(session),
        }
        if prompt_version == "v6.2.1":
            payload["anchor_candidates"] = build_interview_anchor_candidates(
                payload["transcript"]
            )
            payload["source_clarification_required"] = (
                source_clarification_required(payload["transcript"])
            )
        call = self.gateway.generate_interviewer(
            payload,
            prompt_version=prompt_version,
        )
        result = self._result_from_call(
            call,
            input_fingerprint=payload_fingerprint(payload),
            latest_user_text=user_turn.content,
            prompt_version=prompt_version,
            transcript=payload["transcript"],
        )
        if result.prompt_version in {"v6.2.0", "v6.2.1"} and result.session_action == "finish":
            if result.finish_reason in MODEL_NATURAL_CLOSE_REASONS:
                return replace(
                    result,
                    content=EVIDENCE_GATE_CONTINUATION_MESSAGE,
                    session_action="continue",
                    finish_reason=None,
                    quality_flags=[
                        *result.quality_flags,
                        "natural_closure_suppressed_by_evidence_gate",
                    ],
                )
            if result.finish_reason == "user_requested":
                finish_intent = classify_user_finish_request(user_turn.content)
                if finish_intent != "confirmed":
                    return replace(
                        result,
                        content=(
                            AMBIGUOUS_USER_FINISH_INTENT_MESSAGE
                            if finish_intent == "ambiguous"
                            else EVIDENCE_GATE_CONTINUATION_MESSAGE
                        ),
                        session_action="continue",
                        finish_reason=None,
                        quality_flags=[
                            *result.quality_flags,
                            (
                                "ambiguous_user_finish_intent_requires_confirmation"
                                if finish_intent == "ambiguous"
                                else "unconfirmed_user_finish_intent_suppressed"
                            ),
                        ],
                    )
                return replace(
                    result,
                    content=USER_FINISH_INTENT_MESSAGE,
                    session_action="continue",
                    finish_reason=None,
                    quality_flags=[
                        *result.quality_flags,
                        "user_finish_intent_requires_evidence_snapshot",
                    ],
                )
        confirmation_enabled = (
            result.prompt_version == "v6.1.1"
            or session.consent_version in CLOSURE_CONFIRMATION_CONSENT_VERSIONS
        )
        if (
            confirmation_enabled
            and result.session_action == "finish"
            and result.finish_reason in MODEL_NATURAL_CLOSE_REASONS
        ):
            return replace(
                result,
                content=SAFE_CLOSURE_SUGGESTION_MESSAGE,
                session_action=SUGGEST_FINISH_ACTION,
                quality_flags=[
                    *result.quality_flags,
                    "closure_suggestion_message_normalized",
                ],
            )
        if result.session_action == "finish":
            session.phase = "finalizing"
            session.finalization_state = "awaiting_scoring"
        return result

    def freeze_transcript(self, session: AssessmentSession) -> str:
        if session.phase != "finalizing":
            raise FinalizationError("session_not_finalizing")
        fingerprint = transcript_fingerprint(session)
        if session.transcript_fingerprint and session.transcript_fingerprint != fingerprint:
            raise FinalizationError("frozen_transcript_changed")
        session.transcript_fingerprint = fingerprint
        session.transcript_frozen_at = session.transcript_frozen_at or utcnow()
        session.finalization_state = "frozen"
        return fingerprint

    def assess_report_readiness(
        self, session: AssessmentSession
    ) -> ReportReadinessAssessment:
        """Apply the formal scorer's existing evidence rules without mutation.

        The transcript remains open and the validated output is not stored as
        a score, evidence item, or report.  Formal finalization therefore
        always performs its own independent scorer call against the frozen
        transcript.
        """

        transcript = _transcript_rows(session)
        call = self.gateway.generate_final_scorer({"transcript": transcript})
        validated = self._validate_final_output(call.output, transcript)
        sufficient_dimension_count = sum(
            1
            for dimension in validated.dimensions
            if dimension.score is not None
            and dimension.sufficient
            and bool(dimension.quotes)
        )
        minimum_turns_required = settings.natural_interview_min_user_turns
        minimum_turns_met = session.user_answer_count >= minimum_turns_required
        evidence_ready = sufficient_dimension_count == len(DIMENSIONS)
        return ReportReadinessAssessment(
            ready=evidence_ready and minimum_turns_met,
            evidence_ready=evidence_ready,
            sufficient_dimension_count=sufficient_dimension_count,
            minimum_turns_required=minimum_turns_required,
            minimum_turns_met=minimum_turns_met,
            provider=call.provider,
            model=call.model,
            prompt_template_id=NATURAL_FINAL_SCORER_PROMPT_ID,
            prompt_version=NATURAL_FINAL_SCORER_PROMPT_VERSION,
            repair_used=call.repair_used,
            latency_ms=call.latency_ms,
            attempt_count=call.attempt_count,
            output=validated,
        )

    def assess_incremental_evidence(
        self,
        *,
        previous_snapshot: dict[str, Any] | None,
        new_user_turns: list[dict[str, Any]],
        transcript: list[dict[str, Any]],
    ) -> ReportReadinessAssessment:
        call = self.gateway.generate_incremental_evidence(
            {
                "previous_snapshot": previous_snapshot,
                "new_user_turns": new_user_turns,
            }
        )
        validated = self._validate_final_output(call.output, transcript)
        sufficient_dimension_count = sum(
            1
            for dimension in validated.dimensions
            if dimension.score is not None
            and dimension.sufficient
            and bool(dimension.quotes)
        )
        minimum_turns_required = settings.natural_interview_min_user_turns
        saved_user_answer_count = sum(
            1 for item in transcript if item.get("role") == "user"
        )
        minimum_turns_met = saved_user_answer_count >= minimum_turns_required
        evidence_ready = sufficient_dimension_count == len(DIMENSIONS)
        return ReportReadinessAssessment(
            ready=evidence_ready and minimum_turns_met,
            evidence_ready=evidence_ready,
            sufficient_dimension_count=sufficient_dimension_count,
            minimum_turns_required=minimum_turns_required,
            minimum_turns_met=minimum_turns_met,
            provider=call.provider,
            model=call.model,
            prompt_template_id=INCREMENTAL_EVIDENCE_PROMPT_ID,
            prompt_version=INCREMENTAL_EVIDENCE_PROMPT_VERSION,
            repair_used=call.repair_used,
            latency_ms=call.latency_ms,
            attempt_count=call.attempt_count,
            output=validated,
        )

    def assess_evidence_attribution(
        self,
        *,
        transcript: list[dict[str, Any]],
    ) -> EvidenceAttributionAssessment:
        payload = {"user_turns": _attribution_user_turns(transcript)}
        if not payload["user_turns"]:
            # Ending before the first answer is an explicit product path, not an
            # attribution failure. With no participant text there is nothing to
            # classify, so record a deterministic empty audit instead of making
            # a billable model call or weakening the non-empty span contract.
            return EvidenceAttributionAssessment(
                spans=[],
                provider="system",
                model="empty-participant-transcript-v1",
                prompt_template_id=EVIDENCE_ATTRIBUTION_PROMPT_ID,
                prompt_version=EVIDENCE_ATTRIBUTION_PROMPT_VERSION,
                schema_version=EVIDENCE_ATTRIBUTION_SCHEMA_VERSION,
                repair_used=False,
                latency_ms=0,
                attempt_count=0,
                input_fingerprint=payload_fingerprint(payload),
            )
        call = self.gateway.generate_evidence_attribution(payload)
        span_candidates = [
            candidate
            for turn in payload["user_turns"]
            for candidate in turn["span_candidates"]
        ]
        materialized = materialize_attribution_output(
            call.output,
            span_candidates,
        )
        return EvidenceAttributionAssessment(
            spans=validate_attribution_output(
                materialized,
                transcript,
                span_candidates=span_candidates,
            ),
            provider=call.provider,
            model=call.model,
            prompt_template_id=EVIDENCE_ATTRIBUTION_PROMPT_ID,
            prompt_version=EVIDENCE_ATTRIBUTION_PROMPT_VERSION,
            schema_version=EVIDENCE_ATTRIBUTION_SCHEMA_VERSION,
            repair_used=call.repair_used,
            latency_ms=call.latency_ms,
            attempt_count=call.attempt_count,
            input_fingerprint=payload_fingerprint(payload),
        )

    def assess_attributed_evidence(
        self,
        *,
        eligible_spans: list[EvidenceAttributionSpan],
        transcript: list[dict[str, Any]],
        expected_check_id: int,
        expected_transcript_fingerprint: str,
        expected_asset_fingerprint: str,
    ) -> ReportReadinessAssessment:
        payload = {
            "eligible_spans": [
                {
                    "attribution_span_id": span.id,
                    "turn_index": span.turn_index,
                    "start": span.start,
                    "end": span.end,
                    "text": span.quote,
                    "owner": span.owner,
                    "relation": span.relation,
                    "elicitation_level": span.elicitation_level,
                    "eligibility": span.eligibility,
                    "preceding_question": next(
                        (
                            str(item["content"])
                            for item in reversed(transcript)
                            if int(item["turn_index"]) < span.turn_index
                            and item.get("role") == "assistant"
                        ),
                        None,
                    ),
                }
                for span in eligible_spans
            ]
        }
        if eligible_spans:
            call = self.gateway.generate_attributed_evidence(payload)
        else:
            # No eligible participant-owned reasoning can never support a
            # numeric score. This deterministic IE result is the hard gate's
            # only valid outcome and prevents an empty registry from becoming a
            # model-hallucinated score or an unnecessary provider call.
            call = StructuredCallResult(
                output=AttributedFinalScorerOutput.model_validate(
                    {
                        "dimensions": [
                            {
                                "dimension_key": dimension.key,
                                "score": None,
                                "evidence_refs": [],
                                "reason": "没有可用于自动评分的本人推理片段。",
                                "confidence": 0.0,
                                "sufficient": False,
                            }
                            for dimension in DIMENSIONS
                        ],
                        "strengths": [],
                        "priorities": [],
                    }
                ),
                provider="system",
                model="no-eligible-span-gate-v1",
                repair_used=False,
                latency_ms=0,
                attempt_count=0,
            )
        validated = self._validate_attributed_output(
            call.output,
            eligible_spans,
            expected_check_id=expected_check_id,
            expected_transcript_fingerprint=expected_transcript_fingerprint,
            expected_asset_fingerprint=expected_asset_fingerprint,
        )
        sufficient_dimension_count = sum(
            1
            for dimension in validated.dimensions
            if dimension.score is not None
            and dimension.sufficient
            and bool(dimension.evidence_refs)
        )
        minimum_turns_required = settings.natural_interview_min_user_turns
        saved_user_answer_count = sum(
            1 for item in transcript if item.get("role") == "user"
        )
        minimum_turns_met = saved_user_answer_count >= minimum_turns_required
        evidence_ready = sufficient_dimension_count == len(DIMENSIONS)
        return ReportReadinessAssessment(
            ready=evidence_ready and minimum_turns_met,
            evidence_ready=evidence_ready,
            sufficient_dimension_count=sufficient_dimension_count,
            minimum_turns_required=minimum_turns_required,
            minimum_turns_met=minimum_turns_met,
            provider=call.provider,
            model=call.model,
            prompt_template_id=ATTRIBUTED_EVIDENCE_PROMPT_ID,
            prompt_version=ATTRIBUTED_EVIDENCE_PROMPT_VERSION,
            repair_used=call.repair_used,
            latency_ms=call.latency_ms,
            attempt_count=call.attempt_count,
            output=validated,
        )

    def finalize(
        self,
        db: Session,
        session: AssessmentSession,
        *,
        precomputed: StructuredCallResult[
            FinalScorerOutput | AttributedFinalScorerOutput
        ]
        | None = None,
        precomputed_prompt_template_id: str | None = None,
        precomputed_prompt_version: str | None = None,
        attribution_spans: list[EvidenceAttributionSpan] | None = None,
        readiness_check_id: int | None = None,
        expected_asset_fingerprint: str | None = None,
    ) -> AssessmentSession:
        if session.report and session.phase == "completed":
            return session
        if session.phase != "finalizing":
            raise FinalizationError("session_not_finalizing")

        frozen_fingerprint = self.freeze_transcript(session)
        db.commit()
        db.refresh(session)
        attempt = int(
            db.scalar(
                select(func.max(ScoringRun.attempt_number)).where(
                    ScoringRun.session_id == session.id
                )
            )
            or 0
        ) + 1
        run = ScoringRun(
            session_id=session.id,
            attempt_number=attempt,
            status="processing",
            transcript_fingerprint=frozen_fingerprint,
            model_provider="pending",
            model_name="pending",
            prompt_template_id=(
                precomputed_prompt_template_id or NATURAL_FINAL_SCORER_PROMPT_ID
            ),
            prompt_version=(
                precomputed_prompt_version or NATURAL_FINAL_SCORER_PROMPT_VERSION
            ),
        )
        db.add(run)
        db.commit()
        try:
            transcript = _transcript_rows(session)
            call = precomputed or self.gateway.generate_final_scorer(
                {"transcript": transcript}
            )
            strong_scaffold_review = False
            if isinstance(call.output, AttributedFinalScorerOutput):
                if (
                    attribution_spans is None
                    or readiness_check_id is None
                    or expected_asset_fingerprint is None
                ):
                    raise FinalizationError(
                        "attributed_finalization_context_missing"
                    )
                validated_attributed = self._validate_attributed_output(
                    call.output,
                    attribution_spans,
                    expected_check_id=readiness_check_id,
                    expected_transcript_fingerprint=frozen_fingerprint,
                    expected_asset_fingerprint=expected_asset_fingerprint,
                )
                span_by_id = {span.id: span for span in attribution_spans}
                strong_scaffold_review = any(
                    dimension.score is not None
                    and bool(dimension.evidence_refs)
                    and all(
                        span_by_id[reference.attribution_span_id].elicitation_level
                        == "strong_scaffold"
                        for reference in dimension.evidence_refs
                    )
                    for dimension in validated_attributed.dimensions
                )
                validated_for_report = self._legacy_output_from_attributed(
                    validated_attributed, attribution_spans
                )
                run.result_data = validated_attributed.model_dump(mode="json")
                self._persist_attributed_evidence(
                    db,
                    session,
                    run,
                    validated_attributed,
                    attribution_spans,
                    readiness_check_id=readiness_check_id,
                )
                report_version = "v6.2.1"
            else:
                validated_for_report = self._validate_final_output(
                    call.output, transcript
                )
                run.result_data = validated_for_report.model_dump(mode="json")
                self._persist_evidence(db, session, run, validated_for_report)
                report_version = "v6.2" if precomputed is not None else "v6.0"
            run.model_provider = call.provider
            run.model_name = call.model
            run.repair_used = call.repair_used
            run.status = "completed"
            run.completed_at = utcnow()
            report_data = self._build_report(session, validated_for_report)
            report_data["manual_review_recommended"] = (
                bool(report_data["manual_review_recommended"])
                or strong_scaffold_review
            )
            db.add(
                AssessmentReport(
                    session_id=session.id,
                    version=report_version,
                    report_data=report_data,
                    evidence_fingerprint=frozen_fingerprint,
                )
            )
            session.phase = "completed"
            session.finalization_state = "completed"
            session.completed_at = utcnow()
            requires_manual_review = session.ended_early or any(
                dimension.score is None
                for dimension in validated_for_report.dimensions
            ) or strong_scaffold_review
            run.manual_review_recommended = requires_manual_review
            session.manual_review_recommended = requires_manual_review
            db.commit()
            db.refresh(session)
            return session
        except Exception as exc:
            db.rollback()
            failed = db.get(ScoringRun, run.id)
            if failed:
                failed.status = "failed"
                failed.error = f"{type(exc).__name__}: {str(exc)}"[:1000]
                failed.completed_at = utcnow()
                failed.manual_review_recommended = True
            failed_session = db.get(AssessmentSession, session.id)
            if failed_session:
                failed_session.phase = "finalizing"
                failed_session.finalization_state = "failed"
                failed_session.manual_review_recommended = True
            db.commit()
            raise FinalizationError(str(exc)) from exc

    @staticmethod
    def _result_from_call(
        call: StructuredCallResult[NaturalInterviewerOutput],
        *,
        input_fingerprint: str,
        latest_user_text: str | None = None,
        prompt_version: str = NATURAL_INTERVIEWER_PROMPT_VERSION,
        transcript: list[dict[str, Any]] | None = None,
    ) -> InterviewResult:
        quality_flags = _validate_interviewer_output(call.output, latest_user_text)
        prompt_template_id, resolved_version, _ = resolve_natural_interviewer_prompt(
            prompt_version
        )
        return InterviewResult(
            content=call.output.interviewer_message.strip(),
            session_action=call.output.session_action,
            finish_reason=call.output.finish_reason,
            provider=call.provider,
            model=call.model,
            prompt_template_id=prompt_template_id,
            prompt_version=resolved_version,
            repair_used=call.repair_used,
            latency_ms=call.latency_ms,
            attempt_count=call.attempt_count,
            quality_flags=quality_flags,
            input_fingerprint=input_fingerprint,
            model_session_action=call.output.session_action,
            model_finish_reason=call.output.finish_reason,
            navigation=_validated_navigation(
                call.output,
                prompt_version=resolved_version,
                transcript=transcript or [],
            ),
        )

    @staticmethod
    def _validate_attributed_output(
        output: AttributedFinalScorerOutput,
        spans: list[EvidenceAttributionSpan],
        *,
        expected_check_id: int,
        expected_transcript_fingerprint: str,
        expected_asset_fingerprint: str,
    ) -> AttributedFinalScorerOutput:
        by_id = {span.id: span for span in spans}

        def validated_span(span_id: int) -> EvidenceAttributionSpan:
            span = by_id.get(span_id)
            if span is None:
                raise FinalizationError("attributed_scorer_referenced_unknown_span")
            if span.readiness_check_id != expected_check_id:
                raise FinalizationError("attributed_scorer_referenced_wrong_check")
            if span.transcript_fingerprint != expected_transcript_fingerprint:
                raise FinalizationError("attributed_scorer_referenced_stale_transcript")
            if span.asset_fingerprint != expected_asset_fingerprint:
                raise FinalizationError("attributed_scorer_referenced_stale_assets")
            if span.eligibility != "eligible" or span.validation_status != "validated":
                raise FinalizationError("attributed_scorer_referenced_ineligible_span")
            if attribution_text_hash(span.quote) != span.text_hash:
                raise FinalizationError("persisted_attribution_span_hash_mismatch")
            return span

        normalized_dimensions = []
        for dimension in output.dimensions:
            if dimension.score is None:
                if dimension.evidence_refs or dimension.sufficient:
                    raise FinalizationError(
                        "null_score_has_attribution_refs_or_sufficiency"
                    )
            elif not dimension.sufficient or not dimension.evidence_refs:
                raise FinalizationError(
                    "numeric_score_without_eligible_attribution_refs"
                )
            for reference in dimension.evidence_refs:
                validated_span(reference.attribution_span_id)
            reason = (
                dimension.reason
                if _public_report_text_is_safe(dimension.reason)
                else _neutral_reason_for(dimension.score)
            )
            normalized_dimensions.append(
                dimension.model_copy(update={"reason": reason})
            )

        def safe_summaries(items):
            safe = []
            for item in items:
                for span_id in item.attribution_span_ids:
                    validated_span(span_id)
                if _public_report_text_is_safe(item.text):
                    safe.append(item)
            return safe

        return output.model_copy(
            update={
                "dimensions": normalized_dimensions,
                "strengths": safe_summaries(output.strengths),
                "priorities": safe_summaries(output.priorities),
            }
        )

    @staticmethod
    def _validate_final_output(
        output: FinalScorerOutput, transcript: list[dict[str, Any]]
    ) -> FinalScorerOutput:
        user_turns = {
            int(item["turn_index"]): str(item["content"])
            for item in transcript
            if item["role"] == "user"
        }

        def with_safe_public_reason(dimension):
            if _public_report_text_is_safe(dimension.reason):
                return dimension
            return dimension.model_copy(
                update={"reason": _neutral_reason_for(dimension.score)}
            )

        normalized_dimensions = []
        for dimension in output.dimensions:
            if dimension.score is None:
                if dimension.quotes or dimension.sufficient:
                    raise FinalizationError("null_score_has_evidence_or_sufficiency")
                normalized_dimensions.append(with_safe_public_reason(dimension))
                continue
            if not dimension.sufficient or not dimension.quotes:
                raise FinalizationError("numeric_score_without_sufficient_evidence")
            usable_quotes = []
            for evidence in dimension.quotes:
                source = user_turns.get(evidence.turn_index)
                if source is None or evidence.quote not in source:
                    raise FinalizationError("score_quote_is_not_exact_user_substring")
                if not (
                    _is_self_label_or_prompted_claim(evidence.quote)
                    or _source_turn_is_only_self_label(source)
                ):
                    usable_quotes.append(evidence)
            if not usable_quotes:
                # This is not a scorer transport failure: the transcript simply
                # lacks admissible behavioural evidence for the claimed view.
                # Preserve the session flow and report it as not sufficiently
                # measured instead of coercing a score or forcing another turn.
                normalized_dimensions.append(
                    with_safe_public_reason(
                        dimension.model_copy(
                            update={
                                "score": None,
                                "quotes": [],
                                "reason": "证据有限，用户的自我评价不能作为能力证据。",
                                "confidence": 0.0,
                                "sufficient": False,
                            }
                        )
                    )
                )
                continue
            normalized_dimensions.append(
                with_safe_public_reason(
                    dimension.model_copy(update={"quotes": usable_quotes})
                )
            )
        return output.model_copy(
            update={
                "dimensions": normalized_dimensions,
                "strengths": [
                    item.strip()
                    for item in output.strengths
                    if _public_report_text_is_safe(item)
                ],
                "priorities": [
                    item.strip()
                    for item in output.priorities
                    if _public_report_text_is_safe(item)
                ],
            }
        )

    @staticmethod
    def _legacy_output_from_attributed(
        output: AttributedFinalScorerOutput,
        spans: list[EvidenceAttributionSpan],
    ) -> FinalScorerOutput:
        """Adapt validated IDs to the unchanged participant report DTO."""

        by_id = {span.id: span for span in spans}
        return FinalScorerOutput.model_validate(
            {
                "dimensions": [
                    {
                        "dimension_key": dimension.dimension_key,
                        "score": dimension.score,
                        "quotes": [
                            {
                                "turn_index": by_id[
                                    reference.attribution_span_id
                                ].turn_index,
                                "quote": by_id[
                                    reference.attribution_span_id
                                ].quote,
                            }
                            for reference in dimension.evidence_refs
                        ],
                        "reason": dimension.reason,
                        "confidence": dimension.confidence,
                        "sufficient": dimension.sufficient,
                    }
                    for dimension in output.dimensions
                ],
                "strengths": [item.text for item in output.strengths],
                "priorities": [item.text for item in output.priorities],
            }
        )

    @staticmethod
    def _persist_attributed_evidence(
        db: Session,
        session: AssessmentSession,
        run: ScoringRun,
        output: AttributedFinalScorerOutput,
        spans: list[EvidenceAttributionSpan],
        *,
        readiness_check_id: int,
    ) -> None:
        by_id = {span.id: span for span in spans}
        for dimension in output.dimensions:
            if dimension.score is None:
                continue
            for reference in dimension.evidence_refs:
                span = by_id[reference.attribution_span_id]
                db.add(
                    EvidenceItem(
                        session_id=session.id,
                        scoring_run_id=run.id,
                        user_turn_id=span.user_turn_id,
                        attribution_span_id=span.id,
                        readiness_check_id=readiness_check_id,
                        dimension_key=dimension.dimension_key,
                        quote=span.quote,
                        quote_start=span.start,
                        quote_end=span.end,
                        confidence=dimension.confidence,
                        validation_status="validated",
                        validation_reason=(
                            "eligible_strong_scaffold_manual_review"
                            if span.elicitation_level == "strong_scaffold"
                            else "eligible_attribution_span_reference"
                        ),
                    )
                )

    @staticmethod
    def _persist_evidence(
        db: Session,
        session: AssessmentSession,
        run: ScoringRun,
        output: FinalScorerOutput,
    ) -> None:
        turns = {
            turn.turn_index: turn
            for turn in session.turns
            if turn.role == "user"
        }
        for dimension in output.dimensions:
            if dimension.score is None:
                continue
            for quote in dimension.quotes:
                turn = turns[quote.turn_index]
                start = turn.content.index(quote.quote)
                db.add(
                    EvidenceItem(
                        session_id=session.id,
                        scoring_run_id=run.id,
                        user_turn_id=turn.id,
                        dimension_key=dimension.dimension_key,
                        quote=quote.quote,
                        quote_start=start,
                        quote_end=start + len(quote.quote),
                        confidence=dimension.confidence,
                        validation_status="legacy_unclassified",
                        validation_reason="legacy_quote_contract",
                    )
                )

    @staticmethod
    def _build_report(
        session: AssessmentSession, output: FinalScorerOutput
    ) -> dict[str, Any]:
        evidence_by_dimension: dict[str, list[dict[str, Any]]] = {
            item.key: [] for item in DIMENSIONS
        }
        # Build the public report directly from the just-validated scorer
        # output. SQLAlchemy relationship collections may be stale until the
        # next flush, whereas these quotes have already passed the exact-user-
        # substring invariant and are about to be persisted as EvidenceItem.
        for result in output.dimensions:
            if result.score is not None:
                evidence_by_dimension[result.dimension_key] = [
                    {
                        "turn_index": quote.turn_index,
                        "quote": quote.quote,
                        "source_type": "user",
                    }
                    for quote in result.quotes
                ]

        entries: list[dict[str, Any]] = []
        by_key = {item.dimension_key: item for item in output.dimensions}
        user_has_content = session.user_answer_count > 0
        for dimension in DIMENSIONS:
            result = by_key[dimension.key]
            score = result.score
            entries.append(
                {
                    "dimension_key": dimension.key,
                    "dimension_name": dimension.name,
                    "score": score,
                    "status": "sufficient" if score is not None else (
                        "limited" if user_has_content else "unmeasured"
                    ),
                    "reason": result.reason,
                    "strength": (
                        result.reason
                        if score is not None
                        else "本次证据有限，暂不形成该维度的优势判断。"
                    ),
                    "suggestion": _PUBLIC_DIMENSION_SUGGESTIONS[dimension.key],
                    "evidences": evidence_by_dimension[dimension.key],
                    "observable_behaviors": list(dimension.observable_behaviors),
                }
            )
        ended_early_notice = (
            "本次访谈由用户在达到完整报告准备条件前结束；报告仅依据当时已保存的回答，"
            "证据有限部分会如实标注。"
            if session.ended_early
            else None
        )
        return {
            "session_uuid": session.uuid,
            "experimental_notice": " ".join(
                item
                for item in (
                    ended_early_notice,
                    "思衡 V6 是探索性、非标准化的自然访谈演示，不支持跨用户比较或正式效度结论。",
                )
                if item
            ),
            "summary": (
                "本次为提前结束报告。报告只整理当时已保存且可核对的用户原话；"
                "缺少证据的视角不会被补问或强行评分。"
                if session.ended_early
                else "报告只整理本次访谈中可核对的用户原话；缺少证据的视角不会被补问或强行评分。"
            ),
            "dimensions": entries,
            "strengths": output.strengths,
            "priorities": output.priorities,
            "manual_review_recommended": session.ended_early
            or any(item.score is None for item in output.dimensions),
            "disclaimer": "数字结果不是人格判断、职业建议或综合排名；请结合具体情境谨慎理解。",
        }

    @staticmethod
    def trace_input_fingerprint(session: AssessmentSession) -> str:
        return payload_fingerprint({"transcript": _transcript_rows(session)})
