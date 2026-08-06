from __future__ import annotations

import json
from pathlib import Path

from scripts import adjudicate_natural_interviewer_v605 as adjudicator
from scripts import compare_natural_interviewer_prompts_v604 as base
from scripts import compare_natural_interviewer_prompts_v605 as runner


def _make_run(tmp_path: Path) -> tuple[Path, list[Path]]:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    sealed = run_dir / "sealed"
    sealed.mkdir()
    cases = []
    mapping = []
    for index in range(2):
        case_id = f"case_{index}"
        candidate_ids = (f"candidate_{index}_a", f"candidate_{index}_b")
        cases.append(
            {
                "case_id": case_id,
                "candidates": [
                    {"candidate_id": candidate_id, "rounds": []}
                    for candidate_id in candidate_ids
                ],
            }
        )
        mapping.extend(
            [
                {
                    "case_id": case_id,
                    "candidate_id": candidate_ids[0],
                    "prompt_version": "v6.0.4",
                },
                {
                    "case_id": case_id,
                    "candidate_id": candidate_ids[1],
                    "prompt_version": "v6.0.5",
                },
            ]
        )
    packet = {"protocol": runner.RUNNER_PROTOCOL, "cases": cases}
    packet_path = run_dir / "blind_review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    (run_dir / "run_manifest.json").write_text(
        json.dumps({"status": "completed"}), encoding="utf-8"
    )
    (sealed / "opaque_mapping.json").write_text(
        json.dumps({"candidate_arm_key": mapping}), encoding="utf-8"
    )
    packet_sha = base._sha256_file(packet_path)
    reviews: list[Path] = []
    for reviewer_index in range(2):
        review = {
            "protocol": runner.RUNNER_PROTOCOL,
            "reviewer_id": f"reviewer_{reviewer_index}",
            "blind_packet_sha256": packet_sha,
            "cases": [],
        }
        for case in cases:
            review["cases"].append(
                {
                    "case_id": case["case_id"],
                    "preferred_candidate_id": case["candidates"][1]["candidate_id"],
                    "tie": False,
                    "candidates": [
                        {
                            "candidate_id": candidate["candidate_id"],
                            "ratings": {
                                dimension: 4 + candidate_index
                                for dimension in runner.REVIEW_DIMENSIONS
                            },
                            "severe_regression_codes": [],
                            "reviewer_notes": "",
                        }
                        for candidate_index, candidate in enumerate(case["candidates"])
                    ],
                }
            )
        path = tmp_path / f"review_{reviewer_index}.json"
        path.write_text(json.dumps(review), encoding="utf-8")
        reviews.append(path)
    return run_dir, reviews


def test_adjudicator_promotes_only_after_two_complete_blind_reviews(tmp_path: Path) -> None:
    run_dir, reviews = _make_run(tmp_path)

    result = adjudicator.adjudicate(run_dir, reviews)

    assert result["decision"] == "PROMOTE_V605"
    assert result["v605_eligible_for_default"] is True
    assert result["v605_stop_codes"] == []
    assert all(result["noninferiority_by_dimension"].values())
    assert (run_dir / "sealed" / "v605_blind_adjudication.json").exists()


def test_same_severe_code_in_two_distinct_v605_cases_stops_promotion(tmp_path: Path) -> None:
    run_dir, reviews = _make_run(tmp_path)
    for review_path in reviews:
        review = json.loads(review_path.read_text(encoding="utf-8"))
        for case in review["cases"]:
            v605_candidate = case["candidates"][1]
            v605_candidate["severe_regression_codes"] = ["severe_empathy_rupture"]
            v605_candidate["reviewer_notes"] = "Explicit emotion was dismissed."
        review_path.write_text(json.dumps(review), encoding="utf-8")

    result = adjudicator.adjudicate(run_dir, reviews)

    assert result["decision"] == "KEEP_V604"
    assert result["v605_eligible_for_default"] is False
    assert result["v605_stop_codes"] == ["severe_empathy_rupture"]
    assert result["severe_case_counts_by_version"]["v6.0.5"]["severe_empathy_rupture"] == 2
