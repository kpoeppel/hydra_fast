r"""A hand-written replacement for Hydra's ANTLR override grammar.

Transcribed from ``OverrideLexer.g4`` / ``OverrideParser.g4``. Parsing is
memoized on the input string, which matters a great deal for staged sweeps:
those pass the whole sibling config down as several hundred
``++key=value`` overrides per point, and Hydra builds a fresh ANTLR lexer and
parser for every single one.

Accepted forms::

    key=value      +key=value     ++key=value    ~key      ~key=value
    group=option   group@pkg=option            key@pkg=option
    key=[a,b]      key={a:1,b:2}  key='quoted'  key=a,b,c
    key=choice(a,b)  key=range(1,5)  key=interval(0,1)  group=glob(*)
    key=tag(log,choice(a,b))  key=sort(choice(3,1,2))  key=int(1.5)
"""

from __future__ import annotations

import fnmatch
import re
import warnings
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple, Union

from .. import _cache
from ..errors import HydraException, OverrideParseException
from .antlr_errors import (
    TokenVocabulary,
    extraneous_input,
    mismatched_input,
    missing_token,
    no_viable_alternative,
    token_recognition_error,
)

__all__ = [
    "ChoiceSweep",
    "Glob",
    "IntervalSweep",
    "Key",
    "ListExtensionOverrideValue",
    "Override",
    "OverrideType",
    "QuotedString",
    "Quote",
    "RangeSweep",
    "Sweep",
    "Transformer",
    "ValueType",
    "create_functions",
    "default_functions",
    "parse_override",
    "parse_overrides",
]


# ---------------------------------------------------------------------------
# value model (field-compatible with hydra.core.override_parser.types)
# ---------------------------------------------------------------------------
# Backslash runs that precede a quote: the only ones that are escapes.
_ESC_QUOTED = {
    "'": re.compile(r"(\\)+'"),
    '"': re.compile(r'(\\)+"'),
}


class Quote(Enum):
    single = 0
    double = 1


@dataclass(frozen=True)
class QuotedString:
    text: str
    quote: Quote

    def with_quotes(self) -> str:
        r"""Render back to source, re-escaping so it round-trips.

        The subtlety is backslashes that *precede* a quote (including the
        closing one): those have to be doubled, or re-parsing would read them
        as escaping the quote. Transcribed from hydra's implementation --
        ``_unescape_quoted_string`` is the inverse.
        """
        quote_char = "'" if self.quote == Quote.single else '"'
        escaped_quote = f"\\{quote_char}"
        text = self.text + quote_char  # append the closing quote to scan it too
        pattern = _ESC_QUOTED[quote_char]

        match = pattern.search(text) if "\\" in self.text else None
        if match is None:
            # Nothing but quotes to escape.
            return f"{quote_char}{self.text.replace(quote_char, escaped_quote)}{quote_char}"

        tokens = []
        while match is not None:
            start, stop = match.span()
            tokens.append(text[:start])
            # Double the backslash run: its length is the match minus the quote.
            tokens.append("\\" * ((stop - start - 1) * 2))
            if stop < len(text):
                # Not the closing quote, so keep it; the closing one is re-added
                # at the end.
                tokens.append(quote_char)
            text = text[stop:]
            match = pattern.search(text)

        if len(text) > 1:
            tokens.append(text[:-1])  # the rest, without the closing quote

        inner = "".join(tokens).replace(quote_char, escaped_quote)
        return f"{quote_char}{inner}{quote_char}"


@dataclass
class Sweep:
    tags: set = field(default_factory=set)


@dataclass
class ChoiceSweep(Sweep):
    list: List[Any] = field(default_factory=list)
    simple_form: bool = False
    shuffle: bool = False


@dataclass
class FloatRange:
    start: float
    stop: float
    step: float

    def __post_init__(self) -> None:
        self.start = float(self.start)
        self.stop = float(self.stop)
        self.step = float(self.step)
        if self.step == 0:
            raise HydraException("step cannot be 0")

    def __iter__(self) -> Any:
        return FloatRange(self.start, self.stop, self.step)

    def __next__(self) -> float:
        if not hasattr(self, "_current"):
            self._current = self.start  # type: ignore[attr-defined]
        value = self._current  # type: ignore[attr-defined]
        if self.step > 0:
            if value >= self.stop:
                raise StopIteration
        else:
            if value <= self.stop:
                raise StopIteration
        self._current = value + self.step  # type: ignore[attr-defined]
        return value


@dataclass
class RangeSweep(Sweep):
    start: Optional[Union[int, float]] = None
    stop: Optional[Union[int, float]] = None
    step: Union[int, float] = 1
    shuffle: bool = False

    def range(self) -> Any:
        assert self.start is not None and self.stop is not None
        if (
            isinstance(self.start, int)
            and isinstance(self.stop, int)
            and isinstance(self.step, int)
        ):
            return range(self.start, self.stop, self.step)
        return FloatRange(self.start, self.stop, self.step)


@dataclass
class IntervalSweep(Sweep):
    start: Optional[float] = None
    end: Optional[float] = None

    def __eq__(self, other: Any) -> Any:
        if not isinstance(other, IntervalSweep):
            return NotImplemented
        if self.tags == other.tags:
            return self.start == other.start and self.end == other.end
        return False


class OverrideType(Enum):
    CHANGE = 1
    ADD = 2
    FORCE_ADD = 3
    DEL = 4
    EXTEND_LIST = 5


class ValueType(Enum):
    ELEMENT = 1
    CHOICE_SWEEP = 2
    GLOB_CHOICE_SWEEP = 3
    SIMPLE_CHOICE_SWEEP = 4
    RANGE_SWEEP = 5
    INTERVAL_SWEEP = 6


@dataclass
class Key:
    key_or_group: str
    package: Optional[str] = None
    is_value_path: bool = False


@dataclass
class Glob:
    include: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)

    def filter(self, names: List[str]) -> List[str]:
        def match(name: str, globs: List[str]) -> bool:
            return any(fnmatch.fnmatch(name, pattern) for pattern in globs)

        return [
            name
            for name in names
            if match(name, self.include) and not match(name, self.exclude)
        ]


@dataclass
class ListExtensionOverrideValue:
    values: List[Any]


class Transformer:
    @staticmethod
    def identity(x: Any) -> Any:
        return x

    @staticmethod
    def str(x: Any) -> str:
        return Override._get_value_element_as_str(x)

    @staticmethod
    def encode(x: Any) -> Any:
        if isinstance(x, (str, int, float, bool)):
            return x
        return Transformer.str(x)


@dataclass
class Override:
    type: OverrideType
    key_or_group: str
    value_type: Optional[ValueType]
    _value: Any
    package: Optional[str] = None
    input_line: Optional[str] = None
    config_loader: Any = None
    is_value_path: bool = False

    def validate(self) -> None:
        """Warn about forms deprecated at the declared ``version_base``.

        Called once per parsed override, as Hydra does it.
        """
        from ..version import base_at_least

        if self.package is not None and "_name_" in self.package and not base_at_least("1.2"):
            url = "https://hydra.cc/docs/1.2/upgrades/1.0_to_1.1/changes_to_package_header"
            warnings.warn(
                f"In override {self.input_line}: _name_ keyword is deprecated in "
                f"packages, see {url}\n",
                UserWarning,
                stacklevel=2,
            )

    # -- predicates ------------------------------------------------------
    def is_delete(self) -> bool:
        return self.type == OverrideType.DEL

    def is_add(self) -> bool:
        return self.type == OverrideType.ADD

    def is_force_add(self) -> bool:
        return self.type == OverrideType.FORCE_ADD

    def is_list_extend(self) -> bool:
        return self.type == OverrideType.EXTEND_LIST

    def is_sweep_override(self) -> bool:
        return self.value_type is not None and self.value_type != ValueType.ELEMENT

    def is_choice_sweep(self) -> bool:
        return self.value_type in (
            ValueType.SIMPLE_CHOICE_SWEEP,
            ValueType.CHOICE_SWEEP,
            ValueType.GLOB_CHOICE_SWEEP,
        )

    def is_discrete_sweep(self) -> bool:
        return self.is_choice_sweep() or self.is_range_sweep()

    def is_range_sweep(self) -> bool:
        return self.value_type == ValueType.RANGE_SWEEP

    def is_interval_sweep(self) -> bool:
        return self.value_type == ValueType.INTERVAL_SWEEP

    def is_hydra_override(self) -> bool:
        key = self.key_or_group
        return key.startswith("hydra.") or key.startswith("hydra/")

    # -- values ----------------------------------------------------------
    @staticmethod
    def _convert_value(value: Any) -> Any:
        if isinstance(value, list):
            return [Override._convert_value(item) for item in value]
        if isinstance(value, dict):
            return {
                Override._convert_value(key): Override._convert_value(item)
                for key, item in value.items()
            }
        if isinstance(value, QuotedString):
            return value.text
        return value

    def value(self) -> Any:
        if isinstance(self._value, Sweep):
            return self._value
        return Override._convert_value(self._value)

    def sweep_iterator(
        self, transformer: Callable[[Any], Any] = Transformer.identity
    ) -> Iterator:
        """Enumerate a discrete sweep, honouring ``shuffle``."""
        import random

        if self.value_type not in (
            ValueType.CHOICE_SWEEP,
            ValueType.SIMPLE_CHOICE_SWEEP,
            ValueType.GLOB_CHOICE_SWEEP,
            ValueType.RANGE_SWEEP,
        ):
            raise HydraException(
                f"Can only enumerate CHOICE and RANGE sweeps, type is {self.value_type}"
            )

        values: Any
        if isinstance(self._value, list):
            values = self._value
        elif isinstance(self._value, ChoiceSweep):
            if self._value.shuffle:
                values = list(self._value.list)
                random.shuffle(values)
            else:
                values = self._value.list
        elif isinstance(self._value, RangeSweep):
            if self._value.shuffle:
                values = list(self._value.range())
                random.shuffle(values)
                values = iter(values)
            else:
                values = self._value.range()
        elif isinstance(self._value, Glob):
            if self.config_loader is None:
                raise HydraException("ConfigLoader is not set")
            from ..core.object_type import ObjectType

            options = self.config_loader.get_group_options(
                self.key_or_group, results_filter=ObjectType.CONFIG
            )
            return iter(self._value.filter(options))
        else:
            raise HydraException(f"Cannot enumerate a {type(self._value).__name__} sweep")

        return map(transformer, values)

    def sweep_string_iterator(self) -> Iterator[str]:
        return self.sweep_iterator(transformer=Transformer.str)  # type: ignore[return-value]

    # -- string forms ----------------------------------------------------
    def get_key_element(self) -> str:
        prefix = {
            OverrideType.DEL: "~",
            OverrideType.ADD: "+",
            OverrideType.FORCE_ADD: "++",
            OverrideType.EXTEND_LIST: "+",
        }.get(self.type, "")
        key = self.key_or_group
        if self.package is not None:
            key = f"{key}@{self.package}"
        if self.type == OverrideType.EXTEND_LIST:
            key = f"{key}"
        return f"{prefix}{key}"

    @staticmethod
    def _get_value_element_as_str(value: Any, space_after_sep: bool = False) -> str:
        """Render a parsed value back to override syntax.

        Strings are re-escaped, so the result can be fed back to the parser;
        dict keys are rendered without ``space_after_sep`` (only the value
        side gets the space), matching hydra.
        """
        from .utils import escape_special_characters

        comma = ", " if space_after_sep else ","
        colon = ": " if space_after_sep else ":"
        if value is None:
            return "null"
        if isinstance(value, QuotedString):
            return value.with_quotes()
        if isinstance(value, list):
            inner = comma.join(
                Override._get_value_element_as_str(item, space_after_sep=space_after_sep)
                for item in value
            )
            return f"[{inner}]"
        if isinstance(value, dict):
            items = [
                f"{Override._get_value_element_as_str(key)}{colon}"
                f"{Override._get_value_element_as_str(item, space_after_sep=space_after_sep)}"
                for key, item in value.items()
            ]
            return "{" + comma.join(items) + "}"
        if isinstance(value, str):
            return escape_special_characters(value)
        if isinstance(value, (int, bool, float)):
            return str(value)
        from .._structured import is_structured

        if is_structured(value):
            from ..omegaconf_api import OmegaConf

            return Override._get_value_element_as_str(
                OmegaConf.to_container(OmegaConf.structured(value))
            )
        raise HydraException(f"Cannot render {type(value).__name__} as an override value")

    def get_value_string(self) -> str:
        """The value as it would be spelled on the command line."""
        return self._get_value_element_as_str(self._value)

    def get_value_element_as_str(self, space_after_sep: bool = False) -> str:
        if isinstance(self._value, Sweep):
            raise HydraException("Cannot convert sweep to str")
        return self._get_value_element_as_str(self._value, space_after_sep)

    def __repr__(self) -> str:
        return f"Override({self.input_line!r})"


# ---------------------------------------------------------------------------
# lexer
# ---------------------------------------------------------------------------
(
    EQUAL,
    TILDE,
    PLUS,
    AT,
    COLON,
    SLASH,
    VALUE_PATH,
    ID,
    KEY_SPECIAL,
    DOT_PATH,
    POPEN,
    PCLOSE,
    COMMA,
    BRACKET_OPEN,
    BRACKET_CLOSE,
    BRACE_OPEN,
    BRACE_CLOSE,
    FLOAT,
    INT,
    BOOL,
    NULL,
    UNQUOTED_CHAR,
    ESC,
    WS,
    QUOTED_VALUE,
    INTERPOLATION,
    EOF,
) = range(27)

# ANTLR display names and ordering, transcribed from OverrideLexer.g4. The
# index is the token's declaration position, which is what ANTLR sorts
# expected-token sets by; a bare-literal rule displays as its literal.
# VALUE_PATH sits last because it is a hydra-1.4 token hydra-fast accepts as a
# superset -- keeping it out of the way leaves 1.3's set orderings intact.
_VOCAB = TokenVocabulary(
    {
        EQUAL: (1, "EQUAL"),
        TILDE: (2, "'~'"),
        PLUS: (3, "'+'"),
        AT: (4, "'@'"),
        COLON: (5, "':'"),
        SLASH: (6, "'/'"),
        KEY_SPECIAL: (7, "KEY_SPECIAL"),
        DOT_PATH: (8, "DOT_PATH"),
        POPEN: (9, "POPEN"),
        COMMA: (10, "COMMA"),
        PCLOSE: (11, "PCLOSE"),
        BRACKET_OPEN: (12, "BRACKET_OPEN"),
        BRACKET_CLOSE: (13, "BRACKET_CLOSE"),
        BRACE_OPEN: (14, "BRACE_OPEN"),
        BRACE_CLOSE: (15, "BRACE_CLOSE"),
        FLOAT: (16, "FLOAT"),
        INT: (17, "INT"),
        BOOL: (18, "BOOL"),
        NULL: (19, "NULL"),
        UNQUOTED_CHAR: (20, "UNQUOTED_CHAR"),
        ID: (21, "ID"),
        ESC: (22, "ESC"),
        WS: (23, "WS"),
        QUOTED_VALUE: (24, "QUOTED_VALUE"),
        INTERPOLATION: (25, "INTERPOLATION"),
        VALUE_PATH: (26, "VALUE_PATH"),
    }
)

# FIRST sets, named after the grammar rules they start, so error messages can
# say what was expected.
_FIRST_PRIMITIVE = frozenset(
    {COLON, FLOAT, INT, BOOL, NULL, UNQUOTED_CHAR, ID, ESC, WS, QUOTED_VALUE, INTERPOLATION}
)
_FIRST_DICT_KEY = frozenset({FLOAT, INT, BOOL, NULL, UNQUOTED_CHAR, ID, ESC, WS})
_FIRST_ELEMENT = _FIRST_PRIMITIVE | {BRACKET_OPEN, BRACE_OPEN}

# FOLLOW set for `package`, whose empty alternative is viable only where
# something that can follow the rule comes next. This is the union over every
# call site -- `key: packageOrGroup (AT package)?` and `packageOrGroup:
# package | ...` -- because ANTLR's SLL prediction uses the rule's global
# FOLLOW rather than the current call site's. That is what makes the fragment
# `package` accept `@` and `=value` (both can follow a package somewhere) but
# reject `:` (which can follow it nowhere).
_FOLLOW_PACKAGE = frozenset({AT, EQUAL, EOF})

# FIRST(override). `_RENDER` is what hydra 1.3's message lists: VALUE_PATH is
# a later addition that hydra-fast's lexer produces but 1.3 has no token for,
# so it is accepted without appearing in the expectation.
_FIRST_OVERRIDE = frozenset({EQUAL, TILDE, PLUS, AT, KEY_SPECIAL, DOT_PATH, ID, VALUE_PATH})
_FIRST_OVERRIDE_RENDER = frozenset({EQUAL, TILDE, PLUS, AT, KEY_SPECIAL, DOT_PATH, ID})


_KEY_MODE = "KEY"
_VALUE_MODE = "VALUE"

_INT_UNSIGNED = r"(?:0|[1-9](?:_?[0-9])*)"
_POINT_FLOAT = rf"(?:{_INT_UNSIGNED}\.(?![0-9])|{_INT_UNSIGNED}?\.[0-9](?:_?[0-9])*)"
_EXPONENT_FLOAT = rf"(?:(?:{_INT_UNSIGNED}|{_POINT_FLOAT})[eE][+-]?[0-9](?:_?[0-9])*)"
_FLOAT_RE = rf"[+-]?(?:{_EXPONENT_FLOAT}|{_POINT_FLOAT}|[Ii][Nn][Ff]|[Nn][Aa][Nn])"
_ESC_RE = r"(?:\\\\|\\\(|\\\)|\\\[|\\\]|\\\{|\\\}|\\:|\\=|\\,|\\ |\\\t)+"

_KEY_CHAR = r"[a-zA-Z0-9_\-$]"
_KEY_ESCAPE = r"\\[.\[\]=]"
_KEY_ESCAPED = rf"(?:{_KEY_CHAR}*{_KEY_ESCAPE}(?:{_KEY_CHAR}|{_KEY_ESCAPE})*)"
_KEY_SPECIAL = r"[a-zA-Z_$][a-zA-Z0-9_\-$]*"
_DOT_PATH = rf"(?:(?:{_KEY_SPECIAL}|{_INT_UNSIGNED})(?:\.(?:{_KEY_SPECIAL}|{_INT_UNSIGNED}))+)"
_KEY_BRACKET = rf"(?:\[(?:{_KEY_CHAR}|{_KEY_ESCAPE})+\])"
_KEY_SEGMENT = rf"(?:{_KEY_SPECIAL}|{_INT_UNSIGNED}|{_KEY_ESCAPED})"
_VALUE_PATH = (
    rf"(?:(?:{_KEY_SPECIAL}|{_DOT_PATH}){_KEY_BRACKET}"
    rf"|(?:{_KEY_SPECIAL}|{_INT_UNSIGNED}|{_DOT_PATH})\.{_KEY_ESCAPED}"
    rf"|{_KEY_ESCAPED})"
    rf"(?:{_KEY_BRACKET}|\.{_KEY_SEGMENT})*"
)

_QUOTED = r"\"(?:(?:\\\\)*|(?:.)*?[^\\](?:\\\\)*)\"" r"|'(?:(?:\\\\)*|(?:.)*?[^\\](?:\\\\)*)'"

_OVERRIDE_MODES: Dict[str, List[Tuple[str, int, Optional[str]]]] = {
    _KEY_MODE: [
        (r"=[ \t]*", EQUAL, _VALUE_MODE),
        (r"~", TILDE, None),
        (r"\+", PLUS, None),
        (r"@", AT, None),
        (r":", COLON, None),
        (r"/", SLASH, None),
        (_VALUE_PATH, VALUE_PATH, None),
        (r"[a-zA-Z_][a-zA-Z0-9_\-]*", ID, None),
        (_KEY_SPECIAL, KEY_SPECIAL, None),
        (_DOT_PATH, DOT_PATH, None),
    ],
    _VALUE_MODE: [
        (r"[ \t]*\([ \t]*", POPEN, None),
        (r"[ \t]*,[ \t]*", COMMA, None),
        (r"[ \t]*\)", PCLOSE, None),
        (r"\[[ \t]*", BRACKET_OPEN, None),
        (r"[ \t]*\]", BRACKET_CLOSE, None),
        (r"\{[ \t]*", BRACE_OPEN, None),
        (r"[ \t]*\}", BRACE_CLOSE, None),
        (r"[ \t]*:[ \t]*", COLON, None),
        (r"[ \t]*=[ \t]*", EQUAL, None),
        (_FLOAT_RE, FLOAT, None),
        (rf"[+-]?{_INT_UNSIGNED}", INT, None),
        (r"[Tt][Rr][Uu][Ee]|[Ff][Aa][Ll][Ss][Ee]", BOOL, None),
        (r"[Nn][Uu][Ll][Ll]", NULL, None),
        (r"[/\-\\+.$%*@?|]", UNQUOTED_CHAR, None),
        (r"[a-zA-Z_][a-zA-Z0-9_\-]*", ID, None),
        (_ESC_RE, ESC, None),
        (r"[ \t]+", WS, None),
        (_QUOTED, QUOTED_VALUE, None),
        (r"\$\{[^}]+\}", INTERPOLATION, None),
    ],
}

_OV_COMPILED = {
    mode: [(re.compile(pattern), ttype, switch) for pattern, ttype, switch in rules]
    for mode, rules in _OVERRIDE_MODES.items()
}


class _OvToken:
    __slots__ = ("type", "text")

    def __init__(self, ttype: int, text: str) -> None:
        self.type = ttype
        self.text = text

    def __repr__(self) -> str:  # pragma: no cover
        return f"OvToken({self.type}, {self.text!r})"


#: Rules that can consume input without completing: each opens a region that
#: runs to a closing delimiter.
_UNCLOSED_OPENERS = ("'", '"', "${")


def _failed_span(text: str, pos: int) -> str:
    """The text ANTLR's lexer consumed before giving up at ``pos``.

    ANTLR reports from the token start through however far the ATN advanced,
    which is *not* always one character. Only the delimited rules above can
    advance without completing, and they do so only when their closing
    delimiter is missing -- in which case the scan reaches the end of input.
    Anything else fails on the first character.

    This is what makes ``['a\\', 'b']`` report ``']`` rather than ``'``: the
    escaped quote lets ``QUOTED_VALUE`` consume ``'a\\', '``, leaving a final
    quote that opens a string nothing closes.
    """
    if text.startswith(_UNCLOSED_OPENERS, pos):
        return text[pos:]
    return text[pos : pos + 1]


class _TokenStream:
    """Tokens produced on demand, the way ANTLR's lexer produces them.

    Laziness here is semantics rather than speed. ANTLR pulls one token at a
    time as the parser asks for it, so a rule that stops early never scans
    the rest of the input -- which is why ``parse_rule("key=!", "key")``
    *succeeds* upstream: the ``key`` rule matches ``key``, stops, and ``!``
    (which no lexer rule can start) is never looked at. An eager tokenizer
    reports a lexer error for input the parser was never going to read.

    Only the ``override`` rule runs to EOF, so only it sees the whole string.
    """

    __slots__ = ("source", "mode", "scan", "tokens", "done")

    def __init__(self, source: str, mode: str) -> None:
        self.source = source
        self.mode = mode
        self.scan = 0  # how far into source the lexer has run
        self.tokens: List[_OvToken] = []
        self.done = False

    def _pull(self) -> None:
        """Append one token, or the EOF sentinel at the end of input."""
        text, pos = self.source, self.scan
        if pos >= len(text):
            self.tokens.append(_OvToken(EOF, ""))
            self.done = True
            return
        best_len = 0
        best: Any = None
        for regex, ttype, switch in _OV_COMPILED[self.mode]:
            match = regex.match(text, pos)
            if match is None:
                continue
            size = match.end() - pos
            if size > best_len:
                best_len = size
                best = (match, ttype, switch)
        if best is None or best_len == 0:
            raise OverrideParseException(
                text, token_recognition_error(_failed_span(text, pos))
            )
        won, won_type, switch = best
        self.tokens.append(_OvToken(won_type, won.group(0)))
        self.scan = won.end()
        if switch is not None:
            self.mode = switch

    def at(self, index: int) -> _OvToken:
        """The token at ``index``, lexing as far as needed; EOF past the end."""
        while not self.done and len(self.tokens) <= index:
            self._pull()
        if index >= len(self.tokens):
            return self.tokens[-1]  # the EOF sentinel
        return self.tokens[index]

    def text_at(self, index: int) -> str:
        """The token's source text; EOF renders as ANTLR spells it."""
        token = self.at(index)
        return "<EOF>" if token.type == EOF else token.text

    def span(self, start: int, end: int) -> str:
        """Source text from token ``start`` through ``end``, inclusive."""
        self.at(end)
        return "".join(token.text for token in self.tokens[start : end + 1])


def _tokenize_override(text: str, mode: str) -> _TokenStream:
    """A lazy token stream over ``text``, starting in lexer mode ``mode``."""
    return _TokenStream(text, mode)


# ---------------------------------------------------------------------------
# grammar functions
# ---------------------------------------------------------------------------
from . import override_functions as _fn  # noqa: E402

_BUILTIN_FUNCTIONS: Dict[str, Callable[..., Any]] = {
    "int": _fn.cast_int,
    "str": _fn.cast_str,
    "bool": _fn.cast_bool,
    "float": _fn.cast_float,
    "json_str": _fn.cast_json_str,
    "choice": _fn.choice,
    "range": _fn.range_,
    "interval": _fn.interval,
    "tag": _fn.tag,
    "sort": _fn.sort,
    "shuffle": _fn.shuffle,
    "glob": _fn.glob,
    "extend_list": _fn.extend_list,
}

_DEFAULT_REGISTRY: Any = None


def create_functions() -> Any:
    """A registry holding hydra's built-in override functions."""
    from .functions import Functions

    registry = Functions()
    for name, func in _BUILTIN_FUNCTIONS.items():
        registry.register(name=name, func=func)
    return registry


def default_functions() -> Any:
    """The shared built-in registry, built once."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = create_functions()
    return _DEFAULT_REGISTRY


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
class _Mismatch(Exception):
    """A specific token or token set was required and not found.

    Internal: carries the position and expectation so an enclosing rule
    decision can turn it into "no viable alternative" the way ANTLR does.

    ``after`` is the set of tokens that could legally follow the expected one
    at this point -- ANTLR's ``atn.nextTokens(next)``. It decides whether the
    expected token can be treated as merely *missing*; see :meth:`report`.

    ``unwanted`` marks a failure at a ``(...)*`` loop-back whose body is
    *inlined* in the rule. ANTLR runs ``sync()`` there and reports the
    offending token as unwanted outright -- "extraneous input" -- whereas a
    loop whose body is a subrule returns first and then fails a ``match``,
    giving "mismatched input". ``listContainer`` and ``function`` inline their
    bodies; ``dictContainer`` wraps its in ``dictKeyValuePair``, which is why
    the two report differently on the same kind of truncated input.
    """

    __slots__ = ("index", "expected", "with_eof", "after", "unwanted")

    def __init__(
        self,
        index: int,
        expected: Any,
        with_eof: bool = False,
        after: Any = None,
        unwanted: bool = False,
    ) -> None:
        super().__init__("mismatch")
        self.index = index
        self.expected = expected
        self.with_eof = with_eof
        self.after = after
        self.unwanted = unwanted


class _OvParser:
    __slots__ = (
        "tokens",
        "pos",
        "source",
        "functions",
        "_committed",
        "_predicting_from",
        "_dry_run",
    )

    def __init__(self, tokens: _TokenStream, source: str, functions: Any = None) -> None:
        self.tokens = tokens
        self.pos = 0
        self.source = source
        self._committed = False
        # While set, the grammar is walked without *evaluating* anything --
        # see `parse_simple_choice_sweep`.
        self._dry_run = False
        # Token index the innermost *active* prediction started at, if any.
        # A span reported by a failing prediction runs from here, so an error
        # inside a function argument names the whole value being predicted
        # rather than just the argument.
        self._predicting_from: Optional[int] = None
        # None means "the built-in set", resolved lazily so the common path
        # does not build a registry per parse.
        self.functions = functions

    def peek(self, offset: int = 0) -> int:
        return self.tokens.at(self.pos + offset).type

    def advance(self) -> _OvToken:
        token = self.tokens.at(self.pos)
        self.pos += 1
        return token

    # -- error reporting -------------------------------------------------
    #
    # Message shapes and the choice between them mirror ANTLR, since hydra
    # surfaces ANTLR's text verbatim. See grammar/antlr_errors.py.
    def _text_at(self, index: int) -> str:
        """The offending token's text; EOF renders as ``<EOF>``."""
        return self.tokens.text_at(index)

    def _span(self, start: int, end: int) -> str:
        """Source text from token ``start`` through ``end`` inclusive."""
        return self.tokens.span(start, end)

    def mismatch(
        self,
        expected: Any,
        with_eof: bool = False,
        after: Any = None,
        unwanted: bool = False,
    ) -> _Mismatch:
        """Raise-ready: a required token was not found at the current position.

        ``after`` names what could follow ``expected`` here, which is what
        lets :meth:`report` decide the expected token is merely missing.
        """
        return _Mismatch(self.pos, expected, with_eof, after, unwanted)

    def fail(self, message: str) -> OverrideParseException:
        return OverrideParseException(self.source, message)

    def expect(self, ttype: int, after: Any = None) -> _OvToken:
        token = self.tokens.at(self.pos)
        if token.type != ttype:
            raise self.mismatch(ttype, after=after)
        self.pos += 1
        return token

    def expect_one_of(self, expected: Any) -> _OvToken:
        """Consume a token from ``expected``, or report the whole set."""
        if self.peek() not in expected:
            raise self.mismatch(expected)
        return self.advance()

    def decide(self, start: int, first: Any, exc: _Mismatch) -> Exception:
        """Turn an inner mismatch into what ANTLR would emit for a decision.

        Nothing consumed yet (the failure is at the decision's own first
        token): the decision could not be entered at all, so re-raise a
        mismatch widened to the union of the alternatives' FIRST sets, and let
        an *enclosing* decision -- or the top level -- render it. Rendering
        here would stop an outer rule from reporting "no viable alternative",
        which is what ANTLR does once a token has been consumed.

        Something was consumed: an alternative was entered and then failed, so
        report "no viable alternative" with the span from the decision's start
        through the offending token.
        """
        if exc.index <= start:
            return _Mismatch(exc.index, first, exc.with_eof, exc.after, exc.unwanted)
        # Probing one token past the offending one is what ANTLR's error
        # strategy does, and it runs the lexer that much further -- so a lexer
        # error just beyond surfaces here instead, as it does upstream.
        self.peek(exc.index - self.pos + 1)
        return self.fail(no_viable_alternative(self._span(start, exc.index)))

    def report(self, exc: _Mismatch) -> Exception:
        """Render a mismatch that no decision caught, as ANTLR would.

        ANTLR's ``recoverInline`` tries **single-token deletion** before giving
        up: if the token *after* the offending one is what was expected, the
        offending one is extraneous and parsing can continue without it. Only
        if that fails is it an input mismatch.

        The order has an observable side effect. Probing the following token
        runs the lexer one token further, so a *lexer* error just past the
        offending token surfaces here instead of the parser error -- which is
        why ``['a\\', 'b']`` reports the unterminated quote rather than a
        mismatch at ``b``. Reproducing that means doing the probe first, as
        ANTLR does, rather than treating it as a special case.
        """
        expected = (
            exc.expected if isinstance(exc.expected, (set, frozenset)) else {exc.expected}
        )
        rendered = _VOCAB.render(expected, with_eof=exc.with_eof)
        offending = self._text_at(exc.index)

        following = self.peek(exc.index - self.pos + 1)
        if exc.unwanted or following in expected or (exc.with_eof and following == EOF):
            return self.fail(extraneous_input(offending, rendered))

        # Single-token insertion: if what is here could legally follow the
        # token that is missing, say it is missing rather than wrong.
        if exc.after is not None and self.tokens.at(exc.index).type in exc.after:
            return self.fail(missing_token(rendered, offending))

        return self.fail(mismatched_input(offending, rendered))

    # -- override --------------------------------------------------------
    def parse_override(self) -> Override:
        """``override: key EQUAL value? | TILDE key (EQUAL value?)? |
        PLUS PLUS? key EQUAL value?``
        """
        if self.peek() not in _FIRST_OVERRIDE:
            # Nothing consumed yet, so this is the rule's own entry failing.
            raise self.fail(
                mismatched_input(
                    self._text_at(self.pos), _VOCAB.render(_FIRST_OVERRIDE_RENDER)
                )
            )

        kind = OverrideType.CHANGE
        if self.peek() == TILDE:
            self.advance()
            kind = OverrideType.DEL
        elif self.peek() == PLUS:
            self.advance()
            if self.peek() == PLUS:
                self.advance()
                kind = OverrideType.FORCE_ADD
            else:
                kind = OverrideType.ADD

        key = self.parse_key()

        if self.peek() != EQUAL:
            if kind != OverrideType.DEL:
                # `value?` is optional and EOF follows, so ANTLR recovers by
                # imagining the EQUAL rather than reporting a mismatch.
                if self.peek() == EOF:
                    raise self.fail(
                        missing_token(_VOCAB.display(EQUAL), self._text_at(self.pos))
                    )
                raise self.report(self.mismatch(EQUAL))
            self.expect(EOF)
            return Override(
                type=kind,
                key_or_group=key.key_or_group,
                package=key.package,
                value_type=None,
                _value=None,
                input_line=self.source,
                is_value_path=key.is_value_path,
            )

        self.advance()  # EQUAL
        if self.peek() == EOF:
            value: Any = ""
            value_type = ValueType.ELEMENT
        elif self.peek() not in _FIRST_ELEMENT:
            # `value?` cannot start here, so it is absent and EOF is what the
            # rule wants next; ANTLR deletes the offending token and says so.
            raise self.fail(
                extraneous_input(
                    self._text_at(self.pos),
                    _VOCAB.render(_FIRST_ELEMENT, with_eof=True),
                )
            )
        else:
            value, value_type = self.parse_value()
        if self.peek() != EOF:
            # EOF is the whole expectation; it renders from the flag, not the
            # token map, so the expected set itself is empty.
            raise self.report(self.mismatch(frozenset(), with_eof=True))

        if isinstance(value, ListExtensionOverrideValue):
            kind = OverrideType.EXTEND_LIST
            value = value.values

        return Override(
            type=kind,
            key_or_group=key.key_or_group,
            package=key.package,
            value_type=value_type,
            _value=value,
            input_line=self.source,
            is_value_path=key.is_value_path,
        )

    def parse_key(self) -> Key:
        """``key: packageOrGroup (AT package)? | VALUE_PATH``"""
        if self.peek() == VALUE_PATH:
            return Key(key_or_group=self.advance().text, is_value_path=True)

        key_or_group = self.parse_package_or_group()
        package: Optional[str] = None
        if self.peek() == AT:
            self.advance()
            # `key@` with nothing after it selects the _global_ package, so the
            # empty alternative is in play; `=` or end of input may follow.
            package = self.parse_package(_FOLLOW_PACKAGE)
        return Key(key_or_group=key_or_group, package=package)

    def parse_package_or_group(self) -> str:
        """``packageOrGroup: package | ID (SLASH ID)+``

        The two alternatives are distinct: a slash-separated group path may
        only join plain ``ID``s, so ``$foo/bar`` is not a group -- ``$`` is
        allowed in a package name but not in a group segment.
        """
        if self.peek() == ID and self.peek(1) == SLASH:
            pieces = [self.advance().text]
            while self.peek() == SLASH:
                pieces.append(self.advance().text)
                pieces.append(self.expect(ID, after=frozenset({SLASH, AT, EQUAL, EOF})).text)
            return "".join(pieces)

        if self.peek() in (ID, KEY_SPECIAL, DOT_PATH, VALUE_PATH, INT):
            return self.advance().text
        # Falls through to `package`, whose empty alternative makes an empty
        # key legal: `=value` overrides the empty key and `~` deletes it.
        # Degenerate, but hydra accepts it. `@` may follow here as well.
        return self.parse_package(_FOLLOW_PACKAGE)

    def parse_package(self, follow: Any = _FOLLOW_PACKAGE) -> str:
        """``package: ( | ID | KEY_SPECIAL | DOT_PATH)`` -- may be empty.

        The empty alternative is only *viable* where something that can follow
        the rule comes next, which is how ANTLR decides it: prediction for an
        epsilon alternative looks at the rule's FOLLOW set. So ``key@=value``
        parses (``=`` follows a key) while the fragment ``package`` on ``:``
        does not -- ``:`` neither starts a package nor follows one, which
        ANTLR reports as having no viable alternative.
        """
        pieces: List[str] = []
        while self.peek() in (ID, KEY_SPECIAL, DOT_PATH, INT):
            pieces.append(self.advance().text)
        if not pieces and self.peek() not in follow:
            raise self.fail(no_viable_alternative(self._text_at(self.pos)))
        return "".join(pieces)

    def parse_value_only(self) -> Any:
        """The ``value`` rule, returning just the value (not its ValueType)."""
        value, _ = self.parse_value()
        return value

    def parse_simple_choice_sweep(self) -> Any:
        """``simpleChoiceSweep: element (COMMA element)+``

        The `+` is checked before anything is evaluated. hydra builds a parse
        tree and evaluates it afterwards in a visitor, so a sweep with no comma
        fails on the missing COMMA even when its single element would also have
        failed to evaluate: `choice()` reports "mismatched input '<EOF>'
        expecting COMMA", not "empty choice is not legal".

        Reproducing that means walking the first element without evaluating it,
        to find where it ends, then re-walking for real once the comma is
        known to be there. Only this rule needs it, and only one element is
        walked twice.
        """
        start = self.pos
        self._dry_run = True
        try:
            self.parse_element()
            after_first = self.pos
        finally:
            self._dry_run = False
            self.pos = start

        if self.tokens.at(after_first).type != COMMA:
            raise self.report(_Mismatch(after_first, COMMA))

        items = [self.parse_element()]
        while self.peek() == COMMA:
            self.advance()
            items.append(self.parse_element())
        return ChoiceSweep(list=items, simple_form=True)

    # -- values ----------------------------------------------------------
    def parse_value(self) -> Tuple[Any, ValueType]:
        """``value: element | simpleChoiceSweep``

        *This* is the decision that needs unbounded lookahead: telling an
        element from a simple choice sweep means scanning the whole element to
        see whether a top-level comma follows it. So a failure anywhere in
        that scan is a prediction failure, and ANTLR reports it as "no viable
        alternative" spanning from the value's first token to the offending
        one -- which is why ``key=[[1,[2,]]]`` names the whole prefix rather
        than just the inner list.
        """
        start = self.pos
        self._committed = False
        outer = self._predicting_from
        if outer is None:
            self._predicting_from = start
        try:
            return self._parse_value(start)
        except _Mismatch as exc:
            if self._committed:
                raise
            raise self.decide(start, _FIRST_ELEMENT, exc) from None
        finally:
            self._predicting_from = outer

    def _parse_value(self, start: int) -> Tuple[Any, ValueType]:
        first = self.parse_element()  # the prediction scan; wrapped by caller
        if self.peek() != COMMA:
            if isinstance(first, Glob):
                # Hydra keeps the Glob itself as the value; the group options
                # it expands to are only known once a config loader is around.
                return first, ValueType.GLOB_CHOICE_SWEEP
            if isinstance(first, ChoiceSweep):
                return first, ValueType.CHOICE_SWEEP
            if isinstance(first, RangeSweep):
                return first, ValueType.RANGE_SWEEP
            if isinstance(first, IntervalSweep):
                return first, ValueType.INTERVAL_SWEEP
            return first, ValueType.ELEMENT

        # A top-level comma settles the decision: this is a choice sweep, and
        # prediction is done. Failures from here on are reported as themselves
        # rather than as the decision having no viable alternative, which is
        # why `key=1,2,3,` names the element it wanted instead of the whole
        # value. `_committed` carries that past the caller's wrapper.
        items = [first]
        while self.peek() == COMMA:
            self.advance()
            self._committed = True
            self._predicting_from = None  # the decision is settled
            items.append(self.parse_element())
        return (
            ChoiceSweep(list=items, simple_form=True),
            ValueType.SIMPLE_CHOICE_SWEEP,
        )

    def parse_element(self) -> Any:
        """``element: primitive | listContainer | dictContainer | function``

        This decision is LL(1) -- the opening token picks the alternative on
        its own -- so prediction either succeeds immediately or fails on the
        first token. A failure *inside* a chosen alternative is therefore
        reported as itself, not as "no viable alternative"; only a failure at
        the decision's own first token means no alternative was viable, and
        that widens the expectation to the union of their FIRST sets.
        """
        start = self.pos
        ttype = self.peek()
        try:
            if ttype == BRACKET_OPEN:
                return self.parse_list()
            if ttype == BRACE_OPEN:
                return self.parse_dict()
            if ttype == ID and self.peek(1) == POPEN:
                return self.parse_function()
            return self.parse_primitive()
        except _Mismatch as exc:
            if exc.index <= start:
                raise _Mismatch(exc.index, _FIRST_ELEMENT, exc.with_eof, exc.after) from None
            raise

    def parse_list(self) -> List[Any]:
        self.expect(BRACKET_OPEN, after=_FIRST_ELEMENT | {BRACKET_CLOSE})
        if self.peek() == BRACKET_CLOSE:
            self.advance()
            return []
        if self.peek() not in _FIRST_ELEMENT:
            raise self.mismatch(_FIRST_ELEMENT | {BRACKET_CLOSE})
        items = [self.parse_element()]
        while self.peek() == COMMA:
            self.advance()
            items.append(self.parse_element())
        if self.peek() != BRACKET_CLOSE:
            raise self.mismatch(frozenset({COMMA, BRACKET_CLOSE}), unwanted=True)
        self.advance()
        return items

    def parse_dict(self) -> Dict[Any, Any]:
        self.expect(BRACE_OPEN, after=_FIRST_DICT_KEY | {BRACE_CLOSE})
        if self.peek() == BRACE_CLOSE:
            self.advance()
            return {}
        out: Dict[Any, Any] = {}
        while True:
            key = self.parse_dict_key(frozenset({BRACE_CLOSE}))
            self.expect(COLON, after=_FIRST_ELEMENT)
            out[key] = self.parse_element()
            if self.peek() == COMMA:
                self.advance()
                continue
            break
        if self.peek() != BRACE_CLOSE:
            raise self.mismatch(frozenset({COMMA, BRACE_CLOSE}))
        self.advance()
        return out

    def parse_function(self) -> Any:
        """``function: ID POPEN (argName? element (COMMA argName? element)*)? PCLOSE``"""
        start = self.pos
        name = self.expect(ID, after=frozenset({POPEN})).text
        self.expect(POPEN, after=_FIRST_ELEMENT | {PCLOSE})
        args: List[Any] = []
        kwargs: Dict[str, Any] = {}
        if self.peek() != PCLOSE and self.peek() not in _FIRST_ELEMENT:
            raise self.mismatch(_FIRST_ELEMENT | {PCLOSE})
        if self.peek() != PCLOSE:
            in_kwargs = False
            while True:
                # `argName? element` is only an ambiguous decision when the
                # argument starts with an ID -- that is the one token that
                # could begin either `argName` or an `element`. ANTLR then
                # predicts past the argument, so a truncated call fails in
                # prediction rather than at the closing paren.
                arg_start = self.pos
                predicting = self.peek() == ID
                if self.peek() == ID and self.peek(1) == EQUAL:
                    arg_name = self.advance().text
                    self.advance()  # EQUAL
                    kwargs[arg_name] = self.parse_element()
                    in_kwargs = True
                else:
                    if in_kwargs:
                        raise HydraException("positional argument follows keyword argument")
                    args.append(self.parse_element())
                if predicting and self.peek() not in (COMMA, PCLOSE):
                    span_from = (
                        self._predicting_from
                        if self._predicting_from is not None
                        else arg_start
                    )
                    raise self.fail(no_viable_alternative(self._span(span_from, self.pos)))
                if self.peek() == COMMA:
                    self.advance()
                    continue
                break
        if self.peek() != PCLOSE:
            raise self.mismatch(frozenset({COMMA, PCLOSE}), unwanted=True)
        self.advance()

        from .functions import FunctionCall

        if self._dry_run:
            # Walking only: hydra's parser builds the call's tree and leaves
            # evaluation to the visitor, so a dry run must not evaluate either.
            return None

        registry = self.functions if self.functions is not None else default_functions()
        try:
            return registry.eval(FunctionCall(name=name, args=args, kwargs=kwargs))
        except Exception as exc:
            # hydra reports the *source text* of the call, so an error names
            # the thing the user wrote: "ValueError while evaluating
            # 'int(nan)': cannot convert float NaN to integer".
            spelling = self.tokens.span(start, self.pos - 1)
            raise HydraException(
                f"{type(exc).__name__} while evaluating '{spelling}': {exc}"
            ) from exc

    _PRIMITIVE_TOKENS = frozenset(
        {ID, NULL, INT, FLOAT, BOOL, INTERPOLATION, UNQUOTED_CHAR, COLON, ESC, WS}
    )
    _DICT_KEY_TOKENS = frozenset({ID, NULL, INT, FLOAT, BOOL, UNQUOTED_CHAR, ESC, WS})

    def parse_primitive(self) -> Any:
        """``primitive: QUOTED_VALUE | (ID|NULL|INT|FLOAT|BOOL|INTERPOLATION|...)+``"""
        if self.peek() == QUOTED_VALUE:
            text = self.advance().text
            quote = Quote.single if text[0] == "'" else Quote.double
            return QuotedString(text=_unescape_quoted_string(text), quote=quote)

        pieces: List[Tuple[int, str]] = []
        while self.peek() in self._PRIMITIVE_TOKENS:
            token = self.advance()
            pieces.append((token.type, token.text))
        if not pieces:
            raise self.mismatch(_FIRST_PRIMITIVE)
        if all(ttype == WS for ttype, _ in pieces) and not self._dry_run:
            raise HydraException("Trying to parse a primitive that is all whitespaces")
        # Leading and trailing whitespace is not part of the value.
        while pieces and pieces[0][0] == WS:
            pieces.pop(0)
        while pieces and pieces[-1][0] == WS:
            pieces.pop()
        return _reduce_primitive(pieces)

    def parse_dict_key(self, extra: Any = frozenset()) -> Any:
        """``dictKey``: no COLON, no INTERPOLATION and -- unlike ``primitive``
        -- no QUOTED_VALUE. ``{'a': 1}`` is a parse error in hydra."""
        pieces: List[Tuple[int, str]] = []
        while self.peek() in self._DICT_KEY_TOKENS:
            token = self.advance()
            pieces.append((token.type, token.text))
        if not pieces:
            raise self.mismatch(_FIRST_DICT_KEY | extra)
        if all(ttype == WS for ttype, _ in pieces) and not self._dry_run:
            raise HydraException("Trying to parse a primitive that is all whitespaces")
        while pieces and pieces[-1][0] == WS:
            pieces.pop()
        return _reduce_primitive(pieces)


def _unescape_quoted_string(text: str) -> str:
    r"""Unescape a quoted override value, given the enclosing quotes.

    Only a backslash run that *precedes a quote* is an escape; everything else
    passes through untouched. The closing quote is kept during the scan so a
    trailing run counts as preceding a quote:

        "abc\"def"    -> abc"def
        "abc\\\"def"  -> abc\"def
        "abc\\"       -> abc\
        "a\\b"        -> a\\b      (mid-string run: not an escape)
    """
    quote_char = text[0]
    text = text[1:]  # drop the opening quote, keep the closing one
    pattern = _ESC_QUOTED[quote_char]
    match = pattern.search(text)
    if match is None:
        return text[:-1]

    tokens: List[str] = []
    while match is not None:
        start, stop = match.span()
        tokens.append(text[:start])
        # halve the backslash run and keep the quote it escaped
        tokens.append(text[start + 1 : stop : 2])
        text = text[stop:]
        match = pattern.search(text)
    if len(text) > 1:
        tokens.append(text[:-1])
    return "".join(tokens)


def _reduce_primitive(pieces: List[Tuple[int, str]]) -> Any:
    """A single typed token keeps its type; a run becomes a string."""
    if len(pieces) == 1:
        ttype, text = pieces[0]
        if ttype == INT:
            return int(text)
        if ttype == FLOAT:
            return float(text)
        if ttype == BOOL:
            return text.lower() == "true"
        if ttype == NULL:
            return None
        if ttype == ESC:
            return text[1::2]
        return text
    out = []
    for ttype, text in pieces:
        out.append(text[1::2] if ttype == ESC else text)
    return "".join(out)


# ---------------------------------------------------------------------------
# public entry points (cached)
# ---------------------------------------------------------------------------
@_cache.memoize("override_parse")
def _parse_override_cached(line: str) -> Override:
    parser = _OvParser(_tokenize_override(line, _KEY_MODE), line)
    return parser.parse_override()


def parse_override(line: str, config_loader: Any = None) -> Override:
    """Parse one override line. Cached; the result is copied before return.

    Hydra mutates ``config_loader`` on the object and sweepers mutate sweep
    values, so handing out the cached instance would let one call corrupt the
    next.
    """
    cached = _parse_override_cached(line)
    result = Override(
        type=cached.type,
        key_or_group=cached.key_or_group,
        value_type=cached.value_type,
        _value=_copy_value(cached._value),
        package=cached.package,
        input_line=cached.input_line,
        config_loader=config_loader,
        is_value_path=cached.is_value_path,
    )
    # Outside the cache: the deprecation check depends on the version base,
    # which the caller can change between parses. It short-circuits on
    # `package is None`, which is the overwhelmingly common case.
    result.validate()
    return result


def _copy_value(value: Any) -> Any:
    """Shallow-copy only what callers mutate; primitives pass through."""
    if isinstance(value, (str, int, float, bool, type(None), QuotedString)):
        return value
    import copy

    return copy.deepcopy(value)


def parse_overrides(overrides: Sequence[str], config_loader: Any = None) -> List[Override]:
    return [parse_override(line, config_loader) for line in overrides]


@_cache.memoize("override_rule_value")
def parse_value_rule(line: str) -> Any:
    """Parse just the value part of an override (the ``value`` rule)."""
    parser = _OvParser(_tokenize_override(line, _VALUE_MODE), line)
    value, _ = parser.parse_value()
    parser.expect(EOF)
    return value


@_cache.memoize("override_rule_element")
def parse_element_rule(line: str) -> Any:
    """Parse a single element (the ``element`` rule)."""
    parser = _OvParser(_tokenize_override(line, _VALUE_MODE), line)
    value = parser.parse_element()
    parser.expect(EOF)
    return value
