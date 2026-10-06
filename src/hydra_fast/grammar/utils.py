"""Small grammar predicates shared across the package."""

from __future__ import annotations

import re
from typing import Any

from .interpolation import compile_text

__all__ = ["escape_special_characters", "is_interpolation_string"]


def is_interpolation_string(value: Any) -> bool:
    """True if ``value`` is a string containing a well-formed interpolation.

    Matches ``omegaconf.AnyNode(value)._is_interpolation()``: a string without
    ``${`` is not an interpolation, one with a malformed ``${`` raises
    ``GrammarParseError``, and anything else -- including ``x${a}y`` -- is.
    """
    if not isinstance(value, str) or "${" not in value:
        return False
    compile_text(value)  # raises GrammarParseError when malformed
    return True


# Characters that must be escaped; must match the ESC token in the override
# grammar.
_ESC = "\\()[]{}:=, \t"
_ESC_REGEX = re.compile(f"[{re.escape(_ESC)}]+")


def escape_special_characters(text: str) -> str:
    """Escape characters that would otherwise be override-grammar syntax."""
    matches = _ESC_REGEX.findall(text)
    if not matches:
        return text
    special = set("".join(matches))
    # Backslash first, or it would double-escape everything replaced after it.
    if "\\" in special:
        special.discard("\\")
        text = text.replace("\\", "\\\\")
    for char in special:
        text = text.replace(char, f"\\{char}")
    return text
