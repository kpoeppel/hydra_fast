"""``instantiate`` and the ``_target_`` protocol.

The case table lives in ``bench/oracle_instantiate.py`` so it can also be run
standalone against real hydra; this drives the same table through pytest and
pins the answers that matter without it.
"""

from __future__ import annotations

import functools

import pytest

import instantiate_targets as targets
import oracle_instantiate as oracle
from conftest import requires_hydra
from hydra_fast import OmegaConf, instantiate
from hydra_fast.errors import InstantiationException
from hydra_fast.utils import ConvertMode, get_class, get_method, get_object

T = oracle.T


@requires_hydra
@pytest.mark.parametrize(
    "label,config,args,kwargs", oracle.CASES, ids=[case[0] for case in oracle.CASES]
)
def test_matches_hydra(label, config, args, kwargs):
    import hydra.utils as real
    from omegaconf import OmegaConf as RealOmegaConf

    import hydra_fast.utils as fast

    assert oracle.outcome(fast, OmegaConf.create, config, args, kwargs) == oracle.outcome(
        real, RealOmegaConf.create, config, args, kwargs
    )


# ---------------------------------------------------------------------------
# the answers, pinned so these run without hydra installed
# ---------------------------------------------------------------------------
def test_builds_a_target():
    cfg = OmegaConf.create({"_target_": f"{T}.Plain", "a": 5})
    assert instantiate(cfg) == targets.Plain(a=5)


def test_call_site_kwargs_override_the_config():
    cfg = OmegaConf.create({"_target_": f"{T}.Plain", "a": 5})
    assert instantiate(cfg, a=9).a == 9


def test_call_site_args_replace_underscore_args():
    """Not append -- `_args_: [1]` called with (2, 3) passes (2, 3)."""
    cfg = OmegaConf.create({"_target_": f"{T}.Positional", "_args_": [1]})
    assert instantiate(cfg).args == (1,)
    assert instantiate(cfg, 2, 3).args == (2, 3)


def test_recursive_by_default():
    cfg = OmegaConf.create(
        {"_target_": f"{T}.Nested", "inner": {"_target_": f"{T}.Plain", "a": 2}}
    )
    assert instantiate(cfg).inner == targets.Plain(a=2)


def test_recursive_off_leaves_the_inner_config_alone():
    cfg = OmegaConf.create(
        {
            "_target_": f"{T}.Nested",
            "_recursive_": False,
            "inner": {"_target_": f"{T}.Plain"},
        }
    )
    assert not isinstance(instantiate(cfg).inner, targets.Plain)


def test_partial_returns_a_partial():
    cfg = OmegaConf.create({"_target_": f"{T}.Plain", "_partial_": True, "a": 3})
    built = instantiate(cfg)
    assert isinstance(built, functools.partial)
    assert built().a == 3
    assert built(a=8).a == 8


@pytest.mark.parametrize(
    "mode,mapping_type",
    [("none", "DictConfig"), ("partial", "dict"), ("object", "dict"), ("all", "dict")],
)
def test_convert_controls_what_the_target_receives(mode, mapping_type):
    cfg = OmegaConf.create(
        {"_target_": f"{T}.HoldsContainers", "_convert_": mode, "mapping": {"k": 1}}
    )
    assert type(instantiate(cfg).mapping).__name__ == mapping_type


def test_no_target_container_is_walked_not_rejected():
    cfg = OmegaConf.create({"k": {"_target_": f"{T}.Plain", "a": 4}, "plain": 1})
    built = instantiate(cfg)
    assert built["k"] == targets.Plain(a=4)
    assert built["plain"] == 1


def test_interpolations_resolve_before_the_target_sees_them():
    cfg = OmegaConf.create({"base": 7, "obj": {"_target_": f"{T}.Plain", "a": "${base}"}})
    assert instantiate(cfg).obj.a == 7


def test_none_config_is_none():
    assert instantiate(None) is None


@pytest.mark.parametrize(
    "config,match",
    [
        ({"_target_": "nosuchmodule.Thing"}, "Error loading"),
        ({"_target_": f"{T}.NoSuchName"}, "Error loading"),
        ({"_target_": ""}, "Empty path"),
        ({"_target_": ".Plain"}, "Relative imports are not supported"),
        ({"_target_": f"{T}.Raises"}, "Error in call to target"),
    ],
)
def test_failures(config, match):
    with pytest.raises(InstantiationException, match=match):
        instantiate(OmegaConf.create(config))


def test_primitive_top_level_is_rejected():
    with pytest.raises(InstantiationException, match="Top level config must be"):
        instantiate(5)


# ---------------------------------------------------------------------------
# the lookup helpers
# ---------------------------------------------------------------------------
def test_get_helpers():
    assert get_class(f"{T}.Plain") is targets.Plain
    assert get_method(f"{T}.make") is targets.make
    assert get_object(f"{T}.CONSTANT") == 42

    with pytest.raises(InstantiationException, match="non-class"):
        get_class(f"{T}.CONSTANT")
    with pytest.raises(InstantiationException, match="non-callable"):
        get_method(f"{T}.CONSTANT")


def test_convert_mode_values_match_hydras_spelling():
    assert [mode.value for mode in ConvertMode] == ["none", "partial", "object", "all"]
