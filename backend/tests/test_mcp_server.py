from __future__ import annotations

from copy import deepcopy
import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from app.domain.catalog import DIMENSIONS
from app.mcp.server import mcp, mcp_service


@pytest.fixture(autouse=True)
def clear_mcp_protocol_registry() -> None:
    mcp_service.clear()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def transcript_turns() -> list[dict[str, object]]:
    return [
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


def _tool_error_text(result) -> str:
    return "\n".join(
        getattr(item, "text", "")
        for item in result.content
        if getattr(item, "type", None) == "text"
    )


async def _run_attribute_and_score(client, transcript_turns):
    attributed = await client.call_tool(
        "attribute_evidence", {"transcript_turns": transcript_turns}
    )
    assert attributed.isError is False
    spans = attributed.structuredContent["spans"]
    eligible_ids = [
        span["candidate_id"]
        for span in spans
        if span["eligibility"] == "eligible"
    ]
    scored = await client.call_tool(
        "score_six_dimensions",
        {
            "transcript_turns": transcript_turns,
            "eligible_span_ids": eligible_ids,
        },
    )
    assert scored.isError is False
    return spans, scored.structuredContent["dimensions"]


@pytest.mark.anyio
async def test_exposes_only_three_typed_tools_and_no_raw_evidence_score_input() -> None:
    async with create_connected_server_and_client_session(mcp) as client:
        listed = await client.list_tools()

    assert [tool.name for tool in listed.tools] == [
        "attribute_evidence",
        "score_six_dimensions",
        "generate_report",
    ]
    by_name = {tool.name: tool for tool in listed.tools}
    score_schema = by_name["score_six_dimensions"].inputSchema
    assert set(score_schema["properties"]) == {
        "transcript_turns",
        "eligible_span_ids",
    }
    assert "eligible_span_ids" in score_schema["required"]
    score_schema_json = json.dumps(score_schema, ensure_ascii=False)
    assert "evidence_text" not in score_schema_json
    assert '"quote"' not in score_schema_json
    assert by_name["attribute_evidence"].outputSchema is not None
    assert by_name["score_six_dimensions"].outputSchema is not None
    assert by_name["generate_report"].outputSchema is not None


@pytest.mark.anyio
async def test_three_tool_chain_uses_registered_spans_and_existing_report(
    transcript_turns,
) -> None:
    async with create_connected_server_and_client_session(mcp) as client:
        spans, dimensions = await _run_attribute_and_score(client, transcript_turns)
        reported = await client.call_tool(
            "generate_report",
            {
                "six_dimension_scores": dimensions,
                "evidence_spans": spans,
            },
        )

    assert reported.isError is False
    assert {item["dimension_key"] for item in dimensions} == {
        dimension.key for dimension in DIMENSIONS
    }
    assert all(item["status"] == "SCORED" for item in dimensions)
    report = reported.structuredContent
    assert len(report["dimensions"]) == 6
    assert report["dimensions"][0]["evidences"][0]["source_type"] == "user"
    assert report["disclaimer"].startswith("数字结果不是人格判断")
    assert mcp_service.last_audit.tool == "generate_report"
    assert mcp_service.last_audit.model_calls == 0


@pytest.mark.anyio
async def test_score_fails_closed_without_prior_attribution(transcript_turns) -> None:
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "score_six_dimensions",
            {
                "transcript_turns": transcript_turns,
                "eligible_span_ids": [],
            },
        )

    assert result.isError is True
    assert "call attribute_evidence first" in _tool_error_text(result)


@pytest.mark.anyio
async def test_score_rejects_unknown_ineligible_and_incomplete_span_sets(
    transcript_turns,
) -> None:
    external_turns = [
        {
            "turn_no": 0,
            "role": "assistant",
            "text": "这段判断来自哪里？",
        },
        {
            "turn_no": 1,
            "role": "user",
            "text": "论文指出：“这个方案一定成功”。",
        },
    ]
    unknown_id = "span_" + "0" * 64
    async with create_connected_server_and_client_session(mcp) as client:
        attributed = await client.call_tool(
            "attribute_evidence", {"transcript_turns": transcript_turns}
        )
        eligible_ids = [
            span["candidate_id"]
            for span in attributed.structuredContent["spans"]
            if span["eligibility"] == "eligible"
        ]
        unknown = await client.call_tool(
            "score_six_dimensions",
            {
                "transcript_turns": transcript_turns,
                "eligible_span_ids": [unknown_id],
            },
        )
        incomplete = await client.call_tool(
            "score_six_dimensions",
            {
                "transcript_turns": transcript_turns,
                "eligible_span_ids": eligible_ids[:-1],
            },
        )

        external = await client.call_tool(
            "attribute_evidence", {"transcript_turns": external_turns}
        )
        context_only_id = external.structuredContent["spans"][0]["candidate_id"]
        ineligible = await client.call_tool(
            "score_six_dimensions",
            {
                "transcript_turns": external_turns,
                "eligible_span_ids": [context_only_id],
            },
        )

    assert unknown.isError is True
    assert "unknown_or_stale_evidence_span_id" in _tool_error_text(unknown)
    assert incomplete.isError is True
    assert "must_equal_server_validated" in _tool_error_text(incomplete)
    assert ineligible.isError is True
    assert "ineligible_evidence_span_id" in _tool_error_text(ineligible)


@pytest.mark.anyio
async def test_report_preserves_error_boundary_and_rejects_score_mutation(
    transcript_turns,
) -> None:
    async with create_connected_server_and_client_session(mcp) as client:
        spans, dimensions = await _run_attribute_and_score(client, transcript_turns)

        with_error = deepcopy(dimensions)
        with_error[0]["status"] = "ERROR"
        with_error[0]["score"] = None
        with_error[0]["evidence_span_ids"] = []
        error_result = await client.call_tool(
            "generate_report",
            {
                "six_dimension_scores": with_error,
                "evidence_spans": spans,
            },
        )

        tampered = deepcopy(dimensions)
        tampered[0]["score"] = 4
        tampered_result = await client.call_tool(
            "generate_report",
            {
                "six_dimension_scores": tampered,
                "evidence_spans": spans,
            },
        )

    assert error_result.isError is True
    assert "ERROR_cannot_be_rewritten_as_IE" in _tool_error_text(error_result)
    assert tampered_result.isError is True
    assert "do_not_match_registered_scorer_output" in _tool_error_text(
        tampered_result
    )
