"""Run the three-tool MCP chain and write the P7 audit artifacts.

Mock mode uses the synthetic transcript below and a non-billable provider.
Real mode requires an explicitly selected de-identified blind-rating case and
never falls back to mock after a provider or contract failure.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


TRANSCRIPT_TURNS: list[dict[str, Any]] = [
    {
        "turn_no": 0,
        "role": "assistant",
        "text": "请讲讲你如何处理这件事？",
    },
    {
        "turn_no": 1,
        "role": "user",
        "text": "我先界定核心问题和目标，也会核实数据来源并交叉验证证据。",
    },
    {
        "turn_no": 2,
        "role": "assistant",
        "text": "你当时怎么形成判断？",
    },
    {
        "turn_no": 3,
        "role": "user",
        "text": "我会写下假设，因为要找反例检验推理；也会比较家人、导师和团队的不同立场。",
    },
    {
        "turn_no": 4,
        "role": "assistant",
        "text": "最后怎么行动，之后如何变化？",
    },
    {
        "turn_no": 5,
        "role": "user",
        "text": "我权衡风险后决定优先执行方案，并设定触发条件，复盘后再调整。",
    },
]

TASK_FILES = (
    "backend/app/mcp/__init__.py",
    "backend/app/mcp/server.py",
    "backend/requirements.txt",
    "backend/scripts/run_mcp_smoke.py",
    "backend/tests/test_mcp_server.py",
    "research/p7/report_7_2_6.txt",
)


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _head_commit() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _task_tree_hash() -> str:
    digest = hashlib.sha256()
    for relative_path in TASK_FILES:
        path = ROOT / relative_path
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _tool_error_text(result: Any) -> str:
    return "\n".join(
        getattr(item, "text", "")
        for item in result.content
        if getattr(item, "type", None) == "text"
    )


async def _call_and_record(
    client: Any,
    tool_name: str,
    request: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    started = time.perf_counter()
    result = await client.call_tool(tool_name, request)
    wire_duration_ms = round((time.perf_counter() - started) * 1000)
    if result.isError:
        raise RuntimeError(f"{tool_name} failed: {_tool_error_text(result)}")
    response = result.structuredContent
    if response is None:
        raise RuntimeError(f"{tool_name} returned no structuredContent")
    return response, {
        "tool": tool_name,
        "request": request,
        "response": response,
        "duration_ms": wire_duration_ms,
        # The spawned server is forced to mock mode, so it makes no external
        # model-provider calls.  This is distinct from exercising the two
        # existing model-gateway code paths (attribution and scoring).
        "model_calls": 0,
        "gateway_mode": "mock",
    }


async def run_smoke(test_results: dict[str, Any]) -> None:
    output_dir = ROOT / "research" / "p7"
    output_dir.mkdir(parents=True, exist_ok=True)
    calls: list[dict[str, Any]] = []
    total_started = time.perf_counter()
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp.server"],
        cwd=BACKEND,
        env={
            "MODEL_GATEWAY_MODE": "mock",
            "DATABASE_URL": "sqlite:////tmp/critical-thinking-v6-mcp-smoke.db",
            "DEEPSEEK_API_KEY": "",
        },
    )

    async with stdio_client(server, errlog=sys.stderr) as (read, write):
        async with ClientSession(read, write) as client:
            initialized = await client.initialize()
            protocol_version = initialized.protocolVersion
            listed = await client.list_tools()
            tools = [
                {
                    "name": tool.name,
                    "input_schema": tool.inputSchema,
                    "output_schema": tool.outputSchema,
                }
                for tool in listed.tools
            ]
            if [tool["name"] for tool in tools] != [
                "attribute_evidence",
                "score_six_dimensions",
                "generate_report",
            ]:
                raise RuntimeError("unexpected MCP tool registry")

            attribution_request = {"transcript_turns": TRANSCRIPT_TURNS}
            attribution_response, attribution_call = await _call_and_record(
                client, "attribute_evidence", attribution_request
            )
            calls.append(attribution_call)

            eligible_span_ids = [
                span["candidate_id"]
                for span in attribution_response["spans"]
                if span["eligibility"] == "eligible"
            ]
            scoring_request = {
                "transcript_turns": TRANSCRIPT_TURNS,
                "eligible_span_ids": eligible_span_ids,
            }
            scoring_response, scoring_call = await _call_and_record(
                client, "score_six_dimensions", scoring_request
            )
            calls.append(scoring_call)

            report_request = {
                "six_dimension_scores": scoring_response["dimensions"],
                "evidence_spans": attribution_response["spans"],
            }
            _report_response, report_call = await _call_and_record(
                client, "generate_report", report_request
            )
            calls.append(report_call)

    total_duration_ms = round((time.perf_counter() - total_started) * 1000)
    model_calls_by_tool = {call["tool"]: call["model_calls"] for call in calls}
    smoke_log = {
        "protocol_version": protocol_version,
        "server": "siheng-v6-scoring",
        "transport": "stdio",
        "transcript_classification": "synthetic_deidentified",
        "calls": calls,
        "summary": {
            "status": "PASSED",
            "tool_calls": len(calls),
            "duration_ms": total_duration_ms,
            "model_calls": {
                "total": sum(model_calls_by_tool.values()),
                "by_tool": model_calls_by_tool,
            },
        },
    }
    (output_dir / "mcp_smoke_test.log").write_text(
        _json_text(smoke_log), encoding="utf-8"
    )

    manifest = {
        "protocol_version": protocol_version,
        "tools": tools,
        "code_commit": (
            f"WORKTREE@{_head_commit()}#sha256:{_task_tree_hash()}"
        ),
        "test_results": test_results,
        "model_calls": {
            "total": sum(model_calls_by_tool.values()),
            "by_tool": model_calls_by_tool,
            "mode": "mock",
        },
        "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(
            timespec="seconds"
        ),
    }
    (output_dir / "mcp_manifest.json").write_text(
        _json_text(manifest), encoding="utf-8"
    )


def _load_blind_rating_transcript(
    transcript_csv: Path, display_id: str
) -> list[dict[str, Any]]:
    role_map = {"interviewer": "assistant", "participant": "user"}
    with transcript_csv.open(encoding="utf-8", newline="") as handle:
        rows = [
            row
            for row in csv.DictReader(handle)
            if row.get("display_id") == display_id
        ]
    if not rows:
        raise ValueError(f"display_id not found in transcript CSV: {display_id}")
    transcript = []
    seen_turns: set[int] = set()
    for row in sorted(rows, key=lambda item: int(item["turn_no"])):
        turn_no = int(row["turn_no"])
        role = role_map.get(row["role"])
        text = row["text"]
        if role is None or not text.strip() or turn_no in seen_turns:
            raise ValueError("blind rating transcript contract is invalid")
        seen_turns.add(turn_no)
        transcript.append({"turn_no": turn_no, "role": role, "text": text})
    return transcript


async def _call_real_and_record(
    client: Any,
    service: Any,
    tool_name: str,
    request: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    started = time.perf_counter()
    try:
        result = await client.call_tool(tool_name, request)
    except Exception as exc:
        return None, {
            "tool": tool_name,
            "request": request,
            "status": "FAILED",
            "error": f"{type(exc).__name__}: {str(exc)}",
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "model_calls": None,
            "gateway_mode": "real",
        }
    wire_duration_ms = round((time.perf_counter() - started) * 1000)
    audit = service.last_audit
    audit_matches = audit is not None and audit.tool == tool_name
    record: dict[str, Any] = {
        "tool": tool_name,
        "request": request,
        "duration_ms": wire_duration_ms,
        "service_duration_ms": audit.duration_ms if audit_matches else None,
        "model_calls": audit.model_calls if audit_matches else None,
        "gateway_mode": "real",
        "provider": audit.provider if audit_matches else None,
        "model": audit.model if audit_matches else None,
    }
    if result.isError:
        record.update(
            {
                "status": "FAILED",
                "error": _tool_error_text(result),
            }
        )
        return None, record
    response = result.structuredContent
    if response is None:
        record.update(
            {
                "status": "FAILED",
                "error": f"{tool_name} returned no structuredContent",
            }
        )
        return None, record
    if not audit_matches:
        record.update(
            {
                "status": "FAILED",
                "error": f"{tool_name} audit record missing",
            }
        )
        return None, record
    record.update({"status": "PASSED", "response": response})
    return response, record


async def run_real_smoke(
    *,
    transcript_csv: Path,
    display_id: str,
    output_log: Path,
) -> bool:
    # Load the repository-local provider configuration without printing it.
    # Environment mode is forced to real before application settings import.
    from dotenv import load_dotenv

    load_dotenv(BACKEND / ".env", override=False)
    os.environ["MODEL_GATEWAY_MODE"] = "real"
    os.environ["EVIDENCE_ATTRIBUTION_MODE"] = "enforce"

    from mcp.shared.memory import create_connected_server_and_client_session
    from mcp.types import LATEST_PROTOCOL_VERSION

    from app.mcp.server import mcp, mcp_service

    transcript_turns = _load_blind_rating_transcript(transcript_csv, display_id)
    calls: list[dict[str, Any]] = []
    total_started = time.perf_counter()
    mcp_service.clear()
    tools: list[dict[str, Any]] = []

    async with create_connected_server_and_client_session(mcp) as client:
        listed = await client.list_tools()
        tools = [
            {
                "name": tool.name,
                "input_schema": tool.inputSchema,
                "output_schema": tool.outputSchema,
            }
            for tool in listed.tools
        ]
        if [tool["name"] for tool in tools] != [
            "attribute_evidence",
            "score_six_dimensions",
            "generate_report",
        ]:
            raise RuntimeError("unexpected MCP tool registry")

        attribution_request = {"transcript_turns": transcript_turns}
        attribution_response, attribution_call = await _call_real_and_record(
            client,
            mcp_service,
            "attribute_evidence",
            attribution_request,
        )
        calls.append(attribution_call)

        scoring_response: dict[str, Any] | None = None
        if attribution_response is not None:
            eligible_span_ids = [
                span["candidate_id"]
                for span in attribution_response["spans"]
                if span["eligibility"] == "eligible"
            ]
            scoring_request = {
                "transcript_turns": transcript_turns,
                "eligible_span_ids": eligible_span_ids,
            }
            scoring_response, scoring_call = await _call_real_and_record(
                client,
                mcp_service,
                "score_six_dimensions",
                scoring_request,
            )
            calls.append(scoring_call)

        if attribution_response is not None and scoring_response is not None:
            report_request = {
                "six_dimension_scores": scoring_response["dimensions"],
                "evidence_spans": attribution_response["spans"],
            }
            _report_response, report_call = await _call_real_and_record(
                client,
                mcp_service,
                "generate_report",
                report_request,
            )
            calls.append(report_call)

    known_model_calls = [
        call["model_calls"]
        for call in calls
        if isinstance(call.get("model_calls"), int)
    ]
    passed = len(calls) == 3 and all(call["status"] == "PASSED" for call in calls)
    smoke_log = {
        "protocol_version": LATEST_PROTOCOL_VERSION,
        "server": "siheng-v6-scoring",
        "transport": "in_memory_mcp_client_server",
        "transcript_classification": "real_deidentified",
        "transcript_source": {
            "dataset": "blind_rating_25",
            "display_id": display_id,
            "turn_count": len(transcript_turns),
        },
        "calls": calls,
        "summary": {
            "status": "PASSED" if passed else "FAILED",
            "tool_calls": len(calls),
            "duration_ms": round((time.perf_counter() - total_started) * 1000),
            "model_calls": {
                "total": sum(known_model_calls),
                "complete": len(known_model_calls) == len(calls),
                "by_tool": {
                    call["tool"]: call.get("model_calls") for call in calls
                },
            },
        },
    }
    output_log.parent.mkdir(parents=True, exist_ok=True)
    output_log.write_text(_json_text(smoke_log), encoding="utf-8")
    return passed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--gateway-mode",
        choices=("mock", "real"),
        default="mock",
    )
    parser.add_argument(
        "--test-results-json",
        default='{"status":"not_recorded"}',
        help="JSON object containing the completed regression results",
    )
    parser.add_argument("--transcript-csv", type=Path)
    parser.add_argument("--display-id", default="C01")
    parser.add_argument(
        "--output-log",
        type=Path,
        default=ROOT / "research" / "p7" / "mcp_smoke_test_real.log",
    )
    args = parser.parse_args()
    if args.gateway_mode == "real":
        if args.transcript_csv is None:
            raise SystemExit("--transcript-csv is required in real mode")
        passed = asyncio.run(
            run_real_smoke(
                transcript_csv=args.transcript_csv,
                display_id=args.display_id,
                output_log=args.output_log,
            )
        )
        raise SystemExit(0 if passed else 1)
    test_results = json.loads(args.test_results_json)
    if not isinstance(test_results, dict):
        raise SystemExit("--test-results-json must decode to an object")
    asyncio.run(run_smoke(test_results))


if __name__ == "__main__":
    main()
