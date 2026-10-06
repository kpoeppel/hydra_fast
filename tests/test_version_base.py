"""``version_base``: a real setting, and the deprecations it gates.

hydra-fast implements hydra 1.3 semantics at every level, so the base changes
which deprecation warnings fire rather than how configs compose. It still has
to be real, because the warnings are observable -- hydra's own suite asserts
on one of them.
"""

from __future__ import annotations

import warnings

import pytest

from hydra_fast import version
from hydra_fast.core.override_parser.overrides_parser import OverridesParser
from hydra_fast.errors import HydraException


@pytest.fixture(autouse=True)
def restore_base():
    """The base is a singleton, so put it back however the test left it."""
    before = version.VersionBase.instance().getbase()
    yield
    version.VersionBase.instance().version_base = before


def test_none_means_the_hydra_version_implemented_not_hydra_fasts():
    """`version_base=None` is "whatever is current" -- 1.3, not 0.1."""
    version.setbase(None)
    assert str(version.getbase()) == version.__hydra_version__
    assert version.base_at_least("1.2")


def test_explicit_base_round_trips():
    version.setbase("1.1")
    assert str(version.getbase()) == "1.1"
    assert version.base_at_least("1.1")
    assert not version.base_at_least("1.2")


def test_below_the_compat_floor_is_rejected():
    with pytest.raises(HydraException, match='version_base must be >= "1.1"'):
        version.setbase("1.0")


def test_patch_level_is_ignored():
    version.setbase("1.3.7")
    assert str(version.getbase()) == "1.3"


def test_unset_assumes_the_compat_level():
    version.VersionBase.instance().version_base = version._UNSPECIFIED_
    assert version.getbase() is None
    assert version.base_at_least("1.1")
    assert not version.base_at_least("1.2")


def test_versions_order():
    assert version.Version("1.2") < version.Version("1.10")
    assert version.Version("2.0") > version.Version("1.99")
    assert version.Version("1.3") == version.Version("1.3.9")


# ---------------------------------------------------------------------------
# the warning it gates
# ---------------------------------------------------------------------------
def _parse_catching(line: str):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        OverridesParser().parse_rule(line, "override")
    return [str(w.message) for w in caught]


def test_name_package_warns_below_1_2():
    version.setbase("1.1")
    (message,) = _parse_catching("key@_name_=value")
    assert "_name_ keyword is deprecated in packages" in message
    assert "key@_name_=value" in message


def test_name_package_is_silent_from_1_2():
    version.setbase("1.2")
    assert _parse_catching("key@_name_=value") == []


def test_ordinary_package_never_warns():
    version.setbase("1.1")
    assert _parse_catching("key@pkg=value") == []


def test_composition_path_warns_too():
    """The cached parse path must validate as well, or compose() goes quiet."""
    from hydra_fast.grammar.override import parse_overrides

    version.setbase("1.1")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        parse_overrides(["key@_name_=value"])
    assert any("_name_ keyword is deprecated" in str(w.message) for w in caught)


def test_initialize_declares_the_base(tmp_path):
    from hydra_fast import initialize_config_dir

    (tmp_path / "config.yaml").write_text("a: 1\n")
    with initialize_config_dir(version_base="1.1", config_dir=str(tmp_path)):
        assert str(version.getbase()) == "1.1"
