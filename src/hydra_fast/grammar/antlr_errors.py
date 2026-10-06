r"""ANTLR-shaped syntax error messages, without ANTLR.

Hydra and OmegaConf surface ANTLR's parser errors verbatim, so a user who hits
a malformed override sees one of five message shapes:

    token recognition error at: 'X'          (the lexer found no rule)
    mismatched input 'X' expecting Y         (a specific token was required)
    no viable alternative at input 'X'       (no alternative could start here)
    extraneous input 'X' expecting Y         (trailing junk, deletion recovery)
    missing Y at 'X'                         (insertion recovery)

``Y`` is one token's display name, or a set rendered ``{A, B, C}``. The set is
ordered by the token's **declaration index in the .g4 grammar**, and a lexer
rule whose body is a bare literal is displayed as that literal (``':'``)
rather than by name (``COLON``). That rule was verified against ANTLR's own
renderer -- see ``bench/oracle_errors.py``.

Reproducing the *shape* and the offending token is what makes a message
useful, and is what the upstream suites assert. The caller supplies the
expected set, because only the parser knows what it was looking for.
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional, Tuple

__all__ = [
    "TokenVocabulary",
    "extraneous_input",
    "mismatched_input",
    "missing_token",
    "no_viable_alternative",
    "token_recognition_error",
]

EOF_DISPLAY = "<EOF>"


class TokenVocabulary:
    """Maps a parser's token constants to ANTLR's display names and order.

    ``entries`` is ``{token constant: (declaration index, display name)}``.
    The declaration index is the token's position in the ``.g4`` lexer, which
    is what ANTLR sorts expected-token sets by.
    """

    __slots__ = ("_order", "_display")

    def __init__(self, entries: Dict[int, Tuple[int, str]]) -> None:
        self._order = {token: index for token, (index, _) in entries.items()}
        self._display = {token: name for token, (_, name) in entries.items()}

    def display(self, token: Optional[int]) -> str:
        if token is None:
            return EOF_DISPLAY
        return self._display.get(token, str(token))

    def render(self, expected: Iterable[int], *, with_eof: bool = False) -> str:
        """One name, or ``{A, B, C}`` ordered as ANTLR orders it."""
        tokens = sorted(set(expected), key=lambda t: self._order.get(t, 1 << 30))
        names = [self._display[t] for t in tokens if t in self._display]
        if with_eof:
            # EOF is token type -1, so it sorts ahead of everything.
            names = [EOF_DISPLAY] + names
        if len(names) == 1:
            return names[0]
        return "{" + ", ".join(names) + "}"


# ---------------------------------------------------------------------------
# the five shapes
# ---------------------------------------------------------------------------
def token_recognition_error(text: str) -> str:
    """The lexer could not match any rule at this position."""
    return f"token recognition error at: '{text}'"


def mismatched_input(found: str, expected: str) -> str:
    """A specific token (or one of a set) was required here."""
    return f"mismatched input '{found}' expecting {expected}"


def no_viable_alternative(span: str) -> str:
    """No alternative of the current rule could match.

    ``span`` is the source text from the start of the failing rule through the
    offending token, which is what ANTLR reports.
    """
    return f"no viable alternative at input '{span}'"


def extraneous_input(found: str, expected: str) -> str:
    """Input remains where the rule was complete (deletion recovery)."""
    return f"extraneous input '{found}' expecting {expected}"


def missing_token(expected: str, found: str) -> str:
    """A required token is absent but parsing could continue without it."""
    return f"missing {expected} at '{found}'"
