"""The private omegaconf surface the shim stands in for.

``omegaconf._utils`` and the ``Node`` layer are private upstream, so nothing
guarantees them -- but third-party code imports them anyway, which is why the
shim provides a subset. "A subset" is not a contract, so the case table in
``bench/oracle_internals.py`` enumerates it and compares against the real
implementation; this drives the same table through pytest.
"""

from __future__ import annotations

import pytest

import oracle_internals as oracle
from conftest import requires_omegaconf
from hydra_fast import OmegaConf


# ---------------------------------------------------------------------------
# the node layer
# ---------------------------------------------------------------------------
@requires_omegaconf
@pytest.mark.parametrize(
    "label,factory", oracle.CONFIGS, ids=[case[0] for case in oracle.CONFIGS]
)
def test_node_layer_matches_omegaconf(label, factory):
    import omegaconf

    real = factory(omegaconf.OmegaConf)
    fast = factory(OmegaConf)
    for key in list(real.keys()):
        real_node, fast_node = real._get_node(key), fast._get_node(key)
        for call_label, fn in oracle.NODE_CALLS:
            assert oracle.call(fn, real_node) == oracle.call(
                fn, fast_node
            ), f"{label}.{key} {call_label}"


def test_container_nodes_answer_the_scalar_protocol():
    """Code reached a node through `_get_node` without knowing its kind."""
    cfg = OmegaConf.create({"outer": {"inner": 2}, "items": [1, 2]})
    assert cfg._get_node("outer")._value() == {"inner": 2}
    assert cfg._get_node("items")._value() == [1, 2]
    assert cfg._get_node("outer")._is_optional() is True


def test_get_node_is_identity_stable_for_containers():
    """omegaconf stores child nodes in the parent, so it returns one object.

    Code does compare nodes by identity. Only `_get_node` caches: a view from
    ordinary access stays a fresh two-slot object, which is what makes
    attribute access cheap.
    """
    cfg = OmegaConf.create({"outer": {"inner": 2}, "items": [1]})
    assert cfg._get_node("outer") is cfg._get_node("outer")
    assert cfg._get_node("items") is cfg._get_node("items")


def test_the_cached_view_follows_a_reassignment():
    """A key can go from dict to list to scalar; a stale view must not linger."""
    cfg = OmegaConf.create({"x": {"a": 1}})
    assert type(cfg._get_node("x")).__name__ == "DictConfig"
    cfg.x = [1, 2]
    assert type(cfg._get_node("x")).__name__ == "ListConfig"
    cfg.x = 5
    assert cfg._get_node("x")._value() == 5


def test_a_node_is_always_truthy():
    """As omegaconf's is: `ValueNode` defines no `__bool__`.

    That matters because `_get_node` returns None for an absent key, so
    `if cfg._get_node(k):` is how omegaconf code asks whether a key exists.
    A `__bool__` reflecting the value breaks that for 0, "", False and None.
    """
    cfg = OmegaConf.create({"zero": 0, "empty": "", "no": False, "none": None})
    for key in cfg:
        assert bool(cfg._get_node(key)) is True, key
    assert cfg._get_node("absent") is None


# ---------------------------------------------------------------------------
# the _utils helpers
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def shim_utils():
    """The assembled `omegaconf._utils` stand-in.

    Built rather than installed, so these tests do not redirect imports for
    the rest of the session. It is the assembled module and not `oc_utils`
    alone because the shim adds a few names from elsewhere in the package.
    """
    import hydra_fast.compat.omegaconf_shim as shim

    return shim._make_omegaconf_module()[2]


@requires_omegaconf
@pytest.mark.parametrize(
    "label,probe", oracle.UTILS_CALLS, ids=[case[0] for case in oracle.UTILS_CALLS]
)
def test_utils_match_omegaconf(shim_utils, label, probe):
    import omegaconf._utils as real

    assert oracle.call(probe, shim_utils) == oracle.call(probe, real)


def test_every_provided_name_exists():
    """`PROVIDED` is what the shim installs; a typo there would be silent."""
    import hydra_fast.compat.oc_utils as fast

    missing = [name for name in fast.PROVIDED if not hasattr(fast, name)]
    assert not missing, missing


def test_the_shim_installs_them():
    import hydra_fast.compat.oc_utils as fast
    import hydra_fast.compat.omegaconf_shim as shim

    _, _, utils_module, _ = shim._make_omegaconf_module()
    missing = [name for name in fast.PROVIDED if not hasattr(utils_module, name)]
    assert not missing, missing


def test_value_kind_classifies_the_three_states():
    from hydra_fast.compat.oc_utils import ValueKind, get_value_kind

    assert get_value_kind(1) is ValueKind.VALUE
    assert get_value_kind("???") is ValueKind.MANDATORY_MISSING
    assert get_value_kind("${a}") is ValueKind.INTERPOLATION
    assert get_value_kind("x${a}y") is ValueKind.INTERPOLATION
