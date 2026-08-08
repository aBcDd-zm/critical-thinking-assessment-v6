#!/usr/bin/env python3
"""Focused real-model replay for V6.2.1 source clarification and return.

The script talks only to an already-running, non-production local API. It does
not read an env file or credential and refuses to run without an explicit
``V6_REAL_UAT_CONFIRM=1`` guard. The output is an audit record, not a validity
claim; the two semantic checks are deliberately labelled as heuristics for
subsequent blind human review.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import sys
import time
import uuid
from typing import Any

import httpx


CASE_15 = (
    "因为我之前有和它聊过很多有关于这个选题的想法，根据它原有的记忆功能，"
    "我通常会省略掉很多信息，直接提问它我的问题。同时我会喂给它一些论文框架，"
    "让它进行学习并输出对应框架，我再去进行借鉴。AI给出来的信息有时会缺乏稳定性，"
    "当我开始质疑它的时候，它也会质疑自己，并修改它的内容，所以很多内容我都会进行质疑，"
    "这个质疑的过程会持续很长时间，但是每次它都会对之前的框架进行刷新，所以每次给出来的内容都是新的。"
    "这一点让我很难受  这是一次聊天时我向它描述的内容 一个由基金会孵化的公益项目，如何通过18年的组织演进，"
    "在“公益逻辑”与“商业逻辑”的张力中，找到可持续的组织管理模式？这个题目可以是什么？"
    "还有我在想要研究爱德面包坊可能还需要更多深入内部去调查，但是我们可能只能去店里面找店员。"
    "这个我们可以用什么研究方法？？感觉类似于这种访谈法会好一些"
    "上文利用多种数据来源以获得对研究对象的多视角描述，多种数据来源使研究者能对不同证据进行“三角验证”，"
    "从而提高研究信度和效度"
)

CORE_DECISION = (
    "我正在决定是否把爱德面包坊作为研究选题。真正难的是：我们很难进入组织内部，"
    "但又需要判断现有门店访谈能否支撑对公益逻辑和商业逻辑张力的研究。"
)

OWNERSHIP_CLARIFICATION = (
    "研究问题和最后那段三角验证表述来自我给 AI 的问题与论文材料。"
    "我自己的判断是，AI 反复改框架让我不信任直接生成的结论；我暂时仍想研究爱德面包坊，"
    "但只有能接触不同角色并交叉核实材料时才会采纳这个选题。"
)

FORBIDDEN_VISIBLE = (
    "coverage",
    "target_dimension",
    "评分",
    "测评维度",
    "系统提示",
    "prompt",
)


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
            f"{method} {path} failed:{response.status_code}:{response.text[:500]}"
        )
    return response.json()


def parse_events(response: httpx.Response) -> list[dict[str, Any]]:
    if response.status_code >= 400:
        raise AssertionError(
            f"turn failed:{response.status_code}:{response.text[:500]}"
        )
    events = [
        json.loads(line)
        for line in response.text.splitlines()
        if line.strip()
    ]
    error = next((item for item in events if item.get("event") == "error"), None)
    if error:
        raise AssertionError(f"turn stream error:{error.get('code')}:{error.get('message')}")
    return events


def submit(
    client: httpx.Client,
    session_uuid: str,
    index: int,
    content: str,
) -> tuple[dict[str, Any], float]:
    started = time.monotonic()
    response = client.post(
        f"/sessions/{session_uuid}/turns:stream",
        json={
            "content": content,
            "client_turn_id": f"v621-mainline-{index}-{uuid.uuid4().hex[:12]}",
            "input_mode": "text",
            "answer_duration_ms": 15_000,
        },
    )
    wall_ms = round((time.monotonic() - started) * 1000, 3)
    completed = next(
        (
            item["data"]
            for item in reversed(parse_events(response))
            if item.get("event") == "agent_completed"
        ),
        None,
    )
    if completed is None:
        raise AssertionError("agent_completed_missing")
    message = str(completed["turn"]["content"])
    lower = message.casefold()
    if any(term.casefold() in lower for term in FORBIDDEN_VISIBLE):
        raise AssertionError(f"internal_language_leaked:{message}")
    if message.count("？") + message.count("?") > 1:
        raise AssertionError(f"multiple_primary_questions:{message}")
    return completed, wall_ms


def normalized(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", value.casefold())


def substantial_repeat_pairs(messages: list[str], width: int = 12) -> list[list[int]]:
    normalized_messages = [normalized(message) for message in messages]
    repeats: list[list[int]] = []
    for left in range(len(normalized_messages)):
        for right in range(left + 1, len(normalized_messages)):
            source = normalized_messages[left]
            target = normalized_messages[right]
            if len(source) < width or len(target) < width:
                continue
            if any(
                source[index : index + width] in target
                for index in range(len(source) - width + 1)
            ):
                repeats.append([left + 1, right + 1])
    return repeats


def contains_enough(value: str, groups: tuple[tuple[str, ...], ...], minimum: int) -> bool:
    return sum(any(term in value for term in group) for group in groups) >= minimum


def main() -> None:
    if os.getenv("V6_REAL_UAT_CONFIRM") != "1":
        raise SystemExit(
            "Refusing real-model UAT. Set V6_REAL_UAT_CONFIRM=1 only after "
            "confirming the local API uses a disposable database and real DeepSeek."
        )
    base_url = os.getenv(
        "V6_API_BASE_URL", "http://127.0.0.1:8062/api/v1"
    ).rstrip("/")
    started = time.monotonic()
    with httpx.Client(base_url=base_url, timeout=120.0) as client:
        health = request_json(client, "GET", "/health")
        if health.get("version") != "v6.0":
            raise AssertionError(f"unexpected_backend:{health}")
        created = request_json(
            client,
            "POST",
            "/sessions",
            payload={
                "consent_version": "v6.2.1-real-mainline-uat",
                "consent_given": True,
                "participant": {"display_name": "AI 支线盲审"},
            },
        )
        session_uuid = str(created["session"]["uuid"])
        opening = str(created["initial_turn"]["content"])
        completed_1, latency_1 = submit(client, session_uuid, 1, CORE_DECISION)
        completed_2, latency_2 = submit(client, session_uuid, 2, CASE_15)
        completed_3, latency_3 = submit(
            client, session_uuid, 3, OWNERSHIP_CLARIFICATION
        )

    messages = [
        str(completed_1["turn"]["content"]),
        str(completed_2["turn"]["content"]),
        str(completed_3["turn"]["content"]),
    ]
    source_message = messages[1]
    return_message = messages[2]
    source_clarification_heuristic = contains_enough(
        source_message,
        (
            ("外部", "AI", "论文", "材料", "来源"),
            ("自己", "你的判断", "本人"),
            ("采纳", "理由", "为什么"),
        ),
        minimum=2,
    ) and not any(term in source_message for term in ("作弊", "禁止使用", "不该使用AI"))
    return_to_decision_heuristic = contains_enough(
        return_message,
        (
            ("爱德", "面包坊", "选题", "研究"),
            ("决定", "判断", "选择", "取舍"),
            ("调查", "访谈", "材料", "核实", "角色"),
        ),
        minimum=2,
    )
    latencies = [latency_1, latency_2, latency_3]
    p95_ms = round(
        statistics.quantiles(latencies, n=20, method="inclusive")[-1], 3
    )
    early_finish = any(
        item["session_action"] == "finish"
        for item in (completed_1, completed_2, completed_3)
    )
    repeats = substantial_repeat_pairs(messages)
    record = {
        "status": (
            "REAL_DEEPSEEK_V621_MAINLINE_HEURISTICS_PASSED"
            if source_clarification_heuristic
            and return_to_decision_heuristic
            and not early_finish
            else "REAL_DEEPSEEK_V621_MAINLINE_NEEDS_REVIEW"
        ),
        "session_uuid": session_uuid,
        "api_base_url": base_url,
        "opening": opening,
        "interviewer_messages": messages,
        "source_clarification_heuristic": source_clarification_heuristic,
        "return_to_decision_heuristic": return_to_decision_heuristic,
        "mainline_information_gain_events": int(source_clarification_heuristic)
        + int(return_to_decision_heuristic),
        "substantial_repeat_pairs": repeats,
        "early_finish": early_finish,
        "turn_latency_ms": latencies,
        "p95_turn_latency_ms_inclusive": p95_ms,
        "total_wall_ms": round((time.monotonic() - started) * 1000, 3),
        "interpretation": (
            "Heuristic replay evidence only. A blind human reviewer must still judge "
            "neutrality, information gain, repetition, and return to the original decision."
        ),
    }
    print(json.dumps(record, ensure_ascii=False, indent=2))
    if record["status"] != "REAL_DEEPSEEK_V621_MAINLINE_HEURISTICS_PASSED":
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print(f"REAL_DEEPSEEK_V621_MAINLINE_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
