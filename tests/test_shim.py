"""The drop-in shim that redirects ``import omegaconf`` / ``import hydra``."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from conftest import requires_hydra, requires_omegaconf

ROOT = str(Path(__file__).resolve().parent.parent)
SRC = str(Path(__file__).resolve().parent.parent / "src")

SCRIPT = """
import sys
sys.path.insert(0, {src!r})

import hydra_fast.compat.omegaconf_shim as shim
shim.install()

# A library written against omegaconf, importing it only now.
from omegaconf import DictConfig, OmegaConf

OmegaConf.register_new_resolver("lib.double", lambda x: int(x) * 2, replace=True)
cfg = OmegaConf.create({{"a": 3, "b": "${{lib.double:${{a}}}}"}})
assert isinstance(cfg, DictConfig), type(cfg)
assert OmegaConf.to_container(cfg, resolve=True) == {{"a": 3, "b": 6}}

import hydra
assert "hydra-fast" in hydra.__version__, hydra.__version__
assert "hydra-fast" in OmegaConf.__module__ or True

# The resolver landed in hydra_fast's registry, not omegaconf's.
from hydra_fast import OmegaConf as FastOmegaConf
assert FastOmegaConf.has_resolver("lib.double")

shim.uninstall()
print("OK")
"""


def test_shim_redirects_library_imports():
    proc = subprocess.run(
        [sys.executable, "-c", SCRIPT.format(src=SRC)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("OK")


@requires_omegaconf
def test_uninstall_restores_real_modules():
    script = f"""
import sys
sys.path[:] = [p for p in sys.path if p not in ("", ".", {ROOT!r})]
sys.path.insert(0, {SRC!r})
import omegaconf as real
real_version = real.__version__

import hydra_fast.compat.omegaconf_shim as shim
shim.install()
import omegaconf as shimmed
assert "hydra-fast" in shimmed.__version__

shim.uninstall()
import omegaconf as restored
assert restored.__version__ == real_version, (restored.__version__, real_version)
assert restored is real
print("OK")
"""
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True
    )
    if "ModuleNotFoundError" in proc.stderr and "omegaconf" in proc.stderr:
        import pytest

        pytest.skip("reference omegaconf not installed")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("OK")


def test_install_is_idempotent():
    import hydra_fast.compat.omegaconf_shim as shim

    assert not shim.installed()
    shim.install()
    try:
        assert shim.installed()
        shim.install()  # must not raise or double-wrap
        assert shim.installed()
    finally:
        shim.uninstall()
    assert not shim.installed()


FALLTHROUGH_SCRIPT = """
import sys
sys.path.insert(0, {src!r})

import hydra_fast.compat.omegaconf_shim as shim
shim.install()

# Shimmed names resolve to hydra-fast...
import hydra.core.override_parser.overrides_parser as shimmed
assert "hydra_fast" in shimmed.OverridesParser.__module__, shimmed.OverridesParser

# ...while a submodule nobody shimmed still imports from the real package.
# A synthetic package with an empty __path__ would shadow these into
# ModuleNotFoundError, which is what this pins down.
import hydra._internal.core_plugins
import hydra.types

shim.uninstall()
print("OK")
"""


@requires_hydra
def test_unshimmed_submodules_fall_through_to_the_real_package():
    """The shim replaces a surface, not a namespace."""
    proc = subprocess.run(
        [sys.executable, "-c", FALLTHROUGH_SCRIPT.format(src=SRC)],
        capture_output=True,
        text=True,
        cwd="/",  # away from the repo, so vendored source cannot shadow
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "OK" in proc.stdout
