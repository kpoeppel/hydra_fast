"""Differential audit of the Defaults List algorithm.

``_internal/defaults_list.py`` and ``_internal/default_element.py`` are ported
from hydra essentially verbatim and are the most intricate logic here -- and
the least directly tested, since they are exercised through composition rather
than called. This drives them against hydra over a tree built to hit the
corners: ``_self_`` placement, ``override`` at depth, deletions, ``optional``,
package rebinding, interpolated group names, and the error cases.

Each case composes the same tree with both implementations and compares the
resolved container, or the (exception type, first message line) pair.

    pip install hydra-core==1.3.2
    python bench/oracle_defaults_list.py
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import warnings
from pathlib import Path
from typing import Any, Dict, List, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

# ---------------------------------------------------------------------------
# One tree, many root configs. Each root exercises a different corner; the
# groups below are the shared building blocks.
# ---------------------------------------------------------------------------
TREE: Dict[str, str] = {
    # --- building blocks -------------------------------------------------
    "a/one.yaml": "# @package a\nv: a-one\nshared: from-a-one\n",
    "a/two.yaml": "# @package a\nv: a-two\nshared: from-a-two\n",
    "b/one.yaml": "# @package b\nv: b-one\n",
    "b/two.yaml": "# @package b\nv: b-two\n",
    "c/one.yaml": "# @package c\nv: c-one\n",
    # a group option that itself pulls in another group
    "composite/pulls_a_one.yaml": "# @package composite\ndefaults:\n  - /a: one\nv: composite\n",
    "composite/pulls_a_two.yaml": "# @package composite\ndefaults:\n  - /a: two\nv: composite\n",
    # an option with no package header: lands at the root
    "loose/global.yaml": "loose_key: loose-value\n",
    # package rebinding targets
    "pkg/named.yaml": "# @package named\nv: named\n",
    "pkg/here.yaml": "# @package _here_\nv: here\n",
    "pkg/glob.yaml": "# @package _global_\nglobal_key: global-value\n",
    # --- roots ------------------------------------------------------------
    # `_self_` placement decides whether the root's own values win
    "self_last.yaml": "defaults:\n  - a: one\n  - _self_\nshared: root-wins\n",
    "self_first.yaml": "defaults:\n  - _self_\n  - a: one\nshared: root-loses\n",
    "self_middle.yaml": (
        "defaults:\n  - a: one\n  - _self_\n  - b: one\nshared: middle\nextra: m\n"
    ),
    "self_absent.yaml": "defaults:\n  - a: one\nshared: no-self\n",
    # overriding a group an included config already chose
    "override_nested.yaml": (
        "defaults:\n  - composite: pulls_a_one\n  - override a: two\n  - _self_\n"
    ),
    "override_twice.yaml": ("defaults:\n  - a: one\n  - override a: two\n  - _self_\n"),
    # deletion
    "delete_target.yaml": "defaults:\n  - a: one\n  - b: one\n  - _self_\n",
    # optional
    "optional_present.yaml": "defaults:\n  - optional a: one\n  - _self_\n",
    "optional_missing.yaml": "defaults:\n  - optional a: nope\n  - _self_\n",
    "optional_group_missing.yaml": "defaults:\n  - optional nosuch: x\n  - _self_\n",
    # null disables a group
    "null_group.yaml": "defaults:\n  - a: null\n  - _self_\n",
    # package rebinding at the selection site
    "rebind_named.yaml": "defaults:\n  - a@renamed: one\n  - _self_\n",
    "rebind_global.yaml": "defaults:\n  - a@_global_: one\n  - _self_\n",
    "rebind_two_ways.yaml": ("defaults:\n  - a@first: one\n  - a@second: two\n  - _self_\n"),
    # An interpolated group name references an *earlier group choice* -- `a`
    # takes whatever option `b` took. It cannot reach a value in the primary
    # config's own body: that fails in hydra too, which the second root pins.
    "interpolated.yaml": "defaults:\n  - b: two\n  - a: ${b}\n  - _self_\n",
    "interpolated_body.yaml": "defaults:\n  - _self_\n  - a: ${which}\nwhich: two\n",
    # a chain three deep
    "chain.yaml": "defaults:\n  - composite: pulls_a_two\n  - _self_\n",
    # headers that move things around
    "headers.yaml": "defaults:\n  - pkg: named\n  - _self_\n",
    "headers_here.yaml": "defaults:\n  - pkg: here\n  - _self_\n",
    "headers_global.yaml": "defaults:\n  - pkg: glob\n  - _self_\n",
    "loose_root.yaml": "defaults:\n  - loose: global\n  - _self_\n",
    # several groups, to pin ordering
    "ordered.yaml": (
        "defaults:\n  - a: one\n  - b: one\n  - c: one\n  - _self_\norder: root\n"
    ),
    # error shapes
    "dup_group.yaml": "defaults:\n  - a: one\n  - a: two\n  - _self_\n",
    "missing_option.yaml": "defaults:\n  - a: nope\n  - _self_\n",
    "missing_group.yaml": "defaults:\n  - nosuch: x\n  - _self_\n",
    "override_absent.yaml": "defaults:\n  - override a: one\n  - _self_\n",
}

#: (config_name, overrides)
CASES: List[Tuple[str, List[str]]] = [
    # `_self_` placement
    ("self_last", []),
    ("self_first", []),
    ("self_middle", []),
    ("self_absent", []),
    # override semantics
    ("override_nested", []),
    ("override_twice", []),
    ("override_absent", []),
    # from the command line
    ("self_last", ["a=two"]),
    ("override_nested", ["a=one"]),
    ("chain", ["a=one"]),
    ("ordered", ["a=two", "b=two"]),
    # deletion, from the command line
    ("delete_target", ["~b"]),
    ("delete_target", ["~b=one"]),
    ("delete_target", ["~b=two"]),  # wrong value: should fail
    ("delete_target", ["~nosuch"]),
    # optional and null
    ("optional_present", []),
    ("optional_missing", []),
    ("optional_group_missing", []),
    ("null_group", []),
    ("self_last", ["a=null"]),
    # package rebinding
    ("rebind_named", []),
    ("rebind_global", []),
    ("rebind_two_ways", []),
    ("headers", []),
    ("headers_here", []),
    ("headers_global", []),
    ("loose_root", []),
    ("rebind_named", ["a@renamed=two"]),
    # interpolated group name
    ("interpolated", []),
    ("interpolated", ["b=one"]),
    ("interpolated_body", []),
    # adding and removing groups from the command line
    ("self_last", ["+b=one"]),
    ("self_last", ["+c=one"]),
    ("ordered", ["~c"]),
    # errors
    ("dup_group", []),
    ("missing_option", []),
    ("missing_group", []),
    ("self_last", ["a=nope"]),
    ("self_last", ["nosuch=x"]),
    ("self_last", ["+a=two"]),  # already present: should fail
]


def build() -> str:
    root = tempfile.mkdtemp(prefix="hf-defaults-")
    for name, body in TREE.items():
        path = os.path.join(root, name)
        os.makedirs(os.path.dirname(path) or root, exist_ok=True)
        with open(path, "w") as handle:
            handle.write(body)
    return root


def outcome(engine: str, root: str, name: str, overrides: List[str]) -> Tuple[Any, ...]:
    """``("ok", container)`` or ``("raise", type, first line)``.

    Warnings count as part of the outcome: hydra warns about a primary config
    whose defaults list has content but no ``_self_``, and a port that quietly
    dropped that warning would compare equal here without it.
    """
    if engine == "hydra-fast":
        from hydra_fast import OmegaConf, compose, initialize_config_dir
    else:
        from hydra import compose, initialize_config_dir
        from omegaconf import OmegaConf

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            with initialize_config_dir(version_base=None, config_dir=root):
                cfg = compose(config_name=name, overrides=overrides)
            result: Tuple[Any, ...] = ("ok", OmegaConf.to_container(cfg, resolve=True))
        except Exception as exc:  # noqa: BLE001
            result = ("raise", type(exc).__name__, str(exc).splitlines()[0])
    emitted = tuple(sorted(str(w.message).splitlines()[0] for w in caught))
    return result + (emitted,)


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()

    try:
        import hydra  # noqa: F401
    except ImportError:
        print("hydra-core is not installed; nothing to compare against.")
        return 1

    root = build()
    agree = 0
    diffs = []
    for name, overrides in CASES:
        want = outcome("hydra", root, name, list(overrides))
        got = outcome("hydra-fast", root, name, list(overrides))
        if want == got:
            agree += 1
        else:
            diffs.append((name, overrides, want, got))

    print(f"defaults list: {agree}/{len(CASES)} cases match hydra")
    if diffs:
        print(f"\n{len(diffs)} differences:")
        for name, overrides, want, got in diffs:
            print(f"  {name} {overrides}")
            print(f"    hydra     : {want}")
            print(f"    hydra-fast: {got}")
    return 1 if diffs else 0


if __name__ == "__main__":
    raise SystemExit(main())
