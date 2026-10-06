"""Exception types *and* messages, compared with omegaconf.

omegaconf appends a standard trailer to every config error::

    Key 'nope' not in 'Schema'
        full_key: nope
        object_type=Schema

Code in the wild matches on that text and reads ``exc.full_key``, so both are
checked here rather than just the exception class.
"""

from __future__ import annotations

import dataclasses

import pytest

from conftest import requires_omegaconf
from hydra_fast import OmegaConf
from hydra_fast.errors import (
    ConfigAttributeError,
    ConfigKeyError,
    MissingMandatoryValue,
    ReadonlyConfigError,
    ValidationError,
)


@dataclasses.dataclass
class Schema:
    n: int = 1
    inner: dict = dataclasses.field(default_factory=dict)


def _struct_dict(module):
    cfg = module.OmegaConf.create({"a": 1})
    module.OmegaConf.set_struct(cfg, True)
    return cfg


def _readonly(module):
    cfg = module.OmegaConf.create({"a": 1})
    module.OmegaConf.set_readonly(cfg, True)
    cfg.a = 2


# (label, operation) -- the operation is expected to raise
OPS = [
    ("schema attr miss", lambda m: m.OmegaConf.structured(Schema).nope),
    ("schema item miss", lambda m: m.OmegaConf.structured(Schema)["nope"]),
    ("schema attr set", lambda m: setattr(m.OmegaConf.structured(Schema), "nope", 1)),
    ("validation", lambda m: setattr(m.OmegaConf.structured(Schema), "n", "x")),
    ("plain struct miss", lambda m: _struct_dict(m).nope),
    ("missing value", lambda m: m.OmegaConf.create({"a": "???"}).a),
    ("interpolation key", lambda m: m.OmegaConf.create({"b": "${nope}"}).b),
    ("unknown resolver", lambda m: m.OmegaConf.create({"b": "${no.such:1}"}).b),
    ("readonly", _readonly),
    ("pop missing", lambda m: m.OmegaConf.create({"a": 1}).pop("zzz")),
    (
        "nested full_key",
        lambda m: m.OmegaConf.create(
            {"inner": {"deep": {}}}, flags={"struct": True}
        ).inner.deep.nope,
    ),
    (
        "merge unknown key",
        lambda m: m.OmegaConf.merge(
            m.OmegaConf.structured(Schema), m.OmegaConf.create({"zz": 1})
        ),
    ),
    (
        "merge bad type",
        lambda m: m.OmegaConf.merge(
            m.OmegaConf.structured(Schema), m.OmegaConf.create({"n": "xx"})
        ),
    ),
]


@requires_omegaconf
@pytest.mark.parametrize("label,op", OPS, ids=[label for label, _ in OPS])
def test_message_matches_omegaconf(label, op):
    import omegaconf

    import hydra_fast

    rendered = []
    for module in (omegaconf, hydra_fast):
        try:
            op(module)
        except Exception as exc:  # noqa: BLE001
            rendered.append(f"{type(exc).__name__}: {exc}")
        else:
            rendered.append("DID NOT RAISE")
    assert rendered[0] == rendered[1]


# ---------------------------------------------------------------------------
# the attributes, which are read programmatically
# ---------------------------------------------------------------------------
def test_full_key_and_object_type_attributes():
    cfg = OmegaConf.create({"inner": {"deep": {}}}, flags={"struct": True})
    with pytest.raises(ConfigAttributeError) as info:
        cfg.inner.deep.nope
    assert info.value.full_key == "inner.deep.nope"
    assert info.value.object_type == "dict"


def test_object_type_names_the_schema():
    with pytest.raises(ValidationError) as info:
        OmegaConf.structured(Schema).n = "x"
    assert info.value.full_key == "n"
    assert info.value.object_type == "Schema"


def test_schema_backed_struct_message_names_the_class():
    with pytest.raises(ConfigAttributeError, match="not in 'Schema'"):
        OmegaConf.structured(Schema).nope


def test_plain_struct_message_says_struct():
    cfg = OmegaConf.create({"a": 1})
    OmegaConf.set_struct(cfg, True)
    with pytest.raises(ConfigAttributeError, match="is not in struct"):
        cfg.nope


def test_readonly_reports_the_key():
    cfg = OmegaConf.create({"a": 1})
    OmegaConf.set_readonly(cfg, True)
    with pytest.raises(ReadonlyConfigError) as info:
        cfg.a = 2
    assert info.value.full_key == "a"


def test_missing_reports_location():
    with pytest.raises(MissingMandatoryValue) as info:
        OmegaConf.create({"deep": {"a": "???"}}).deep.a
    assert info.value.full_key == "deep.a"


def test_config_key_error_str_is_not_repr():
    """KeyError.__str__ reprs its argument; omegaconf's does not."""
    with pytest.raises(ConfigKeyError) as info:
        OmegaConf.create({"a": 1}).pop("zzz")
    assert str(info.value).startswith("Key not found: 'zzz'")


# ---------------------------------------------------------------------------
# ANTLR-shaped messages for malformed overrides
#
# Hydra surfaces ANTLR's text verbatim, so these are part of the observable
# behaviour and hydra's own suite asserts on them. The expectations here were
# captured from real ANTLR; `bench/oracle_errors.py` re-measures the whole
# corpus against it.
# ---------------------------------------------------------------------------
ANTLR_MESSAGES = [
    # the lexer found no rule -- and reports how far it got, not one character
    ("listContainer", r"['a\', 'b']", "token recognition error at: '']'"),
    ("dictContainer", r"{a: 'a\', b: 'b'}", "token recognition error at: ''}'"),
    ("override", "key=[1,2,3]'", "token recognition error at: '''"),
    # a specific token was required
    ("override", "", "mismatched input '<EOF>' expecting "
                     "{EQUAL, '~', '+', '@', KEY_SPECIAL, DOT_PATH, ID}"),
    ("dictContainer", "{a:1", "mismatched input '<EOF>' expecting {COMMA, BRACE_CLOSE}"),
    # insertion recovery: what is here could follow the token that is missing
    ("dictContainer", "a b", "missing BRACE_OPEN at 'a'"),
    ("override", "key", "missing EQUAL at '<EOF>'"),
    # deletion recovery, including at an inlined loop-back
    ("listContainer", "[1,2", "extraneous input '<EOF>' expecting {COMMA, BRACKET_CLOSE}"),
    ("override", "key=[1,2]]", "extraneous input ']' expecting <EOF>"),
    # no alternative of the decision was viable; the span is the prediction's
    ("override", "key=[[1,[2,]]]", "no viable alternative at input '[[1,[2,]'"),
    ("package", ":", "no viable alternative at input ':'"),
]


@pytest.mark.parametrize("rule,text,expected", ANTLR_MESSAGES)
def test_antlr_message_text(rule, text, expected):
    from hydra_fast.core.override_parser.overrides_parser import OverridesParser
    from hydra_fast.errors import OverrideParseException

    with pytest.raises(OverrideParseException) as info:
        OverridesParser().parse_rule(text, rule)
    assert expected in str(info.value)


def test_lexing_is_lazy_so_a_rule_that_stops_early_never_rejects_the_rest():
    """ANTLR pulls tokens on demand, so `!` is never scanned here."""
    from hydra_fast.core.override_parser.overrides_parser import OverridesParser

    assert OverridesParser().parse_rule("key=!", "key").key_or_group == "key"


def test_empty_package_is_viable_only_where_something_may_follow():
    """`package` has an empty alternative, gated on its FOLLOW set."""
    from hydra_fast.core.override_parser.overrides_parser import OverridesParser
    from hydra_fast.errors import OverrideParseException

    parser = OverridesParser()
    # `=` can follow a package, so the empty alternative applies
    assert parser.parse_rule("key@=value", "override").package == ""
    assert parser.parse_rule("", "package") == ""
    # `:` can follow a package nowhere
    with pytest.raises(OverrideParseException):
        parser.parse_rule(":", "package")
