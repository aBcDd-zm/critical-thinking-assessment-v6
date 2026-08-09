#!/usr/bin/env python3
"""Private synthetic blind comparison for interviewer v6.2.1/v6.2.3.

The runner exercises the production ``generate_interviewer`` boundary with
canonical V6.2 anchor/source inputs and the production interview call profile.
It never reads the application database or participant data.  An explicit
confirmation is required before any directory is created or network request is
made.  Review text and the version key are written separately to a new,
repository-external private directory.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal
from urllib.parse import urlsplit


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent.resolve()
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import settings  # noqa: E402
from app.schemas import NaturalInterviewerOutput  # noqa: E402
from app.services import model_gateway as gateway_module  # noqa: E402
from app.services.model_gateway import (  # noqa: E402
    ModelGatewayService,
    build_interview_anchor_candidates,
    resolve_natural_interviewer_prompt,
    source_clarification_required,
)
from app.services.orchestrator import (  # noqa: E402
    _validate_interviewer_output,
    _validated_navigation,
)
from scripts import compare_natural_interviewer_prompts_v604 as private_io  # noqa: E402


EXPECTED_MODEL = "deepseek-v4-flash"
EXPECTED_PROVIDER = "deepseek"
EXPECTED_THINKING = "disabled"
EXPECTED_MAX_TOKENS = 512
OFFICIAL_DEEPSEEK_BASE_URL_ALLOWLIST = frozenset(
    {
        "https://api.deepseek.com",
        "https://api.deepseek.com/v1",
    }
)
OFFICIAL_DEEPSEEK_COMPLETION_URL = "https://api.deepseek.com/chat/completions"
OFFICIAL_DEEPSEEK_HOST = "api.deepseek.com"
CONFIRMATION = "RUN_V623_SYNTHETIC_BLIND_AB"
PROMPT_VERSIONS = ("v6.2.1", "v6.2.3")
REPLICATES_PER_SCENARIO = 2
ROUNDS_PER_ARM = 3
MAX_PHYSICAL_REQUESTS = 144
RUNNER_PROTOCOL = "natural_interviewer_v623_blind_ab_v1"
CANONICAL_OPENING = (
    "这里没有标准答案，我更关心你怎样作出判断。接下来我会一次只问一个问题。"
    "请从工作、学习或生活中，想起最近一件真实、具体、需要你认真判断或权衡的事情"
    "——当时最难判断的是什么？"
)

RoundPolicy = Literal["continue_question", "continue_optional_question", "finish"]


class RunnerHardStop(BaseException):
    """Bypass provider retry logic for a protocol-level hard stop."""


def official_deepseek_endpoint(base_url: str) -> dict[str, str]:
    normalized_base_url = str(base_url)
    if normalized_base_url not in OFFICIAL_DEEPSEEK_BASE_URL_ALLOWLIST:
        raise RunnerHardStop("deepseek_base_url_must_be_official")
    root_url = (
        normalized_base_url[:-3]
        if normalized_base_url.endswith("/v1")
        else normalized_base_url
    )
    completion_url = f"{root_url}/chat/completions"
    parsed = urlsplit(completion_url)
    if (
        completion_url != OFFICIAL_DEEPSEEK_COMPLETION_URL
        or parsed.scheme != "https"
        or parsed.hostname != OFFICIAL_DEEPSEEK_HOST
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RunnerHardStop("deepseek_completion_url_must_be_official")
    return {
        "configured_base_url": normalized_base_url,
        "completion_url": completion_url,
        "completion_url_sha256": private_io._sha256_text(completion_url),
        "host": OFFICIAL_DEEPSEEK_HOST,
        "host_sha256": private_io._sha256_text(OFFICIAL_DEEPSEEK_HOST),
    }


@dataclass(frozen=True)
class SyntheticScenario:
    scenario_type: str
    user_turns: tuple[str, str, str]
    round_policies: tuple[RoundPolicy, RoundPolicy, RoundPolicy]
    source_clarification_round: int | None = None
    return_to_mainline_round: int | None = None


SCENARIOS: tuple[SyntheticScenario, ...] = (
    SyntheticScenario(
        "relationship_loss_and_uncertainty",
        (
            "我刚失恋了，这两天总是哭。我不知道男朋友还爱不爱我，也不知道该怎么面对这件事。",
            "我怀疑他在和别人交往，因为他最近总躲着我回复消息，也取消了原来约好的见面。",
            "最难判断的是，这些变化到底说明关系已经结束，还是只是他最近压力很大；我不想只凭猜测下结论。",
        ),
        ("continue_question", "continue_question", "continue_question"),
    ),
    SyntheticScenario(
        "vague_work_complaint_to_event",
        (
            "最近上班全是琐事，我每天都很烦，也不知道应该从哪一件事情开始说起。",
            "最近一次是主管临时让我重做已经确认过的表格，同时同事还在等我交接另一项任务。",
            "我当时要判断是先按主管的新要求重做，还是先完成已经答应同事的交接，因为两边都会被耽误。",
        ),
        ("continue_question", "continue_question", "continue_question"),
    ),
    SyntheticScenario(
        "neutral_work_release_decision",
        (
            "我在决定课程项目要不要提前发布。核心功能完成了，不过两项边缘流程还没有测完。",
            "我主要看真实使用是否会经过那两项流程，以及出错以后能不能安全地回退。",
            "如果使用概率低而且有回退，我会按计划发布；否则就多留三天补完测试。",
        ),
        ("continue_question", "continue_question", "continue_question"),
    ),
    SyntheticScenario(
        "explicit_correction_and_repair",
        (
            "最近我把同时做的任务减少了，想把主要精力放在一个研究项目上。",
            "不是因为我怕失败。你刚才把重点理解错了，我担心的是任务相互依赖会拖累团队。",
            "我希望先把会阻塞别人的部分做完，再处理那些只会影响我自己的细节。",
        ),
        ("continue_question", "continue_optional_question", "continue_question"),
    ),
    SyntheticScenario(
        "privacy_boundary_and_topic_switch",
        (
            "有些家庭经历我不想谈。我更愿意聊一次团队意见冲突，以及我是怎么处理的。",
            "我先请双方分别说最担心的后果，发现一边担心延期，另一边担心质量。",
            "我们最后做了一个可以回退的小实验，约好第二天一起检查结果以后再决定。",
        ),
        ("continue_optional_question", "continue_question", "continue_question"),
    ),
    SyntheticScenario(
        "mixed_ai_source_and_return",
        (
            "我正在决定是否把一家公益面包坊作为研究选题，但不确定现有条件能不能支撑这个研究。",
            "我问过 AI，也给它看过论文材料，它建议只访谈门店员工；我自己觉得这个办法可能可行，但还没有完全相信。",
            "访谈门店员工是 AI 的建议，三角验证来自论文。我的判断是，只有还能接触顾客或管理者并交叉核实，我才会采用这个选题。",
        ),
        ("continue_question", "continue_question", "continue_question"),
        source_clarification_round=2,
        return_to_mainline_round=3,
    ),
    SyntheticScenario(
        "confusion_and_request_for_explanation",
        (
            "我正在比较两个实习机会，一个成长空间更大，另一个工作方式更适合我现在的状态。",
            "我没听懂你刚才问的那个问题是什么意思，可以先换一种更清楚的说法吗？",
            "如果你是问什么情况会让我改变选择，那我会先核实导师投入和每天实际做的工作。",
        ),
        ("continue_question", "continue_optional_question", "continue_question"),
    ),
    SyntheticScenario(
        "ambiguous_continue_then_explicit_finish",
        (
            "我最后决定先做两周试点，记录错误率和参与者反馈，再决定是否扩大范围。",
            "这件事我大概说完了，不过如果你还有具体问题可以继续问，我现在还不准备提交。",
            "我现在想结束访谈并生成报告，不再继续回答了。",
        ),
        ("continue_question", "continue_question", "finish"),
    ),
)

NORMAL_LOGICAL_CALLS = (
    len(SCENARIOS)
    * REPLICATES_PER_SCENARIO
    * len(PROMPT_VERSIONS)
    * ROUNDS_PER_ARM
)

REVIEW_DIMENSIONS = (
    "warmth_and_emotional_attunement",
    "naturalness_and_human_likeness",
    "non_interrogative_tone",
    "evidence_grounded_tentativeness",
    "concrete_event_anchoring",
    "information_gain_and_mainline_continuity",
    "repair_and_boundary_respect",
    "willingness_to_continue",
    "single_clear_question_and_safety",
)

SEVERE_REGRESSION_CODES = (
    "severe_empathy_or_interrogation_rupture",
    "severe_overclaim_or_diagnosis",
    "severe_structure_or_no_question_failure",
    "severe_mainline_or_finish_regression",
    "severe_boundary_or_repair_failure",
)

_INTERNAL_LANGUAGE = (
    "coverage",
    "target_dimension",
    "评分",
    "测评维度",
    "系统提示",
    "prompt",
    "考试",
    "测验",
)
_DIAGNOSTIC_LANGUAGE = re.compile(
    r"(?:抑郁症|焦虑症|人格障碍|依恋障碍|边缘型人格|心理诊断|精神疾病)"
)


def scenario_plan_payload() -> list[dict[str, Any]]:
    return [
        {
            "scenario_type": item.scenario_type,
            "user_turns": list(item.user_turns),
            "round_policies": list(item.round_policies),
            "source_clarification_round": item.source_clarification_round,
            "return_to_mainline_round": item.return_to_mainline_round,
        }
        for item in SCENARIOS
    ]


def _source_hashes() -> dict[str, str]:
    paths = (
        Path("backend/app/core/config.py"),
        Path("backend/app/schemas.py"),
        Path("backend/app/services/model_gateway.py"),
        Path("backend/app/services/orchestrator.py"),
        Path("backend/scripts/compare_natural_interviewer_prompts_v623.py"),
        Path("backend/scripts/adjudicate_natural_interviewer_v623.py"),
    )
    return {
        str(path): private_io._sha256_file(REPOSITORY_ROOT / path)
        for path in paths
    }


def _usage_totals(usages: list[dict[str, Any]]) -> dict[str, int]:
    keys = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
    )
    return {
        key: sum(
            int(usage.get(key, 0))
            for usage in usages
            if isinstance(usage.get(key, 0), int)
        )
        for key in keys
    }


class AsyncPhysicalCallAudit:
    """Count and validate every provider attempt before async network I/O."""

    def __init__(
        self,
        original_post: Callable[..., Awaitable[Any]],
        ledger: private_io.PrivateEventLedger,
        *,
        max_requests: int = MAX_PHYSICAL_REQUESTS,
        expected_endpoint: dict[str, str] | None = None,
    ) -> None:
        self.original_post = original_post
        self.ledger = ledger
        self.max_requests = max_requests
        self.expected_endpoint = (
            dict(expected_endpoint)
            if expected_endpoint is not None
            else official_deepseek_endpoint(settings.deepseek_base_url)
        )
        self.count = 0
        self.usages: list[dict[str, Any]] = []
        self.successful_responses: dict[int, dict[str, Any]] = {}

    async def __call__(self, url: str, **kwargs: Any) -> Any:
        parsed_url = urlsplit(str(url))
        if (
            str(url) != self.expected_endpoint["completion_url"]
            or parsed_url.scheme != "https"
            or parsed_url.hostname != self.expected_endpoint["host"]
            or parsed_url.port is not None
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise RunnerHardStop("provider_request_url_mismatch")
        if self.count >= self.max_requests:
            self.ledger.append(
                {
                    "event": "physical_call_blocked",
                    "reason": "physical_request_budget_exhausted",
                    "physical_request_count": self.count,
                    "physical_request_cap": self.max_requests,
                }
            )
            raise RunnerHardStop("physical_request_budget_exhausted")

        request_json = kwargs.get("json")
        if not isinstance(request_json, dict):
            raise RunnerHardStop("provider_request_json_missing")
        if request_json.get("model") != EXPECTED_MODEL:
            raise RunnerHardStop("requested_model_mismatch")
        if request_json.get("thinking") != {"type": EXPECTED_THINKING}:
            raise RunnerHardStop("requested_thinking_profile_mismatch")
        if request_json.get("max_tokens") != EXPECTED_MAX_TOKENS:
            raise RunnerHardStop("requested_max_tokens_mismatch")

        self.count += 1
        call_index = self.count
        self.ledger.append(
            {
                "event": "physical_call_started",
                "physical_request_index": call_index,
                "physical_request_cap": self.max_requests,
                "request_url": str(url),
                "request_url_sha256": private_io._sha256_text(str(url)),
                "request_host": str(parsed_url.hostname),
                "request_host_sha256": private_io._sha256_text(
                    str(parsed_url.hostname)
                ),
                "request_payload_sha256": private_io._sha256_text(
                    private_io._canonical_json(request_json)
                ),
                "requested_model": request_json["model"],
                "requested_thinking": request_json["thinking"],
                "requested_max_tokens": request_json["max_tokens"],
            }
        )
        try:
            response = await self.original_post(url, **kwargs)
        except BaseException as exc:
            self.ledger.append(
                {
                    "event": "physical_call_failed",
                    "physical_request_index": call_index,
                    "exception_type": type(exc).__name__,
                }
            )
            raise

        status_code = getattr(response, "status_code", None)
        event: dict[str, Any] = {
            "event": "physical_call_completed",
            "physical_request_index": call_index,
            "http_status_code": status_code,
        }
        if isinstance(status_code, int) and 200 <= status_code < 300:
            try:
                body = response.json()
            except BaseException as exc:
                self.ledger.append({**event, "response_json_error": type(exc).__name__})
                raise RunnerHardStop("provider_response_not_json") from exc
            if not isinstance(body, dict):
                self.ledger.append({**event, "response_json_type": type(body).__name__})
                raise RunnerHardStop("provider_response_not_object")
            event.update(private_io._safe_response_metadata(body))
            usage = body.get("usage")
            if isinstance(usage, dict):
                self.usages.append(usage)
            self.ledger.append(event)
            if body.get("model") != EXPECTED_MODEL:
                raise RunnerHardStop("api_raw_model_mismatch")
            raw_content_sha256 = event.get("raw_content_sha256")
            if not isinstance(raw_content_sha256, str):
                raise RunnerHardStop("api_raw_content_hash_missing")
            self.successful_responses[call_index] = {
                "physical_request_index": call_index,
                "provider": EXPECTED_PROVIDER,
                "api_raw_model": body["model"],
                "request_url": str(url),
                "request_url_sha256": private_io._sha256_text(str(url)),
                "request_host": str(parsed_url.hostname),
                "request_host_sha256": private_io._sha256_text(
                    str(parsed_url.hostname)
                ),
                "raw_content_sha256": raw_content_sha256,
            }
        else:
            self.ledger.append(event)
        return response

    def verified_logical_provenance(
        self,
        *,
        first_physical_request_index: int,
        last_physical_request_index: int,
    ) -> list[dict[str, Any]]:
        if (
            first_physical_request_index < 1
            or last_physical_request_index < first_physical_request_index
        ):
            raise RunnerHardStop("logical_call_physical_provenance_missing")
        values: list[dict[str, Any]] = []
        for request_index in range(
            first_physical_request_index,
            last_physical_request_index + 1,
        ):
            value = self.successful_responses.get(request_index)
            if value is None:
                raise RunnerHardStop("logical_call_physical_completion_missing")
            if (
                value.get("provider") != EXPECTED_PROVIDER
                or value.get("api_raw_model") != EXPECTED_MODEL
            ):
                raise RunnerHardStop("logical_call_provider_provenance_mismatch")
            values.append(dict(value))
        return values


def build_production_payload(transcript: list[dict[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "participant": {
            "display_name": "合成参与者",
            "identity_type": "other",
            "occupation": "",
            "experience_level": "",
            "collaboration_role": "",
        },
        "transcript": list(transcript),
    }
    payload["anchor_candidates"] = build_interview_anchor_candidates(
        payload["transcript"]
    )
    payload["source_clarification_required"] = source_clarification_required(
        payload["transcript"]
    )
    return payload


def _question_count(message: str) -> int:
    return message.count("?") + message.count("？")


def _validate_scenario_contract(
    *,
    scenario: SyntheticScenario,
    round_index: int,
    output: NaturalInterviewerOutput,
    prompt_version: str,
    transcript: list[dict[str, Any]],
) -> dict[str, Any] | None:
    message = output.interviewer_message.strip()
    _validate_interviewer_output(output, scenario.user_turns[round_index - 1])
    navigation = _validated_navigation(
        output,
        prompt_version=prompt_version,
        transcript=transcript,
    )
    lower = message.casefold()
    if any(term.casefold() in lower for term in _INTERNAL_LANGUAGE):
        raise RunnerHardStop("internal_language_leaked")
    if _DIAGNOSTIC_LANGUAGE.search(message):
        raise RunnerHardStop("diagnostic_language_leaked")

    policy = scenario.round_policies[round_index - 1]
    questions = _question_count(message)
    if policy == "finish":
        if output.session_action != "finish" or output.finish_reason != "user_requested":
            raise RunnerHardStop("explicit_finish_not_honored")
        if questions:
            raise RunnerHardStop("finish_response_must_not_ask_question")
    else:
        if output.session_action != "continue" or output.finish_reason is not None:
            raise RunnerHardStop("unexpected_early_finish")
        # The new clear-question contract belongs only to the candidate.  The
        # v6.2.1 baseline deliberately remains byte-for-byte and behaviourally
        # frozen, so a question-free baseline response must stay visible to the
        # blind reviewers instead of aborting the comparison that is meant to
        # measure that difference.
        if (
            policy == "continue_question"
            and prompt_version == "v6.2.3"
            and questions != 1
        ):
            raise RunnerHardStop("v623_normal_probe_must_have_exactly_one_question")
        if policy == "continue_optional_question" and questions > 1:
            raise RunnerHardStop("repair_response_has_multiple_questions")

    if scenario.source_clarification_round == round_index:
        if navigation is None or (
            navigation.get("focus_kind") != "source_ownership"
            or navigation.get("mainline_relation") != "source_clarification"
        ):
            raise RunnerHardStop("source_clarification_navigation_missing")
    if scenario.return_to_mainline_round == round_index:
        if navigation is None or navigation.get("mainline_relation") not in {
            "core",
            "return",
        }:
            raise RunnerHardStop("return_to_mainline_navigation_missing")
    return navigation


def audit_only_flags(
    message: str,
    latest_user: str,
    prior_outputs: list[str],
) -> list[str]:
    flags: list[str] = []
    compact = re.sub(r"\s+", "", message)
    if _question_count(message) == 0:
        flags.append("question_free_response")
    if re.match(r"^(?:为什么|哪些具体事情|最难判断的是什么)", compact):
        flags.append("bare_interrogative_opening")
    if re.search(r"(?:我听到|你提到|听起来|我理解)[^，，]{0,60}[，,]", message):
        flags.append("formulaic_acknowledgement_opening")
    if re.search(r"(?:你|心里)(?:应该|一定|肯定).{0,18}(?:难受|痛苦|害怕|委屈|焦虑)", message):
        flags.append("assertive_inferred_affect")
    latest_compact = re.sub(r"\s+", "", latest_user)
    if len(latest_compact) >= 12 and any(
        latest_compact[index : index + 12] in compact
        for index in range(len(latest_compact) - 11)
    ):
        flags.append("repeated_user_wording")
    opening = re.split(r"[。！？?!]", compact, maxsplit=1)[0]
    if any(
        opening
        and opening
        == re.split(r"[。！？?!]", re.sub(r"\s+", "", prior), maxsplit=1)[0]
        for prior in prior_outputs
    ):
        flags.append("repeated_opening_within_arm")
    if len(compact) < 12:
        flags.append("very_short_response")
    if len(compact) > 180:
        flags.append("very_long_response")
    return sorted(set(flags))


def prompt_assets_payload() -> dict[str, Any]:
    prompt_assets: dict[str, Any] = {}
    for version in PROMPT_VERSIONS:
        prompt_id, resolved_version, prompt = resolve_natural_interviewer_prompt(version)
        prompt_assets[version] = {
            "prompt_id": prompt_id,
            "prompt_version": resolved_version,
            "prompt_sha256": private_io._sha256_text(prompt),
        }
    return prompt_assets


def _events_jsonl_metadata(output_dir: Path) -> dict[str, Any]:
    path = output_dir / "events.jsonl"
    if not path.is_file():
        raise RunnerHardStop("events_jsonl_missing")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RunnerHardStop("events_jsonl_unreadable") from exc
    if not lines or any(not line.strip() for line in lines):
        raise RunnerHardStop("events_jsonl_empty_or_blank")
    return {
        "events_jsonl_sha256": private_io._sha256_file(path),
        "events_jsonl_event_count": len(lines),
    }


def _attach_events_jsonl_metadata(
    manifest: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    manifest.update(_events_jsonl_metadata(output_dir))
    return manifest


def _refresh_written_manifest_events_metadata(output_dir: Path) -> None:
    manifest_path = output_dir / "run_manifest.json"
    events_path = output_dir / "events.jsonl"
    if not manifest_path.is_file() or not events_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunnerHardStop("written_manifest_unreadable") from exc
    if not isinstance(manifest, dict):
        raise RunnerHardStop("written_manifest_not_object")
    _attach_events_jsonl_metadata(manifest, output_dir)
    private_io._write_private_json(manifest_path, manifest)


def _base_manifest(output_dir: Path) -> dict[str, Any]:
    endpoint = official_deepseek_endpoint(settings.deepseek_base_url)
    return {
        "protocol": RUNNER_PROTOCOL,
        "status": "running",
        "stop_reason": None,
        "synthetic_only": True,
        "reads_application_database": False,
        "automated_scoring_performed": False,
        "expected_api_raw_model": EXPECTED_MODEL,
        "expected_provider": EXPECTED_PROVIDER,
        "configured_deepseek_base_url": endpoint["configured_base_url"],
        "provider_completion_url": endpoint["completion_url"],
        "provider_completion_url_sha256": endpoint[
            "completion_url_sha256"
        ],
        "provider_host": endpoint["host"],
        "provider_host_sha256": endpoint["host_sha256"],
        "expected_thinking": EXPECTED_THINKING,
        "expected_max_tokens": EXPECTED_MAX_TOKENS,
        "normal_logical_call_plan": NORMAL_LOGICAL_CALLS,
        "logical_calls_completed": 0,
        "physical_request_cap": MAX_PHYSICAL_REQUESTS,
        "physical_requests_observed": 0,
        "repair_calls_observed": 0,
        "events_jsonl_sha256": None,
        "events_jsonl_event_count": 0,
        "output_directory": str(output_dir),
        "git_commit_sha": private_io._git_text("rev-parse", "HEAD"),
        "git_status_sha256": private_io._sha256_text(
            private_io._git_text("status", "--porcelain=v1")
        ),
        "source_sha256": _source_hashes(),
        "prompt_assets": prompt_assets_payload(),
        "scenario_plan_sha256": private_io._sha256_text(
            private_io._canonical_json(scenario_plan_payload())
        ),
    }


def _write_blind_review_template(output_dir: Path) -> None:
    packet_path = output_dir / "blind_review_packet.json"
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    template = {
        "protocol": RUNNER_PROTOCOL,
        "reviewer_id": "",
        "blind_packet_sha256": private_io._sha256_file(packet_path),
        "instructions": (
            "Do not open run_manifest.json or sealed files. Rate the complete visible "
            "conversation from 1 to 5 without inferring prompt identity. A severe code "
            "requires a concrete note and problematic round index."
        ),
        "rating_scale": {"minimum": 1, "maximum": 5},
        "severe_regression_codes": list(SEVERE_REGRESSION_CODES),
        "cases": [
            {
                "case_id": case["case_id"],
                "preferred_candidate_id": None,
                "tie": False,
                "preference_reason": "",
                "candidates": [
                    {
                        "candidate_id": candidate["candidate_id"],
                        "ratings": {
                            dimension: None for dimension in REVIEW_DIMENSIONS
                        },
                        "severe_regression_codes": [],
                        "problematic_round_indices": [],
                        "reviewer_notes": "",
                    }
                    for candidate in case["candidates"]
                ],
            }
            for case in packet["cases"]
        ],
    }
    private_io._write_private_json(output_dir / "blind_review_template.json", template)


def run_plan(
    *,
    output_dir: Path,
    gateway: ModelGatewayService,
    physical_audit: AsyncPhysicalCallAudit | Any,
    ledger: private_io.PrivateEventLedger | None = None,
) -> dict[str, Any]:
    if type(gateway) is not ModelGatewayService:
        raise RunnerHardStop("production_model_gateway_required")
    if settings.model_gateway_mode != "real":
        raise RunnerHardStop("model_gateway_mode_must_be_real")
    if type(physical_audit) is not AsyncPhysicalCallAudit:
        raise RunnerHardStop("production_physical_audit_required")
    if ledger is None:
        raise RunnerHardStop("events_ledger_required")
    endpoint = official_deepseek_endpoint(settings.deepseek_base_url)
    if physical_audit.expected_endpoint != endpoint:
        raise RunnerHardStop("physical_audit_endpoint_mismatch")
    manifest = _base_manifest(output_dir)
    sealed_mapping: dict[str, Any] = {
        "protocol": RUNNER_PROTOCOL,
        "case_key": [],
        "candidate_arm_key": [],
    }
    blind_cases: list[dict[str, Any]] = []
    logical_records: list[dict[str, Any]] = []

    for scenario_index, scenario in enumerate(SCENARIOS):
        for replicate_index in range(1, REPLICATES_PER_SCENARIO + 1):
            case_id = private_io._opaque_id("case")
            sealed_mapping["case_key"].append(
                {
                    "case_id": case_id,
                    "scenario_type": scenario.scenario_type,
                    "replicate_index": replicate_index,
                    "scenario_sha256": private_io._sha256_text(
                        private_io._canonical_json(
                            {
                                "user_turns": list(scenario.user_turns),
                                "round_policies": list(scenario.round_policies),
                            }
                        )
                    ),
                }
            )
            versions = (
                PROMPT_VERSIONS
                if (scenario_index + replicate_index) % 2 == 0
                else tuple(reversed(PROMPT_VERSIONS))
            )
            candidates: list[dict[str, Any]] = []
            for version in versions:
                prompt_id, resolved_version, prompt = resolve_natural_interviewer_prompt(
                    version
                )
                candidate_id = private_io._opaque_id("cand")
                sealed_mapping["candidate_arm_key"].append(
                    {
                        "case_id": case_id,
                        "candidate_id": candidate_id,
                        "prompt_id": prompt_id,
                        "prompt_version": resolved_version,
                        "prompt_sha256": private_io._sha256_text(prompt),
                    }
                )
                transcript: list[dict[str, Any]] = [
                    {
                        "turn_index": 0,
                        "role": "assistant",
                        "content": CANONICAL_OPENING,
                    }
                ]
                visible_rounds: list[dict[str, Any]] = []
                prior_outputs: list[str] = []
                for round_index, user_text in enumerate(
                    scenario.user_turns, start=1
                ):
                    user_turn_index = len(transcript)
                    transcript.append(
                        {
                            "turn_index": user_turn_index,
                            "role": "user",
                            "content": user_text,
                        }
                    )
                    payload = build_production_payload(transcript)
                    before_physical = int(physical_audit.count)
                    logical_context = {
                        "scenario_type": scenario.scenario_type,
                        "replicate_index": replicate_index,
                        "prompt_version": version,
                        "round_index": round_index,
                        "case_id": case_id,
                        "candidate_id": candidate_id,
                    }
                    if ledger is not None:
                        ledger.append(
                            {"event": "logical_call_started", **logical_context}
                        )
                    try:
                        result = gateway.generate_interviewer(
                            payload,
                            prompt_version=version,
                        )
                        actual_provider = getattr(result, "provider", None)
                        actual_model = getattr(result, "model", None)
                        if actual_provider != EXPECTED_PROVIDER:
                            raise RunnerHardStop(
                                "structured_result_provider_mismatch"
                            )
                        if actual_model != EXPECTED_MODEL:
                            raise RunnerHardStop("structured_result_model_mismatch")
                        after_physical = int(physical_audit.count)
                        message = result.output.interviewer_message.strip()
                        navigation = _validate_scenario_contract(
                            scenario=scenario,
                            round_index=round_index,
                            output=result.output,
                            prompt_version=version,
                            transcript=payload["transcript"],
                        )
                        if not isinstance(physical_audit, AsyncPhysicalCallAudit):
                            raise RunnerHardStop(
                                "physical_audit_provenance_unavailable"
                            )
                        physical_provenance = (
                            physical_audit.verified_logical_provenance(
                                first_physical_request_index=before_physical + 1,
                                last_physical_request_index=after_physical,
                            )
                        )
                        attempt_count = int(result.attempt_count)
                        repair_used = bool(result.repair_used)
                        if attempt_count != len(physical_provenance):
                            raise RunnerHardStop(
                                "logical_attempt_count_provenance_mismatch"
                            )
                        if repair_used is not (attempt_count > 1):
                            raise RunnerHardStop("logical_repair_flag_mismatch")
                        if any(
                            item.get("request_url") != endpoint["completion_url"]
                            or item.get("request_url_sha256")
                            != endpoint["completion_url_sha256"]
                            or item.get("request_host") != endpoint["host"]
                            or item.get("request_host_sha256")
                            != endpoint["host_sha256"]
                            for item in physical_provenance
                        ):
                            raise RunnerHardStop(
                                "logical_provider_endpoint_provenance_mismatch"
                            )
                    except BaseException as exc:
                        if ledger is not None:
                            ledger.append(
                                {
                                    "event": "logical_call_failed",
                                    **logical_context,
                                    "exception_type": type(exc).__name__,
                                    "physical_requests_for_logical_call": (
                                        int(physical_audit.count) - before_physical
                                    ),
                                }
                            )
                        partial = {
                            **manifest,
                            "status": "stopped",
                            "stop_reason": "logical_call_failed",
                            "logical_calls_completed": len(logical_records),
                            "physical_requests_observed": int(physical_audit.count),
                            "repair_calls_observed": sum(
                                1
                                for record in logical_records
                                if record["repair_used"]
                            ),
                            "usage_totals": _usage_totals(
                                list(getattr(physical_audit, "usages", []))
                            ),
                            "logical_call_records": logical_records,
                            "failure_context": {
                                **logical_context,
                                "exception_type": type(exc).__name__,
                            },
                        }
                        if ledger is not None:
                            _attach_events_jsonl_metadata(partial, output_dir)
                        private_io._write_private_json(
                            output_dir / "run_manifest.json", partial
                        )
                        private_io._write_private_json(
                            output_dir / "sealed" / "partial_opaque_mapping.json",
                            sealed_mapping,
                        )
                        raise
                    flags = audit_only_flags(message, user_text, prior_outputs)
                    record = {
                        "case_id": case_id,
                        "candidate_id": candidate_id,
                        "prompt_version": version,
                        "round_index": round_index,
                        "input_payload_sha256": private_io._sha256_text(
                            private_io._canonical_json(payload)
                        ),
                        "output_text_sha256": private_io._sha256_text(message),
                        "session_action": result.output.session_action,
                        "finish_reason": result.output.finish_reason,
                        "provider": actual_provider,
                        "model": actual_model,
                        "api_raw_model": physical_provenance[-1][
                            "api_raw_model"
                        ],
                        "provider_request_url": endpoint["completion_url"],
                        "provider_request_url_sha256": endpoint[
                            "completion_url_sha256"
                        ],
                        "provider_request_host": endpoint["host"],
                        "provider_request_host_sha256": endpoint[
                            "host_sha256"
                        ],
                        "physical_response_raw_content_sha256": [
                            item["raw_content_sha256"]
                            for item in physical_provenance
                        ],
                        "repair_used": repair_used,
                        "attempt_count": attempt_count,
                        "latency_ms": int(result.latency_ms),
                        "physical_requests_for_logical_call": (
                            after_physical - before_physical
                        ),
                        "audit_only_quality_flags": flags,
                        "navigation": navigation,
                    }
                    logical_records.append(record)
                    if ledger is not None:
                        ledger.append(
                            {
                                "event": "logical_call_completed",
                                **logical_context,
                                "output_text_sha256": record["output_text_sha256"],
                                "session_action": record["session_action"],
                                "finish_reason": record["finish_reason"],
                                "provider": record["provider"],
                                "model": record["model"],
                                "api_raw_model": record["api_raw_model"],
                                "provider_request_url": record[
                                    "provider_request_url"
                                ],
                                "provider_request_url_sha256": record[
                                    "provider_request_url_sha256"
                                ],
                                "provider_request_host": record[
                                    "provider_request_host"
                                ],
                                "provider_request_host_sha256": record[
                                    "provider_request_host_sha256"
                                ],
                                "physical_response_raw_content_sha256": record[
                                    "physical_response_raw_content_sha256"
                                ],
                                "repair_used": record["repair_used"],
                                "attempt_count": record["attempt_count"],
                                "latency_ms": record["latency_ms"],
                                "physical_requests_for_logical_call": record[
                                    "physical_requests_for_logical_call"
                                ],
                            }
                        )
                    visible_rounds.append(
                        {
                            "round_index": round_index,
                            "user_text": user_text,
                            "assistant_text": message,
                            "session_action": result.output.session_action,
                            "finish_reason": result.output.finish_reason,
                            "conversation_ended_after_this_turn": (
                                result.output.session_action == "finish"
                            ),
                        }
                    )
                    transcript.append(
                        {
                            "turn_index": len(transcript),
                            "role": "assistant",
                            "content": message,
                        }
                    )
                    prior_outputs.append(message)
                    if result.output.session_action == "finish":
                        break
                candidates.append(
                    {
                        "candidate_id": candidate_id,
                        "opening": CANONICAL_OPENING,
                        "rounds": visible_rounds,
                    }
                )
            candidates.sort(key=lambda item: item["candidate_id"])
            blind_cases.append({"case_id": case_id, "candidates": candidates})

    blind_cases.sort(key=lambda item: item["case_id"])
    manifest.update(
        {
            "status": "completed",
            "stop_reason": "normal_completion",
            "logical_calls_completed": len(logical_records),
            "physical_requests_observed": int(physical_audit.count),
            "repair_calls_observed": sum(
                1 for record in logical_records if record["repair_used"]
            ),
            "usage_totals": _usage_totals(
                list(getattr(physical_audit, "usages", []))
            ),
            "logical_call_records": logical_records,
        }
    )
    if ledger is None:
        raise RunnerHardStop("events_ledger_required")
    _attach_events_jsonl_metadata(manifest, output_dir)
    private_io._write_private_json(output_dir / "run_manifest.json", manifest)
    private_io._write_private_json(
        output_dir / "blind_review_packet.json",
        {
            "protocol": RUNNER_PROTOCOL,
            "synthetic_only": True,
            "automated_scoring_performed": False,
            "review_dimensions": list(REVIEW_DIMENSIONS),
            "instructions": (
                "Review only visible text. Candidate order and identifiers are opaque; "
                "do not infer prompt identity or open other run files."
            ),
            "cases": blind_cases,
        },
    )
    private_io._write_private_json(
        output_dir / "sealed" / "opaque_mapping.json", sealed_mapping
    )
    _write_blind_review_template(output_dir)
    manifest["artifact_sha256"] = {
        "blind_review_packet.json": private_io._sha256_file(
            output_dir / "blind_review_packet.json"
        ),
        "blind_review_template.json": private_io._sha256_file(
            output_dir / "blind_review_template.json"
        ),
        "sealed/opaque_mapping.json": private_io._sha256_file(
            output_dir / "sealed" / "opaque_mapping.json"
        ),
    }
    private_io._write_private_json(output_dir / "run_manifest.json", manifest)
    return manifest


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-real", action="store_true")
    parser.add_argument("--confirmation", default="")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--max-physical-requests",
        type=int,
        default=MAX_PHYSICAL_REQUESTS,
    )
    return parser.parse_args(argv)


def _existing_stopped_manifest(output_dir: Path) -> dict[str, Any]:
    manifest_path = output_dir / "run_manifest.json"
    if manifest_path.exists():
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            value = None
        if isinstance(value, dict) and value.get("status") == "stopped":
            return value
    return _base_manifest(output_dir)


def execute(argv: list[str] | None = None) -> dict[str, Any]:
    args = _parse_args(argv)
    if not args.execute_real:
        raise SystemExit("missing_execute_real")
    if args.confirmation != CONFIRMATION:
        raise SystemExit("missing_or_invalid_confirmation")
    if args.max_physical_requests != MAX_PHYSICAL_REQUESTS:
        raise SystemExit("physical_request_cap_must_equal_144")
    output_dir = private_io.validate_output_target(args.output_dir)
    if settings.model_gateway_mode != "real":
        raise SystemExit("model_gateway_mode_must_be_real")
    if settings.deepseek_model != EXPECTED_MODEL:
        raise SystemExit("configured_model_must_equal_deepseek_v4_flash")
    if settings.deepseek_interview_thinking != EXPECTED_THINKING:
        raise SystemExit("interview_thinking_must_be_disabled")
    if settings.deepseek_interview_max_tokens != EXPECTED_MAX_TOKENS:
        raise SystemExit("interview_max_tokens_must_equal_512")
    if not settings.deepseek_api_key.strip():
        raise SystemExit("missing_deepseek_api_key")
    try:
        expected_endpoint = official_deepseek_endpoint(
            settings.deepseek_base_url
        )
    except RunnerHardStop as exc:
        raise SystemExit(str(exc)) from exc

    previous_umask = os.umask(0o077)
    ledger: private_io.PrivateEventLedger | None = None
    original_post = gateway_module._async_http_post
    output_created = False
    audit: AsyncPhysicalCallAudit | None = None
    try:
        private_io.create_private_directory(output_dir)
        output_created = True
        private_io.create_private_directory(output_dir / "sealed")
        ledger = private_io.PrivateEventLedger(output_dir / "events.jsonl")
        audit = AsyncPhysicalCallAudit(
            original_post,
            ledger,
            expected_endpoint=expected_endpoint,
        )
        gateway_module._async_http_post = audit
        return run_plan(
            output_dir=output_dir,
            gateway=ModelGatewayService(),
            physical_audit=audit,
            ledger=ledger,
        )
    except RunnerHardStop as exc:
        if ledger is not None:
            ledger.append({"event": "run_stopped", "reason": str(exc)})
        if output_created:
            stopped = _existing_stopped_manifest(output_dir)
            stopped.update(
                {
                    "status": "stopped",
                    "stop_reason": str(exc),
                    "physical_requests_observed": audit.count if audit else 0,
                    "usage_totals": _usage_totals(audit.usages if audit else []),
                }
            )
            private_io._write_private_json(
                output_dir / "run_manifest.json", stopped
            )
        raise SystemExit(f"run_hard_stopped:{exc}") from exc
    except BaseException as exc:
        if ledger is not None:
            ledger.append(
                {
                    "event": "run_stopped",
                    "reason": "unhandled_runner_error",
                    "exception_type": type(exc).__name__,
                }
            )
        if output_created:
            stopped = _existing_stopped_manifest(output_dir)
            stopped.update(
                {
                    "status": "stopped",
                    "stop_reason": "unhandled_runner_error",
                    "exception_type": type(exc).__name__,
                    "physical_requests_observed": audit.count if audit else 0,
                    "usage_totals": _usage_totals(audit.usages if audit else []),
                }
            )
            private_io._write_private_json(
                output_dir / "run_manifest.json", stopped
            )
        raise
    finally:
        gateway_module._async_http_post = original_post
        try:
            if ledger is not None:
                ledger.close()
            if output_created:
                _refresh_written_manifest_events_metadata(output_dir)
        finally:
            os.umask(previous_umask)


def main() -> None:
    manifest = execute()
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "logical_calls_completed": manifest["logical_calls_completed"],
                "physical_requests_observed": manifest["physical_requests_observed"],
                "usage_totals": manifest["usage_totals"],
                "output_directory": manifest["output_directory"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
