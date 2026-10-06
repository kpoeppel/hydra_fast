"""Override grammar: parsed shape compared against Hydra's ANTLR parser."""

from __future__ import annotations

import pytest

from conftest import requires_hydra
from hydra_fast.errors import OverrideParseException
from hydra_fast.grammar.override import OverrideType, ValueType, parse_override

pytestmark = requires_hydra

try:
    from hydra.core.override_parser import overrides_parser as hydra_parser
except ImportError:  # pragma: no cover - guarded by the marker above
    hydra_parser = None

LINES = [
    # plain values and their types
    "key=value",
    "key=1",
    "key=1.5",
    "key=true",
    "key=False",
    "key=null",
    "key=",
    "key=2.5e-4",
    "key=1_000_000",
    "key=inf",
    "key=-10",
    "key=a-b_c",
    "key=/a/b/c",
    "key=100%",
    "key=a.b.c",
    # dotted keys
    "a.b.c=1",
    "db.driver=mysql",
    # prefixes
    "+key=value",
    "++key=value",
    "~key",
    "~key=value",
    "+key.sub=1",
    "++a.b.c=1",
    # groups and packages
    "db=mysql",
    "db@pkg=mysql",
    "hydra/launcher=basic",
    "db@_global_=mysql",
    "db@=mysql",
    "group=null",
    # quoting and escaping
    "key='hello world'",
    'key="hello world"',
    "key='a,b'",
    "key='${a.b}'",
    r"key='it\'s'",
    'key="back\\\\slash"',
    'key="a\\\\b"',
    'key="abc\\\\"',
    "key=''",
    # containers
    "key=[]",
    "key=[1,2,3]",
    "key=[a,b,[1,2]]",
    "key={}",
    "key={a:1,b:2}",
    "key={a:{b:[1,2]}}",
    "key=[1, 2, 3]",
    # interpolations pass through as strings
    "key=${a.b}",
    "key=${oc.env:USER,me}",
    "key=pre${a}post",
    # sweeps
    "key=a,b,c",
    "key=1,2,3",
    "key=choice(a,b)",
    "key=choice(1,2,3)",
    "key=range(1,5)",
    "key=range(0,10,2)",
    "key=range(1.0,2.0,0.5)",
    "key=interval(0,1)",
    "group=glob(*)",
    "group=glob(*,exclude=foo)",
    "key=tag(log,choice(a,b))",
    "key=sort(choice(3,1,2))",
    "key=sort(choice(1,2,3),reverse=true)",
    "key=int(1.5)",
    "key=float(1)",
    "key=str(1)",
    "key=bool(true)",
    # escapes in values
    r"key=a\,b",
    r"key=a\ b",
    r"key=\[notalist\]",
]


def _shape(override):
    return (
        override.type.name,
        override.key_or_group,
        override.package,
        override.value_type.name if override.value_type else None,
        repr(override.value()),
    )


@pytest.mark.parametrize("line", LINES)
def test_matches_hydra(line):
    parser = hydra_parser.OverridesParser.create()
    try:
        expected = _shape(parser.parse_override(line))
        expected_error = None
    except Exception:  # noqa: BLE001
        expected, expected_error = None, True

    try:
        got = _shape(parse_override(line))
        got_error = None
    except Exception:  # noqa: BLE001
        got, got_error = None, True

    if expected_error or got_error:
        assert expected_error and got_error, f"hydra={expected!r} fast={got!r}"
        return
    assert expected == got


# hydra 1.3 rejects bracket indexing in override keys; hydra 1.4-dev accepts
# it, and so does hydra-fast. Documented in docs/compatibility.md.
@pytest.mark.parametrize("line", ["a.b[0]=1", "a[0].b=1"])
def test_value_path_superset(line):
    override = parse_override(line)
    assert override.is_value_path
    assert override.key_or_group == line.split("=")[0]


def test_parse_cache_returns_independent_objects():
    """Callers mutate overrides, so the cache must not hand out one instance."""
    first = parse_override("key=[1,2]")
    second = parse_override("key=[1,2]")
    assert first is not second
    first.value().append(3)
    assert second.value() == [1, 2]


def test_config_loader_is_not_cached_across_calls():
    sentinel = object()
    first = parse_override("key=1", config_loader=sentinel)
    second = parse_override("key=1")
    assert first.config_loader is sentinel
    assert second.config_loader is None


def test_bad_override_raises():
    with pytest.raises(OverrideParseException):
        parse_override("no_equals_sign")


def test_types():
    assert parse_override("+k=1").type is OverrideType.ADD
    assert parse_override("++k=1").type is OverrideType.FORCE_ADD
    assert parse_override("~k").type is OverrideType.DEL
    assert parse_override("k=1").type is OverrideType.CHANGE
    assert parse_override("k=a,b").value_type is ValueType.SIMPLE_CHOICE_SWEEP
    assert parse_override("k=glob(*)").value_type is ValueType.GLOB_CHOICE_SWEEP
