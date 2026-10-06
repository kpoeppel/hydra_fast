"""Test setup.

Two hazards worth knowing about:

1. The repository root holds ``hydra/`` and ``omegaconf/`` source checkouts
   (reference material, not part of the package). With the root on
   ``sys.path`` -- which pytest does by default -- ``import omegaconf`` finds
   that directory as a *namespace package*: the import succeeds but the module
   is empty, so ``pytest.importorskip`` passes and every differential test
   then fails on ``AttributeError``. The root is dropped from ``sys.path``
   below so the installed packages win.

2. Caches are process-wide by design. Each test starts cold, so a stale entry
   cannot make a later test pass.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Drop the repo root (and "") so the reference hydra/omegaconf resolve to the
# installed distributions rather than the vendored source trees.
_root_strings = {str(ROOT), "", "."}
sys.path[:] = [entry for entry in sys.path if entry not in _root_strings]

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "bench"))


def _reference(module_name: str, required_attr: str):
    """Import a reference module, or return None if it is absent/unusable."""
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None
    # guards against the namespace-package shadowing described above
    return module if hasattr(module, required_attr) else None


REFERENCE_OMEGACONF = _reference("omegaconf", "OmegaConf")
REFERENCE_HYDRA = _reference("hydra", "compose")

requires_omegaconf = pytest.mark.skipif(
    REFERENCE_OMEGACONF is None,
    reason="reference omegaconf not installed; differential comparison skipped",
)
requires_hydra = pytest.mark.skipif(
    REFERENCE_HYDRA is None,
    reason="reference hydra not installed; differential comparison skipped",
)


@pytest.fixture(autouse=True)
def _clear_caches():
    import hydra_fast

    hydra_fast.clear_caches()
    yield
    hydra_fast.clear_caches()


def pytest_report_header(config):
    import hydra_fast

    if REFERENCE_HYDRA and REFERENCE_OMEGACONF:
        reference = (
            f"hydra {REFERENCE_HYDRA.__version__}, "
            f"omegaconf {REFERENCE_OMEGACONF.__version__}"
        )
    else:
        reference = "reference hydra/omegaconf unavailable -- differential tests skipped"
    return f"hydra-fast {hydra_fast.__version__}; comparing against {reference}"
