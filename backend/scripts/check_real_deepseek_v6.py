#!/usr/bin/env python3
"""Explicit real-model UAT for the local-only V6 natural interview.

This script never starts a server, reads a .env file, or sends credentials. It
only talks to an already-running local V6 API that the operator configured in
real-model mode. Set V6_REAL_UAT_CONFIRM=1 deliberately before running it.

Example:
  V6_REAL_UAT_CONFIRM=1 V6_API_BASE_URL=http://127.0.0.1:8060/api/v1 \
  python backend/scripts/check_real_deepseek_v6.py
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass
from typing import Any

import httpx


FORBIDDEN_VISIBLE = (
    "coverage",
    "candidate_questions",
    "target_dimension",
    "评分",
    "测评维度",
    "系统提示",
    "prompt",
    "你应该",
    "建议你",
)


@dataclass
class FlowResult:
    label: str
    session_uuid: str
    answer_count: int
    closed_by_model: bool
    scored_dimensions: int
    null_dimensions: int
    manual_quality_checks: list[dict[str, Any]]


def request_json(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = client.request(method, path, json=payload)
    if response.status_code >= 400:
        raise AssertionError(
            f"{method} {path} failed: {response.status_code}: {response.text[:800]}"
        )
    return response.json()


def parse_events(response: httpx.Response) -> list[dict[str, Any]]:
    if response.status_code >= 400:
        raise AssertionError(f"turn failed: {response.status_code}: {response.text[:800]}")
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def create_session(client: httpx.Client, label: str) -> dict[str, Any]:
    payload = request_json(
        client,
        "POST",
        "/sessions",
        payload={
            "consent_version": "v6-real-uat-2026-08",
            "consent_given": True,
            "participant": {"display_name": label},
        },
    )
    session = payload["session"]
    if session["phase"] != "interviewing":
        raise AssertionError(f"unexpected_create_phase:{session['phase']}")
    opening = payload["initial_turn"]["content"]
    assert_visible_message(opening)
    return session


def submit(
    client: httpx.Client,
    session_uuid: str,
    index: int,
    content: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = {
        "content": content,
        "client_turn_id": f"real-v6-{index:02d}-{uuid.uuid4().hex[:12]}",
        "input_mode": "text",
        "answer_duration_ms": 15_000,
    }
    response = client.post(f"/sessions/{session_uuid}/turns:stream", json=payload)
    events = parse_events(response)
    completed = next(
        (item["data"] for item in reversed(events) if item.get("event") == "agent_completed"),
        None,
    )
    if not completed:
        raise AssertionError(f"agent_completed_missing:{events[-1] if events else 'none'}")
    return completed["session"], completed


def assert_visible_message(message: str) -> None:
    if not message.strip():
        raise AssertionError("empty_interviewer_message")
    lower = message.casefold()
    if any(token.casefold() in lower for token in FORBIDDEN_VISIBLE):
        raise AssertionError(f"forbidden_visible_language:{message}")
    if message.count("？") + message.count("?") > 1:
        raise AssertionError(f"multiple_primary_questions:{message}")


def report_audit(client: httpx.Client, session: dict[str, Any]) -> tuple[int, int]:
    report = request_json(client, "GET", f"/sessions/{session['uuid']}/report")
    assert "total_score" not in report
    assert "ranking" not in report
    assert len(report["dimensions"]) == 6
    assert all("confidence" not in item for item in report["dimensions"])

    turns = {
        turn["turn_index"]: turn["content"]
        for turn in session["turns"]
        if turn["role"] == "user"
    }
    for dimension in report["dimensions"]:
        if dimension["score"] is not None:
            assert dimension["status"] == "sufficient"
            assert dimension["evidences"]
        for evidence in dimension["evidences"]:
            source = turns.get(evidence["turn_index"])
            if source is None or evidence["quote"] not in source:
                raise AssertionError("report_quote_is_not_exact_user_text")
    return (
        sum(item["score"] is not None for item in report["dimensions"]),
        sum(item["score"] is None for item in report["dimensions"]),
    )


def run_flow(client: httpx.Client, label: str, answers: list[str]) -> FlowResult:
    session = create_session(client, label)
    quality_checks: list[dict[str, Any]] = []
    closed_by_model = False
    for index, answer in enumerate(answers, start=1):
        session, completed = submit(client, session["uuid"], index, answer)
        message = completed["turn"]["content"]
        assert_visible_message(message)
        quality_checks.append(
            {
                "turn": index,
                "question_count": message.count("？") + message.count("?"),
                "quality_flags": completed["turn"].get("quality_flags", []),
                "session_action": completed["session_action"],
                "finish_reason": completed["finish_reason"],
            }
        )
        if completed["session_action"] == "finish":
            closed_by_model = True
            break

    if session["phase"] in {"interviewing", "finalizing"}:
        finalized = request_json(client, "POST", f"/sessions/{session['uuid']}/finalize")
        session = finalized["session"]
    if session["phase"] != "completed":
        raise AssertionError(f"unexpected_final_phase:{session['phase']}")
    scored, nulls = report_audit(client, session)
    return FlowResult(
        label=label,
        session_uuid=session["uuid"],
        answer_count=session["user_answer_count"],
        closed_by_model=closed_by_model,
        scored_dimensions=scored,
        null_dimensions=nulls,
        manual_quality_checks=quality_checks,
    )


def main() -> None:
    if os.getenv("V6_REAL_UAT_CONFIRM") != "1":
        raise SystemExit(
            "Refusing real-model UAT. Set V6_REAL_UAT_CONFIRM=1 after confirming "
            "the local server uses real mode and a non-production experimental database."
        )
    base_url = os.getenv("V6_API_BASE_URL", "http://127.0.0.1:8060/api/v1").rstrip("/")
    with httpx.Client(base_url=base_url, timeout=90.0) as client:
        health = request_json(client, "GET", "/health")
        if health.get("version") != "v6.0":
            raise AssertionError(f"unexpected_backend:{health}")
        results = [
            run_flow(
                client,
                "真实验收：留学申请与人生目标",
                [
                    "我在考虑申请海外硕士，但也担心这会把人生安排变成只追求一张文凭。",
                    "我想先区分真正想得到的成长和对他人期待的回应，再核实课程、成本与未来选择。",
                    "家人看重预算，我也在意长期方向；如果课程匹配和资金条件都不成立，我会调整。",
                ],
            ),
            run_flow(
                client,
                "真实验收：项目决策",
                [
                    "团队在考虑提前发布新功能，我担心速度会掩盖可靠性问题。",
                    "我会比较上线收益、回滚成本和受影响用户，并核实测试数据是否足够。",
                    "如果线上反馈出现关键故障，我会暂停推进并和产品、工程一起复盘。",
                ],
            ),
            run_flow(
                client,
                "真实验收：短答与提前结束",
                [
                    "我还没想清楚。",
                    "我想先结束这次谈话。",
                ],
            ),
        ]
    print(
        json.dumps(
            {
                "status": "REAL_DEEPSEEK_V6_UAT_COMPLETED",
                "api_base_url": base_url,
                "flows": [item.__dict__ for item in results],
                "interpretation": (
                    "这是本地真实模型体验验收，不构成跨用户可比性、心理测量效度、"
                    "语音麦克风或生产部署验证。请人工检查每轮是否承接原话、是否突然补维度、"
                    "是否过度解释、是否给选项或教学，以及结束轮数是否自然变化。"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print(f"REAL_DEEPSEEK_V6_UAT_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
