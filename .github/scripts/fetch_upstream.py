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
import inspect
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
    # A literal https URL; the scheme cannot be influenced here.
    with urllib.request.urlopen(  # nosec B310
        f"https://pypi.org/pypi/{name}/{version}/json", timeout=60
    ) as response:
        meta = json.load(response)
    for entry in meta["urls"]:
        if entry["packagetype"] == "sdist":
            return entry["url"], entry["digests"]["sha256"]
    raise SystemExit(f"no sdist published for {name}=={version}")


def _https(url: str) -> str:
    """Reject anything but https -- the API response should not be trusted
    blindly to name a scheme, and `urlopen` would happily take `file:`."""
    if not url.startswith("https://"):
        raise SystemExit(f"refusing non-https URL: {url}")
    return url


def fetch(url: str, expected_sha256: str, into: Path) -> Path:
    archive = into / url.rsplit("/", 1)[-1]
    if not archive.exists():
        with urllib.request.urlopen(_https(url), timeout=120) as response:  # nosec B310
            archive.write_bytes(response.read())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != expected_sha256:
        archive.unlink()
        raise SystemExit(f"sha256 mismatch for {archive.name}: {digest} != {expected_sha256}")
    return archive


#: Whether `TarFile.extractall` accepts `filter=`. Added in 3.12 and
#: backported to 3.10.12 / 3.11.4 for CVE-2007-4559, so feature-detect rather
#: than compare version tuples.
_HAS_EXTRACT_FILTER = "filter" in inspect.signature(tarfile.TarFile.extractall).parameters


def extract(archive: Path, into: Path) -> None:
    """Extract ``archive`` into ``into``, refusing anything that escapes it."""
    destination = into.resolve()
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            # A path that resolves outside the destination -- `../` traversal.
            target = (into / member.name).resolve()
            if not str(target).startswith(str(destination)):
                raise SystemExit(f"{archive.name} holds an unsafe path: {member.name}")
            # A link can escape even when its own path does not: extract a
            # symlink pointing outside, then a later member writes through it.
            # The path check above cannot see that, so links are refused
            # outright -- an sdist has no business containing them.
            if member.issym() or member.islnk():
                raise SystemExit(f"{archive.name} holds a link member: {member.name}")
            if not (member.isfile() or member.isdir()):
                raise SystemExit(f"{archive.name} holds a special file: {member.name}")
        if _HAS_EXTRACT_FILTER:
            # "data" is the conservative policy and the default from 3.14 on;
            # it re-checks the above and strips ownership and permission bits.
            tar.extractall(into, filter="data")
        else:
            tar.extractall(into)  # nosec B202


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
