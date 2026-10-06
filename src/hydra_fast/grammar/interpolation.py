r"""A hand-written replacement for OmegaConf's ANTLR interpolation grammar.

Two things make this fast where the ANTLR path is slow.

**Compile to closures.** Each distinct string is lexed and parsed once, then
*compiled* into a Python closure. Resolving ``${a.b}`` for the thousandth time
calls a closure that does one dict walk -- there is no parse tree to re-walk
and no visitor dispatch. ANTLR re-walks its tree on every resolution.

**Cache on the string.** :func:`compile_text` is memoized, so a sweep that
resolves the same few dozen interpolation strings across hundreds of points
pays the parse cost a few dozen times total.

The token rules are transcribed from ``OmegaConfGrammarLexer.g4`` and the
reductions from ``grammar_visitor.py``, including ANTLR's longest-match /
first-rule-wins disambiguation, so the accepted syntax is the same:

    ${a.b}  ${.rel}  ${..rel}  ${a[b].c}  ${oc.env:VAR,default}
    ${a.${b}}  ${f:'quoted ${x}'}  ${f:[1,2],{a:1}}  \${literal}

Target syntax level is omegaconf 2.3 / hydra 1.3.
"""

from __future__ import annotations

import contextlib
import re
import warnings
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence, Tuple

from .. import _cache
from ..errors import GrammarParseError, InterpolationResolutionError

__all__ = [
    "Analysis",
    "MISSING_MARKER",
    "NodeRef",
    "analyze",
    "parse",
    "compile_single_element",
    "compile_text",
    "has_interpolation",
    "split_key",
]

MISSING_MARKER = "???"

# ---------------------------------------------------------------------------
# token types
# ---------------------------------------------------------------------------
(
    ANY_STR,
    ESC,
    ESC_INTER,
    TOP_ESC,
    QUOTED_ESC,
    INTER_OPEN,
    INTER_CLOSE,
    BRACE_OPEN,
    BRACE_CLOSE,
    QUOTE_OPEN_SINGLE,
    QUOTE_OPEN_DOUBLE,
    MATCHING_QUOTE_CLOSE,
    COMMA,
    BRACKET_OPEN,
    BRACKET_CLOSE,
    COLON,
    FLOAT,
    INT,
    BOOL,
    NULL,
    UNQUOTED_CHAR,
    ID,
    WS,
    DOT,
    INTER_KEY,
    EOF,
) = range(26)

# ---------------------------------------------------------------------------
# lexer
# ---------------------------------------------------------------------------
DEFAULT_MODE = "DEFAULT_MODE"
VALUE_MODE = "VALUE_MODE"
INTERPOLATION_MODE = "INTERPOLATION_MODE"
QUOTED_SINGLE_MODE = "QUOTED_SINGLE_MODE"
QUOTED_DOUBLE_MODE = "QUOTED_DOUBLE_MODE"

# mode actions
_PUSH = 1
_POP = 2
_SET = 3

_INT_UNSIGNED = r"(?:0|[1-9](?:_?[0-9])*)"
_POINT_FLOAT = rf"(?:{_INT_UNSIGNED}\.(?![0-9])|{_INT_UNSIGNED}?\.[0-9](?:_?[0-9])*)"
_EXPONENT_FLOAT = rf"(?:(?:{_INT_UNSIGNED}|{_POINT_FLOAT})[eE][+-]?[0-9](?:_?[0-9])*)"
_FLOAT = rf"[+-]?(?:{_EXPONENT_FLOAT}|{_POINT_FLOAT}|[Ii][Nn][Ff]|[Nn][Aa][Nn])"
_ESC_SEQ = r"(?:\\\\|\\\(|\\\)|\\\[|\\\]|\\\{|\\\}|\\:|\\=|\\,|\\ |\\\t)+"

# Each entry: (compiled regex, token type, mode action, mode argument).
# Order matters: on equal match length the earlier rule wins, matching ANTLR.
_MODES: dict = {
    DEFAULT_MODE: [
        (r"\$\{[ \t]*", INTER_OPEN, _PUSH, INTERPOLATION_MODE),
        (r"[^$]*[^\\$]", ANY_STR, None, None),
        (r"(?:\\\\)*\\\$\{", ESC_INTER, None, None),
        (r"(?:\\\\)+", TOP_ESC, None, None),
        (r"\\+", ANY_STR, None, None),
        (r"\$", ANY_STR, None, None),
    ],
    VALUE_MODE: [
        (r"\$\{[ \t]*", INTER_OPEN, _PUSH, INTERPOLATION_MODE),
        (r"\{[ \t]*", BRACE_OPEN, _PUSH, VALUE_MODE),
        (r"[ \t]*\}", BRACE_CLOSE, _POP, None),
        (r"'", QUOTE_OPEN_SINGLE, _PUSH, QUOTED_SINGLE_MODE),
        (r"\"", QUOTE_OPEN_DOUBLE, _PUSH, QUOTED_DOUBLE_MODE),
        (r"[ \t]*,[ \t]*", COMMA, None, None),
        (r"\[[ \t]*", BRACKET_OPEN, None, None),
        (r"[ \t]*\]", BRACKET_CLOSE, None, None),
        (r"[ \t]*:[ \t]*", COLON, None, None),
        (_FLOAT, FLOAT, None, None),
        (rf"[+-]?{_INT_UNSIGNED}", INT, None, None),
        (r"[Tt][Rr][Uu][Ee]|[Ff][Aa][Ll][Ss][Ee]", BOOL, None, None),
        (r"[Nn][Uu][Ll][Ll]", NULL, None, None),
        (r"[/\-\\+.$%*@?|]", UNQUOTED_CHAR, None, None),
        (r"[a-zA-Z_][a-zA-Z0-9_\-]*", ID, None, None),
        (_ESC_SEQ, ESC, None, None),
        (r"[ \t]+", WS, None, None),
    ],
    INTERPOLATION_MODE: [
        (r"\$\{[ \t]*", INTER_OPEN, _PUSH, INTERPOLATION_MODE),
        (r"[ \t]*:[ \t]*", COLON, _SET, VALUE_MODE),
        (r"[ \t]*\}", INTER_CLOSE, _POP, None),
        (r"\.", DOT, None, None),
        (r"\[", BRACKET_OPEN, None, None),
        (r"\]", BRACKET_CLOSE, None, None),
        (r"[a-zA-Z_][a-zA-Z0-9_\-]*", ID, None, None),
        (r"(?:[^\\{}()\[\]:. \t'\"]|\\\\|\\\.|\\\[|\\\]|\\:|\\=)+", INTER_KEY, None, None),
    ],
    QUOTED_SINGLE_MODE: [
        (r"\$\{[ \t]*", INTER_OPEN, _PUSH, INTERPOLATION_MODE),
        (r"'", MATCHING_QUOTE_CLOSE, _POP, None),
        (r"[^'$]*[^'\\$]", ANY_STR, None, None),
        (r"(?:\\\\)*\\\$\{", ESC_INTER, None, None),
        (r"(?:\\\\)*\\'", ESC, None, None),
        (r"(?:\\\\)+", QUOTED_ESC, None, None),
        (r"\\+", ANY_STR, None, None),
        (r"\$", ANY_STR, None, None),
    ],
    QUOTED_DOUBLE_MODE: [
        (r"\$\{[ \t]*", INTER_OPEN, _PUSH, INTERPOLATION_MODE),
        (r"\"", MATCHING_QUOTE_CLOSE, _POP, None),
        (r"[^\"$]*[^\"\\$]", ANY_STR, None, None),
        (r"(?:\\\\)*\\\$\{", ESC_INTER, None, None),
        (r"(?:\\\\)*\\\"", ESC, None, None),
        (r"(?:\\\\)+", QUOTED_ESC, None, None),
        (r"\\+", ANY_STR, None, None),
        (r"\$", ANY_STR, None, None),
    ],
}

# compile once at import
_COMPILED: dict = {
    mode: [(re.compile(pattern), ttype, action, arg) for pattern, ttype, action, arg in rules]
    for mode, rules in _MODES.items()
}


class _Token:
    __slots__ = ("type", "text")

    def __init__(self, ttype: int, text: str) -> None:
        self.type = ttype
        self.text = text

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Token({self.type}, {self.text!r})"


def _tokenize(value: str, mode: str) -> List[_Token]:
    """Longest-match lexing with an explicit mode stack."""
    tokens: List[_Token] = []
    stack = [mode]
    pos = 0
    length = len(value)
    while pos < length:
        rules = _COMPILED[stack[-1]]
        best_len = 0
        best: Any = None
        for regex, ttype, action, arg in rules:
            match = regex.match(value, pos)
            if match is None:
                continue
            size = match.end() - pos
            # strictly greater keeps the first rule on a tie, as ANTLR does
            if size > best_len:
                best_len = size
                best = (match, ttype, action, arg)
        if best is None or best_len == 0:
            raise GrammarParseError(
                f"token recognition error at: '{value[pos : pos + 10]}' in {value!r}"
            )
        match, ttype, action, arg = best
        tokens.append(_Token(ttype, match.group(0)))
        pos = match.end()
        if action == _PUSH:
            stack.append(arg)
        elif action == _POP:
            if len(stack) == 1:
                raise GrammarParseError("Empty Stack")
            stack.pop()
        elif action == _SET:
            stack[-1] = arg
    tokens.append(_Token(EOF, ""))
    return tokens


# ---------------------------------------------------------------------------
# key paths
# ---------------------------------------------------------------------------
_KEY_SPLIT = re.compile(r"\\([\\.\[\]:=])")
_BRACKET_SPLIT = re.compile(r"\[([^\[\]]*)\]|([^.\[\]]+)")


def _unescape_key(text: str) -> str:
    return _KEY_SPLIT.sub(r"\1", text)


def split_key(key: str) -> List[str]:
    """``"a.b"``/``"a[b]"``/``"[a].b"`` -> ``["a", "b"]``."""
    if not key:
        return []
    parts: List[str] = []
    for match in _BRACKET_SPLIT.finditer(key):
        bracket, plain = match.group(1), match.group(2)
        if bracket is not None:
            parts.append(bracket)
        elif plain is not None:
            parts.extend(plain.split("."))
    return [p for p in parts if p != ""]


_HAS_INTER = re.compile(r"\$\{")


def has_interpolation(value: str) -> bool:
    """Cheap pre-filter: does this string need the grammar at all?"""
    return "${" in value


# ---------------------------------------------------------------------------
# AST -> closure compiler
# ---------------------------------------------------------------------------
# A compiled node is a callable taking the evaluation context and returning a
# value. Constants compile to a closure over the value; that keeps the
# evaluation loop uniform and still beats re-dispatching on node type.

Compiled = Callable[[Any], Any]


@dataclass(frozen=True)
class NodeRef:
    """One ``${a.b}`` reference, as written."""

    spelling: str
    parts: Tuple[str, ...]
    relative_dots: int
    dynamic: bool
    """True when part of the path is itself an interpolation, so ``parts`` is
    what could be determined statically and may be incomplete."""


@dataclass(frozen=True)
class Analysis:
    """What an interpolation string references, without resolving it.

    A compiled closure is opaque -- that is the point, it is a function. But
    the compiler knows the structure while it is building one, so it is
    recorded here rather than discarded. This is what an ANTLR parse tree
    would otherwise be walked for: dependency analysis, "which keys does this
    config read?", linting.
    """

    node_refs: Tuple[NodeRef, ...] = ()
    resolvers: Tuple[str, ...] = ()
    dynamic: bool = False
    """True if any referenced path or resolver name is computed at resolve
    time, so the lists above are a lower bound."""

    @property
    def has_interpolation(self) -> bool:
        return bool(self.node_refs or self.resolvers or self.dynamic)

    def referenced_keys(self) -> Tuple[str, ...]:
        """Absolute keys this string reads, as dotted paths.

        Statically-known references only. A computed path (``${a.${k}}``)
        cannot be resolved to a key without evaluating, so it is excluded
        here and reported via :attr:`dynamic` and
        ``node_refs[i].dynamic`` instead -- callers doing dependency analysis
        need to know the list is a lower bound rather than silently get a
        placeholder.
        """
        return tuple(
            ref.spelling for ref in self.node_refs if not ref.relative_dots and not ref.dynamic
        )


_EMPTY_ANALYSIS = Analysis()


def analysis_of(compiled: Compiled) -> Analysis:
    """The :class:`Analysis` recorded for a compiled closure."""
    return getattr(compiled, "hf_analysis", _EMPTY_ANALYSIS)


def to_plain(value: Any) -> Any:
    """Unwrap a config view to a plain Python value.

    Rebound by :mod:`hydra_fast.container` at import time. Compiled closures
    call it through the module global, so the late binding costs nothing extra
    and this module stays free of a circular import.
    """
    return value


def _const(value: Any) -> Compiled:
    def run(_ctx: Any) -> Any:
        return value

    return run


class _Parser:
    """Recursive descent over the token list."""

    __slots__ = ("tokens", "pos", "source", "refs", "resolvers", "dynamic")

    def __init__(self, tokens: List[_Token], source: str) -> None:
        self.tokens = tokens
        self.pos = 0
        self.source = source
        # Recorded as the closures are built, for Analysis. Compile-time only;
        # resolution never touches these.
        self.refs: List[NodeRef] = []
        self.resolvers: List[str] = []
        self.dynamic = False

    # -- token helpers ---------------------------------------------------
    @property
    def current(self) -> _Token:
        return self.tokens[self.pos]

    def peek(self, offset: int = 0) -> int:
        index = self.pos + offset
        return self.tokens[index].type if index < len(self.tokens) else EOF

    def advance(self) -> _Token:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def expect(self, ttype: int) -> _Token:
        token = self.tokens[self.pos]
        if token.type != ttype:
            raise GrammarParseError(
                f"unexpected token {token.text!r} while parsing {self.source!r}"
            )
        self.pos += 1
        return token

    def error(self, message: str) -> GrammarParseError:
        return GrammarParseError(f"{message} while parsing {self.source!r}")

    # -- rules -----------------------------------------------------------
    def parse_config_value(self) -> Compiled:
        """``configValue: text EOF``"""
        node = self.parse_text(stop={EOF})
        self.expect(EOF)
        return node

    def parse_single_element(self) -> Compiled:
        """``singleElement: element EOF``"""
        node = self.parse_element()
        self.expect(EOF)
        return node

    _TEXT_TOKENS = frozenset({ANY_STR, ESC, ESC_INTER, TOP_ESC, QUOTED_ESC, INTER_OPEN})

    def parse_text(self, stop: frozenset | set) -> Compiled:
        """``text: (interpolation | ANY_STR | ESC | ESC_INTER | TOP_ESC | QUOTED_ESC)+``"""
        pieces: List[Tuple[int, Any]] = []
        while True:
            ttype = self.peek()
            if ttype in stop or ttype not in self._TEXT_TOKENS:
                break
            if ttype == INTER_OPEN:
                pieces.append((INTER_OPEN, self.parse_interpolation()))
            else:
                pieces.append((ttype, self.advance().text))
        if not pieces:
            raise self.error("empty text")
        # A lone interpolation yields its value unchanged -- that is how
        # `${a}` can produce an int, a list or a nested config rather than a
        # string. Anything else is a string concatenation.
        if len(pieces) == 1 and pieces[0][0] == INTER_OPEN:
            return pieces[0][1]
        return _compile_unescape(pieces)

    def parse_element(self) -> Compiled:
        """``element: primitive | quotedValue | listContainer | dictContainer``"""
        ttype = self.peek()
        if ttype == BRACKET_OPEN:
            return self.parse_list()
        if ttype == BRACE_OPEN:
            return self.parse_dict()
        if ttype in (QUOTE_OPEN_SINGLE, QUOTE_OPEN_DOUBLE):
            return self.parse_quoted()
        return self.parse_primitive()

    def parse_quoted(self) -> Compiled:
        """``quotedValue: (QUOTE_OPEN_SINGLE | QUOTE_OPEN_DOUBLE) text? MATCHING_QUOTE_CLOSE``

        A quoted value is always a string, even when it holds exactly one
        interpolation -- that is the documented way to force ``${x}`` to
        stringify.
        """
        self.advance()  # opening quote
        if self.peek() == MATCHING_QUOTE_CLOSE:
            self.advance()
            return _const("")
        pieces: List[Tuple[int, Any]] = []
        while True:
            ttype = self.peek()
            if ttype == MATCHING_QUOTE_CLOSE:
                break
            if ttype == EOF:
                raise self.error("unterminated quoted value")
            if ttype == INTER_OPEN:
                pieces.append((INTER_OPEN, self.parse_interpolation()))
            elif ttype in self._TEXT_TOKENS:
                pieces.append((ttype, self.advance().text))
            else:
                raise self.error(f"unexpected token {self.current.text!r} in quoted value")
        self.expect(MATCHING_QUOTE_CLOSE)
        return _compile_unescape(pieces)

    def parse_list(self) -> Compiled:
        """``listContainer: BRACKET_OPEN sequence? BRACKET_CLOSE``"""
        self.expect(BRACKET_OPEN)
        if self.peek() == BRACKET_CLOSE:
            self.advance()
            return _const([])
        items = self.parse_sequence(stop={BRACKET_CLOSE})
        self.expect(BRACKET_CLOSE)
        values = [item for item, _ in items]

        def run(ctx: Any) -> Any:
            return [value(ctx) for value in values]

        return run

    def parse_dict(self) -> Compiled:
        """``dictContainer: BRACE_OPEN (dictKeyValuePair (COMMA dictKeyValuePair)*)? BRACE_CLOSE``"""
        self.expect(BRACE_OPEN)
        if self.peek() == BRACE_CLOSE:
            self.advance()
            return _const({})
        pairs: List[Tuple[Any, Compiled]] = []
        while True:
            key = self.parse_dict_key()
            self.expect(COLON)
            pairs.append((key, self.parse_element()))
            if self.peek() == COMMA:
                self.advance()
                continue
            break
        self.expect(BRACE_CLOSE)

        def run(ctx: Any) -> Any:
            return {key: value(ctx) for key, value in pairs}

        return run

    _PRIMITIVE_TOKENS = frozenset(
        {ID, NULL, INT, FLOAT, BOOL, UNQUOTED_CHAR, COLON, ESC, WS, INTER_OPEN}
    )
    _DICT_KEY_TOKENS = frozenset({ID, NULL, INT, FLOAT, BOOL, UNQUOTED_CHAR, ESC, WS})

    def parse_dict_key(self) -> Any:
        """``dictKey`` -- like primitive but no COLON and no interpolation."""
        pieces: List[Tuple[int, Any]] = []
        while self.peek() in self._DICT_KEY_TOKENS:
            token = self.advance()
            pieces.append((token.type, token.text))
        if not pieces:
            raise self.error("empty dict key")
        # Trailing whitespace before the ':' belongs to the COLON token, but a
        # key like `a b` keeps its inner space.
        return _reduce_static(pieces)

    def parse_primitive(self) -> Compiled:
        """``primitive: (ID|NULL|INT|FLOAT|BOOL|UNQUOTED_CHAR|COLON|ESC|WS|interpolation)+``"""
        pieces: List[Tuple[int, Any]] = []
        while True:
            ttype = self.peek()
            if ttype not in self._PRIMITIVE_TOKENS:
                break
            if ttype == INTER_OPEN:
                pieces.append((INTER_OPEN, self.parse_interpolation()))
            else:
                token = self.advance()
                pieces.append((token.type, token.text))
        if not pieces:
            raise self.error(f"unexpected token {self.current.text!r}")
        if len(pieces) == 1:
            ttype, payload = pieces[0]
            if ttype == INTER_OPEN:
                return payload
            return _const(_scalar(ttype, payload))
        return _compile_unescape(pieces)

    def parse_sequence(self, stop: set) -> List[Tuple[Compiled, str]]:
        """``sequence: (element (COMMA element?)*) | (COMMA element?)+``

        Returns (compiled, spelling) pairs; the spelling feeds resolvers
        registered the legacy way, which receive their arguments as strings.
        """
        items: List[Tuple[Compiled, str]] = []
        previous_was_comma = True
        sequence_start = self.pos
        while True:
            ttype = self.peek()
            if ttype in stop or ttype == EOF:
                break
            if ttype == COMMA:
                self.advance()
                if previous_was_comma:
                    self._warn_missing_element(sequence_start)
                    items.append((_const(""), ""))
                previous_was_comma = True
                continue
            if not previous_was_comma:
                # Elements must be separated by a comma: `'a''b'` is a parse
                # error, not two elements.
                raise self.error(f"expected ',' before {self.current.text!r} in sequence")
            start = self.pos
            element = self.parse_element()
            spelling = "".join(token.text for token in self.tokens[start : self.pos])
            items.append((element, spelling))
            previous_was_comma = False
        if previous_was_comma and items:
            # Trailing comma.
            self._warn_missing_element(sequence_start)
            items.append((_const(""), ""))
        return items

    def _warn_missing_element(self, sequence_start: int) -> None:
        """omegaconf's deprecation warning for an omitted sequence element.

        ``${oc.env:VAR,}`` means "default to the empty string", and omegaconf
        has asked for `''` instead since 2.1. Dropping the warning would lose
        that signal, so it is emitted here -- but at *compile* time rather
        than per resolution, because the compiled closure is cached on the
        string. A given malformed interpolation therefore warns once per
        process instead of once per resolution; Python's default filter
        collapses omegaconf's repeats to one anyway.
        """
        text = "".join(token.text for token in self.tokens[sequence_start : self.pos])
        warnings.warn(
            f"In the sequence `{text}` some elements are missing: please replace "
            f"them with empty quoted strings. "
            f"See https://github.com/omry/omegaconf/issues/572 for details.",
            category=UserWarning,
            stacklevel=2,
        )

    # -- interpolations --------------------------------------------------
    def parse_interpolation(self) -> Compiled:
        """``interpolation: interpolationNode | interpolationResolver``

        The lexer already tells the two apart: it only emits ``COLON`` inside
        an interpolation when a resolver name ended, so the segment list before
        the COLON/INTER_CLOSE can be parsed once and classified after.
        """
        self.expect(INTER_OPEN)

        # The grammar puts every relative-reference dot in a single run right
        # after INTER_OPEN (`DOT*`), so consuming them here is unambiguous.
        leading_dots = 0
        while self.peek() == DOT:
            self.advance()
            leading_dots += 1

        segments: List[Tuple[str, Any]] = []  # (kind, payload)
        while True:
            ttype = self.peek()
            if ttype in (COLON, INTER_CLOSE, EOF):
                break
            if ttype == DOT:
                self.advance()
                segments.append(("sep", "."))
            elif ttype == BRACKET_OPEN:
                self.advance()
                segments.append(("open", "["))
            elif ttype == BRACKET_CLOSE:
                self.advance()
                segments.append(("close", "]"))
            elif ttype == ID:
                segments.append(("lit", self.advance().text))
            elif ttype == INTER_KEY:
                segments.append(("key", _unescape_key(self.advance().text)))
            elif ttype == INTER_OPEN:
                segments.append(("inter", self.parse_interpolation()))
            else:
                raise self.error(f"unexpected token {self.current.text!r} in interpolation")

        if self.peek() == COLON:
            self.advance()
            return self._finish_resolver(segments, leading_dots)
        self.expect(INTER_CLOSE)
        self._check_key_path(segments)
        return self._finish_node(segments, leading_dots)

    def _check_key_path(self, segments: List[Tuple[str, Any]]) -> None:
        """Enforce the shape of ``interpolationNode``'s key path.

        ``(configKey | '[' configKey ']') ('.' configKey | '[' configKey ']')*``
        -- so a bracket must open, hold exactly one key, and close. Without
        this check ``${ab[de}`` would be read as the path ``ab.de`` instead of
        rejected.
        """
        expect = "sep_or_open"  # a leading '[' is allowed: ${[a].b}
        first = True
        for kind, _ in segments:
            if kind == "open":
                if not first and expect not in ("sep_or_open",):
                    raise self.error("unexpected '[' in interpolation key")
                expect = "bracket_key"
                first = False
            elif kind == "close":
                first = False
                if expect != "close":
                    raise self.error("unexpected ']' in interpolation key")
                expect = "sep_or_open"
            elif kind == "sep":
                first = False
                if expect not in ("sep_or_open", "sep"):
                    raise self.error("unexpected '.' in interpolation key")
                expect = "key"
            else:  # lit | key | inter
                first = False
                if expect == "bracket_key":
                    expect = "close"
                elif expect in ("key", "sep_or_open"):
                    expect = "sep_or_open"
                else:
                    raise self.error("unexpected key segment in interpolation")
        if expect not in ("sep_or_open", "key"):
            raise self.error("unterminated '[' in interpolation key")

    def _finish_node(self, segments: List[Tuple[str, Any]], leading_dots: int) -> Compiled:
        """Compile ``${a.b[c].${d}}`` into a path lookup."""
        if not segments:
            if leading_dots == 0:
                raise self.error("empty interpolation")
            # `${.}` / `${..}` select the (grand)parent node itself
            dots = leading_dots

            def run_self(ctx: Any) -> Any:
                return ctx.node(dots, (), "." * dots)

            return run_self

        dots = leading_dots

        # Static fast path: no nested interpolation in the key, so the path is
        # known at compile time and resolution is one dict walk.
        if all(kind != "inter" for kind, _ in segments):
            parts = tuple(_segments_to_parts(segments))
            spelling = "." * dots + "".join(payload for _, payload in segments)
            self.refs.append(NodeRef(spelling, parts, dots, dynamic=False))

            def run_static(ctx: Any) -> Any:
                return ctx.node(dots, parts, spelling)

            return run_static

        frozen = tuple(segments)
        # Part of the path is itself an interpolation, so only the literal
        # segments are knowable now.
        known = tuple(_segments_to_parts([s for s in segments if s[0] != "inter"]))
        self.refs.append(
            NodeRef(
                "." * dots
                + "".join(
                    payload if kind != "inter" else "${...}" for kind, payload in segments
                ),
                known,
                dots,
                dynamic=True,
            )
        )
        self.dynamic = True

        def run_dynamic(ctx: Any) -> Any:
            resolved: List[Tuple[str, str]] = []
            extra_dots = 0
            seen_key = False
            for kind, payload in frozen:
                if kind == "inter":
                    text = str(to_plain(payload(ctx)))
                    if not seen_key:
                        # `${${ref}}` where ref is ".x" contributes its own
                        # relative dots, exactly as a literal `.` would.
                        stripped = text.lstrip(".")
                        extra_dots += len(text) - len(stripped)
                        text = stripped
                    if text:
                        resolved.append(("key", text))
                        seen_key = True
                else:
                    resolved.append((kind, payload))
                    if kind in ("lit", "key"):
                        seen_key = True
            parts = tuple(_segments_to_parts(resolved))
            total = dots + extra_dots
            spelling = "." * total + "".join(payload for _, payload in resolved)
            return ctx.node(total, parts, spelling)

        return run_dynamic

    def _finish_resolver(self, segments: List[Tuple[str, Any]], leading_dots: int) -> Compiled:
        """Compile ``${ns.fn:args}`` into a resolver call."""
        if leading_dots:
            raise self.error("a resolver name cannot start with '.'")
        name_parts: List[Any] = []
        for kind, payload in segments:
            if kind == "sep":
                continue
            if kind in ("lit", "key") or kind == "inter":
                name_parts.append(payload)
            else:
                raise self.error("invalid character in resolver name")

        args = self.parse_sequence(stop={BRACE_CLOSE})
        self.expect(BRACE_CLOSE)

        compiled_args = tuple(item for item, _ in args)
        arg_spellings = tuple(spelling for _, spelling in args)

        if all(isinstance(part, str) for part in name_parts):
            name = ".".join(name_parts)
            self.resolvers.append(name)

            def run_static(ctx: Any) -> Any:
                values = tuple(arg(ctx) for arg in compiled_args)
                return ctx.resolver(name, values, arg_spellings)

            return run_static

        frozen_name = tuple(name_parts)
        # The resolver name is computed, e.g. `${ns.${which}:x}`.
        self.dynamic = True

        def run_dynamic(ctx: Any) -> Any:
            pieces = []
            for part in frozen_name:
                if isinstance(part, str):
                    pieces.append(part)
                else:
                    value = to_plain(part(ctx))
                    if not isinstance(value, str):
                        raise InterpolationResolutionError(
                            "The name of a resolver must be a string, but the "
                            f"interpolation resolved to `{value}` which is of type "
                            f"{type(value)}"
                        )
                    pieces.append(value)
            values = tuple(arg(ctx) for arg in compiled_args)
            return ctx.resolver(".".join(pieces), values, arg_spellings)

        return run_dynamic


def _segments_to_parts(segments: Sequence[Tuple[str, Any]]) -> List[str]:
    """Flatten ``a.b[c]`` segment markers into ``["a", "b", "c"]``."""
    parts: List[str] = []
    for kind, payload in segments:
        if kind in ("lit", "key"):
            if "." in payload or "[" in payload:
                parts.extend(split_key(payload))
            else:
                parts.append(payload)
    return parts


# ---------------------------------------------------------------------------
# reductions
# ---------------------------------------------------------------------------
def _scalar(ttype: int, text: str) -> Any:
    """A single primitive token's Python value."""
    if ttype in (ID, UNQUOTED_CHAR, COLON):
        return text
    if ttype == NULL:
        return None
    if ttype == INT:
        return int(text)
    if ttype == FLOAT:
        return float(text)
    if ttype == BOOL:
        return text.lower() == "true"
    if ttype == ESC:
        return text[1::2]
    if ttype == WS:  # pragma: no cover - always absorbed by a neighbour
        return text
    raise GrammarParseError(f"unexpected token type {ttype}")


def _token_text(ttype: int, text: str, next_type: Optional[int]) -> str:
    """One token's contribution to a concatenation, unescaped as needed.

    Mirrors ``grammar_visitor._unescape``: which backslash runs collapse
    depends on what follows them.
    """
    if ttype == ESC_INTER:
        # `\\...\${` -- drop half the backslashes, keep the `${` literal
        return text[-(len(text) // 2 + 1) :]
    if ttype == ESC:
        return text[1::2]
    if ttype == TOP_ESC and next_type == INTER_OPEN:
        return text[1::2]
    if ttype == QUOTED_ESC and (next_type is None or next_type == INTER_OPEN):
        return text[1::2]
    return text


def _reduce_static(pieces: List[Tuple[int, Any]]) -> Any:
    """Concatenate an interpolation-free token run."""
    if len(pieces) == 1:
        return _scalar(pieces[0][0], pieces[0][1])
    out = []
    for index, (ttype, text) in enumerate(pieces):
        next_type = pieces[index + 1][0] if index + 1 < len(pieces) else None
        out.append(_token_text(ttype, text, next_type))
    return "".join(out)


def _compile_unescape(pieces: List[Tuple[int, Any]]) -> Compiled:
    """Compile a concatenation of literals and interpolations."""
    # Pre-compute every literal's unescaped text; only interpolations are left
    # to evaluate at resolution time.
    plan: List[Tuple[bool, Any]] = []
    for index, (ttype, payload) in enumerate(pieces):
        if ttype == INTER_OPEN:
            plan.append((True, payload))
            continue
        next_type = pieces[index + 1][0] if index + 1 < len(pieces) else None
        plan.append((False, _token_text(ttype, payload, next_type)))

    if not any(dynamic for dynamic, _ in plan):
        return _const("".join(text for _, text in plan))

    # Collapse neighbouring literals so the hot loop is as short as possible.
    collapsed: List[Tuple[bool, Any]] = []
    for dynamic, payload in plan:
        if not dynamic and collapsed and not collapsed[-1][0]:
            collapsed[-1] = (False, collapsed[-1][1] + payload)
        else:
            collapsed.append((dynamic, payload))
    frozen = tuple(collapsed)

    def run(ctx: Any) -> Any:
        out = []
        for dynamic, payload in frozen:
            if dynamic:
                out.append(str(to_plain(payload(ctx))))
            else:
                out.append(payload)
        return "".join(out)

    return run


# ---------------------------------------------------------------------------
# public entry points (cached)
# ---------------------------------------------------------------------------
def _finish(parser: _Parser, compiled: Compiled) -> Compiled:
    """Attach the compile-time analysis to the closure.

    A function attribute, so calling the closure is exactly as fast as before.
    """
    analysis = Analysis(
        node_refs=tuple(parser.refs),
        resolvers=tuple(parser.resolvers),
        dynamic=parser.dynamic,
    )
    with contextlib.suppress(AttributeError):  # a non-function callable
        compiled.hf_analysis = analysis  # type: ignore[attr-defined]
    return compiled


@_cache.memoize("grammar_text")
def compile_text(value: str) -> Compiled:
    """Compile a top-level config value (``configValue`` rule)."""
    parser = _Parser(_tokenize(value, DEFAULT_MODE), value)
    return _finish(parser, parser.parse_config_value())


@_cache.memoize("grammar_element")
def compile_single_element(value: str) -> Compiled:
    """Compile a single element (``singleElement`` rule, ``VALUE_MODE``)."""
    parser = _Parser(_tokenize(value, VALUE_MODE), value)
    return _finish(parser, parser.parse_single_element())


def parse(
    value: str,
    parser_rule: str = "configValue",
    lexer_mode: str = "DEFAULT_MODE",
) -> Compiled:
    """Parse ``value``, returning its compiled form.

    Signature-compatible with ``omegaconf.grammar_parser.parse``, which returns
    an ANTLR ``ParserRuleContext``. Here the compiled closure plays that role:
    it is an opaque handle on a parsed interpolation that
    :meth:`Container.resolve_parse_tree` evaluates against a config. Use
    :func:`analyze` to inspect the structure.
    """
    if parser_rule == "singleElement":
        return compile_single_element(value)
    if parser_rule != "configValue":
        raise GrammarParseError(f"Unknown parser rule: {parser_rule}")
    if lexer_mode == "VALUE_MODE":
        return compile_single_element(value)
    return compile_text(value)


def analyze(value: str) -> Analysis:
    """What ``value`` references, without resolving it.

    Recovers what a parse tree would be walked for::

        >>> analyze("${db.host}:${db.port}/${oc.env:NAME,prod}").referenced_keys()
        ('db.host', 'db.port')
        >>> analyze("${db.host}").resolvers
        ()

    Shares the compile cache, so asking is free after the first time.
    """
    if not isinstance(value, str) or "${" not in value:
        return _EMPTY_ANALYSIS
    return analysis_of(compile_text(value))
