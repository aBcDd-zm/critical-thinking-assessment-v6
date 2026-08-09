from scripts.check_real_deepseek_v621_mainline import (
    contains_enough,
    substantial_repeat_pairs,
)


def test_source_and_return_heuristic_requires_independent_signal_groups() -> None:
    assert contains_enough(
        "哪些是论文材料，哪些是你自己的判断，最终采纳它的理由是什么？",
        (
            ("论文", "材料"),
            ("自己的判断",),
            ("采纳", "理由"),
        ),
        minimum=2,
    )
    assert not contains_enough(
        "论文材料还需要继续看看。",
        (
            ("论文", "材料"),
            ("自己的判断",),
            ("采纳", "理由"),
        ),
        minimum=2,
    )


def test_repeated_question_audit_reports_message_positions() -> None:
    messages = [
        "回到原来的研究选择，你最在意的依据是什么？",
        "先区分哪些是论文原文，哪些是你自己的判断？",
        "回到原来的研究选择，你最在意的依据是什么？",
    ]

    assert substantial_repeat_pairs(messages) == [[1, 3]]
