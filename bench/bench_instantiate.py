"""Does supporting ``_target_`` cost anything when it is not used?

``instantiate`` runs *after* composition and reads ``_target_`` as an ordinary
string key, so in principle a sweep that never calls it should be unaffected
even if every config in the tree carries one. This measures that rather than
asserting it, three ways:

1. composing a tree with no ``_target_`` keys at all;
2. composing the same tree with a ``_target_`` in every node -- the cost of
   *supporting* the protocol;
3. composing and then instantiating -- the cost of *using* it.

    python bench/bench_instantiate.py
"""

from __future__ import annotations

import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, List

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE.parent / "tests"))

POINTS = 100
GROUPS, OPTIONS = 6, 4


def build_tree(with_target: bool) -> str:
    """A tree of the usual shape, optionally with a `_target_` in every node."""
    root = tempfile.mkdtemp(prefix="hf-inst-")
    target = "_target_: instantiate_targets.Plain\n" if with_target else ""
    body = "\n".join(f"a{index}: {index}" for index in range(200))
    with open(os.path.join(root, "config.yaml"), "w") as handle:
        handle.write(
            "defaults:\n"
            + "".join(f"  - g{g}: o0\n" for g in range(GROUPS))
            + "  - _self_\n"
            + body
            + "\n"
        )
    for group in range(GROUPS):
        os.makedirs(os.path.join(root, f"g{group}"), exist_ok=True)
        for option in range(OPTIONS):
            with open(os.path.join(root, f"g{group}/o{option}.yaml"), "w") as handle:
                handle.write(f"# @package g{group}\n{target}a: {option}\nb: ${{a1}}\n")
    return root


def sweep(root: str, instantiate_after: bool) -> float:
    """Best-of-5 seconds per point."""
    from hydra_fast import OmegaConf, clear_caches, compose, initialize_config_dir
    from hydra_fast import instantiate as hf_instantiate

    points = [
        [f"++a{index % 200}={index}", f"g0=o{index % OPTIONS}"] for index in range(POINTS)
    ]
    best = None
    for _ in range(5):
        clear_caches()
        start = time.perf_counter()
        for overrides in points:
            with initialize_config_dir(version_base=None, config_dir=root):
                cfg = compose(config_name="config", overrides=overrides)
            resolved = OmegaConf.to_container(cfg, resolve=True)
            if instantiate_after:
                hf_instantiate(cfg)
            del resolved
        elapsed = time.perf_counter() - start
        best = elapsed if best is None else min(best, elapsed)
    assert best is not None
    return best / POINTS * 1000


def main() -> int:
    plain = build_tree(with_target=False)
    targeted = build_tree(with_target=True)

    rows: List[Any] = []
    measure: List[tuple[str, Callable[[], float]]] = [
        ("no _target_ anywhere", lambda: sweep(plain, False)),
        ("_target_ in every node, not used", lambda: sweep(targeted, False)),
        ("_target_ in every node, instantiated", lambda: sweep(targeted, True)),
    ]
    # Interleaved across rounds so machine drift hits every row equally.
    samples: dict[str, List[float]] = {label: [] for label, _ in measure}
    for _ in range(3):
        for label, run in measure:
            samples[label].append(run())

    print(f"{POINTS} sweep points, {GROUPS} groups x {OPTIONS} options, 200 base keys")
    print(f"{'':38s} {'ms/point':>10s}")
    baseline = None
    for label, _ in measure:
        best = min(samples[label])
        if baseline is None:
            baseline = best
        rows.append((label, best, best / baseline))
        print(f"  {label:36s} {best:8.3f}   {best / baseline:5.2f}x baseline")

    supporting = rows[1][2]
    print(
        f"\nSupporting the protocol costs {(supporting - 1) * 100:+.1f}% on a sweep that "
        "never calls instantiate."
    )
    print(
        f"Using it costs {(rows[2][2] - 1) * 100:+.1f}%, proportional to the objects built "
        "rather than to the sweep."
    )
    medians = [round(statistics.median(samples[label]), 3) for label, _ in measure]
    print(f"\n(median of 3 rounds, best-of-5 each: {medians})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
