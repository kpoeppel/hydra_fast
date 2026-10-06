"""Interpolation grammar: syntax coverage, plus a diff against omegaconf."""

from __future__ import annotations

import pytest

from conftest import REFERENCE_OMEGACONF as omegaconf
from conftest import requires_omegaconf
from hydra_fast import OmegaConf
from hydra_fast.errors import (
    GrammarParseError,
    InterpolationKeyError,
    InterpolationResolutionError,
    MissingMandatoryValue,
    UnsupportedInterpolationType,
)

pytestmark = requires_omegaconf


def _echo(*args):
    return list(args) if len(args) != 1 else args[0]


@pytest.fixture(autouse=True)
def _resolvers():
    OmegaConf.register_new_resolver("hf.echo", _echo, replace=True)
    if omegaconf is not None:
        omegaconf.OmegaConf.register_new_resolver("hf.echo", _echo, replace=True)


# Each case: (config data, key to read)
CASES = [
    # node references
    ({"a": 1, "b": "${a}"}, "b"),
    ({"a": {"b": {"c": 7}}, "x": "${a.b.c}"}, "x"),
    ({"a": [10, 20, 30], "x": "${a[1]}"}, "x"),
    ({"a": {"b": 1}, "x": "${a[b]}"}, "x"),
    ({"a": {"b": 1}, "x": "${[a].b}"}, "x"),
    # relative references
    ({"a": {"b": 1, "c": "${.b}"}}, "a.c"),
    ({"a": {"b": {"c": "${..d}"}, "d": 5}}, "a.b.c"),
    ({"top": 1, "a": {"b": {"c": "${...top}"}}}, "a.b.c"),
    # typing is preserved through a lone interpolation
    ({"a": 1, "b": "${a}"}, "b"),
    ({"a": 1.5, "b": "${a}"}, "b"),
    ({"a": True, "b": "${a}"}, "b"),
    ({"a": None, "b": "${a}"}, "b"),
    ({"a": [1, 2], "b": "${a}"}, "b"),
    ({"a": {"k": 1}, "b": "${a}"}, "b"),
    # concatenation always stringifies
    ({"a": 1, "b": "x${a}y"}, "b"),
    ({"a": 1, "b": "${a}${a}"}, "b"),
    ({"a": 1, "b": "a=${a}, b=${a}"}, "b"),
    # escapes
    ({"b": r"\${a}"}, "b"),
    ({"a": 1, "b": "\\\\${a}"}, "b"),
    ({"b": "a\\b"}, "b"),
    ({"b": "100%"}, "b"),
    ({"b": "a$b"}, "b"),
    ({"b": "$"}, "b"),
    ({"b": "}"}, "b"),
    ({"b": "{}"}, "b"),
    # nested interpolation inside the key
    ({"a": {"x": 5}, "k": "x", "b": "${a.${k}}"}, "b"),
    ({"ref": "a", "a": 3, "b": "${${ref}}"}, "b"),
    # builtin resolvers
    ({"b": "${oc.env:HF_TEST_UNSET,fallback}"}, "b"),
    ({"b": "${oc.env:HF_TEST_UNSET,'a,b'}"}, "b"),
    ({"a": 2, "b": "${oc.select:a,9}"}, "b"),
    ({"b": "${oc.select:nope,9}"}, "b"),
    ({"a": {"x": 1, "y": 2}, "b": "${oc.dict.keys:a}"}, "b"),
    ({"a": {"x": 1, "y": 2}, "b": "${oc.dict.values:a}"}, "b"),
    ({"b": "${oc.decode:'[1,2,3]'}"}, "b"),
    ({"b": "${oc.decode:'{a: 1}'}"}, "b"),
    ({"b": "${oc.decode:null}"}, "b"),
    # relative keys in resolvers
    ({"s": {"env": {"A": 1, "B": 2}, "k": "${oc.dict.keys:.env}"}}, "s.k"),
    ({"s": {"env": {"A": 1}, "v": "${oc.dict.values:.env}"}}, "s.v"),
    ({"s": {"x": 5, "y": "${oc.select:.x,0}"}}, "s.y"),
    # resolver argument forms
    ({"b": "${hf.echo:[1,2,[3,4]]}"}, "b"),
    ({"b": "${hf.echo:{a:1,b:two}}"}, "b"),
    ({"b": "${hf.echo:'hello world'}"}, "b"),
    ({"b": '${hf.echo:"hello world"}'}, "b"),
    ({"a": 4, "b": "${hf.echo:'v=${a}'}"}, "b"),
    ({"b": "${hf.echo:}"}, "b"),
    ({"b": "${hf.echo:null}"}, "b"),
    ({"b": "${hf.echo:true,False}"}, "b"),
    ({"b": "${hf.echo:1_000}"}, "b"),
    ({"b": "${hf.echo:2.5e-4}"}, "b"),
    ({"b": "${hf.echo:-1}"}, "b"),
    ({"b": "${hf.echo:a/b-c.d}"}, "b"),
    ({"b": "${hf.echo:x\\,y}"}, "b"),
    ({"b": "${hf.echo:'a${hf.echo:b}c'}"}, "b"),
    ({"a": 1, "b": "pre${hf.echo:${a}}post"}, "b"),
    ({"n": "echo", "b": "${hf.${n}:7}"}, "b"),
    # whitespace tolerance
    ({"a": 1, "b": "${ a }"}, "b"),
    ({"b": "${hf.echo: 1 , 2 }"}, "b"),
    # chained interpolation
    ({"a": 1, "b": "${a}", "c": "${b}"}, "c"),
    ({"a": {"b": "${..c}"}, "c": "${d}", "d": 9}, "a.b"),
]


def _read(module, data, key):
    cfg = module.OmegaConf.create(data)
    value = module.OmegaConf.select(cfg, key)
    if isinstance(value, (module.DictConfig, module.ListConfig)):
        return module.OmegaConf.to_container(value, resolve=True)
    return value


@pytest.mark.parametrize(
    "data,key", CASES, ids=[f"{d.get(k.split('.')[0], k)!r:.40s}" for d, k in CASES]
)
def test_matches_omegaconf(data, key):
    import hydra_fast

    try:
        expected = _read(omegaconf, data, key)
        expected_error = None
    except Exception as exc:  # noqa: BLE001
        expected, expected_error = None, type(exc).__name__

    try:
        got = _read(hydra_fast, data, key)
        got_error = None
    except Exception as exc:  # noqa: BLE001
        got, got_error = None, type(exc).__name__

    if expected_error or got_error:
        assert (
            expected_error and got_error
        ), f"omegaconf={expected_error or expected!r} fast={got_error or got!r}"
        return
    # repr comparison so NaN compares equal to itself
    assert expected == got or repr(expected) == repr(got), f"{expected!r} != {got!r}"


# ---------------------------------------------------------------------------
# behaviour that does not need omegaconf to state
# ---------------------------------------------------------------------------
def test_missing_raises_on_access():
    cfg = OmegaConf.create({"a": "???"})
    with pytest.raises(MissingMandatoryValue):
        cfg.a
    assert OmegaConf.is_missing(cfg, "a")


def test_interpolation_to_missing_raises():
    cfg = OmegaConf.create({"a": "???", "b": "${a}"})
    with pytest.raises(InterpolationResolutionError):
        cfg.b


def test_unknown_key_raises():
    cfg = OmegaConf.create({"b": "${nope}"})
    with pytest.raises(InterpolationKeyError):
        cfg.b


def test_unknown_resolver_raises():
    cfg = OmegaConf.create({"b": "${no.such.resolver:1}"})
    with pytest.raises(UnsupportedInterpolationType):
        cfg.b


def test_recursive_interpolation_raises():
    cfg = OmegaConf.create({"a": "${b}", "b": "${a}"})
    with pytest.raises(InterpolationResolutionError):
        cfg.a


def test_malformed_interpolation_raises():
    with pytest.raises(GrammarParseError):
        OmegaConf.to_container(OmegaConf.create({"a": "${"}), resolve=True)


def test_quoted_interpolation_stringifies():
    """A quoted value is a string even when it holds one interpolation."""
    cfg = OmegaConf.create({"a": 1, "b": "${hf.echo:'${a}'}"})
    assert cfg.b == "1"


def test_resolver_receives_config_node():
    """`${f:${some.node}}` must arrive as a DictConfig, not a plain dict."""
    seen = {}

    def grab(value):
        seen["type"] = type(value).__name__
        return OmegaConf.to_container(value, resolve=True)

    OmegaConf.register_new_resolver("hf.grab", grab, replace=True)
    cfg = OmegaConf.create({"node": {"x": 1}, "out": "${hf.grab:${node}}"})
    assert cfg.out == {"x": 1}
    assert seen["type"] == "DictConfig"


def test_use_cache_keys_on_spelling():
    calls = []

    def counter(value):
        calls.append(value)
        return len(calls)

    OmegaConf.register_new_resolver("hf.count", counter, replace=True, use_cache=True)
    cfg = OmegaConf.create({"a": 1, "x": "${hf.count:${a}}", "y": "${hf.count:${a}}"})
    assert cfg.x == 1
    assert cfg.y == 1
    assert len(calls) == 1


def test_resolve_in_place():
    cfg = OmegaConf.create({"a": 1, "b": "${a}", "c": {"d": "${a}"}})
    OmegaConf.resolve(cfg)
    assert OmegaConf.to_container(cfg) == {"a": 1, "b": 1, "c": {"d": 1}}


def test_oc_dict_values_is_lazy():
    """A retained list keeps tracking the source, as omegaconf's does."""
    cfg = OmegaConf.create({"src": {"a": 1, "b": 2}, "vals": "${oc.dict.values:src}"})
    held = OmegaConf.select(cfg, "vals")
    assert OmegaConf.to_container(held) == ["${src.a}", "${src.b}"]
    cfg.src.a = 99
    assert OmegaConf.to_container(held, resolve=True) == [99, 2]


def test_oc_dict_values_reflects_added_keys():
    cfg = OmegaConf.create({"src": {"a": 1}, "vals": "${oc.dict.values:src}"})
    assert OmegaConf.to_container(cfg, resolve=True)["vals"] == [1]
    cfg.src.b = 2
    assert OmegaConf.to_container(cfg, resolve=True)["vals"] == [1, 2]


def test_oc_dict_values_falls_back_for_unspellable_keys():
    """A key with a dot cannot be spelled in a reference; resolve it instead."""
    cfg = OmegaConf.create({"src": {"a.b": 1, "c": 2}, "vals": "${oc.dict.values:src}"})
    assert OmegaConf.to_container(cfg, resolve=True)["vals"] == [1, 2]


def test_referenced_subtree_resolves_in_its_own_context():
    """`${a}` pointing at a subtree with `${.sibling}` refs resolves correctly."""
    cfg = OmegaConf.create({"a": {"x": 1, "y": "${.x}"}, "alias": "${a}"})
    assert OmegaConf.to_container(cfg, resolve=True)["alias"] == {"x": 1, "y": 1}
