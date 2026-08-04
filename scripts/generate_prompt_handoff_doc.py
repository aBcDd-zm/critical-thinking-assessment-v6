#!/usr/bin/env python3
"""Point maintainers to the canonical V6 Prompt handoff document.

V6 deliberately keeps Prompt governance in versioned Markdown instead of
generating a detached DOCX copy that can drift from the JSON contracts.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HANDOFF = ROOT / "docs" / "V6_PROMPT_HANDOFF.md"


def main() -> int:
    if not HANDOFF.is_file():
        raise SystemExit(f"Missing canonical V6 handoff: {HANDOFF}")
    print(f"V6 Prompt handoff is maintained in {HANDOFF.relative_to(ROOT)}")
    print("No DOCX is generated: update the Markdown contract and code together.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
