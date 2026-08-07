#!/usr/bin/env python3
"""Private synthetic blind comparison for interviewer v6.0.5/v6.1.0.

The protocol focuses on light-guidance behavior: anchoring a concrete event,
keeping one event thread, recovering from drift, eliciting usable reasoning
evidence, and preserving the naturalness gates used for v6.0.5. It never reads
participant data and never changes the runtime default prompt.
"""

from __future__ import annotations

import json
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from scripts import compare_natural_interviewer_prompts_v605 as base


EXPECTED_MODEL = base.EXPECTED_MODEL
CONFIRMATION = "RUN_V610_SYNTHETIC_BLIND_AB"
PROMPT_VERSIONS = ("v6.0.5", "v6.1.0")
ROUNDS_PER_ARM = 3
# The inherited fail-closed transport audit has an import-time cap of 80.
# Keep this protocol aligned with that frozen boundary.
MAX_PHYSICAL_REQUESTS = 80
RUNNER_PROTOCOL = "synthetic_light_guidance_pair_blind_v1"


SCENARIOS: tuple[base.base.SyntheticScenario, ...] = (
    base.base.SyntheticScenario(
        "abstract_principle_to_real_event",
        "你好。最近有什么让你想多说一点？",
        (
            "我觉得做决定就是要理性一点，多考虑后果。",
            "最近小组要不要提前发布项目时，我在进度和稳定性之间犹豫了很久。",
            "最难的是现有测试只能证明主流程能跑，不能证明边缘流程不会影响真实用户。",
        ),
    ),
    base.base.SyntheticScenario(
        "off_topic_then_return",
        "你好。最近有什么让你想多说一点？",
        (
            "我在决定要不要接一个新的协调任务，担心会影响已经答应团队的工作。",
            "说起来我最近还看了一部电影，里面的人也总是很纠结。",
            "回到那次决定，真正让我犹豫的是新任务会不会阻塞已有项目的交付。",
        ),
    ),
    base.base.SyntheticScenario(
        "conclusion_without_basis",
        "你好。最近有什么让你想多说一点？",
        (
            "我最后决定暂时不换实习。",
            "当时最关键的信息是新岗位的导师实际每周只能投入半小时，与最初承诺差距很大。",
            "如果导师投入和项目权限能够写进明确安排，我会重新考虑这个选择。",
        ),
    ),
    base.base.SyntheticScenario(
        "long_multi_focus_answer",
        "你好。最近有什么让你想多说一点？",
        (
            "团队在讨论减少发布检查。我既担心延期，也担心返工，还要考虑使用者、同事排期和负责人承诺，几件事混在一起。",
            "对决定影响最大的是安全检查，因为一旦遗漏，回退也无法消除已经造成的数据影响。",
            "我会保留安全与数据完整性检查，其余项目先按历史缺陷记录决定是否抽样。",
        ),
    ),
    base.base.SyntheticScenario(
        "complete_plan_missing_boundary",
        "你好。最近有什么让你想多说一点？",
        (
            "我比较了三个方案，最后决定先做范围较小、可以回退的试点。",
            "我选择它是因为成本可控，也能先收集真实使用数据再决定是否扩大。",
            "如果试点出现隐私风险，或者关键数据无法稳定记录，我会立即停止并重新设计。",
        ),
    ),
    base.base.SyntheticScenario(
        "correction_and_boundary",
        "你好。最近有什么让你想多说一点？",
        (
            "我愿意聊团队分工，但不想讨论家庭经历。",
            "不是因为我怕承担责任。你刚才理解错了，我担心的是任务依赖会拖累其他人。",
            "我先处理会阻塞他人的部分，再安排只影响自己的工作。",
        ),
    ),
    base.base.SyntheticScenario(
        "request_for_explanation",
        "你好。最近有什么让你想多说一点？",
        (
            "我在比较两个课程项目方向，但还没有决定。",
            "我没听懂你说的‘判断边界’是什么意思，可以先解释一下吗？",
            "如果是问什么会让我改变决定，我会先核实数据能否取得以及导师能投入多少时间。",
        ),
    ),
    base.base.SyntheticScenario(
        "explicit_uncertainty_without_pressure",
        "你好。最近有什么让你想多说一点？",
        (
            "我最近确实遇到一件需要选择的事，但还不知道怎么说。",
            "我暂时不知道。",
            "可以先从项目是否继续说起；我需要判断已有结果值不值得再投入一个月。",
        ),
    ),
)

NORMAL_LOGICAL_CALLS = len(SCENARIOS) * len(PROMPT_VERSIONS) * ROUNDS_PER_ARM

REVIEW_DIMENSIONS = (
    "concrete_event_anchoring",
    "same_event_continuity",
    "off_topic_recovery",
    "evidence_and_reasoning_elicitation",
    "decision_boundary_elicitation",
    "single_primary_action",
    "task_purpose_clarity",
    "warmth_and_human_likeness",
    "respect_for_user_intent_and_boundaries",
    "willingness_to_continue",
)

SEVERE_REGRESSION_CODES = (
    "severe_free_chat_drift",
    "severe_event_abandonment",
    "severe_multi_question_turn",
    "severe_intent_or_empathy_regression",
    "severe_internal_measurement_leak",
)


_V605_AUDIT_ONLY_FLAGS = base.audit_only_flags
_HIDDEN_TERMS = (
    "问题界定",
    "证据评估",
    "推理与论证",
    "多元视角",
    "综合决策",
    "动态调整",
    "覆盖率",
    "高分",
)


def audit_only_flags(
    message: str,
    latest_user: str,
    prior_messages: Iterable[str],
) -> list[str]:
    """Record deterministic review signals without rewriting model output."""

    flags = list(_V605_AUDIT_ONLY_FLAGS(message, latest_user, prior_messages))
    visible_length = len(re.sub(r"\s+", "", message))
    if visible_length > 90:
        flags.append("normal_reply_over_90_visible_characters")
    if any(term in message for term in _HIDDEN_TERMS):
        flags.append("internal_measurement_language_visible")
    if re.search(r"(?:随便聊聊|想聊什么都可以|最近有什么想多说)", message):
        flags.append("free_chat_invitation")
    return sorted(set(flags))


def _source_hashes() -> dict[str, str]:
    sources = (
        Path("backend/app/core/config.py"),
        Path("backend/app/schemas.py"),
        Path("backend/app/services/model_gateway.py"),
        Path("backend/scripts/compare_natural_interviewer_prompts_v604.py"),
        Path("backend/scripts/compare_natural_interviewer_prompts_v605.py"),
        Path("backend/scripts/compare_natural_interviewer_prompts_v610.py"),
    )
    return {
        str(path): base.base._sha256_file(base.base.REPOSITORY_ROOT / path)
        for path in sources
    }


_OVERRIDDEN_NAMES = (
    "__doc__",
    "CONFIRMATION",
    "PROMPT_VERSIONS",
    "ROUNDS_PER_ARM",
    "SCENARIOS",
    "NORMAL_LOGICAL_CALLS",
    "MAX_PHYSICAL_REQUESTS",
    "RUNNER_PROTOCOL",
    "REVIEW_DIMENSIONS",
    "SEVERE_REGRESSION_CODES",
    "audit_only_flags",
    "_source_hashes",
)


@contextmanager
def protocol_context() -> Iterator[None]:
    """Temporarily configure the frozen v6.0.5 runner and restore it."""

    snapshot = {name: getattr(base, name) for name in _OVERRIDDEN_NAMES}
    replacements = {
        "__doc__": __doc__,
        "CONFIRMATION": CONFIRMATION,
        "PROMPT_VERSIONS": PROMPT_VERSIONS,
        "ROUNDS_PER_ARM": ROUNDS_PER_ARM,
        "SCENARIOS": SCENARIOS,
        "NORMAL_LOGICAL_CALLS": NORMAL_LOGICAL_CALLS,
        "MAX_PHYSICAL_REQUESTS": MAX_PHYSICAL_REQUESTS,
        "RUNNER_PROTOCOL": RUNNER_PROTOCOL,
        "REVIEW_DIMENSIONS": REVIEW_DIMENSIONS,
        "SEVERE_REGRESSION_CODES": SEVERE_REGRESSION_CODES,
        "audit_only_flags": audit_only_flags,
        "_source_hashes": _source_hashes,
    }
    for name, value in replacements.items():
        setattr(base, name, value)
    try:
        yield
    finally:
        for name, value in snapshot.items():
            setattr(base, name, value)


def execute(argv: list[str] | None = None) -> dict[str, Any]:
    with protocol_context():
        return base.execute(argv)


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
