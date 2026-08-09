#!/usr/bin/env python3
"""Real local-only V6.2.1 enforce UAT through participant APIs.

This script deliberately does not import application settings, load ``.env``,
read a database, call an administrator endpoint, or accept a model credential.
It talks directly to an already-running disposable API bound to a loopback IP.

The participant contract intentionally hides the internal readiness snapshot.
Consequently this script can prove the exact readiness/finalize boundary and
the public report invariants, but it cannot prove that the persisted formal
snapshot is ``AttributedFinalScorerOutput`` with ``evidence_refs``. The JSON
result therefore always identifies the separate DB audit that is still needed.

Example::

    V6_REAL_UAT_CONFIRM=1 \
    V6_API_BASE_URL=http://127.0.0.1:8062/api/v1 \
    python backend/scripts/check_real_deepseek_v621_enforce.py

Only run this against a non-production local server configured by the operator
for real DeepSeek, ``NATURAL_INTERVIEWER_PROMPT_VERSION=v6.2.1``, and
``EVIDENCE_ATTRIBUTION_MODE=enforce``.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import sys
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

import httpx


DEFAULT_API_BASE_URL = "http://127.0.0.1:8062/api/v1"
EXPECTED_DIMENSIONS = {
    "problem_definition",
    "evidence_evaluation",
    "reasoning_argumentation",
    "multiple_perspectives",
    "integrative_decision",
    "dynamic_adjustment",
}
INTERNAL_ATTRIBUTION_KEYS = {
    "asset_fingerprint",
    "attribution_span_id",
    "attribution_span_ids",
    "elicitation_level",
    "eligibility",
    "evidence_attributions",
    "evidence_refs",
    "owner",
    "readiness_check_id",
    "relation",
    "schema_version",
    "validation_reason",
    "validation_status",
}
EXTERNAL_AI_QUOTE = "直接全量上线最省时间，不需要再做小范围验证"
EXTERNAL_AI_EVIDENCE_MARKERS = (
    "AI 建议的原文",
    "直接全量上线最省时间",
    "不需要再做小范围验证",
)
FORBIDDEN_INTERVIEWER_TERMS = (
    "coverage",
    "target_dimension",
    "评分维度",
    "系统提示",
    "prompt",
)
RECOVERABLE_TURN_ERROR_CODES = {
    "model_connection_interrupted",
    "model_empty_response",
}
READINESS_TIMEOUT_SECONDS = 300.0
READINESS_POLL_SECONDS = 2.0


ANSWERS = [
    (
        "我需要决定是否把一项面向新生的学习互助工具直接全量上线。核心目标是验证它是否真的提高求助效率，"
        "而不是只追求注册人数；范围先限定在两个学院，约束是四周时间、有限客服人力和学生隐私。"
    ),
    (
        "我先核对了注册、实际提问和问题解决时长三类记录。现有问卷主要来自活跃用户，不能代表没使用的人，"
        "而且只有两周数据，所以我还要补访谈退出用户，并把日志口径和问卷口径逐项对齐。"
    ),
    (
        f"下面这句是 AI 建议的原文，不是我的判断：\u201c{EXTERNAL_AI_QUOTE}。\u201d"
        "我没有采纳，因为它忽略了样本偏差、隐私投诉和回滚成本；我的判断是先用可撤回的小范围试点核实风险。"
    ),
    (
        "学生在意回复速度和匿名性，辅导员担心错误信息，运营同事在意值班负担，学院则关心公平覆盖。"
        "这些立场并不等价，我会分别记录收益和风险，再检查试点是否把不活跃学生排除在外。"
    ),
    (
        "注册增长可能来自开学宣传，而不一定是工具本身有效，这是一个替代解释。若试点组的解决时长下降，"
        "但未使用者的求助成功率没有变化，我不会把相关性当成因果结论，还会比较同期未推广学院。"
    ),
    (
        "综合目标、证据缺口和各方风险后，我选择先做两周、两个学院的可回滚试点，不做全量上线。"
        "我接受增长较慢的代价，换取可核验的数据和较低风险；负责人、停止条件和复盘日期会在启动前写清楚。"
    ),
    (
        "实际试点后，提问响应时间从约九小时降到五小时，但匿名投诉比预期多，夜间值班也超出人力预算。"
        "这个结果部分支持效率假设，却否定了我对隐私和运营成本的乐观估计，因此不能按原计划直接扩大。"
    ),
    (
        "我会先关闭默认公开昵称、缩短夜间服务时段并增加退出访谈，再运行一周。若投诉率降到百分之二以下且"
        "响应时间仍低于六小时，就扩大到第三个学院；否则暂停上线，保留日志并重新设计匿名流程。"
    ),
]


class UATFailure(AssertionError):
    """A contract or acceptance gate failed."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise UATFailure(message)


def require_local_api_base_url(raw: str) -> str:
    """Reject DNS names, redirects-by-configuration, and non-loopback targets."""

    parsed = urlsplit(raw)
    require(parsed.scheme == "http", "api_base_url_must_use_plain_http_loopback")
    require(not parsed.username and not parsed.password, "api_base_url_must_not_contain_credentials")
    require(not parsed.query and not parsed.fragment, "api_base_url_must_not_have_query_or_fragment")
    require(parsed.hostname is not None, "api_base_url_host_missing")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as exc:
        raise UATFailure("api_base_url_must_use_a_loopback_ip_literal") from exc
    require(address.is_loopback, "api_base_url_is_not_loopback")
    require(parsed.port is not None, "api_base_url_requires_explicit_local_port")
    require(parsed.path.rstrip("/") == "/api/v1", "api_base_url_path_must_be_/api/v1")
    return raw.rstrip("/")


def body_preview(response: httpx.Response, limit: int = 800) -> str:
    try:
        return response.text[:limit]
    except Exception:
        return "<unavailable>"


def response_json(response: httpx.Response, label: str) -> dict[str, Any]:
    try:
        value = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise UATFailure(f"{label}_invalid_json:{body_preview(response)}") from exc
    require(isinstance(value, dict), f"{label}_json_is_not_an_object")
    return value


def request_json(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    expected_statuses: set[int] | None = None,
) -> tuple[dict[str, Any], float, int]:
    started = time.monotonic()
    response = client.request(method, path, json=payload)
    latency_ms = round((time.monotonic() - started) * 1000, 3)
    allowed = expected_statuses or {200}
    require(
        response.status_code in allowed,
        f"{method}_{path}_unexpected_status:{response.status_code}:{body_preview(response)}",
    )
    return response_json(response, f"{method}_{path}"), latency_ms, response.status_code


def parse_stream(response: httpx.Response) -> list[dict[str, Any]]:
    require(
        response.status_code == 200,
        f"turn_stream_http_{response.status_code}:{body_preview(response)}",
    )
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(response.text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise UATFailure(f"turn_stream_invalid_ndjson_line:{line_number}") from exc
        require(isinstance(item, dict), f"turn_stream_line_not_object:{line_number}")
        events.append(item)
    require(events, "turn_stream_empty")
    return events


def stream_outcome(events: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None]:
    completed = next(
        (
            item.get("data")
            for item in reversed(events)
            if item.get("event") == "agent_completed" and isinstance(item.get("data"), dict)
        ),
        None,
    )
    error = next((item for item in reversed(events) if item.get("event") == "error"), None)
    error_code = str(error.get("code") or "stream_error") if isinstance(error, dict) else None
    return completed, error_code


def validate_interviewer_message(value: Any) -> None:
    message = str(value or "").strip()
    require(bool(message), "assistant_message_empty")
    lowered = message.casefold()
    require(
        not any(term.casefold() in lowered for term in FORBIDDEN_INTERVIEWER_TERMS),
        f"interviewer_internal_language_leaked:{message[:300]}",
    )
    require(
        message.count("？") + message.count("?") <= 1,
        f"interviewer_asked_multiple_primary_questions:{message[:300]}",
    )


def validate_completed_turn(
    completed: dict[str, Any], *, expected_answer_count: int
) -> None:
    turn = completed.get("turn")
    session = completed.get("session")
    require(isinstance(turn, dict), "agent_completed_turn_missing")
    require(isinstance(session, dict), "agent_completed_session_missing")
    require(turn.get("role") == "assistant", "agent_completed_turn_is_not_assistant")
    validate_interviewer_message(turn.get("content"))
    require(
        session.get("user_answer_count") == expected_answer_count,
        f"unexpected_answer_count_after_turn:{session.get('user_answer_count')}",
    )
    require(session.get("phase") == "interviewing", f"session_closed_before_eight_turns:{session.get('phase')}")


def submit_turn(
    client: httpx.Client,
    session_uuid: str,
    *,
    index: int,
    content: str,
    client_turn_id: str,
    force_idempotency_replay: bool,
) -> dict[str, Any]:
    payload = {
        "content": content,
        "client_turn_id": client_turn_id,
        "input_mode": "text",
        "answer_duration_ms": 18_000 + index * 1_000,
    }
    attempts: list[dict[str, Any]] = []
    completed: dict[str, Any] | None = None
    transient_retry_used = False

    for attempt in range(1, 3):
        started = time.monotonic()
        try:
            response = client.post(f"/sessions/{session_uuid}/turns:stream", json=payload)
            latency_ms = round((time.monotonic() - started) * 1000, 3)
            events = parse_stream(response)
            leaked_keys = nested_keys(events) & INTERNAL_ATTRIBUTION_KEYS
            require(
                not leaked_keys,
                f"turn_stream_exposes_internal_attribution:{sorted(leaked_keys)}",
            )
            candidate, error_code = stream_outcome(events)
            attempts.append(
                {
                    "attempt": attempt,
                    "latency_ms": latency_ms,
                    "http_status": response.status_code,
                    "events": [str(item.get("event")) for item in events],
                    "heartbeat_count": sum(item.get("event") == "heartbeat" for item in events),
                    "outcome": "completed" if candidate is not None else "error",
                    "error_code": error_code,
                }
            )
            if candidate is not None:
                completed = candidate
                break
            if candidate is None and error_code not in RECOVERABLE_TURN_ERROR_CODES:
                raise UATFailure(
                    f"turn_{index}_nonrecoverable_stream_error:{error_code}"
                )
        except httpx.RequestError as exc:
            latency_ms = round((time.monotonic() - started) * 1000, 3)
            attempts.append(
                {
                    "attempt": attempt,
                    "latency_ms": latency_ms,
                    "outcome": "request_or_contract_error",
                    "error_code": type(exc).__name__,
                    "detail": str(exc)[:300],
                }
            )
        except UATFailure as exc:
            latency_ms = round((time.monotonic() - started) * 1000, 3)
            attempts.append(
                {
                    "attempt": attempt,
                    "latency_ms": latency_ms,
                    "outcome": "contract_failure",
                    "error_code": type(exc).__name__,
                    "detail": str(exc)[:300],
                }
            )
            raise
        if attempt == 1:
            transient_retry_used = True
            time.sleep(1.0)

    require(completed is not None, f"turn_{index}_failed_after_same_id_retry")
    validate_completed_turn(completed, expected_answer_count=index)

    replay_record: dict[str, Any] | None = None
    if force_idempotency_replay and not transient_retry_used:
        started = time.monotonic()
        replay = client.post(f"/sessions/{session_uuid}/turns:stream", json=payload)
        replay_latency_ms = round((time.monotonic() - started) * 1000, 3)
        replay_events = parse_stream(replay)
        leaked_keys = nested_keys(replay_events) & INTERNAL_ATTRIBUTION_KEYS
        require(
            not leaked_keys,
            f"idempotency_replay_exposes_internal_attribution:{sorted(leaked_keys)}",
        )
        replay_completed, replay_error = stream_outcome(replay_events)
        require(replay_error is None, f"idempotency_replay_error:{replay_error}")
        require(replay_completed is not None, "idempotency_replay_missing_agent_completed")
        validate_completed_turn(replay_completed, expected_answer_count=index)
        require(
            replay_completed.get("turn", {}).get("id") == completed.get("turn", {}).get("id"),
            "idempotency_replay_created_a_different_assistant_turn",
        )
        replay_record = {
            "same_client_turn_id": True,
            "latency_ms": replay_latency_ms,
            "events": [str(item.get("event")) for item in replay_events],
            "answer_count_unchanged": True,
            "assistant_turn_replayed": True,
        }

    return {
        "turn": index,
        "client_turn_id_sha256": hashlib.sha256(client_turn_id.encode()).hexdigest(),
        "attempts": attempts,
        "transient_retry_used": transient_retry_used,
        "idempotency_replay": replay_record,
        "assistant_session_action": completed.get("session_action"),
        "assistant_finish_reason": completed.get("finish_reason"),
        "assistant_quality_flags": completed.get("turn", {}).get("quality_flags", []),
    }


def readiness_error_code(payload: dict[str, Any]) -> str | None:
    code = payload.get("code")
    if code is None and isinstance(payload.get("detail"), dict):
        code = payload["detail"].get("code")
    return str(code) if code else None


def poll_exact_readiness(
    client: httpx.Client,
    session_uuid: str,
    *,
    expected_answer_count: int,
) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + READINESS_TIMEOUT_SECONDS
    identity: tuple[int, str] | None = None
    observations: list[dict[str, Any]] = []
    failed_retry_used = False
    polls = 0

    while time.monotonic() < deadline:
        polls += 1
        suffix = "?retry_failed=true" if failed_retry_used else ""
        response = client.post(f"/sessions/{session_uuid}/report-readiness{suffix}")
        elapsed_ms = round((time.monotonic() - started) * 1000, 3)
        payload = response_json(response, "report_readiness")
        status = payload.get("status")
        error_code = readiness_error_code(payload)

        if response.status_code in {200, 202}:
            check_id = payload.get("check_id")
            fingerprint = payload.get("transcript_fingerprint")
            require(isinstance(check_id, int) and check_id > 0, "readiness_check_id_invalid")
            require(
                isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{64}", fingerprint) is not None,
                "readiness_transcript_fingerprint_invalid",
            )
            current_identity = (check_id, fingerprint)
            if identity is None:
                identity = current_identity
            require(current_identity == identity, "readiness_identity_changed_while_polling_exact_transcript")

        marker = {
            "poll": polls,
            "elapsed_ms": elapsed_ms,
            "http_status": response.status_code,
            "status": status,
            "error_code": error_code,
        }
        if not observations or any(
            observations[-1].get(key) != marker.get(key)
            for key in ("http_status", "status", "error_code")
        ):
            observations.append(marker)

        if response.status_code == 200 and status in {"ready", "insufficient"}:
            require(identity is not None, "terminal_readiness_identity_missing")
            require(
                payload.get("minimum_turns_required") == 8,
                f"unexpected_minimum_turns_required:{payload.get('minimum_turns_required')}",
            )
            require(
                payload.get("minimum_turns_met") is (expected_answer_count >= 8),
                "minimum_turns_met_does_not_match_saved_answers",
            )
            return {
                "answer_count": expected_answer_count,
                "status": status,
                "ready": payload.get("ready"),
                "cached": payload.get("cached"),
                "check_id": identity[0],
                "transcript_fingerprint": identity[1],
                "poll_count": polls,
                "elapsed_ms": elapsed_ms,
                "failed_snapshot_retry_used": failed_retry_used,
                "observations": observations,
                "public_payload_keys": sorted(payload),
            }

        if response.status_code == 200 and status == "failed":
            if failed_retry_used:
                raise UATFailure(f"readiness_failed_after_retry:turn_{expected_answer_count}")
            failed_retry_used = True
            time.sleep(1.0)
            continue

        transient_unavailable = response.status_code == 503 and error_code in {
            "evidence_snapshot_unavailable",
            "evidence_snapshot_failed",
        }
        require(
            response.status_code == 202 or transient_unavailable,
            f"unexpected_readiness_response:{response.status_code}:{payload}",
        )
        time.sleep(READINESS_POLL_SECONDS)

    raise UATFailure(f"readiness_timeout:turn_{expected_answer_count}")


def nested_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            nested
            for item in value.values()
            for nested in nested_keys(item)
        }
    if isinstance(value, list):
        return {nested for item in value for nested in nested_keys(item)}
    return set()


def percentile(values: list[float], ratio: float) -> float:
    require(bool(values), "percentile_requires_values")
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * ratio
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    weighted = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return round(weighted, 3)


def audit_report(
    report: dict[str, Any],
    session: dict[str, Any],
    *,
    expected_session_uuid: str,
) -> dict[str, Any]:
    require(report.get("session_uuid") == expected_session_uuid, "report_session_uuid_mismatch")
    report_keys = nested_keys(report)
    require("confidence" not in report_keys, "participant_report_exposes_model_confidence")
    require("total_score" not in report_keys, "participant_report_exposes_total_score")
    require("ranking" not in report_keys, "participant_report_exposes_ranking")
    dimensions = report.get("dimensions")
    require(isinstance(dimensions, list) and len(dimensions) == 6, "report_must_have_six_dimensions")
    keys = {item.get("dimension_key") for item in dimensions if isinstance(item, dict)}
    require(keys == EXPECTED_DIMENSIONS, f"report_dimension_keys_invalid:{sorted(str(item) for item in keys)}")

    ordered_user_turn_rows = sorted(
        (
            item
            for item in session.get("turns", [])
            if isinstance(item, dict) and item.get("role") == "user"
        ),
        key=lambda item: int(item.get("turn_index", -1)),
    )
    user_turns = {
        item.get("turn_index"): str(item.get("content") or "")
        for item in ordered_user_turn_rows
    }
    answer_ordinal_by_turn_index = {
        item.get("turn_index"): ordinal
        for ordinal, item in enumerate(ordered_user_turn_rows, start=1)
    }
    require(len(user_turns) == 8, f"report_audit_expected_eight_user_turns:{len(user_turns)}")
    evidence_count = 0
    evidence_turns: set[int] = set()
    for dimension in dimensions:
        require(isinstance(dimension, dict), "report_dimension_not_object")
        score = dimension.get("score")
        require(isinstance(score, int) and 1 <= score <= 5, "ready_report_dimension_score_invalid")
        require(dimension.get("status") == "sufficient", "ready_report_dimension_not_sufficient")
        evidences = dimension.get("evidences")
        require(isinstance(evidences, list) and evidences, "scored_dimension_has_no_evidence")
        for evidence in evidences:
            require(isinstance(evidence, dict), "report_evidence_not_object")
            turn_index = evidence.get("turn_index")
            quote = evidence.get("quote")
            require(turn_index in user_turns, f"evidence_does_not_reference_user_turn:{turn_index}")
            require(isinstance(quote, str) and bool(quote.strip()), "report_evidence_quote_empty")
            require(quote in user_turns[turn_index], "report_evidence_is_not_exact_user_substring")
            require(evidence.get("source_type") == "user", "report_evidence_source_is_not_user")
            require(
                evidence.get("answer_ordinal")
                == answer_ordinal_by_turn_index[turn_index],
                f"report_evidence_answer_ordinal_invalid:{turn_index}",
            )
            require(
                not any(marker in quote for marker in EXTERNAL_AI_EVIDENCE_MARKERS),
                "external_ai_quote_was_used_as_scoring_evidence",
            )
            evidence_count += 1
            evidence_turns.add(int(turn_index))

    require(isinstance(report.get("strengths"), list), "report_strengths_not_list")
    require(isinstance(report.get("priorities"), list), "report_priorities_not_list")
    require(bool(str(report.get("disclaimer") or "").strip()), "report_disclaimer_missing")
    require(bool(str(report.get("experimental_notice") or "").strip()), "experimental_notice_missing")
    return {
        "dimension_count": len(dimensions),
        "scored_dimension_count": sum(item.get("score") is not None for item in dimensions),
        "evidence_count": evidence_count,
        "evidence_user_turn_indices": sorted(evidence_turns),
        "all_evidence_exact_user_substrings": True,
        "all_evidence_answer_ordinals_match_user_turn_order": True,
        "external_ai_quote_rejected": True,
    }


def run() -> dict[str, Any]:
    require(
        os.getenv("V6_REAL_UAT_CONFIRM") == "1",
        (
            "set_V6_REAL_UAT_CONFIRM=1_only_after_confirming_a_disposable_local_"
            "real_DeepSeek_v621_enforce_server"
        ),
    )
    base_url = require_local_api_base_url(
        os.getenv("V6_API_BASE_URL", DEFAULT_API_BASE_URL).strip()
    )
    run_id = uuid.uuid4().hex[:12]
    audit: dict[str, Any] = {
        "status": "RUNNING",
        "contract": "real-local-v6.2.1-enforce-participant-api-uat-v1",
        "api_base_url": base_url,
        "locality_guard": "loopback_ip_literal_http_explicit_port",
        "session_uuid": None,
        "turns": [],
        "readiness": [],
    }
    session_uuid: str | None = None
    completed = False
    started = time.monotonic()

    timeout = httpx.Timeout(connect=5.0, read=180.0, write=30.0, pool=5.0)
    with httpx.Client(
        base_url=base_url,
        timeout=timeout,
        trust_env=False,
        follow_redirects=False,
        headers={"User-Agent": "v621-enforce-local-uat/1"},
    ) as client:
        try:
            health, health_ms, _ = request_json(client, "GET", "/health")
            require(health.get("status") == "ok", f"backend_unhealthy:{health}")
            require(health.get("version") == "v6.0", f"unexpected_backend:{health}")
            require(
                str(health.get("app_env", "")).casefold() != "production",
                "health_identifies_server_as_production",
            )
            audit["preflight"] = {
                "health": health,
                "latency_ms": health_ms,
                "server_mode_publicly_observable": False,
                "operator_confirmed_expected_runtime": {
                    "model_gateway_mode": "real",
                    "interviewer_prompt_version": "v6.2.1",
                    "evidence_attribution_mode": "enforce",
                    "database": "disposable_non_production",
                },
            }

            created, create_ms, status_code = request_json(
                client,
                "POST",
                "/sessions",
                payload={
                    "consent_version": "v6.2.1-real-enforce-local-uat",
                    "consent_given": True,
                    "participant": {},
                },
                expected_statuses={201},
            )
            session = created.get("session")
            opening = created.get("initial_turn")
            require(isinstance(session, dict), "created_session_missing")
            require(isinstance(opening, dict), "initial_turn_missing")
            require(session.get("phase") == "interviewing", "created_session_not_interviewing")
            require(session.get("participant", {}).get("display_name", "") == "", "session_not_anonymous")
            require(opening.get("role") == "assistant", "opening_is_not_assistant")
            validate_interviewer_message(opening.get("content"))
            session_uuid = str(session.get("uuid") or "")
            require(bool(session_uuid), "created_session_uuid_missing")
            audit["session_uuid"] = session_uuid
            audit["create"] = {"http_status": status_code, "latency_ms": create_ms, "anonymous": True}

            for index, answer in enumerate(ANSWERS, start=1):
                client_turn_id = f"v621-enforce-{run_id}-{index:02d}"
                turn_record = submit_turn(
                    client,
                    session_uuid,
                    index=index,
                    content=answer,
                    client_turn_id=client_turn_id,
                    force_idempotency_replay=index == 2,
                )
                audit["turns"].append(turn_record)
                readiness_record = poll_exact_readiness(
                    client,
                    session_uuid,
                    expected_answer_count=index,
                )
                audit["readiness"].append(readiness_record)
                if index < 8:
                    require(
                        readiness_record["status"] == "insufficient"
                        and readiness_record["ready"] is False,
                        f"readiness_should_be_insufficient_before_turn_8:turn_{index}",
                    )

            exact = audit["readiness"][-1]
            require(exact["status"] == "ready" and exact["ready"] is True, "eight_turn_readiness_not_ready")
            require(exact["cached"] is True, "terminal_readiness_not_cached")

            finalize_payload = {
                "evidence_check_id": exact["check_id"],
                "expected_transcript_fingerprint": exact["transcript_fingerprint"],
                "allow_incomplete": False,
            }
            finalized, finalize_ms, _ = request_json(
                client,
                "POST",
                f"/sessions/{session_uuid}/finalize",
                payload=finalize_payload,
            )
            final_session = finalized.get("session")
            final_report = finalized.get("report")
            require(isinstance(final_session, dict), "finalized_session_missing")
            require(isinstance(final_report, dict), "finalized_report_missing")
            require(final_session.get("phase") == "completed", "final_session_not_completed")
            require(final_session.get("finalization_state") == "completed", "finalization_state_not_completed")
            require(final_session.get("user_answer_count") == 8, "final_session_answer_count_not_eight")
            require(
                final_session.get("transcript_fingerprint") == exact["transcript_fingerprint"],
                "finalized_fingerprint_does_not_match_exact_readiness",
            )

            fetched_session, session_get_ms, _ = request_json(
                client, "GET", f"/sessions/{session_uuid}"
            )
            fetched_report, report_get_ms, _ = request_json(
                client, "GET", f"/sessions/{session_uuid}/report"
            )
            require(fetched_report == final_report, "fetched_report_differs_from_finalize_report")
            report_audit = audit_report(
                fetched_report,
                fetched_session,
                expected_session_uuid=session_uuid,
            )

            pdf_started = time.monotonic()
            pdf_response = client.get(f"/sessions/{session_uuid}/report.pdf")
            pdf_ms = round((time.monotonic() - pdf_started) * 1000, 3)
            require(pdf_response.status_code == 200, f"pdf_http_{pdf_response.status_code}")
            require(
                pdf_response.headers.get("content-type", "").split(";", 1)[0]
                == "application/pdf",
                "pdf_content_type_invalid",
            )
            require(pdf_response.content.startswith(b"%PDF-"), "pdf_signature_invalid")
            require(b"%%EOF" in pdf_response.content[-64:], "pdf_eof_marker_missing")
            require(len(pdf_response.content) >= 1_000, "pdf_is_unexpectedly_small")

            public_payloads: list[Any] = [
                created,
                *audit["readiness"],
                finalized,
                fetched_session,
                fetched_report,
            ]
            exposed_internal_keys = sorted(
                set().union(*(nested_keys(item) for item in public_payloads))
                & INTERNAL_ATTRIBUTION_KEYS
            )
            require(not exposed_internal_keys, f"participant_api_exposes_internal_attribution:{exposed_internal_keys}")

            turn_latencies = [
                sum(float(item.get("latency_ms", 0)) for item in turn["attempts"])
                for turn in audit["turns"]
            ]
            readiness_latencies = [float(item["elapsed_ms"]) for item in audit["readiness"]]
            transient_turn_retries = sum(
                bool(item["transient_retry_used"]) for item in audit["turns"]
            )
            failed_snapshot_retries = sum(
                bool(item["failed_snapshot_retry_used"])
                for item in audit["readiness"]
            )
            require(
                transient_turn_retries == 0,
                f"release_gate_requires_zero_turn_retries:{transient_turn_retries}",
            )
            require(
                failed_snapshot_retries == 0,
                f"release_gate_requires_zero_snapshot_retries:{failed_snapshot_retries}",
            )
            audit.update(
                {
                    "status": "PUBLIC_CONTRACT_PASSED_DB_AUDIT_REQUIRED",
                    "exact_final_readiness": {
                        "check_id": exact["check_id"],
                        "transcript_fingerprint": exact["transcript_fingerprint"],
                        "status": exact["status"],
                        "ready": exact["ready"],
                    },
                    "finalize": {
                        "latency_ms": finalize_ms,
                        "used_exact_check_id_and_fingerprint": True,
                        "phase": final_session.get("phase"),
                    },
                    "report": report_audit,
                    "pdf": {
                        "latency_ms": pdf_ms,
                        "bytes": len(pdf_response.content),
                        "sha256": hashlib.sha256(pdf_response.content).hexdigest(),
                        "signature_valid": True,
                        "eof_marker_present": True,
                    },
                    "formal_attributed_snapshot": {
                        "participant_api_observable": False,
                        "verified_by_this_script": False,
                        "required_internal_shape": (
                            "AttributedFinalScorerOutput.dimensions[*]."
                            "evidence_refs[*].attribution_span_id"
                        ),
                        "public_api_internal_keys_exposed": exposed_internal_keys,
                        "db_audit_required": True,
                        "db_audit_expectation": (
                            "the exact readiness check result and promoted scoring run use "
                            "evidence_refs, every persisted EvidenceItem binds that check's "
                            "eligible validated attribution_span_id, and report version is v6.2.1"
                        ),
                    },
                    "retry_summary": {
                        "turn_retry_limit": 1,
                        "same_client_turn_id_on_retry": True,
                        "transient_turn_retries": transient_turn_retries,
                        "deliberate_idempotency_replays": sum(
                            item["idempotency_replay"] is not None for item in audit["turns"]
                        ),
                        "failed_snapshot_retries": failed_snapshot_retries,
                        "release_gate_requires_zero_retries": True,
                    },
                    "latency_ms": {
                        "turns": turn_latencies,
                        "turn_p50": percentile(turn_latencies, 0.50),
                        "turn_p95": percentile(turn_latencies, 0.95),
                        "readiness": readiness_latencies,
                        "readiness_p50": percentile(readiness_latencies, 0.50),
                        "readiness_p95": percentile(readiness_latencies, 0.95),
                        "finalize": finalize_ms,
                        "session_get": session_get_ms,
                        "report_get": report_get_ms,
                        "pdf_get": pdf_ms,
                        "total_wall": round((time.monotonic() - started) * 1000, 3),
                    },
                    "scope": (
                        "Real local participant-API UAT only; not production, psychometric "
                        "validity, cross-person comparability, admin UI, microphone, or TTS proof."
                    ),
                }
            )
            completed = True
            return audit
        finally:
            if session_uuid is not None and not completed:
                try:
                    cleanup = client.post(
                        f"/sessions/{session_uuid}/exit",
                        json={"reason": "real_v621_enforce_uat_failed"},
                    )
                    audit["cleanup"] = {
                        "attempted": True,
                        "http_status": cleanup.status_code,
                    }
                except httpx.RequestError as exc:
                    audit["cleanup"] = {
                        "attempted": True,
                        "error": f"{type(exc).__name__}:{str(exc)[:200]}",
                    }


def main() -> None:
    try:
        result = run()
    except (UATFailure, httpx.RequestError) as exc:
        result = {
            "status": "REAL_DEEPSEEK_V621_ENFORCE_UAT_FAILED",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "formal_attributed_snapshot": {
                "participant_api_observable": False,
                "verified_by_this_script": False,
                "db_audit_required": True,
            },
        }
        print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
