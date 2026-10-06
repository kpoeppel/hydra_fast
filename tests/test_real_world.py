"""Differential check against a real production config tree.

Uses the OpenEuroLLM ``oellm-autoexp`` configs -- 239 files, deep group
nesting, ~30 custom resolvers, the workload hydra-fast was built for. Skipped
unless the tree is available; point ``OELLM_AUTOEXP`` at a checkout:

    git clone --depth 1 https://github.com/OpenEuroLLM/oellm-autoexp
    OELLM_AUTOEXP=$PWD/oellm-autoexp pytest tests/test_real_world.py

Each side runs in a subprocess: the autoexp modules bind
``from omegaconf import OmegaConf`` at import time, so the shim has to be
installed before they load and the two backends cannot share a process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import _sources  # noqa: E402  (bench/ is on sys.path via conftest)

AUTOEXP = _sources.autoexp_src()

from conftest import requires_hydra


def _autoexp_importable() -> bool:
    """Is the config tree's own package importable?

    The tree registers ~30 custom resolvers from `oellm_autoexp`, which pulls
    in third-party dependencies. Without them there is nothing to compare, so
    skip rather than report a failure that is about the environment.
    """
    if AUTOEXP is None or not (AUTOEXP / "config").is_dir():
        return False
    import importlib.util
    import sys

    saved = list(sys.path)
    sys.path.insert(0, str(AUTOEXP))
    try:
        for module in (
            "oellm_autoexp.hydra_staged_sweep.config.resolvers",
            "oellm_autoexp.backends.megatron.cli_metadata",
        ):
            try:
                importlib.import_module(module)
            except Exception:  # noqa: BLE001 - any import failure means skip
                return False
        return True
    finally:
        sys.path[:] = saved


pytestmark = [
    requires_hydra,
    pytest.mark.skipif(
        not _autoexp_importable(),
        reason="oellm-autoexp tree or its dependencies unavailable (set OELLM_AUTOEXP)",
    ),
]

BENCH = Path(__file__).resolve().parent.parent / "bench" / "check_compose.py"

OVERRIDE_SETS = [
    ["slurm=juwels", "container=juwels"],
    ["slurm=juwels", "container=juwels", "++index=3"],
    ["slurm=leonardo", "container=leonardo"],
    ["slurm=lumi", "container=lumi"],
    ["slurm=base", "container=none"],
    ["slurm=juwels", "container=juwels", "job=auto_restart"],
    ["slurm=juwels", "container=juwels", "++backend.megatron.lr=0.001"],
    ["slurm=juwels", "container=juwels", "++backend.megatron.global_batch_size=256"],
    ["slurm=juwels", "container=juwels", "backend=megatron_torchdist"],
    ["slurm=juwels", "container=juwels", "postprocess=eval_opensci"],
    ["slurm=snellius", "container=snellius"],
    ["slurm=marenostrum", "container=marenostrum"],
]


def _env():
    env = dict(os.environ)
    env.setdefault("OUTPUT_DIR", "./output")
    env.setdefault("PROJECT_DIR", ".")
    env.setdefault("HF_HOME", "/tmp/hf")
    env.setdefault("SLURM_ACCOUNT", "test")
    env.setdefault("SLURM_PARTITION", "test")
    env["OELLM_AUTOEXP"] = str(AUTOEXP)
    env["HYDRA_STAGED_SWEEP_CACHE"] = "0"
    return env


def _compose(impl: str, overrides):
    proc = subprocess.run(
        [
            sys.executable,
            str(BENCH),
            "--worker",
            impl,
            "--config-dir",
            str(AUTOEXP / "config"),
            "--config-name",
            "autoexp",
            "--overrides",
            *overrides,
        ],
        capture_output=True,
        text=True,
        cwd=str(Path.home()),
        env=_env(),
    )
    out = proc.stdout.strip().splitlines()
    if not out:
        pytest.fail(f"{impl} produced no output:\n{proc.stderr[-2000:]}")
    return json.loads(out[-1])


@pytest.mark.parametrize("overrides", OVERRIDE_SETS, ids=lambda o: "_".join(o)[:60])
def test_matches_hydra_on_real_tree(overrides):
    expected = _compose("hydra", overrides)
    got = _compose("fast", overrides)

    if "__error__" in expected or "__error__" in got:
        assert "__error__" in expected and "__error__" in got, (
            f"hydra={expected.get('__error__', 'ok')} "
            f"fast={got.get('__error__', 'ok')}: {got.get('msg', '')}"
        )
        return
    assert expected["__ok__"] == got["__ok__"]
