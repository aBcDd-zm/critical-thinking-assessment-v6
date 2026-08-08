from __future__ import annotations

import json
from typing import Any

from app.core.config import settings


FORBIDDEN_ATTRIBUTION_KEYS = {
    "asset_fingerprint",
    "attribution_span_id",
    "attribution_span_ids",
    "eliciting_question",
    "elicitation_level",
    "eligibility",
    "evidence_attributions",
    "final_scoring_dimension_keys",
    "owner",
    "readiness_check_id",
    "relation",
    "schema_version",
    "snapshot_used_dimension_keys",
    "source_label",
    "validation_reason",
    "validation_status",
}


def _all_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            nested
            for item in value.values()
            for nested in _all_keys(item)
        }
    if isinstance(value, list):
        return {nested for item in value for nested in _all_keys(item)}
    return set()


def test_participant_endpoints_do_not_expose_admin_attribution_contract(
    client, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "natural_interviewer_prompt_version", "v6.2.1")
    monkeypatch.setattr(settings, "evidence_attribution_mode", "enforce")

    created = client.post(
        "/api/v1/sessions",
        json={
            "consent_version": "v6.2.1-public-contract-test",
            "consent_given": True,
            "participant": {},
        },
    )
    assert created.status_code == 201, created.text
    session_uuid = created.json()["session"]["uuid"]

    fetched = client.get(f"/api/v1/sessions/{session_uuid}")
    assert fetched.status_code == 200, fetched.text
    turn = client.post(
        f"/api/v1/sessions/{session_uuid}/turns:stream",
        json={
            "content": "AI 建议直接上线，但我不同意，因为样本只覆盖老用户。",
            "client_turn_id": "public-contract-0001",
            "input_mode": "text",
            "answer_duration_ms": 1200,
        },
    )
    assert turn.status_code == 200, turn.text
    turn_events = [json.loads(line) for line in turn.text.splitlines() if line]

    readiness = client.post(
        f"/api/v1/sessions/{session_uuid}/report-readiness?retry_failed=true"
    )
    assert readiness.status_code == 200, readiness.text
    readiness_payload = readiness.json()
    assert readiness_payload["status"] in {"ready", "insufficient"}

    finalized = client.post(
        f"/api/v1/sessions/{session_uuid}/finalize",
        json={
            "evidence_check_id": readiness_payload["check_id"],
            "expected_transcript_fingerprint": readiness_payload[
                "transcript_fingerprint"
            ],
            "allow_incomplete": True,
        },
    )
    assert finalized.status_code == 200, finalized.text
    report = client.get(f"/api/v1/sessions/{session_uuid}/report")
    assert report.status_code == 200, report.text

    for payload in (
        created.json(),
        fetched.json(),
        turn_events,
        readiness_payload,
        finalized.json(),
        report.json(),
    ):
        assert not (_all_keys(payload) & FORBIDDEN_ATTRIBUTION_KEYS)
