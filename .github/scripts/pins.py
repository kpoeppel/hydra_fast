#!/usr/bin/env python3
"""Print the reference-package pins as pip requirement specifiers.

The differential checks compare against specific hydra and omegaconf versions,
declared once in ``bench/_sources.py`` so the fetch script and the installer
cannot disagree. This exists so a workflow can say

    pins=$(python .github/scripts/pins.py)
    python -m pip install -e ".[test]" $pins

instead of embedding a long ``python -c`` one-liner in YAML twice over.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "bench"))

import _sources  # noqa: E402

print(f"hydra-core=={_sources.HYDRA_VERSION}", f"omegaconf=={_sources.OMEGACONF_VERSION}")
