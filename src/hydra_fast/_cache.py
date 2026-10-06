"""Process-wide caches.

The single biggest cost in a sweep is re-reading the same YAML files once per
sweep point. ``read_text``/``load_yaml`` here are read-through caches keyed by
file identity, so a file touched a thousand times during one process is read
and parsed exactly once.

Validation policy is set by :func:`set_validation`:

``"stat"`` (default)
    Re-``stat()`` on every lookup and invalidate when ``(mtime_ns, size,
    inode)`` changes. Editing a config on disk mid-process is picked up, and a
    ``stat()`` is ~1000x cheaper than a parse.
``"never"``
    Trust the first read for the life of the process. Shaves the ``stat()``
    too; correct for a sweep build, which does not rewrite its own inputs.

**Bounded.** Every cache evicts oldest-first past a limit, so a long-lived
process composing many *distinct* config trees does not grow without end. The
limits are far above any single sweep's working set, so a sweep never evicts.
Tune with :func:`set_max_entries`.

**Thread safety.** Lookups are lock-free on purpose: ``dict.get`` is atomic
under the GIL, and taking a lock on every cache *hit* would tax the hot path
to protect against a case that cannot corrupt anything -- the worst a racing
reader sees is a miss, which is merely slower. Mutations (insert, evict,
clear) do take a lock, because eviction is a read-modify-write and
:func:`clear_all` must not interleave with it. The hit/miss counters are
plain ``+=`` and so are approximate under concurrency; they are diagnostics,
not behaviour.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = [
    "clear_all",
    "load_yaml",
    "memoize",
    "new_cache",
    "read_text",
    "set_max_entries",
    "set_validation",
    "stats",
]

_Fingerprint = Tuple[int, int, int]

_VALIDATION = "stat"
_lock = threading.RLock()

# Defaults chosen to sit far above a single sweep's working set: a config tree
# has tens to hundreds of files, and a sweep has a handful of distinct group
# selections and a few dozen distinct interpolation strings.
_DEFAULT_MAX = 4096
_GRAMMAR_MAX = 1 << 16

# name -> cache
_REGISTRY: Dict[str, "_BoundedCache"] = {}
_counters: Dict[str, int] = {}


class _BoundedCache(dict):
    """A dict that evicts in insertion order once it is full.

    FIFO rather than LRU deliberately: tracking recency means touching the
    cache on every *read*, which is exactly the path that has to stay cheap.
    A sweep's working set is small and stable, so it never reaches the bound;
    the bound exists for the process that keeps meeting new config trees.
    """

    __slots__ = ("maxsize", "name")

    def __init__(self, name: str, maxsize: int) -> None:
        super().__init__()
        self.name = name
        self.maxsize = maxsize

    def __setitem__(self, key: Any, value: Any) -> None:
        with _lock:
            if key not in self and len(self) >= self.maxsize:
                # Drop a batch rather than one entry, so eviction is amortized
                # instead of happening on every subsequent insert.
                for victim in list(self.keys())[: max(1, self.maxsize // 8)]:
                    dict.pop(self, victim, None)
                _counters[self.name + "_evicted"] = (
                    _counters.get(self.name + "_evicted", 0) + 1
                )
            dict.__setitem__(self, key, value)

    def clear(self) -> None:
        with _lock:
            dict.clear(self)


def new_cache(name: str, maxsize: int = _DEFAULT_MAX) -> _BoundedCache:
    """Create a registered, bounded cache. Participates in ``clear_all()``."""
    cache = _BoundedCache(name, maxsize)
    _REGISTRY[name] = cache
    _counters.setdefault(name + "_hit", 0)
    _counters.setdefault(name + "_miss", 0)
    return cache


def set_max_entries(maxsize: int, name: Optional[str] = None) -> None:
    """Raise or lower the eviction bound, for one cache or all of them."""
    if maxsize < 1:
        raise ValueError("maxsize must be at least 1")
    with _lock:
        targets = _REGISTRY.values() if name is None else [_REGISTRY[name]]
        for cache in targets:
            cache.maxsize = maxsize


def set_validation(mode: str) -> None:
    """Choose ``"stat"`` (default) or ``"never"``."""
    global _VALIDATION
    if mode not in ("stat", "never"):
        raise ValueError(f"unknown validation mode: {mode!r}")
    _VALIDATION = mode


def get_validation() -> str:
    return _VALIDATION


def _register(name: str, maxsize: int = _DEFAULT_MAX) -> _BoundedCache:
    return new_cache(name, maxsize)


def stats() -> Dict[str, int]:
    """Hit/miss/eviction counters, plus the live size of each cache."""
    out = dict(_counters)
    for name, cache in _REGISTRY.items():
        out[name + "_size"] = len(cache)
    return out


def reset_stats() -> None:
    for key in list(_counters):
        _counters[key] = 0


def clear_all() -> None:
    """Drop every cached entry. Caches refill on next use."""
    with _lock:
        for cache in _REGISTRY.values():
            cache.clear()
    reset_stats()


def _fingerprint(path: str) -> Optional[_Fingerprint]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


# ---------------------------------------------------------------------------
# file existence / listing
# ---------------------------------------------------------------------------
_exists_cache = _register("exists", _GRAMMAR_MAX)
_listdir_cache = _register("listdir")


def exists(path: str) -> bool:
    """``os.path.exists`` with a cache.

    Only cached under ``validation="never"``; under ``"stat"`` the syscall *is*
    the validation, so caching it would buy nothing and could go stale.
    """
    if _VALIDATION == "never":
        hit = _exists_cache.get(path)
        if hit is not None:
            _counters["exists_hit"] += 1
            return hit
        _counters["exists_miss"] += 1
        result = os.path.exists(path)
        _exists_cache[path] = result
        return result
    return os.path.exists(path)


def listdir(path: str) -> List[str]:
    """``os.listdir`` with a stat-validated cache; ``[]`` if absent."""
    fingerprint = _fingerprint(path) if _VALIDATION == "stat" else None
    entry = _listdir_cache.get(path)
    if entry is not None and (_VALIDATION == "never" or entry[0] == fingerprint):
        _counters["listdir_hit"] += 1
        return entry[1]
    _counters["listdir_miss"] += 1
    try:
        names = sorted(os.listdir(path))
    except OSError:
        names = []
    _listdir_cache[path] = (fingerprint, names)
    return names


# ---------------------------------------------------------------------------
# text reads
# ---------------------------------------------------------------------------
_text_cache = _register("read_text")


def read_text(path: str, encoding: str = "utf-8") -> str:
    """Read a file, once per process (per validation policy)."""
    key = (path, encoding)
    entry = _text_cache.get(key)
    if _VALIDATION == "never":
        if entry is not None:
            _counters["read_text_hit"] += 1
            return entry[1]
    else:
        fingerprint = _fingerprint(path)
        if entry is not None and entry[0] == fingerprint:
            _counters["read_text_hit"] += 1
            return entry[1]

    _counters["read_text_miss"] += 1
    with open(path, encoding=encoding) as handle:
        text = handle.read()
    # Fingerprint *after* the read: if the file changed while we were reading
    # it, the stored fingerprint describes content we may not have, so the next
    # lookup must miss. Re-stat and store that instead.
    final = None if _VALIDATION == "never" else _fingerprint(path)
    _text_cache[key] = (final, text)
    return text


# ---------------------------------------------------------------------------
# parsed YAML
# ---------------------------------------------------------------------------
_yaml_cache = _register("load_yaml")


def load_yaml(path: str, loader: Callable[[str], Any]) -> Any:
    """Parse a YAML file once per process.

    The returned object is shared between callers, so treat it as immutable.
    Composition copies before mutating (see ``_internal.config_source``).
    """
    entry = _yaml_cache.get(path)
    if _VALIDATION == "never":
        if entry is not None:
            _counters["load_yaml_hit"] += 1
            return entry[1]
    else:
        fingerprint = _fingerprint(path)
        if entry is not None and entry[0] == fingerprint:
            _counters["load_yaml_hit"] += 1
            return entry[1]

    _counters["load_yaml_miss"] += 1
    text = read_text(path)
    data = loader(text)
    # Re-stat after parsing, for the same reason read_text does.
    final = None if _VALIDATION == "never" else _fingerprint(path)
    _yaml_cache[path] = (final, data)
    return data


# ---------------------------------------------------------------------------
# generic memo for pure functions of hashable args
# ---------------------------------------------------------------------------
def memoize(name: str, maxsize: int = _GRAMMAR_MAX) -> Callable[[Callable], Callable]:
    """Memo that participates in ``clear_all()`` and evicts past ``maxsize``.

    ``functools.lru_cache`` would do, but those caches are invisible to
    ``clear_all()`` and cannot be sized per call site from here.
    """
    cache = _register(name, maxsize)
    hit_key = name + "_hit"
    miss_key = name + "_miss"

    def decorate(fn: Callable) -> Callable:
        def wrapper(*args: Any) -> Any:
            try:
                value = cache[args]
            except KeyError:
                pass
            except TypeError:  # unhashable arg: bypass the cache
                return fn(*args)
            else:
                _counters[hit_key] += 1
                return value
            _counters[miss_key] += 1
            value = fn(*args)
            cache[args] = value
            return value

        wrapper.__name__ = getattr(fn, "__name__", name)
        wrapper.__doc__ = fn.__doc__
        wrapper.cache_clear = cache.clear  # type: ignore[attr-defined]
        return wrapper

    return decorate
