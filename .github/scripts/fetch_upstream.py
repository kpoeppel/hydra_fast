#!/usr/bin/env python3
"""Fetch the upstream source trees the differential checks compare against.

hydra's and omegaconf's own test suites and grammar corpus ship in their
*sdists* but not their wheels, so they are downloaded and extracted into
``.upstream/`` -- the directory :mod:`bench._sources` looks in.

The sdist is pulled straight from the PyPI JSON API and its recorded sha256 is
verified. That avoids ``pip download --no-binary :all:``, which has to generate
each sdist's metadata and so needs a working build environment -- an
irrelevant dependency for what is really just "download and untar".

Versions come from ``bench/_sources.py``, so this cannot drift from what the
checks expect.

    python .github/scripts/fetch_upstream.py
"""

from __future__ import annotations

import hashlib
import json
import sys
import tarfile
import urllib.request
from pathlib import Path
from typing import Tuple

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bench"))

import _sources  # noqa: E402

#: (distribution, version, a file that must exist once extracted)
WANTED = [
    ("hydra-core", _sources.HYDRA_VERSION, "tests/test_overrides_parser.py"),
    ("omegaconf", _sources.OMEGACONF_VERSION, "tests/test_readonly.py"),
    ("omegaconf", _sources.OMEGACONF_VERSION, "tests/test_grammar.py"),
]


def sdist_url(name: str, version: str) -> Tuple[str, str]:
    """``(url, sha256)`` of the sdist for ``name==version``."""
    with urllib.request.urlopen(
        f"https://pypi.org/pypi/{name}/{version}/json", timeout=60
    ) as response:
        meta = json.load(response)
    for entry in meta["urls"]:
        if entry["packagetype"] == "sdist":
            return entry["url"], entry["digests"]["sha256"]
    raise SystemExit(f"no sdist published for {name}=={version}")


def fetch(url: str, expected_sha256: str, into: Path) -> Path:
    archive = into / url.rsplit("/", 1)[-1]
    if not archive.exists():
        with urllib.request.urlopen(url, timeout=120) as response:
            archive.write_bytes(response.read())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != expected_sha256:
        archive.unlink()
        raise SystemExit(f"sha256 mismatch for {archive.name}: {digest} != {expected_sha256}")
    return archive


def extract(archive: Path, into: Path) -> None:
    destination = into.resolve()
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            # Refuse any path that would escape the destination.
            if not str((into / member.name).resolve()).startswith(str(destination)):
                raise SystemExit(f"{archive.name} holds an unsafe path: {member.name}")
        # `filter=` arrived in 3.12 and warns when omitted; "data" is the
        # conservative setting, and the default from 3.14 on.
        if sys.version_info >= (3, 12):
            tar.extractall(into, filter="data")
        else:
            tar.extractall(into)


def main() -> int:
    into = _sources.UPSTREAM_DIR
    into.mkdir(parents=True, exist_ok=True)

    for name, version in sorted({(n, v) for n, v, _ in WANTED}):
        url, sha256 = sdist_url(name, version)
        print(f"fetching {name}=={version}")
        extract(fetch(url, sha256, into), into)

    # Fail loudly rather than letting the suite skip: a broken download must
    # not read as "nothing to check".
    missing = [
        f"{name}-{version}/{marker}"
        for name, version, marker in WANTED
        if not (into / f"{name}-{version}" / marker).is_file()
    ]
    if missing:
        print(f"::error::missing after extraction: {missing}", file=sys.stderr)
        for path in sorted(into.iterdir()):
            print(f"  {path.name}", file=sys.stderr)
        return 1

    print("upstream trees ready:")
    for path in sorted(p for p in into.iterdir() if p.is_dir()):
        print(f"  {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
