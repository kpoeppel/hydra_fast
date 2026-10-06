"""The Defaults List algorithm, compared case-by-case with hydra.

``_internal/defaults_list.py`` and ``_internal/default_element.py`` are ported
from hydra essentially verbatim and are the most intricate logic here. They are
also the least *directly* tested, since nothing calls them -- they are reached
through composition. The case table lives in ``bench/oracle_defaults_list.py``
so it can be run standalone; this drives the same table through pytest.
"""

from __future__ import annotations

import pytest

import oracle_defaults_list as oracle
from conftest import requires_hydra


@pytest.fixture(scope="module")
def tree():
    return oracle.build()


@requires_hydra
@pytest.mark.parametrize(
    "name,overrides",
    oracle.CASES,
    ids=[f"{name}-{'_'.join(ov) or 'plain'}" for name, ov in oracle.CASES],
)
def test_matches_hydra(tree, name, overrides):
    assert oracle.outcome("hydra-fast", tree, name, list(overrides)) == oracle.outcome(
        "hydra", tree, name, list(overrides)
    )


# ---------------------------------------------------------------------------
# The answers that matter, pinned so these still run without hydra.
# ---------------------------------------------------------------------------

def _composed(tree, name, overrides=()):
    """The resolved container, asserting composition succeeded."""
    result = oracle.outcome("hydra-fast", tree, name, list(overrides))
    assert result[0] == "ok", result
    return result[1]


def _failure(tree, name, overrides=()):
    """The ``(type, first message line)`` of a composition that must fail."""
    result = oracle.outcome("hydra-fast", tree, name, list(overrides))
    assert result[0] == "raise", result
    return result[1], result[2]

def test_self_last_lets_the_root_win(tree):
    assert _composed(tree, "self_last")["shared"] == "root-wins"


def test_a_package_header_means_there_is_no_conflict(tree):
    """`_self_` placement only matters for keys that actually collide.

    `a/one.yaml` carries `# @package a`, so its `shared` lands at `a.shared`
    and never meets the root's. Worth pinning because it is the thing one
    expects `_self_` ordering to decide and it does not.
    """
    out = _composed(tree, "self_first")
    assert out["shared"] == "root-loses", "the root's own key is untouched"
    assert out["a"]["shared"] == "from-a-one"


def test_override_suggests_the_package_qualified_key(tree):
    """The suggestion is the actionable part of this failure.

    A config pulled in by a group lands its own groups under that package, so
    `override a` cannot reach it -- `override a@composite.a` can. hydra names
    that in the message and so must this: a previous deviation here replaced
    the suggestion with a claim that no group default existed, which was both
    less useful and not true.
    """
    _, message = _failure(tree, "override_nested")
    assert "Could not override 'a'" in message


def test_override_with_no_match_says_so(tree):
    _, message = _failure(tree, "override_absent")
    assert "No match in the defaults list" in message


def test_deleting_a_group_from_the_command_line(tree):
    out = _composed(tree, "delete_target", ["~b"])
    assert "b" not in out
    assert out["a"]["v"] == "a-one", "the other group is untouched"


def test_deleting_with_the_wrong_value_fails(tree):
    _failure(tree, "delete_target", ["~b=two"])


def test_optional_missing_option_is_skipped(tree):
    assert "a" not in _composed(tree, "optional_missing")


def test_null_disables_a_group(tree):
    assert "a" not in _composed(tree, "null_group")


def test_package_rebinding(tree):
    renamed = _composed(tree, "rebind_named")
    assert renamed["renamed"]["v"] == "a-one" and "a" not in renamed

    twice = _composed(tree, "rebind_two_ways")
    assert twice["first"]["v"] == "a-one"
    assert twice["second"]["v"] == "a-two", "one group, two packages, two options"


def test_global_and_here_headers(tree):
    glob = _composed(tree, "headers_global")
    assert glob["global_key"] == "global-value", "_global_ lands at the root"
    assert "pkg" not in glob, "and nothing is left under the group"

    # `_here_` in a *group option* is taken literally rather than resolved to
    # the group's package -- checked against hydra, which does the same.
    here = _composed(tree, "headers_here")
    assert here == {"_here_": {"v": "here"}}


def test_interpolated_group_name_follows_an_earlier_choice(tree):
    """`- a: ${b}` makes `a` take whatever option `b` took, override included."""
    out = _composed(tree, "interpolated")
    assert (out["a"]["v"], out["b"]["v"]) == ("a-two", "b-two")

    overridden = _composed(tree, "interpolated", ["b=one"])
    assert (overridden["a"]["v"], overridden["b"]["v"]) == ("a-one", "b-one")


def test_interpolated_group_name_cannot_reach_the_config_body(tree):
    """It resolves against group choices, not arbitrary values -- as in hydra."""
    _, message = _failure(tree, "interpolated_body")
    assert "Error resolving interpolation" in message


def test_a_nested_config_pulls_in_its_own_group(tree):
    assert _composed(tree, "chain")["a"]["v"] == "a-two", "composite/pulls_a_two"


@pytest.mark.parametrize(
    "name,overrides",
    [
        ("dup_group", []),
        ("missing_option", []),
        ("missing_group", []),
        ("self_last", ["a=nope"]),
        ("self_last", ["nosuch=x"]),
        ("self_last", ["+a=two"]),
    ],
)
def test_failures(tree, name, overrides):
    _failure(tree, name, overrides)
