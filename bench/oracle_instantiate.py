"""Differential check of ``instantiate`` against hydra's implementation.

Each case is a config plus call-site args, run through both
``hydra.utils.instantiate`` and ``hydra_fast.utils.instantiate``, comparing
the repr of the result or the (exception type, message) pair.

    pip install hydra-core==1.3.2
    python bench/oracle_instantiate.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE.parent / "tests"))

#: Module holding the targets, importable because tests/ is on sys.path.
T = "instantiate_targets"

#: (label, config, args, kwargs)
CASES: List[Tuple[str, Any, Tuple[Any, ...], Dict[str, Any]]] = [
    # --- the basics -------------------------------------------------------
    ("plain", {"_target_": f"{T}.Plain"}, (), {}),
    ("plain-kwargs", {"_target_": f"{T}.Plain", "a": 5, "b": "z"}, (), {}),
    ("override-at-call", {"_target_": f"{T}.Plain", "a": 5}, (), {"a": 9}),
    ("function-target", {"_target_": f"{T}.make", "a": 3}, (), {}),
    ("no-target-dict", {"a": 1, "b": {"c": 2}}, (), {}),
    ("none-config", None, (), {}),
    ("primitive-config", 5, (), {}),
    # --- positional -------------------------------------------------------
    ("args", {"_target_": f"{T}.Positional", "_args_": [1, 2]}, (), {}),
    ("args-plus-call", {"_target_": f"{T}.Positional", "_args_": [1]}, (2, 3), {}),
    ("args-and-kwargs", {"_target_": f"{T}.Positional", "_args_": [1], "k": 2}, (), {}),
    ("call-args-only", {"_target_": f"{T}.Positional"}, (7,), {}),
    # --- recursion --------------------------------------------------------
    (
        "nested",
        {"_target_": f"{T}.Nested", "inner": {"_target_": f"{T}.Plain", "a": 2}},
        (),
        {},
    ),
    (
        "nested-non-recursive",
        {
            "_target_": f"{T}.Nested",
            "_recursive_": False,
            "inner": {"_target_": f"{T}.Plain", "a": 2},
        },
        (),
        {},
    ),
    (
        "nested-recursive-off-at-call",
        {"_target_": f"{T}.Nested", "inner": {"_target_": f"{T}.Plain"}},
        (),
        {"_recursive_": False},
    ),
    (
        "list-of-targets",
        {"items": [{"_target_": f"{T}.Plain", "a": 1}, {"_target_": f"{T}.Plain", "a": 2}]},
        (),
        {},
    ),
    (
        "target-in-list-arg",
        {"_target_": f"{T}.Positional", "_args_": [{"_target_": f"{T}.Plain"}]},
        (),
        {},
    ),
    ("bare-list", [{"_target_": f"{T}.Plain", "a": 4}], (), {}),
    # --- convert ----------------------------------------------------------
    *[
        (
            f"convert-{mode}",
            {
                "_target_": f"{T}.HoldsContainers",
                "_convert_": mode,
                "mapping": {"k": 1},
                "sequence": [1, 2],
            },
            (),
            {},
        )
        for mode in ("none", "partial", "object", "all")
    ],
    (
        "convert-propagates-to-nested",
        {
            "_target_": f"{T}.Nested",
            "_convert_": "all",
            "inner": {"_target_": f"{T}.HoldsContainers", "mapping": {"k": 1}},
        },
        (),
        {},
    ),
    # --- partial ----------------------------------------------------------
    ("partial", {"_target_": f"{T}.Plain", "_partial_": True, "a": 3}, (), {}),
    ("partial-at-call", {"_target_": f"{T}.Plain", "a": 3}, (), {"_partial_": True}),
    (
        "partial-nested",
        {
            "_target_": f"{T}.Nested",
            "inner": {"_target_": f"{T}.Plain", "_partial_": True},
        },
        (),
        {},
    ),
    # --- interpolation ----------------------------------------------------
    ("interpolation", {"base": 7, "obj": {"_target_": f"{T}.Plain", "a": "${base}"}}, (), {}),
    # --- errors -----------------------------------------------------------
    ("bad-module", {"_target_": "nosuchmodule.Thing"}, (), {}),
    ("bad-attribute", {"_target_": f"{T}.NoSuchName"}, (), {}),
    ("empty-target", {"_target_": ""}, (), {}),
    ("relative-target", {"_target_": ".Plain"}, (), {}),
    ("non-string-target", {"_target_": 5}, (), {}),
    ("target-raises", {"_target_": f"{T}.Raises"}, (), {}),
    ("unexpected-kwarg", {"_target_": f"{T}.Plain", "nope": 1}, (), {}),
    ("constant-as-class", {"_target_": f"{T}.CONSTANT"}, (), {}),
]


def outcome(module: Any, create: Any, config: Any, args: Any, kwargs: Any) -> Tuple[str, str]:
    try:
        built = config if not isinstance(config, (dict, list)) else create(config)
    except Exception as exc:  # noqa: BLE001
        return ("create-raise", f"{type(exc).__name__}")
    try:
        return ("ok", repr(module.instantiate(built, *args, **kwargs)))
    except Exception as exc:  # noqa: BLE001
        return ("raise", f"{type(exc).__name__}")


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()

    try:
        import hydra.utils as real
        from omegaconf import OmegaConf as RealOmegaConf
    except ImportError:
        print("hydra-core is not installed; nothing to compare against.")
        return 1

    import hydra_fast.utils as fast
    from hydra_fast import OmegaConf as FastOmegaConf

    agree = 0
    diffs = []
    for label, config, call_args, call_kwargs in CASES:
        want = outcome(real, RealOmegaConf.create, config, call_args, call_kwargs)
        got = outcome(fast, FastOmegaConf.create, config, call_args, call_kwargs)
        if want == got:
            agree += 1
        else:
            diffs.append((label, want, got))

    print(f"instantiate: {agree}/{len(CASES)} cases match hydra")
    if diffs:
        print(f"\n{len(diffs)} differences:")
        for label, want, got in diffs:
            print(f"  {label}")
            print(f"    hydra     : {want[0]}: {want[1]}")
            print(f"    hydra-fast: {got[0]}: {got[1]}")
    return 1 if diffs else 0


if __name__ == "__main__":
    raise SystemExit(main())
