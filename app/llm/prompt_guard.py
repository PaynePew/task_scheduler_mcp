"""Prompt-integrity guards for the operator-funded LLM actions (ADR-052 §5 / ADR-071).

``language`` / ``focus`` are user-controlled params interpolated into the FIXED
operator-authored system prompt (``llm_summarize`` / ``llm_polish``). This module
keeps them from breaking out of a single line and injecting an extra instruction.
Shared here so the two handlers cannot drift apart.
"""

from __future__ import annotations

# Line/paragraph separators that would let a caller start a new instruction line
# inside the fixed system prompt. C0 CR/LF plus the Unicode separators that a
# model may also treat as a line break: NEL (U+0085), LINE SEPARATOR (U+2028),
# PARAGRAPH SEPARATOR (U+2029). All collapse to a single space.
_LINE_SEPARATORS = ("\r", "\n", "", " ", " ")


def sanitize_prompt_field(value: str) -> str:
    """Collapse line separators to a space and drop C0/DEL control chars.

    ``language`` / ``focus`` ride in the SYSTEM role (ADR-052), so a newline
    (ASCII or Unicode) would let the caller inject a standalone instruction line
    and turn the cost-capped transform into a general-purpose generator. Printable
    Unicode (incl. CJK topic terms) is preserved; length is capped separately by
    the ``Field`` constraints on each param.

    Note: this bounds *structural* injection (extra lines), not *semantic*
    injection within one line (e.g. ``"en. Ignore INPUT"``). The real containment
    for the latter is the length cap plus the fact that output only ever reaches
    the caller's own sink on the caller's own token budget (ADR-071 residual).
    """
    for sep in _LINE_SEPARATORS:
        value = value.replace(sep, " ")
    return "".join(ch for ch in value if ch >= " " and ch != "\x7f")
