"""``ValueNode`` proxies: the omegaconf-internal layer some libraries use."""

from __future__ import annotations

import dataclasses
import enum
from typing import List, Optional

import pytest

from conftest import requires_omegaconf
from hydra_fast import (
    AnyNode,
    BooleanNode,
    DictConfig,
    EnumNode,
    FloatNode,
    IntegerNode,
    ListConfig,
    OmegaConf,
    StringNode,
    ValueNode,
)


class Color(enum.Enum):
    RED = 1


@dataclasses.dataclass
class Typed:
    n: int = 1
    s: str = "x"
    f: float = 1.0
    b: bool = True
    opt: Optional[int] = None
    e: Color = Color.RED
    lst: List[int] = dataclasses.field(default_factory=lambda: [1])


PLAIN = {"a": 1, "d": {"x": 1}, "l": [1], "i": "${a}", "m": "???", "nn": None}


@requires_omegaconf
@pytest.mark.parametrize("key", ["n", "s", "f", "b", "opt", "e", "lst"])
def test_typed_node_class_matches_omegaconf(key):
    import omegaconf

    expected = type(omegaconf.OmegaConf.structured(Typed)._get_node(key)).__name__
    got = type(OmegaConf.structured(Typed)._get_node(key)).__name__
    assert expected == got


@requires_omegaconf
@pytest.mark.parametrize("key", sorted(PLAIN))
def test_plain_node_class_matches_omegaconf(key):
    import omegaconf

    expected = type(omegaconf.OmegaConf.create(PLAIN)._get_node(key)).__name__
    got = type(OmegaConf.create(PLAIN)._get_node(key)).__name__
    assert expected == got


def test_typed_classes():
    cfg = OmegaConf.structured(Typed)
    assert isinstance(cfg._get_node("n"), IntegerNode)
    assert isinstance(cfg._get_node("s"), StringNode)
    assert isinstance(cfg._get_node("f"), FloatNode)
    assert isinstance(cfg._get_node("b"), BooleanNode)
    assert isinstance(cfg._get_node("e"), EnumNode)
    # containers are returned as containers, not value nodes
    assert isinstance(cfg._get_node("lst"), ListConfig)


def test_untyped_is_anynode():
    assert isinstance(OmegaConf.create({"a": 1})._get_node("a"), AnyNode)


def test_identity_is_stable():
    cfg = OmegaConf.create({"a": 1})
    assert cfg._get_node("a") is cfg._get_node("a")


def test_node_is_live_for_reads():
    cfg = OmegaConf.create({"a": 1})
    node = cfg._get_node("a")
    cfg.a = 7
    assert node._value() == 7, "a proxy must not snapshot"


def test_node_writes_through():
    cfg = OmegaConf.create({"a": 1})
    cfg._get_node("a")._set_value(5)
    assert cfg.a == 5


def test_node_write_is_validated():
    cfg = OmegaConf.structured(Typed)
    from hydra_fast.errors import ValidationError

    with pytest.raises(ValidationError):
        cfg._get_node("n")._set_value("xyz")
    cfg._get_node("n")._set_value("42")
    assert cfg.n == 42


def test_value_is_unresolved():
    cfg = OmegaConf.create({"a": 1, "i": "${a}"})
    assert cfg._get_node("i")._value() == "${a}"
    assert cfg._get_node("i")._is_interpolation()


def test_predicates():
    cfg = OmegaConf.create(PLAIN)
    assert cfg._get_node("m")._is_missing()
    assert cfg._get_node("nn")._is_none()
    assert not cfg._get_node("a")._is_missing()


def test_full_key_and_key():
    cfg = OmegaConf.create({"d": {"x": 1}})
    node = cfg._get_node("d")._get_node("x")
    assert node._get_full_key("") == "d.x"
    assert node._key() == "x"


def test_node_behaves_like_its_value():
    node = OmegaConf.create({"a": 5})._get_node("a")
    assert node == 5
    assert str(node) == "5"
    assert bool(node)


def test_node_parent_and_root():
    cfg = OmegaConf.create({"d": {"x": 1}})
    node = cfg._get_node("d")._get_node("x")
    assert isinstance(node._get_parent(), DictConfig)
    assert node._get_parent()._get_full_key("") == "d"


def test_container_metadata():
    cfg = OmegaConf.create({"a": 1})
    assert cfg._metadata.object_type is dict
    assert cfg._metadata.key is None
    assert OmegaConf.create([1])._metadata.object_type is list


def test_schema_metadata_names_the_class():
    cfg = OmegaConf.structured(Typed)
    assert cfg._metadata.object_type is Typed


def test_node_metadata():
    node = OmegaConf.create({"a": 1})._get_node("a")
    assert node._metadata.key == "a"


def test_list_node_access():
    cfg = OmegaConf.create({"l": [1, {"x": 2}]})
    lst = cfg._get_node("l")
    assert isinstance(lst, ListConfig)
    assert isinstance(lst._get_node(0), ValueNode)
    assert isinstance(lst._get_node(1), DictConfig)


def test_nodes_are_not_built_unless_asked():
    """The proxy cache stays empty for normal use, so it costs nothing."""
    cfg = OmegaConf.create({"a": 1, "b": {"c": 2}})
    OmegaConf.to_container(cfg, resolve=True)
    assert cfg._hf_root.node_cache == {}
    cfg._get_node("a")
    assert len(cfg._hf_root.node_cache) == 1
