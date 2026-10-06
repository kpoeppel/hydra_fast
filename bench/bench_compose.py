"""Benchmark: compose a config tree once per sweep point, resolve it, repeat.

This mirrors what a staged sweep does -- one ``compose()`` per point, each with
a handful of ``++key=value`` overrides, followed by a full resolve. Run it
against stock hydra+omegaconf to get the baseline, then against hydra_fast.

    python bench/bench_compose.py --points 100
    python bench/bench_compose.py --points 100 --impl fast
    python bench/bench_compose.py --points 100 --profile
"""

from __future__ import annotations

import argparse
import cProfile
import os
import pstats
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "src"))

import _sources  # noqa: E402

# The real-world config tree this project is being optimized for.
AUTOEXP = _sources.autoexp_src()


def sweep_overrides(n: int) -> list[list[str]]:
    """Sweep points: group selections plus per-point value overrides."""
    lrs = [1e-4, 2.5e-4, 5e-4]
    batch = [32, 64, 128]
    stages = ["stable", "decay6B", "decay12B", "decay30B", "decay50B"]
    points = []
    i = 0
    while len(points) < n:
        lr = lrs[i % len(lrs)]
        bs = batch[(i // len(lrs)) % len(batch)]
        stage = stages[(i // (len(lrs) * len(batch))) % len(stages)]
        points.append(
            [
                "slurm=juwels",
                "container=juwels",
                "job=default",
                f"++index={i}",
                f"++stage={stage}",
                f"++backend.megatron.lr={lr}",
                f"++backend.megatron.global_batch_size={bs}",
                f"++backend.megatron.train_iters={1000 + i}",
                f"++job.name=bench_{stage}_lr{lr}_bs{bs}",
                "++backend.megatron.aux.tokens=50000000000",
            ]
        )
        i += 1
    return points


# --------------------------------------------------------------------------
# stock hydra + omegaconf
# --------------------------------------------------------------------------
def run_hydra(points: list[list[str]], config_dir: Path, config_name: str) -> list:
    from hydra import compose, initialize_config_dir
    from omegaconf import OmegaConf

    results = []
    for ov in points:
        with initialize_config_dir(version_base=None, config_dir=str(config_dir)):
            cfg = compose(config_name=config_name, overrides=ov)
            results.append(OmegaConf.to_container(cfg, resolve=True))
    return results


def run_hydra_cached(points: list[list[str]], config_dir: Path, config_name: str) -> list:
    """Stock hydra with oellm-autoexp's own monkeypatch cache layer enabled."""
    sys.path.insert(0, str(AUTOEXP))
    os.environ["HYDRA_STAGED_SWEEP_CACHE"] = "1"
    from oellm_autoexp.hydra_staged_sweep.config import cache

    cache.enable()
    return run_hydra(points, config_dir, config_name)


# --------------------------------------------------------------------------
# hydra_fast
# --------------------------------------------------------------------------
def run_fast(points: list[list[str]], config_dir: Path, config_name: str) -> list:
    from hydra_fast import OmegaConf, compose, initialize_config_dir

    results = []
    for ov in points:
        with initialize_config_dir(version_base=None, config_dir=str(config_dir)):
            cfg = compose(config_name=config_name, overrides=ov)
            results.append(OmegaConf.to_container(cfg, resolve=True))
    return results


IMPLS = {"hydra": run_hydra, "hydra-cached": run_hydra_cached, "fast": run_fast}


def register_resolvers(impl: str) -> None:
    """Register oellm-autoexp's custom resolvers against the chosen backend."""
    sys.path.insert(0, str(AUTOEXP))
    if impl != "hydra-cached":
        # autoexp installs its monkeypatch cache from a module import side
        # effect; keep it out of the stock baseline.
        os.environ["HYDRA_STAGED_SWEEP_CACHE"] = "0"
    if impl == "fast":
        # hydra_fast ships an omegaconf-compatible shim; point the module at it
        # so register_default_resolvers() lands in the fast registry.
        import hydra_fast.compat.omegaconf_shim as shim

        shim.install()
    from oellm_autoexp.hydra_staged_sweep.config.resolvers import register_default_resolvers

    register_default_resolvers()

    # The megatron backend registers these at import time, but importing it
    # pulls in torch. Register them directly instead.
    from oellm_autoexp.argparse_schema.resolver import register_argparse_resolver
    from oellm_autoexp.backends.megatron.cli_metadata import (
        MEGATRON_ACTION_SPECS,
        MEGATRON_ARG_METADATA,
    )

    register_argparse_resolver(
        "argsmegatron",
        arg_metadata=dict(MEGATRON_ARG_METADATA),
        action_specs=dict(MEGATRON_ACTION_SPECS),
        skip_defaults=True,
    )
    register_argparse_resolver("cliargs")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--points", type=int, default=100)
    ap.add_argument("--impl", choices=sorted(IMPLS), default="hydra")
    ap.add_argument("--config-dir", default=str(AUTOEXP / "config"))
    ap.add_argument("--config-name", default="autoexp")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("--profile-rows", type=int, default=35)
    args = ap.parse_args()

    config_dir = Path(args.config_dir).resolve()
    if not config_dir.is_dir():
        print(f"config dir not found: {config_dir}", file=sys.stderr)
        return 2

    os.environ.setdefault("OUTPUT_DIR", "./output")
    os.environ.setdefault("PROJECT_DIR", ".")
    os.environ.setdefault("HF_HOME", "/tmp/hf")
    os.environ.setdefault("SLURM_ACCOUNT", "bench")
    os.environ.setdefault("SLURM_PARTITION", "bench")

    register_resolvers(args.impl)
    runner = IMPLS[args.impl]
    points = sweep_overrides(args.points)

    # warm-up (imports, module init) is excluded from the measurement
    runner(points[:1], config_dir, args.config_name)

    if args.profile:
        prof = cProfile.Profile()
        prof.enable()
        runner(points, config_dir, args.config_name)
        prof.disable()
        pstats.Stats(prof).sort_stats("cumulative").print_stats(args.profile_rows)
        return 0

    best = None
    for _ in range(args.repeat):
        t0 = time.perf_counter()
        out = runner(points, config_dir, args.config_name)
        dt = time.perf_counter() - t0
        best = dt if best is None else min(best, dt)
        assert len(out) == args.points

    print(
        f"impl={args.impl:13s} points={args.points:5d} "
        f"total={best * 1e3:9.1f} ms  per_point={best / args.points * 1e3:7.2f} ms"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
