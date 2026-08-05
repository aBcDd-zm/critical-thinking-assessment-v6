"""Versioned release gates for the V6.1 natural interview.

These constants control completion eligibility and data-quality validation only.
They do not select topics, dimensions, questions, or a fixed interview script.
"""

from __future__ import annotations

import re
import unicodedata


INTERVIEW_PROTOCOL_VERSION = "natural_interviewer_v6.1"
MIN_VALID_ANSWERS = 40
MAX_USER_ANSWERS = 45
MIN_SUBSEQUENT_ANSWER_VISIBLE_CHARACTERS = 20
MAX_CLARIFICATION_VISIBLE_CHARACTERS = 24

# These complete-intent patterns deliberately mirror the frontend's fixed
# clarification semantics. A marker occurring inside an otherwise substantive
# answer is not enough: the full normalized utterance must itself be a request
# to explain, repeat, or rephrase.
_CLARIFICATION_INTENT_PATTERNS = (
    re.compile(
        r"^(?:我)?(?:没|不)(?:听懂|理解|明白)"
        r"(?:你?刚才(?:的)?(?:问题|意思|说法)?)?$"
    ),
    re.compile(r"^(?:这|刚才|你刚才说的)?什么意思$"),
    re.compile(
        r"^(?:请|可以|能不能|麻烦)?"
        r"(?:换(?:一个|一种|个|种)?(?:问法|说法)|"
        r"再(?:说|解释)(?:一遍|一下)?)$"
    ),
)


def normalized_semantic_text(value: str) -> str:
    """Return only NFKC-normalized Unicode letters and numbers."""

    return "".join(
        char
        for char in unicodedata.normalize("NFKC", value).lower()
        if unicodedata.category(char)[0] in {"L", "N"}
    )


def normalized_visible_character_count(value: str) -> int:
    """Count visible semantic characters after Unicode normalization.

    Only Unicode letters and numbers satisfy the research answer-length gate.
    Whitespace, separators, punctuation, control/format characters, emoji, and
    decorative symbols therefore cannot be repeated to bypass it.
    """

    return len(normalized_semantic_text(value))


def is_bounded_clarification_request(value: str) -> bool:
    """Validate a client-declared clarification without model classification.

    The client hint is accepted only when the complete short utterance is a
    request to explain, repeat, or rephrase. Arbitrary prose containing one of
    those phrases is therefore still counted and length-gated as an answer.
    """

    semantic = normalized_semantic_text(value)
    return (
        0 < len(semantic) <= MAX_CLARIFICATION_VISIBLE_CHARACTERS
        and (
            semantic == "我没理解请换一种问法"
            or any(pattern.fullmatch(semantic) for pattern in _CLARIFICATION_INTENT_PATTERNS)
        )
    )


def minimum_visible_characters_for_next_answer(valid_answer_count: int) -> int:
    """The first valid answer may be short; later valid answers need detail."""

    return (
        1
        if valid_answer_count == 0
        else MIN_SUBSEQUENT_ANSWER_VISIBLE_CHARACTERS
    )
