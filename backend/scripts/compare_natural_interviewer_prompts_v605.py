#!/usr/bin/env python3
"""Private synthetic blind comparison for interviewer v6.0.4/v6.0.5.

This versioned protocol reuses the hardened v6.0.4 runner boundary while
freezing a new ten-scenario, three-turn comparison plan.  It never reads the
application database or participant data.  Prompt identity remains sealed,
and no automated score is used to select a winner.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from scripts import compare_natural_interviewer_prompts_v604 as base


EXPECTED_MODEL = base.EXPECTED_MODEL
CONFIRMATION = "RUN_V605_SYNTHETIC_BLIND_AB"
PROMPT_VERSIONS = ("v6.0.4", "v6.0.5")
ROUNDS_PER_ARM = 3
MAX_PHYSICAL_REQUESTS = 80
RUNNER_PROTOCOL = "synthetic_interviewer_pair_blind_v1"


SCENARIOS: tuple[base.SyntheticScenario, ...] = (
    base.SyntheticScenario(
        "explicit_emotion_and_loss",
        "你好。最近有什么让你想多说一点？",
        (
            "我和一位合作很久的伙伴结束合作了。理性上知道决定有必要，但我还是很难过，也有点舍不得。",
            "最难受的是以前很多重要时刻都有她参与，现在看到那些共同做过的东西会突然想起她。",
            "我并不是想马上解决这种感觉，只是希望能先把这段关系对我的意义说清楚。",
        ),
    ),
    base.SyntheticScenario(
        "neutral_fact_and_decision",
        "你好。最近有什么让你想多说一点？",
        (
            "我在决定课程项目要不要提前发布。核心功能完成了，不过两项边缘流程还没测完。",
            "我主要看真实使用是否会经过那两项流程，以及出错后能不能安全回退。",
            "如果使用概率低且有回退，我会按计划发布；否则就多留三天补测试。",
        ),
    ),
    base.SyntheticScenario(
        "explicit_correction_and_repair",
        "你好。最近有什么让你想多说一点？",
        (
            "最近我把同时做的任务减少了，想把精力放在一个研究项目上。",
            "不是因为我怕失败。你刚才把重点理解错了，我担心的是任务相互依赖会拖累团队。",
            "我希望先把会阻塞别人的部分做完，再处理只影响我自己的细节。",
        ),
    ),
    base.SyntheticScenario(
        "confusion_and_request_for_explanation",
        "你好。最近有什么让你想多说一点？",
        (
            "我在比较两个实习机会，一个方向新，另一个工作方式更适合我。",
            "我没听懂你刚才问的‘决定的边界’是什么意思，可以先解释一下吗？",
            "如果你是问什么会让我改变选择，那我会先核实导师投入和每天实际做的工作。",
        ),
    ),
    base.SyntheticScenario(
        "privacy_boundary_and_topic_shift",
        "你好。最近有什么让你想多说一点？",
        (
            "有些家庭经历我不想谈。我愿意聊一次团队意见冲突，以及我是怎么处理的。",
            "我先请双方分别说最担心的后果，发现一边担心延期，另一边担心质量。",
            "我们最后做了一个可以回退的小实验，第二天一起检查结果再决定。",
        ),
    ),
    base.SyntheticScenario(
        "need_for_space_without_question",
        "你好。最近有什么让你想多说一点？",
        (
            "今天发生的事情有点多，我现在脑子很乱，不太想马上分析。",
            "能先别继续问吗？我只是想停一下，整理好再说。",
            "谢谢。我现在可以先从最让我放不下的那件事慢慢说。",
        ),
    ),
    base.SyntheticScenario(
        "terse_then_specific",
        "你好。最近有什么让你想多说一点？",
        (
            "我有点犹豫。",
            "主要是要不要接一个新的协调任务。",
            "我担心接下来会没有时间完成已经答应别人的工作。",
        ),
    ),
    base.SyntheticScenario(
        "long_multi_focus_answer",
        "你好。最近有什么让你想多说一点？",
        (
            "我们在讨论是否减少发布前的检查。支持的人担心错过窗口，反对的人担心返工；我负责协调，还要考虑使用者受影响的程度。",
            "我会把安全和数据完整性检查保留，再用过去几次缺陷记录判断其他检查能不能抽样。",
            "如果风险集中在两个关键环节，我会保留它们，同时写清出现异常时立即回退的条件。",
        ),
    ),
    base.SyntheticScenario(
        "explicit_uncertainty",
        "你好。最近有什么让你想多说一点？",
        (
            "我在想毕业后的方向，可现在还没有结论。",
            "我真的还没想好，几种选择看起来都有道理。",
            "至少我不想只因为别人说稳定就决定，我想先知道每天实际会做什么。",
        ),
    ),
    base.SyntheticScenario(
        "repetitive_noisy_expression",
        "你好。最近有什么让你想多说一点？",
        (
            "我总想着以前以前以前那段合作，脑子里一直重复，感觉很难往前走。",
            "她以前对我很好，我说不清，就是会反复想起那些小事。",
            "比如每次赶项目时她都会留到最后，确认大家都知道第二天该做什么。",
        ),
    ),
)

NORMAL_LOGICAL_CALLS = len(SCENARIOS) * len(PROMPT_VERSIONS) * ROUNDS_PER_ARM

REVIEW_DIMENSIONS = (
    "empathy_accuracy_and_evidence",
    "warmth",
    "human_likeness",
    "respect_for_user_pacing_and_boundaries",
    "tentative_understanding_without_overclaiming",
    "correction_and_relationship_repair",
    "relevance_and_information_value",
    "willingness_to_continue",
    "single_primary_action_and_safety",
)

SEVERE_REGRESSION_CODES = (
    "severe_empathy_rupture",
    "severe_repair_failure",
    "severe_followup_regression",
)


_BASE_AUDIT_ONLY_FLAGS = base.audit_only_flags


def _has_preface_then_question(message: str) -> bool:
    match = re.search(r"[?\uff1f]", message)
    if match is None:
        return False
    preface = re.sub(r"\s+", "", message[: match.start()])
    return len(preface) >= 16 and bool(re.search(r"[\uff0c\u3002\uff1b\u2014]", preface))


def audit_only_flags(
    message: str,
    latest_user: str,
    prior_messages: Iterable[str],
) -> list[str]:
    """Add review signals without rewriting or rejecting model output."""

    prior = list(prior_messages)
    flags = list(_BASE_AUDIT_ONLY_FLAGS(message, latest_user, prior))
    if (
        len(prior) >= 2
        and _has_preface_then_question(message)
        and all(_has_preface_then_question(item) for item in prior[-2:])
    ):
        flags.append("three_consecutive_preface_question_shape")
    if re.search(
        r"(?:\u4f60(?:\u73b0\u5728|\u6b64\u523b)?|\u5fc3\u91cc)(?:\u5e94\u8be5|\u4e00\u5b9a|\u80af\u5b9a).{0,18}"
        r"(?:\u96be\u53d7|\u75db\u82e6|\u7126\u8651|\u5bb3\u6015|\u59d4\u5c48|\u6e29\u6696|\u5931\u671b|\u6124\u6012|\u540e\u6094|\u5f88\u7d2f)",
        message,
    ):
        flags.append("assertive_inferred_affect")
    return sorted(set(flags))


def _source_hashes() -> dict[str, str]:
    sources = (
        Path("backend/app/core/config.py"),
        Path("backend/app/schemas.py"),
        Path("backend/app/services/model_gateway.py"),
        Path("backend/scripts/compare_natural_interviewer_prompts_v604.py"),
        Path("backend/scripts/compare_natural_interviewer_prompts_v605.py"),
        Path("backend/scripts/adjudicate_natural_interviewer_v605.py"),
    )
    return {
        str(path): base._sha256_file(base.REPOSITORY_ROOT / path)
        for path in sources
    }


def configure_protocol() -> None:
    """Apply only versioned protocol constants to the frozen base runner."""

    base.__doc__ = __doc__
    base.CONFIRMATION = CONFIRMATION
    base.PROMPT_VERSIONS = PROMPT_VERSIONS
    base.ROUNDS_PER_ARM = ROUNDS_PER_ARM
    base.SCENARIOS = SCENARIOS
    base.NORMAL_LOGICAL_CALLS = NORMAL_LOGICAL_CALLS
    base.MAX_PHYSICAL_REQUESTS = MAX_PHYSICAL_REQUESTS
    base.RUNNER_PROTOCOL = RUNNER_PROTOCOL
    base.REVIEW_DIMENSIONS = REVIEW_DIMENSIONS
    base.audit_only_flags = audit_only_flags
    base._source_hashes = _source_hashes


_BASE_PROTOCOL_ATTRIBUTE_NAMES = (
    "__doc__",
    "CONFIRMATION",
    "PROMPT_VERSIONS",
    "ROUNDS_PER_ARM",
    "SCENARIOS",
    "NORMAL_LOGICAL_CALLS",
    "MAX_PHYSICAL_REQUESTS",
    "RUNNER_PROTOCOL",
    "REVIEW_DIMENSIONS",
    "audit_only_flags",
    "_source_hashes",
)


@contextmanager
def protocol_context() -> Iterator[None]:
    """Temporarily configure the frozen runner and always restore its globals."""

    snapshot = {
        name: getattr(base, name)
        for name in _BASE_PROTOCOL_ATTRIBUTE_NAMES
    }
    configure_protocol()
    try:
        yield
    finally:
        for name, value in snapshot.items():
            setattr(base, name, value)


def _write_blind_review_template(output_dir: Path) -> None:
    packet_path = output_dir / "blind_review_packet.json"
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    template = {
        "protocol": RUNNER_PROTOCOL,
        "reviewer_id": "",
        "blind_packet_sha256": base._sha256_file(packet_path),
        "instructions": (
            "Review without opening sealed files. Score every dimension from 1 to 5. "
            "Use a severe code only for a material rupture, not for ordinary style preferences. "
            "Do not infer prompt identity."
        ),
        "rating_scale": {"minimum": 1, "maximum": 5},
        "severe_regression_codes": list(SEVERE_REGRESSION_CODES),
        "cases": [
            {
                "case_id": case["case_id"],
                "preferred_candidate_id": None,
                "tie": False,
                "candidates": [
                    {
                        "candidate_id": candidate["candidate_id"],
                        "ratings": {dimension: None for dimension in REVIEW_DIMENSIONS},
                        "severe_regression_codes": [],
                        "reviewer_notes": "",
                    }
                    for candidate in case["candidates"]
                ],
            }
            for case in packet["cases"]
        ],
    }
    base._write_private_json(output_dir / "blind_review_template.json", template)


def execute(argv: list[str] | None = None) -> dict[str, Any]:
    with protocol_context():
        manifest = base.execute(argv)
        if manifest.get("status") == "completed":
            _write_blind_review_template(Path(manifest["output_directory"]))
        return manifest


def main() -> None:
    manifest = execute()
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "logical_calls_completed": manifest["logical_calls_completed"],
                "physical_requests_observed": manifest["physical_requests_observed"],
                "output_directory": manifest["output_directory"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
