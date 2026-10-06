"""Self-contained sweep benchmark: generates its own config tree.

Anyone can reproduce the README numbers with this -- no private config tree
required. The generated tree is modelled on a real ML training config: a few
config groups, a large base config, and interpolations that chain.

    python bench/bench_synthetic.py
    python bench/bench_synthetic.py --points 200 --keys 600
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))


def build_tree(root: Path, keys: int, groups: int, options: int) -> Path:
    """A config tree with `groups` groups of `options` options each."""
    conf = root / "conf"
    conf.mkdir(parents=True, exist_ok=True)

    defaults = "\n".join(f"  - group{g}: opt0" for g in range(groups))
    # A large base config whose values interpolate off each other, which is
    # what makes resolution cost real rather than trivial.
    base_lines = []
    for index in range(keys):
        if index % 5 == 0:
            base_lines.append(f"  key{index}: {index}")
        elif index % 5 == 1:
            base_lines.append(f"  key{index}: ${{base.key{index - 1}}}")
        elif index % 5 == 2:
            base_lines.append(f'  key{index}: "prefix-${{base.key{index - 1}}}-suffix"')
        elif index % 5 == 3:
            base_lines.append(f"  key{index}: ${{oc.select:base.key{index - 1},0}}")
        else:
            base_lines.append(f'  key{index}: "${{base.key{index - 2}}}/${{base.key{index - 1}}}"')
    base_block = "\n".join(base_lines)

    (conf / "config.yaml").write_text(
        f"""defaults:
{defaults}
  - _self_

index: 0
stage: ""
name: run
out_dir: "outputs/${{name}}/${{stage}}/${{index}}"
base:
{base_block}
summary: "${{base.key0}}-${{base.key1}}-${{base.key2}}"
"""
    )

    for g in range(groups):
        group_dir = conf / f"group{g}"
        group_dir.mkdir(exist_ok=True)
        for o in range(options):
            (group_dir / f"opt{o}.yaml").write_text(
                f"""selected: opt{o}
value: {g * 100 + o}
derived: "${{group{g}.selected}}-${{group{g}.value}}"
nested:
  a: {o}
  b: ${{group{g}.nested.a}}
  c: "${{group{g}.nested.a}}/${{group{g}.value}}"
"""
            )
    return conf


def sweep_overrides(points: int, groups: int, options: int) -> list:
    out = []
    for index in range(points):
        picks = [f"group{g}=opt{(index // (g + 1)) % options}" for g in range(min(groups, 2))]
        out.append(
            picks
            + [
                f"++index={index}",
                f"++stage=stage{index % 4}",
                f"++name=run{index}",
                f"++base.key0={index}",
            ]
        )
    return out


def run_hydra(points, config_dir, config_name):
    from hydra import compose, initialize_config_dir
    from omegaconf import OmegaConf

    results = []
    for overrides in points:
        with initialize_config_dir(version_base=None, config_dir=str(config_dir)):
            cfg = compose(config_name=config_name, overrides=overrides)
            results.append(OmegaConf.to_container(cfg, resolve=True))
    return results


def run_fast(points, config_dir, config_name):
    from hydra_fast import OmegaConf, compose, initialize_config_dir

    results = []
    for overrides in points:
        with initialize_config_dir(version_base=None, config_dir=str(config_dir)):
            cfg = compose(config_name=config_name, overrides=overrides)
            results.append(OmegaConf.to_container(cfg, resolve=True))
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--points", type=int, default=100, help="sweep points to compose")
    ap.add_argument("--keys", type=int, default=400, help="keys in the base config")
    ap.add_argument("--groups", type=int, default=6)
    ap.add_argument("--options", type=int, default=4)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--validation", choices=["stat", "never"], default="stat")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="hydra-fast-bench-"))
    try:
        config_dir = build_tree(tmp, args.keys, args.groups, args.options)
        points = sweep_overrides(args.points, args.groups, args.options)
        files = sum(1 for _ in config_dir.rglob("*.yaml"))
        print(
            f"tree: {files} yaml files, {args.keys} keys in base, "
            f"{args.groups} groups x {args.options} options; {args.points} sweep points"
        )

        results = {}

        # equivalence first: a speed number is meaningless if the output differs
        try:
            import hydra_fast

            hydra_fast.set_validation(args.validation)
            expected = run_hydra(points[:3], config_dir, "config")
            got = run_fast(points[:3], config_dir, "config")
            print("equivalence on first 3 points:", "OK" if expected == got else "MISMATCH")
            if expected != got:
                return 1
            have_hydra = True
        except ImportError:
            print("reference hydra not installed; skipping its measurement")
            have_hydra = False

        for name, runner in (("hydra+omegaconf", run_hydra), ("hydra-fast", run_fast)):
            if name == "hydra+omegaconf" and not have_hydra:
                continue
            import hydra_fast

            hydra_fast.clear_caches()
            runner(points[:1], config_dir, "config")  # warm imports
            best = None
            for _ in range(args.repeat):
                hydra_fast.clear_caches()
                start = time.perf_counter()
                runner(points, config_dir, "config")
                elapsed = time.perf_counter() - start
                best = elapsed if best is None else min(best, elapsed)
            results[name] = best
            print(
                f"  {name:16s} total={best * 1e3:9.1f} ms   "
                f"per_point={best / args.points * 1e3:7.2f} ms"
            )

        if len(results) == 2:
            speedup = results["hydra+omegaconf"] / results["hydra-fast"]
            print(f"\nspeedup: {speedup:.1f}x")

        import hydra_fast

        stats = hydra_fast.cache_stats()
        print(
            f"yaml parses: {stats['load_yaml_miss']} "
            f"(vs {stats['load_yaml_miss'] + stats['load_yaml_hit']} reads requested)"
        )
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
