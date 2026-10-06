"""The OmegaConf-compatible container and facade API."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import Enum
from typing import List

import pytest

from conftest import requires_omegaconf
from hydra_fast import (
    II,
    SI,
    DictConfig,
    ListConfig,
    OmegaConf,
    SCMode,
    flag_override,
    open_dict,
    read_write,
)
from hydra_fast.errors import (
    ConfigAttributeError,
    ConfigKeyError,
    MissingMandatoryValue,
    ReadonlyConfigError,
    ValidationError,
)


# ---------------------------------------------------------------------------
# create / to_container
# ---------------------------------------------------------------------------
def test_create_from_dict_and_roundtrip():
    data = {"a": 1, "b": {"c": [1, 2, {"d": 3}]}, "e": None, "f": True}
    cfg = OmegaConf.create(data)
    assert OmegaConf.to_container(cfg) == data


def test_create_from_yaml_string():
    cfg = OmegaConf.create("a: 1\nb:\n  c: two\n")
    assert cfg.a == 1
    assert cfg.b.c == "two"


def test_create_from_list():
    cfg = OmegaConf.create([1, 2, 3])
    assert isinstance(cfg, ListConfig)
    assert list(cfg) == [1, 2, 3]


def test_create_none_gives_empty_dict():
    assert OmegaConf.to_container(OmegaConf.create()) == {}


def test_create_rejects_unsupported():
    with pytest.raises(ValidationError):
        OmegaConf.create(object())


def test_create_copies_input():
    data = {"a": {"b": 1}}
    cfg = OmegaConf.create(data)
    cfg.a.b = 2
    assert data["a"]["b"] == 1, "create() must not alias its input"


def test_yaml_typing_matches_omegaconf_rules():
    cfg = OmegaConf.create("lr: 2.5e-4\nexp: 1e5\nwhen: 2024-01-02\nflag: true\n")
    assert isinstance(cfg.lr, float) and cfg.lr == 0.00025
    assert isinstance(cfg.exp, float)
    assert cfg.when == "2024-01-02", "dates stay strings, as in omegaconf"
    assert cfg.flag is True


# ---------------------------------------------------------------------------
# access
# ---------------------------------------------------------------------------
def test_attr_and_item_access():
    cfg = OmegaConf.create({"a": {"b": 1}})
    assert cfg.a.b == 1
    assert cfg["a"]["b"] == 1
    assert cfg.get("a").get("b") == 1


def test_missing_key_without_struct_returns_none_from_get():
    cfg = OmegaConf.create({"a": 1})
    assert cfg.get("nope") is None
    assert cfg.get("nope", "dflt") == "dflt"


def test_struct_mode_blocks_unknown_keys():
    cfg = OmegaConf.create({"a": 1})
    OmegaConf.set_struct(cfg, True)
    with pytest.raises(ConfigAttributeError):
        cfg.nope
    with pytest.raises(ConfigKeyError):
        cfg["nope"]
    with pytest.raises(ConfigAttributeError):
        cfg.nope = 1


def test_open_dict_allows_adding():
    cfg = OmegaConf.create({"a": 1})
    OmegaConf.set_struct(cfg, True)
    with open_dict(cfg):
        cfg.added = 2
    assert cfg.added == 2
    with pytest.raises(ConfigAttributeError):
        cfg.another = 3


def test_struct_is_inherited_by_children():
    cfg = OmegaConf.create({"a": {"b": 1}})
    OmegaConf.set_struct(cfg, True)
    with pytest.raises(ConfigAttributeError):
        cfg.a.nope


def test_readonly():
    cfg = OmegaConf.create({"a": 1})
    OmegaConf.set_readonly(cfg, True)
    with pytest.raises(ReadonlyConfigError):
        cfg.a = 2
    with read_write(cfg):
        cfg.a = 2
    assert cfg.a == 2


def test_flag_override_restores():
    cfg = OmegaConf.create({"a": 1})
    OmegaConf.set_struct(cfg, True)
    with flag_override(cfg, "struct", False):
        cfg.new = 1
    assert OmegaConf.is_readonly(cfg) in (None, False)
    with pytest.raises(ConfigAttributeError):
        cfg.another = 2


def test_views_are_live():
    cfg = OmegaConf.create({"a": {"b": 1}})
    view = cfg.a
    cfg.a.b = 5
    assert view.b == 5, "a child view must see writes through the parent"


# ---------------------------------------------------------------------------
# mutation
# ---------------------------------------------------------------------------
def test_set_get_delete():
    cfg = OmegaConf.create({"a": 1, "b": 2})
    cfg.a = 10
    cfg["c"] = 3
    del cfg["b"]
    assert OmegaConf.to_container(cfg) == {"a": 10, "c": 3}


def test_pop():
    cfg = OmegaConf.create({"a": 1, "nested": {"x": 1}})
    assert cfg.pop("a") == 1
    popped = cfg.pop("nested")
    assert OmegaConf.to_container(popped) == {"x": 1}
    assert OmegaConf.to_container(cfg) == {}
    with pytest.raises(ConfigKeyError):
        cfg.pop("gone")
    assert cfg.pop("gone", "dflt") == "dflt"


def test_update():
    cfg = OmegaConf.create({"a": {"b": 1}})
    OmegaConf.update(cfg, "a.c", 2)
    assert OmegaConf.to_container(cfg) == {"a": {"b": 1, "c": 2}}
    OmegaConf.update(cfg, "a", {"d": 3}, merge=True)
    assert OmegaConf.to_container(cfg) == {"a": {"b": 1, "c": 2, "d": 3}}
    OmegaConf.update(cfg, "a", {"only": 1}, merge=False)
    assert OmegaConf.to_container(cfg) == {"a": {"only": 1}}


def test_update_force_add_in_struct():
    cfg = OmegaConf.create({"a": 1})
    OmegaConf.set_struct(cfg, True)
    with pytest.raises(ConfigAttributeError):
        OmegaConf.update(cfg, "deep.key", 1)
    OmegaConf.update(cfg, "deep.key", 1, force_add=True)
    assert cfg.deep.key == 1


def test_list_mutation():
    cfg = OmegaConf.create([1, 2])
    cfg.append(3)
    cfg.extend([4, 5])
    cfg.insert(0, 0)
    assert list(cfg) == [0, 1, 2, 3, 4, 5]
    assert cfg.pop() == 5
    cfg.remove(0)
    assert list(cfg) == [1, 2, 3, 4]
    assert cfg.index(3) == 2
    assert cfg.count(2) == 1
    assert 3 in cfg


def test_list_add_both_directions():
    cfg = OmegaConf.create([2, 3])
    assert list(cfg + [4]) == [2, 3, 4]
    assert list([1] + cfg) == [1, 2, 3]


# ---------------------------------------------------------------------------
# copy semantics
# ---------------------------------------------------------------------------
def test_deepcopy_is_independent():
    cfg = OmegaConf.create({"a": {"b": [1, 2]}})
    clone = copy.deepcopy(cfg)
    clone.a.b.append(3)
    assert list(cfg.a.b) == [1, 2]


def test_copy_method_is_deep():
    cfg = OmegaConf.create({"a": {"b": 1}})
    clone = cfg.copy()
    clone.a.b = 2
    assert cfg.a.b == 1


def test_deepcopy_keeps_flags():
    cfg = OmegaConf.create({"a": 1})
    OmegaConf.set_struct(cfg, True)
    clone = copy.deepcopy(cfg)
    with pytest.raises(ConfigAttributeError):
        clone.nope


def test_subtree_clone_reroots_flags():
    cfg = OmegaConf.create({"a": {"b": 1}})
    OmegaConf.set_struct(cfg, True)
    sub = copy.deepcopy(cfg.a)
    with pytest.raises(ConfigAttributeError):
        sub.nope


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "dest,src,expected",
    [
        ({"a": 1}, {"b": 2}, {"a": 1, "b": 2}),
        ({"a": 1}, {"a": 2}, {"a": 2}),
        ({"a": {"b": 1, "c": 2}}, {"a": {"b": 9}}, {"a": {"b": 9, "c": 2}}),
        ({"l": [1, 2, 3]}, {"l": [9]}, {"l": [9]}),
        ({"k": 1}, {"k": {"a": 2}}, {"k": {"a": 2}}),
        ({"k": {"a": 1}}, {"k": 5}, {"k": 5}),
        ({"k": None}, {"k": {"a": 1}}, {"k": {"a": 1}}),
        ({"x": 1}, {"x": None}, {"x": None}),
        # a MISSING source value does not clobber an existing one
        ({"x": 1, "y": {"z": 2}}, {"x": "???", "y": {"z": "???"}}, {"x": 1, "y": {"z": 2}}),
        # ... but it does introduce a new key as missing
        ({"a": 1}, {"b": "???"}, {"a": 1, "b": "???"}),
    ],
)
def test_merge_semantics(dest, src, expected):
    merged = OmegaConf.merge(OmegaConf.create(dest), OmegaConf.create(src))
    assert OmegaConf.to_container(merged) == expected


def test_merge_does_not_mutate_inputs():
    first = OmegaConf.create({"a": {"b": 1}})
    second = OmegaConf.create({"a": {"b": 2}})
    OmegaConf.merge(first, second)
    assert first.a.b == 1
    assert second.a.b == 2


def test_merge_keeps_interpolations_lazy():
    merged = OmegaConf.merge(
        OmegaConf.create({"a": 1, "b": "${a}"}), OmegaConf.create({"a": 5})
    )
    assert OmegaConf.to_container(merged, resolve=True) == {"a": 5, "b": 5}


def test_merge_with_dotlist():
    cfg = OmegaConf.create({"a": {"b": 1}})
    cfg.merge_with_dotlist(["a.b=2", "a.c=three", "d=true", "e=1.5"])
    assert OmegaConf.to_container(cfg) == {
        "a": {"b": 2, "c": "three"},
        "d": True,
        "e": 1.5,
    }


def test_from_dotlist():
    cfg = OmegaConf.from_dotlist(["a.b=1", "c=[1,2]"])
    assert cfg.a.b == 1


# ---------------------------------------------------------------------------
# select / predicates
# ---------------------------------------------------------------------------
def test_select():
    cfg = OmegaConf.create({"a": {"b": {"c": 1}}, "l": [10, 20]})
    assert OmegaConf.select(cfg, "a.b.c") == 1
    assert OmegaConf.select(cfg, "l[1]") == 20
    assert OmegaConf.select(cfg, "nope") is None
    assert OmegaConf.select(cfg, "nope", default="d") == "d"
    assert OmegaConf.to_container(OmegaConf.select(cfg, "a.b")) == {"c": 1}


def test_select_relative():
    cfg = OmegaConf.create({"a": {"b": 1, "c": {"d": 2}}})
    assert OmegaConf.select(cfg.a, ".b") == 1
    assert OmegaConf.select(cfg.a.c, "..b") == 1


def test_select_missing_behaviour():
    cfg = OmegaConf.create({"a": "???"})
    assert OmegaConf.select(cfg, "a") is None
    with pytest.raises(MissingMandatoryValue):
        OmegaConf.select(cfg, "a", throw_on_missing=True)


def test_predicates():
    cfg = OmegaConf.create({"a": 1, "m": "???", "i": "${a}", "n": None})
    assert OmegaConf.is_config(cfg)
    assert OmegaConf.is_dict(cfg)
    assert not OmegaConf.is_list(cfg)
    assert OmegaConf.is_list(OmegaConf.create([]))
    assert OmegaConf.is_missing(cfg, "m")
    assert not OmegaConf.is_missing(cfg, "a")
    assert OmegaConf.is_interpolation(cfg, "i")
    assert not OmegaConf.is_interpolation(cfg, "a")
    assert OmegaConf.is_none(cfg, "n")


def test_missing_keys():
    cfg = OmegaConf.create({"a": "???", "b": {"c": "???", "d": 1}})
    assert OmegaConf.missing_keys(cfg) == {"a", "b.c"}


def test_contains_treats_missing_as_absent():
    cfg = OmegaConf.create({"a": "???", "b": 1})
    assert "b" in cfg
    assert "a" not in cfg


# ---------------------------------------------------------------------------
# yaml / io
# ---------------------------------------------------------------------------
def test_to_yaml_and_back(tmp_path):
    cfg = OmegaConf.create({"a": 1, "b": {"c": "text"}, "l": [1, 2]})
    text = OmegaConf.to_yaml(cfg)
    assert OmegaConf.to_container(OmegaConf.create(text)) == OmegaConf.to_container(cfg)

    path = tmp_path / "out.yaml"
    OmegaConf.save(cfg, path)
    assert OmegaConf.to_container(OmegaConf.load(path)) == OmegaConf.to_container(cfg)


def test_to_yaml_keeps_interpolations_unresolved():
    cfg = OmegaConf.create({"a": 1, "b": "${a}"})
    assert "${a}" in OmegaConf.to_yaml(cfg)
    assert "${a}" not in OmegaConf.to_yaml(cfg, resolve=True)


def test_load_returns_independent_copies(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("a:\n  b: 1\n")
    first = OmegaConf.load(path)
    first.a.b = 2
    second = OmegaConf.load(path)
    assert second.a.b == 1, "the read cache must not hand out shared storage"


# ---------------------------------------------------------------------------
# structured configs
# ---------------------------------------------------------------------------
@dataclass
class Inner:
    x: int = 1
    y: str = "why"


@dataclass
class Outer:
    name: str = "outer"
    count: int = 0
    inner: Inner = field(default_factory=Inner)
    items: List[int] = field(default_factory=list)
    required: str = "???"


def test_structured_create():
    cfg = OmegaConf.create(Outer)
    assert cfg.name == "outer"
    assert cfg.inner.x == 1
    assert OmegaConf.is_missing(cfg, "required")


def test_structured_instance():
    cfg = OmegaConf.create(Outer(name="given", count=3))
    assert cfg.name == "given"
    assert cfg.count == 3


def test_structured_merge_with_yaml():
    schema = OmegaConf.create(Outer)
    override = OmegaConf.create({"count": 7, "inner": {"x": 9}, "required": "ok"})
    merged = OmegaConf.merge(schema, override)
    assert merged.count == 7
    assert merged.inner.x == 9
    assert merged.inner.y == "why"
    assert merged.required == "ok"


def test_structured_as_struct():
    cfg = OmegaConf.structured(Outer)
    with pytest.raises(ConfigAttributeError):
        cfg.not_a_field


def test_get_type_reports_dataclass():
    cfg = OmegaConf.create(Outer)
    assert OmegaConf.get_type(cfg) is Outer


class Color(Enum):
    RED = 1
    GREEN = 2


@dataclass
class WithEnum:
    c: Color = Color.RED
    inner: Inner = field(default_factory=Inner)


def test_enum_to_str_uses_member_name():
    """omegaconf renders `RED`, not `Color.RED`, so the YAML stays loadable."""
    cfg = OmegaConf.structured(WithEnum)
    assert OmegaConf.to_container(cfg, enum_to_str=True)["c"] == "RED"
    assert "c: RED" in OmegaConf.to_yaml(cfg)


def test_enum_preserved_without_enum_to_str():
    cfg = OmegaConf.structured(WithEnum)
    assert OmegaConf.to_container(cfg)["c"] is Color.RED


def test_scmode_dict():
    cfg = OmegaConf.structured(WithEnum)
    out = OmegaConf.to_container(cfg, structured_config_mode=SCMode.DICT)
    assert isinstance(out, dict) and isinstance(out["inner"], dict)


def test_scmode_dict_config():
    cfg = OmegaConf.structured(WithEnum)
    out = OmegaConf.to_container(cfg, structured_config_mode=SCMode.DICT_CONFIG)
    assert isinstance(out, DictConfig)


def test_scmode_instantiate_rebuilds_nested_dataclasses():
    cfg = OmegaConf.structured(WithEnum)
    out = OmegaConf.to_container(cfg, structured_config_mode=SCMode.INSTANTIATE)
    assert isinstance(out, WithEnum)
    assert isinstance(out.inner, Inner)
    assert out.c is Color.RED


def test_to_object_instantiates():
    cfg = OmegaConf.structured(WithEnum)
    out = OmegaConf.to_object(cfg)
    assert isinstance(out, WithEnum)


def test_instantiate_falls_back_when_keys_do_not_fit():
    """A config that gained extra keys cannot be instantiated; return the dict."""
    cfg = OmegaConf.create(WithEnum)
    cfg.unexpected = 1
    out = OmegaConf.to_container(cfg, structured_config_mode=SCMode.INSTANTIATE)
    assert isinstance(out, dict)
    assert out["unexpected"] == 1


YAML_DUMP_CASES = [
    {"n": 1, "y": 2, "on": 3, "no": 4, "yes": 5},
    {"s": "plain", "num": "42", "flt": "1.5", "b": "true", "Y": "Y", "neg": "-7"},
    {"multi": "line one\nline two\n"},
    {"empty": "", "space": " ", "colon": "a: b", "hash": "a #b", "quote": "it's"},
    {"deep": {"a": [1, "2", {"b": "true"}]}},
    {"interp": "${a.b}", "pct": "100%"},
    {"nullv": None, "t": True, "f": False, "i": 0, "fl": 0.5},
    {"unicode": "héllo ✓"},
    {"list": ["1", "true", "x"]},
]


@requires_omegaconf
@pytest.mark.parametrize("data", YAML_DUMP_CASES, ids=range(len(YAML_DUMP_CASES)))
def test_yaml_dump_is_byte_identical_to_omegaconf(data):
    """A string that looks like a bool/int/float must be quoted, as omegaconf does."""
    import omegaconf

    assert OmegaConf.to_yaml(OmegaConf.create(data)) == omegaconf.OmegaConf.to_yaml(
        omegaconf.OmegaConf.create(data)
    )


@pytest.mark.parametrize("data", YAML_DUMP_CASES, ids=range(len(YAML_DUMP_CASES)))
def test_yaml_dump_roundtrips(data):
    text = OmegaConf.to_yaml(OmegaConf.create(data))
    assert OmegaConf.to_container(OmegaConf.create(text)) == data


def test_yaml_roundtrip_of_ambiguous_keys():
    """Keys like `n`/`on` must survive a dump/load cycle as strings."""
    cfg = OmegaConf.create({"n": 1, "y": 2, "on": 3, "no": 4})
    text = OmegaConf.to_yaml(cfg)
    assert OmegaConf.to_container(OmegaConf.create(text)) == {
        "n": 1,
        "y": 2,
        "on": 3,
        "no": 4,
    }


# ---------------------------------------------------------------------------
# misc helpers
# ---------------------------------------------------------------------------
def test_si_and_ii():
    cfg = OmegaConf.create({"a": 1, "b": SI("${a}"), "c": II("a")})
    assert cfg.b == 1
    assert cfg.c == 1


def test_masked_copy():
    cfg = OmegaConf.create({"a": 1, "b": 2, "c": 3})
    masked = OmegaConf.masked_copy(cfg, ["a", "c"])
    assert OmegaConf.to_container(masked) == {"a": 1, "c": 3}


def test_items_values_keys():
    cfg = OmegaConf.create({"a": 1, "b": {"c": 2}})
    assert list(cfg.keys()) == ["a", "b"]
    assert [k for k, _ in cfg.items()] == ["a", "b"]
    values = cfg.values()
    assert values[0] == 1
    assert isinstance(values[1], DictConfig)


def test_equality():
    assert OmegaConf.create({"a": 1}) == OmegaConf.create({"a": 1})
    assert OmegaConf.create({"a": 1}) == {"a": 1}
    assert OmegaConf.create({"a": 1}) != {"a": 2}
    assert OmegaConf.create([1, 2]) == [1, 2]


def test_len_and_bool():
    assert len(OmegaConf.create({"a": 1})) == 1
    assert not OmegaConf.create({})
    assert OmegaConf.create({"a": 1})


def test_to_container_throw_on_missing():
    cfg = OmegaConf.create({"a": "???"})
    assert OmegaConf.to_container(cfg) == {"a": "???"}
    with pytest.raises(MissingMandatoryValue):
        OmegaConf.to_container(cfg, throw_on_missing=True)


def test_clear_resolver_restores_builtins():
    OmegaConf.register_new_resolver("hf.temp", lambda: 1, replace=True)
    assert OmegaConf.has_resolver("hf.temp")
    OmegaConf.clear_resolvers()
    assert not OmegaConf.has_resolver("hf.temp")
    assert OmegaConf.has_resolver("oc.env"), "built-ins must survive"


def test_duplicate_resolver_needs_replace():
    OmegaConf.register_new_resolver("hf.dup", lambda: 1, replace=True)
    with pytest.raises(ValueError):
        OmegaConf.register_new_resolver("hf.dup", lambda: 2)
    OmegaConf.register_new_resolver("hf.dup", lambda: 2, replace=True)
