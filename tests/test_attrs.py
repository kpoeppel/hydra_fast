"""attrs classes as structured configs.

Parity with omegaconf where omegaconf works, plus one documented superset:
omegaconf 2.3 raises ``ValidationError`` for an attrs *class* (not instance)
whose field has a factory default -- it hands the ``Factory`` object itself to
the container. hydra-fast calls the factory, so the class form works. That is
strictly more permissive; anything omegaconf accepts behaves identically.
"""

from __future__ import annotations

from typing import List, Optional

import pytest

from conftest import requires_omegaconf
from hydra_fast import OmegaConf
from hydra_fast.errors import ConfigAttributeError, ValidationError

attr = pytest.importorskip("attr", reason="attrs not installed")


@attr.s(auto_attribs=True)
class Inner:
    x: int = 1


@attr.s(auto_attribs=True)
class Simple:
    n: int = 1
    s: str = "x"


@attr.s(auto_attribs=True)
class Outer:
    name: str = "o"
    n: int = 1
    opt: Optional[int] = None
    lst: List[int] = attr.Factory(list)
    inner: Inner = attr.Factory(Inner)


# ---------------------------------------------------------------------------
# parity on what omegaconf supports
# ---------------------------------------------------------------------------
@requires_omegaconf
@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: Simple, id="simple-class"),
        pytest.param(lambda: Simple(), id="simple-instance"),
        pytest.param(lambda: Simple(n=5, s="y"), id="simple-populated"),
        pytest.param(lambda: Outer(), id="outer-instance"),
        pytest.param(lambda: Outer(name="given", n=3), id="outer-populated"),
    ],
)
def test_matches_omegaconf(build):
    import omegaconf

    expected = omegaconf.OmegaConf.to_container(omegaconf.OmegaConf.create(build()))
    got = OmegaConf.to_container(OmegaConf.create(build()))
    assert expected == got


@requires_omegaconf
def test_merge_matches_omegaconf():
    import omegaconf

    import hydra_fast

    def merged(module):
        return module.OmegaConf.to_container(
            module.OmegaConf.merge(
                # an *instance*, since omegaconf cannot take the class here
                module.OmegaConf.structured(Outer()),
                module.OmegaConf.create({"n": "7", "inner": {"x": "9"}}),
            )
        )

    assert merged(omegaconf) == merged(hydra_fast)


# ---------------------------------------------------------------------------
# the behaviour itself
# ---------------------------------------------------------------------------
def test_class_with_factory_default_works():
    """The documented superset: omegaconf 2.3 raises here, hydra-fast does not."""
    out = OmegaConf.to_container(OmegaConf.create(Outer))
    assert out == {"name": "o", "n": 1, "opt": None, "lst": [], "inner": {"x": 1}}


def test_nested_attrs_defaults():
    assert OmegaConf.to_container(OmegaConf.create(Outer))["inner"] == {"x": 1}


def test_type_enforcement_applies():
    cfg = OmegaConf.structured(Outer)
    with pytest.raises(ValidationError):
        cfg.n = "xyz"
    cfg.n = "42"
    assert cfg.n == 42


def test_nested_type_enforcement():
    cfg = OmegaConf.structured(Outer)
    with pytest.raises(ValidationError):
        cfg.inner.x = "xyz"


def test_merge_coerces():
    merged = OmegaConf.merge(
        OmegaConf.structured(Outer), OmegaConf.create({"n": "7", "inner": {"x": "9"}})
    )
    out = OmegaConf.to_container(merged)
    assert out["n"] == 7 and type(out["n"]) is int
    assert out["inner"]["x"] == 9 and type(out["inner"]["x"]) is int


def test_struct_rejects_unknown_field():
    with pytest.raises(ConfigAttributeError, match="Outer"):
        OmegaConf.structured(Outer).nope


def test_optional_field():
    cfg = OmegaConf.structured(Outer)
    cfg.opt = None
    assert cfg.opt is None
    cfg.opt = "3"
    assert cfg.opt == 3


def test_list_element_type():
    cfg = OmegaConf.structured(Outer)
    cfg.lst.append("5")
    assert list(cfg.lst) == [5]
    with pytest.raises(ValidationError):
        cfg.lst.append("xyz")


def test_get_type_reports_the_attrs_class():
    assert OmegaConf.get_type(OmegaConf.create(Outer)) is Outer


def test_node_class_from_attrs_annotation():
    from hydra_fast import IntegerNode

    assert isinstance(OmegaConf.structured(Outer)._get_node("n"), IntegerNode)


def test_yaml_roundtrip():
    cfg = OmegaConf.create(Outer)
    text = OmegaConf.to_yaml(cfg)
    assert OmegaConf.to_container(OmegaConf.create(text)) == OmegaConf.to_container(cfg)
