"""Where to find the external source trees the differential checks need.

Several checks compare against material that is *not* vendored into this
repository: upstream's own test suites, and a real production config tree.
Each is located the same way, in order:

1. an explicit environment variable (``HYDRA_SRC``, ``OMEGACONF_SRC``,
   ``OELLM_AUTOEXP``) -- what CI sets;
2. a conventional ``.upstream/<name>`` directory under the repository root,
   which is gitignored -- what the README's setup snippet populates for local
   work;
3. nothing, in which case the dependent checks skip.

Step 3 is a real hazard: a missing tree turns the strongest checks in the
project into silent skips, and a green run then means very little. Setting
``HYDRA_FAST_REQUIRE_UPSTREAM=1`` makes absence an error instead, which is
what CI does so a broken download cannot pass as success.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent

#: Gitignored directory holding locally fetched source trees.
UPSTREAM_DIR = ROOT / ".upstream"

#: Pinned versions the differential checks are written against.
HYDRA_VERSION = "1.3.2"
OMEGACONF_VERSION = "2.3.0"

REQUIRE = os.environ.get("HYDRA_FAST_REQUIRE_UPSTREAM", "").lower() in {"1", "true", "yes"}


def _locate(env_var: str, *, directory: str, marker: str) -> Optional[Path]:
    """The tree named by ``env_var`` or found under ``.upstream/``.

    ``marker`` is a path that must exist inside the tree, so a half-extracted
    or wrong directory is reported as missing rather than used.
    """
    candidates = []
    from_env = os.environ.get(env_var)
    if from_env:
        candidates.append(Path(from_env))
    candidates.append(UPSTREAM_DIR / directory)

    for candidate in candidates:
        if (candidate / marker).exists():
            return candidate
    return None


def hydra_src() -> Optional[Path]:
    """Hydra's source checkout, for running its own test suite verbatim."""
    return _locate(
        "HYDRA_SRC",
        directory=f"hydra-core-{HYDRA_VERSION}",
        marker="tests/test_overrides_parser.py",
    )


def omegaconf_src() -> Optional[Path]:
    """OmegaConf's source checkout, for its grammar corpus and suites."""
    return _locate(
        "OMEGACONF_SRC",
        directory=f"omegaconf-{OMEGACONF_VERSION}",
        marker="tests/test_readonly.py",
    )


def autoexp_src() -> Optional[Path]:
    """The oellm-autoexp config tree -- the real-world composition case."""
    return _locate("OELLM_AUTOEXP", directory="oellm-autoexp", marker="config")


def missing_reason(what: str, env_var: str) -> str:
    """Skip reason for an absent tree, naming how to provide it."""
    return (
        f"{what} source tree not available: set {env_var}, or populate "
        f"{UPSTREAM_DIR.name}/ (see README, 'Running the differential tests')"
    )


def require(tree: Optional[Path], what: str, env_var: str) -> Optional[Path]:
    """Return ``tree``, or raise when ``HYDRA_FAST_REQUIRE_UPSTREAM`` is set.

    Lets CI insist that a check actually ran instead of accepting a skip.
    """
    if tree is None and REQUIRE:
        raise RuntimeError(
            f"HYDRA_FAST_REQUIRE_UPSTREAM is set but {missing_reason(what, env_var)}"
        )
    return tree
