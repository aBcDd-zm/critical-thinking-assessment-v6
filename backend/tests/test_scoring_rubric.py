from app.domain.catalog import DIMENSIONS, RUBRIC_VERSION
from app.services.model_gateway import (
    NATURAL_FINAL_SCORER_PROMPT_ID,
    NATURAL_FINAL_SCORER_PROMPT_VERSION,
    NATURAL_FINAL_SCORER_SYSTEM_PROMPT,
    NATURAL_INTERVIEWER_SYSTEM_PROMPT,
)


def test_each_v6_dimension_has_the_previous_five_level_rubric() -> None:
    assert RUBRIC_VERSION == "agent_test_v2_five_level"
    assert len(DIMENSIONS) == 6
    for dimension in DIMENSIONS:
        assert set(dimension.bars) == {1, 2, 3, 4, 5}
        assert all(dimension.bars[level].strip() for level in range(1, 6))
        assert dimension.invalid_evidence.strip()


def test_final_scorer_receives_rubric_but_interviewer_does_not() -> None:
    assert NATURAL_FINAL_SCORER_PROMPT_ID == "natural_final_scorer_v6.1.0"
    assert NATURAL_FINAL_SCORER_PROMPT_VERSION == "v6.1.0"
    assert "五档行为标准" in NATURAL_FINAL_SCORER_SYSTEM_PROMPT
    assert "证据不足时输出 IE" in NATURAL_FINAL_SCORER_SYSTEM_PROMPT
    assert "1分：" in NATURAL_FINAL_SCORER_SYSTEM_PROMPT
    assert "5分：" in NATURAL_FINAL_SCORER_SYSTEM_PROMPT
    assert "五档行为标准" not in NATURAL_INTERVIEWER_SYSTEM_PROMPT
    assert "主要复述情境或直接表态" not in NATURAL_INTERVIEWER_SYSTEM_PROMPT
