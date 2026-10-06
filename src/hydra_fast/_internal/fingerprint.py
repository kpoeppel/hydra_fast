"""File-identity fingerprints for cache validation.

Under the default ``validation="stat"`` policy, the defaults-list and composed-
config caches revalidate on every lookup against the identity of every file
that could feed them. Done naively that is a ``realpath`` plus a ``stat`` per
(default, search path) pair, twice per composition -- which for a 19-entry
defaults list over 3 search paths is ~230 syscalls, enough to dominate a
composition that otherwise takes a couple of milliseconds.

Two things make it cheap:

* ``realpath`` results are memoized. Resolving ``conf`` + ``db/mysql.yaml`` to
  an absolute path is pure string work plus symlink resolution, and the answer
  does not change while a sweep runs.
* the fingerprint tuple is computed once per composition and shared by both
  caches, via :func:`begin_composition`.

Only the ``stat`` calls remain, and those are what actually detect an edit.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Sequence, Tuple

from .. import _cache

__all__ = [
    "begin_composition",
    "contributing_files",
    "end_composition",
    "path_kind",
    "resolve_path",
]

_realpath_cache = _cache.new_cache("realpath", maxsize=1 << 16)

# Set for the duration of one composition so the defaults-list cache and the
# compose cache do not each walk the tree.
_scope: Dict[Any, Tuple] = {}

# Directory mtimes, re-read once per composition. A directory's mtime changes
# exactly when an entry is added or removed from it, which is exactly when an
# "does this config exist?" answer can change -- editing a file's *contents*
# does not change existence, and content changes are caught separately by the
# read cache. So one stat per directory per composition replaces one stat per
# candidate path per query.
_dir_tokens: Dict[str, Any] = {}
_path_kinds = _cache.new_cache("path_kind", maxsize=1 << 16)


def begin_composition() -> None:
    _scope.clear()
    if _cache.get_validation() != "never":
        # Under "never" the tokens stay valid for the life of the process.
        _dir_tokens.clear()


def end_composition() -> None:
    _scope.clear()


def resolve_path(source_path: str, config_path: str) -> str:
    """``realpath(join(source_path, config_path))``, memoized.

    Symlink resolution is not free and the answer does not change while a
    sweep runs.
    """
    key = (source_path, config_path)
    try:
        return _realpath_cache[key]  # type: ignore[return-value]
    except KeyError:
        pass
    full = os.path.realpath(os.path.join(source_path, config_path))
    _realpath_cache[key] = full
    return full


def _dir_token(dirname: str) -> Any:
    try:
        return _dir_tokens[dirname]
    except KeyError:
        pass
    try:
        token: Any = os.stat(dirname).st_mtime_ns
    except OSError:
        token = None
    _dir_tokens[dirname] = token
    return token


def path_kind(full_path: str) -> Optional[str]:
    """``"file"``, ``"dir"`` or ``None``, cached against the parent's mtime."""
    dirname = os.path.dirname(full_path)
    key = (full_path, _dir_token(dirname))
    try:
        kind = _path_kinds[key]
    except KeyError:
        pass
    else:
        _cache._counters["path_kind_hit"] += 1
        return kind

    _cache._counters["path_kind_miss"] += 1
    try:
        mode = os.stat(full_path).st_mode
    except OSError:
        kind = None
    else:
        import stat as _stat

        kind = "dir" if _stat.S_ISDIR(mode) else "file" if _stat.S_ISREG(mode) else None
    _path_kinds[key] = kind
    return kind


def _resolved_config_path(source_path: str, config_path: str, normalize: Any) -> Optional[str]:
    try:
        normalized = normalize(config_path)
    except Exception:  # noqa: BLE001 - a name with a bad extension has no file
        return None
    return resolve_path(source_path, normalized)


def contributing_files(defaults: Sequence[Any], repo: Any) -> Tuple:
    """Identity of every file that could feed this composition.

    ``()`` under ``validation="never"`` -- there is nothing to revalidate, and
    skipping this is most of why that mode is faster.
    """
    if _cache.get_validation() == "never":
        return ()

    paths = tuple(
        default.config_path
        for default in defaults
        if getattr(default, "config_path", None) is not None
    )
    sources = [s for s in repo.get_sources() if s.scheme() == "file"]
    key = (paths, tuple(s.path for s in sources))
    cached = _scope.get(key)
    if cached is not None:
        return cached

    found = set()
    for config_path in paths:
        for source in sources:
            full = _resolved_config_path(source.path, config_path, source._normalize_file_name)
            if full is None:
                continue
            try:
                stat = os.stat(full)
            except OSError:
                continue
            found.add((full, stat.st_mtime_ns, stat.st_size, stat.st_ino))
    result = tuple(sorted(found))
    _scope[key] = result
    return result
