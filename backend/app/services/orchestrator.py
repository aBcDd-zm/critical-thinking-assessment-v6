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
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.domain.catalog import DIMENSIONS, DIMENSION_BY_KEY
from app.domain.final_scorer_contract import FINAL_SCORER_BARS_CONTRACT_SHA256
from app.domain.interview_protocol import MIN_VALID_ANSWERS, MAX_USER_ANSWERS
from app.models import (
    AssessmentReport,
    AssessmentSession,
    DialogueTurn,
    EvidenceItem,
    ScoringRun,
    utcnow,
)
from app.schemas import FinalScorerOutput, NaturalInterviewerOutput
from app.services.model_gateway import (
    NATURAL_FINAL_SCORER_PROMPT_ID,
    NATURAL_FINAL_SCORER_PROMPT_VERSION,
    NATURAL_INTERVIEWER_PROMPT_ID,
    NATURAL_INTERVIEWER_PROMPT_VERSION,
    ModelGatewayError,
    ModelGatewayService,
    StructuredCallResult,
    payload_fingerprint,
)


FINALIZING_MESSAGE = "这段访谈已被冻结，正在仅依据你的原话整理报告。"
SAFETY_STOP_MESSAGE = (
    "你刚才提到的内容可能涉及当下的人身安全。此刻比继续访谈更重要的是先获得"
    "现实中的即时支持；如果你或他人有立即危险，请联系当地紧急服务、身边可信的人，"
    "或尽快到安全的地方。我们先在这里停下。"
)

# This is a release gate, not an interview script or a dimension-coverage
# controller. It controls when the independently chosen natural conversation
# may close; it never chooses a topic or question for the model.
MINIMUM_EVIDENCE_CONTINUATION = (
    "我们先不急着收束。为了不把你的意思理解得太快，你愿意再讲一个"
    "具体情况，让我更明白刚才的想法是怎样形成的吗？"
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
    "伤害他人",
    "伤害别人",
    "伤害其他人",
    "伤害自己",
    "伤害我自己",
    "我要伤害他",
    "我想伤害他",
    "我要伤害她",
    "我想伤害她",
    "我要伤害你",
    "我想伤害你",
    "我要伤害人",
    "我想伤害人",
    "杀害他人",
    "杀害别人",
    "杀害其他人",
    "我要杀人",
    "我想杀人",
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
    quality_flags: list[str]
    input_fingerprint: str
    requested_model: str | None = None
    actual_model: str | None = None
    response_id: str | None = None
    request_id: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    transport_retry_count: int = 0


def _assign_model_provenance(target: object, source: object) -> None:
    """Copy only non-content model-call audit fields onto an ORM row."""

    requested_model = getattr(source, "requested_model", None)
    actual_model = getattr(source, "actual_model", None) or getattr(
        source, "model", None
    )
    provider = getattr(source, "provider", None)
    if isinstance(provider, str) and provider:
        setattr(target, "model_provider", provider)
    if isinstance(actual_model, str) and actual_model:
        setattr(target, "model_name", actual_model)
    if requested_model is not None:
        setattr(target, "requested_model", requested_model)
    if actual_model is not None:
        setattr(target, "actual_model", actual_model)
    for field in (
        "response_id",
        "request_id",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "transport_retry_count",
    ):
        value = getattr(source, field, None)
        if field == "transport_retry_count":
            value = int(value or 0)
        setattr(target, field, value)


def normalized_text(value: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKC", value).casefold().translate(_TRANSLATION)
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )


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


def _minimum_model_finish_evidence_met(session: AssessmentSession) -> bool:
    """Return whether a model-led close has reached the V6.1 minimum.

    ``user_answer_count`` is the persisted count of valid answers. Explicit
    clarification turns do not increment it. Topic and dimension selection
    remain entirely outside this deterministic gate.
    """

    return session.user_answer_count >= MIN_VALID_ANSWERS


def _protocol_gate_result(
    *,
    content: str,
    session_action: str,
    finish_reason: str | None,
    quality_flags: list[str],
    input_text: str,
) -> InterviewResult:
    """Build an auditable result without spending another model call."""

    return InterviewResult(
        content=content,
        session_action=session_action,
        finish_reason=finish_reason,
        provider="protocol_gate",
        model="none",
        prompt_template_id="natural_interviewer_v6.1_release_gate",
        prompt_version=NATURAL_INTERVIEWER_PROMPT_VERSION,
        repair_used=False,
        latency_ms=0,
        quality_flags=quality_flags,
        input_fingerprint=hashlib.sha256(input_text.encode("utf-8")).hexdigest(),
    )


def _user_explicitly_requests_interview_end(value: str) -> bool:
    """Recognize a direct request to stop this conversation, not a topic word.

    The model is not trusted to grant itself the ``user_requested`` bypass.
    Typed requests remain available alongside the explicit finalize endpoint,
    while phrases such as ``结束这个项目`` do not accidentally end the
    interview merely because they contain the word ``结束``.
    """

    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized:
        return False
    direct_requests = (
        r"^(?:我)?(?:现在)?(?:想|要|希望|决定)?(?:就|先)?(?:结束|停止|退出)(?:这次|本次)?(?:访谈|对话|聊天|交流)?(?:了|吧)?$",
        r"^(?:我)?不想(?:再)?(?:继续|聊|回答|说)(?:了|下去)?$",
        r"^(?:就)?到这里(?:就好|可以)?(?:了|吧)?$",
        r"^(?:先)?这样(?:就好|可以)?(?:了|吧)?$",
        r"^(?:不用|不要|别)(?:再)?(?:问|继续)(?:了|吧)?$",
        r"^(?:现在)?(?:请)?(?:结束访谈|结束对话|停止访谈|停止对话|生成报告)(?:了|吧)?$",
    )
    # A participant may first finish a substantive answer and then append a
    # direct request to stop.  Evaluate the whole response and its final
    # punctuation-delimited clause, while retaining full-match semantics so
    # topic phrases such as "结束这个项目" cannot trigger the bypass.
    clauses = [normalized]
    clauses.extend(
        part
        for part in re.split(r"[，,。.!！?？；;:：\n]+", normalized)
        if part.strip()
    )
    for clause in clauses[-2:]:
        compact = re.sub(r"\s+", "", clause)
        if any(re.fullmatch(pattern, compact) for pattern in direct_requests):
            return True
    return False


def transcript_fingerprint(session: AssessmentSession) -> str:
    canonical = json.dumps(
        _transcript_rows(session), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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


class InterviewOrchestrator:
    def __init__(self, gateway: ModelGatewayService | None = None) -> None:
        self.gateway = gateway or ModelGatewayService()

    def opening(self, participant: dict[str, str]) -> InterviewResult:
        payload = {"participant": participant, "transcript": []}
        call = self.gateway.generate_opening(participant)
        return self._result_from_call(call, input_fingerprint=payload_fingerprint(payload))

    def process(self, session: AssessmentSession, user_turn: DialogueTurn) -> InterviewResult:
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
                quality_flags=["safety_stopped"],
                input_fingerprint=hashlib.sha256(user_turn.content.encode("utf-8")).hexdigest(),
            )
        if _user_explicitly_requests_interview_end(user_turn.content):
            if session.user_answer_count < MIN_VALID_ANSWERS:
                session.phase = "exited"
                session.finalization_state = "withdrawn_incomplete"
                session.ended_early = True
                return _protocol_gate_result(
                    content=(
                        "好的，这次访谈已经停止。本次记录会保留为未完成访谈，"
                        "不会生成正式报告。"
                    ),
                    session_action="finish",
                    finish_reason="user_requested",
                    quality_flags=["user_withdrew_before_minimum"],
                    input_text=user_turn.content,
                )
            session.phase = "finalizing"
            session.finalization_state = "awaiting_scoring"
            return _protocol_gate_result(
                content="好的，谢谢你完成这次访谈。接下来将只依据你的原话整理报告。",
                session_action="finish",
                finish_reason="user_requested",
                quality_flags=["user_requested_finish_after_minimum"],
                input_text=user_turn.content,
            )
        if session.user_answer_count >= MAX_USER_ANSWERS:
            session.phase = "finalizing"
            session.finalization_state = "awaiting_scoring"
            return _protocol_gate_result(
                content="谢谢你完成这次访谈。接下来将只依据你的原话整理报告。",
                session_action="finish",
                # This is a deterministic protocol close, never a natural or
                # model-led closure. Keep the origin explicit in the public
                # event as well as the provider and quality flag.
                finish_reason="technical_limit",
                quality_flags=["technical_maximum_reached"],
                input_text=user_turn.content,
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
            "completion_gate": {
                "valid_answer_count": session.user_answer_count,
                "minimum_valid_answers": MIN_VALID_ANSWERS,
                "maximum_user_answers": MAX_USER_ANSWERS,
                "can_model_finish": session.user_answer_count >= MIN_VALID_ANSWERS,
            },
        }
        call = self.gateway.generate_interviewer(payload)
        result = self._result_from_call(
            call,
            input_fingerprint=payload_fingerprint(payload),
            latest_user_text=user_turn.content,
        )
        # Direct user exit requests have already been handled above. A model
        # cannot manufacture the user-requested bypass from unrelated text.
        trusted_user_requested = False
        if result.finish_reason == "user_requested" and not trusted_user_requested:
            result = replace(
                result,
                finish_reason="natural_closure",
                quality_flags=[
                    *result.quality_flags,
                    "unverified_user_requested_finish_reason",
                ],
            )
        if (
            result.session_action == "finish"
            and not _minimum_model_finish_evidence_met(session)
        ):
            result = replace(
                result,
                content=MINIMUM_EVIDENCE_CONTINUATION,
                session_action="continue",
                finish_reason=None,
                quality_flags=[
                    *result.quality_flags,
                    "finish_deferred_minimum_valid_answers",
                    "deterministic_release_guard",
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

    def finalize(self, db: Session, session: AssessmentSession) -> AssessmentSession:
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
            model_provider=(
                "deepseek" if settings.model_gateway_mode == "real" else "mock"
            ),
            model_name="pending",
            requested_model=(
                settings.deepseek_model
                if settings.model_gateway_mode == "real"
                else None
            ),
            prompt_template_id=NATURAL_FINAL_SCORER_PROMPT_ID,
            prompt_version=NATURAL_FINAL_SCORER_PROMPT_VERSION,
            final_scorer_contract_sha256=FINAL_SCORER_BARS_CONTRACT_SHA256,
        )
        db.add(run)
        db.commit()
        call: StructuredCallResult[FinalScorerOutput] | None = None
        try:
            transcript = _transcript_rows(session)
            call = self.gateway.generate_final_scorer({"transcript": transcript})
            _assign_model_provenance(run, call)
            validated = self._validate_final_output(call.output, transcript)
            run.repair_used = call.repair_used
            run.result_data = validated.model_dump(mode="json")
            run.status = "completed"
            run.completed_at = utcnow()
            self._persist_evidence(db, session, run, validated)
            report_data = self._build_report(session, validated)
            db.add(
                AssessmentReport(
                    session_id=session.id,
                    version="v6.0",
                    report_data=report_data,
                    evidence_fingerprint=frozen_fingerprint,
                )
            )
            session.phase = "completed"
            session.finalization_state = "completed"
            session.completed_at = utcnow()
            requires_manual_review = any(
                dimension.score is None for dimension in validated.dimensions
            )
            run.manual_review_recommended = requires_manual_review
            session.manual_review_recommended = requires_manual_review
            db.commit()
            db.refresh(session)
            return session
        except Exception as exc:
            db.rollback()
            failed = db.get(ScoringRun, run.id)
            if failed:
                _assign_model_provenance(failed, call or exc)
                failed.repair_used = bool(
                    getattr(call or exc, "repair_used", failed.repair_used)
                )
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
    ) -> InterviewResult:
        try:
            quality_flags = _validate_interviewer_output(call.output, latest_user_text)
        except InterviewContractError as exc:
            # A response rejected by the visibility/safety contract still has
            # useful non-content provenance for the failed AgentTrace.
            for field in (
                "provider",
                "model",
                "requested_model",
                "actual_model",
                "response_id",
                "request_id",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "transport_retry_count",
                "repair_used",
            ):
                setattr(exc, field, getattr(call, field, None))
            raise
        return InterviewResult(
            content=call.output.interviewer_message.strip(),
            session_action=call.output.session_action,
            finish_reason=call.output.finish_reason,
            provider=call.provider,
            model=call.model,
            prompt_template_id=NATURAL_INTERVIEWER_PROMPT_ID,
            prompt_version=NATURAL_INTERVIEWER_PROMPT_VERSION,
            repair_used=call.repair_used,
            latency_ms=call.latency_ms,
            quality_flags=quality_flags,
            input_fingerprint=input_fingerprint,
            requested_model=call.requested_model,
            actual_model=call.actual_model or call.model,
            response_id=call.response_id,
            request_id=call.request_id,
            prompt_tokens=call.prompt_tokens,
            completion_tokens=call.completion_tokens,
            total_tokens=call.total_tokens,
            transport_retry_count=call.transport_retry_count,
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
        for dimension in DIMENSIONS:
            result = by_key[dimension.key]
            score = result.score
            entries.append(
                {
                    "dimension_key": dimension.key,
                    "dimension_name": dimension.name,
                    "score": score,
                    "status": "sufficient" if score is not None else (
                        "limited" if result.opportunity_observed else "unmeasured"
                    ),
                    "reason": result.reason,
                    "observation": result.reason,
                    "strength": (
                        result.reason
                        if score is not None and score >= 4
                        else None
                    ),
                    "suggestion": _PUBLIC_DIMENSION_SUGGESTIONS[dimension.key],
                    "evidences": evidence_by_dimension[dimension.key],
                    "observable_behaviors": list(dimension.observable_behaviors),
                }
            )
        high_score_reasons = [
            item.reason
            for item in output.dimensions
            if item.score is not None and item.score >= 4
        ]
        # The scorer contract restricts public strengths to level 4--5
        # dimensions. Preserve its already-sanitized wording when present;
        # otherwise derive the same concept from high-level dimension reasons.
        public_strengths = output.strengths or high_score_reasons
        return {
            "session_uuid": session.uuid,
            "experimental_notice": "思衡 V6 是探索性、非标准化的自然访谈演示，不支持跨用户比较或正式效度结论。",
            "summary": "报告只整理本次访谈中可核对的用户原话；缺少证据的视角不会被补问或强行评分。",
            "dimensions": entries,
            "strengths": public_strengths[:2],
            "priorities": output.priorities,
            "manual_review_recommended": any(item.score is None for item in output.dimensions),
            "disclaimer": "数字结果不是人格判断、职业建议或综合排名；请结合具体情境谨慎理解。",
        }

    @staticmethod
    def trace_input_fingerprint(session: AssessmentSession) -> str:
        return payload_fingerprint({"transcript": _transcript_rows(session)})
