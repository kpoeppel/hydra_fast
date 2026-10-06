"""The config repository: resolve a config name to a source, and load it.

Ported from ``hydra._internal.config_repository`` (hydra-core, MIT), with two
changes that matter for speed:

* the defaults list is parsed by the *source*, where it can be cached on file
  identity, rather than re-derived from a freshly loaded config here;
* :class:`CachingConfigRepository` no longer deep-copies its delegate up
  front. That copy exists only so ``initialize_sources()`` cannot mutate the
  loader's shared repository, which happens only when a config overrides
  ``hydra.searchpath`` -- so the copy is deferred until something actually
  calls it.
"""

from __future__ import annotations

import copy
from typing import Dict, List, Optional

from .. import _cache
from ..core.object_type import ObjectType
from .config_search_path import ConfigSearchPath
from .config_source import ConfigResult, ConfigSource, create_config_source

__all__ = ["CachingConfigRepository", "ConfigRepository", "IConfigRepository"]


class IConfigRepository:
    def get_schema_source(self) -> ConfigSource:
        raise NotImplementedError

    def load_config(self, config_path: str) -> Optional[ConfigResult]:
        raise NotImplementedError

    def group_exists(self, config_path: str) -> bool:
        raise NotImplementedError

    def config_exists(self, config_path: str) -> bool:
        raise NotImplementedError

    def get_group_options(
        self, group_name: str, results_filter: Optional[ObjectType] = ObjectType.CONFIG
    ) -> List[str]:
        raise NotImplementedError

    def get_sources(self) -> List[ConfigSource]:
        raise NotImplementedError

    def initialize_sources(self, config_search_path: ConfigSearchPath) -> None:
        raise NotImplementedError


class ConfigRepository(IConfigRepository):
    config_search_path: ConfigSearchPath
    sources: List[ConfigSource]

    def __init__(self, config_search_path: ConfigSearchPath) -> None:
        self.initialize_sources(config_search_path)

    def initialize_sources(self, config_search_path: ConfigSearchPath) -> None:
        self.sources = []
        for element in config_search_path.get_path():
            assert element.path is not None and element.provider is not None
            self.sources.append(create_config_source(element.provider, element.path))
        # `_find_object_source` answers are keyed on this signature, so compute
        # it once per source list instead of per lookup.
        self._sources_key = tuple(
            (source.scheme(), source.provider, source.path) for source in self.sources
        )

    def get_schema_source(self) -> ConfigSource:
        source = self.sources[-1]
        assert (
            source.__class__.__name__ == "StructuredConfigSource"
            and source.provider == "schema"
        ), "schema config source must be last"
        return source

    def load_config(self, config_path: str) -> Optional[ConfigResult]:
        source = self._find_object_source(config_path, ObjectType.CONFIG)
        if source is None:
            return None
        result = source.load_config(config_path=config_path)
        result.is_schema_source = (
            source.__class__.__name__ == "StructuredConfigSource"
            and source.provider == "schema"
        )
        return result

    def group_exists(self, config_path: str) -> bool:
        return self._find_object_source(config_path, ObjectType.GROUP) is not None

    def config_exists(self, config_path: str) -> bool:
        return self._find_object_source(config_path, ObjectType.CONFIG) is not None

    def get_group_options(
        self, group_name: str, results_filter: Optional[ObjectType] = ObjectType.CONFIG
    ) -> List[str]:
        options: List[str] = []
        for source in self.sources:
            if source.is_group(config_path=group_name):
                options.extend(source.list(config_path=group_name, results_filter=results_filter))
        return sorted(set(options))

    def get_sources(self) -> List[ConfigSource]:
        return self.sources

    def _find_object_source(
        self, config_path: str, object_type: Optional[ObjectType]
    ) -> Optional[ConfigSource]:
        """Which search path provides this name?

        Resolving a defaults list asks this once per candidate per search path,
        each answer costing a ``realpath`` plus a ``stat``. The answer only
        changes if the config tree is edited on disk, so it is cached as an
        *index* into ``self.sources`` -- the source objects themselves are
        rebuilt for every composition, so caching the object would hand back a
        stale one.
        """
        key = (config_path, object_type, self._sources_key)
        index = _lookup_source_index(key)
        if index is not _MISS:
            return None if index is None else self.sources[index]

        found = None
        for position, source in enumerate(self.sources):
            if object_type == ObjectType.CONFIG:
                if source.is_config(config_path):
                    found = position
                    break
            elif object_type == ObjectType.GROUP:
                if source.is_group(config_path):
                    found = position
                    break
            else:
                raise ValueError("Unexpected object_type")
        _store_source_index(key, found)
        return None if found is None else self.sources[found]

    @staticmethod
    def _get_scheme(path: str) -> str:
        index = path.find("://")
        return "file" if index == -1 else path[:index]


# The lookup cache is only safe while nothing is being written to disk, so it
# is active under validation="never" and bypassed under "stat" -- where the
# syscall *is* the validation and caching it would buy nothing.
_SOURCE_INDEX = _cache.new_cache("source_lookup", maxsize=1 << 16)
_MISS = object()


def _lookup_source_index(key: tuple):
    if _cache.get_validation() != "never":
        return _MISS
    try:
        value = _SOURCE_INDEX[key]
    except KeyError:
        return _MISS
    _cache._counters["source_lookup_hit"] += 1
    return value


def _store_source_index(key: tuple, index: Optional[int]) -> None:
    if _cache.get_validation() != "never":
        return
    _cache._counters["source_lookup_miss"] += 1
    _SOURCE_INDEX[key] = index


class CachingConfigRepository(IConfigRepository):
    """Per-composition cache in front of a shared :class:`ConfigRepository`."""

    def __init__(self, delegate: IConfigRepository) -> None:
        self.delegate = delegate
        self.cache: Dict[str, Optional[ConfigResult]] = {}
        self._owns_delegate = False

    def get_schema_source(self) -> ConfigSource:
        return self.delegate.get_schema_source()

    def initialize_sources(self, config_search_path: ConfigSearchPath) -> None:
        # Only now does mutating the delegate become a problem, so this is
        # where the copy hydra does up front actually has to happen.
        if not self._owns_delegate:
            self.delegate = copy.deepcopy(self.delegate)
            self._owns_delegate = True
        self.delegate.initialize_sources(config_search_path)
        # Deliberately not clearing the cache: the only entry at this point is
        # the primary config, and it is still wanted.

    def load_config(self, config_path: str) -> Optional[ConfigResult]:
        key = f"config_path={config_path}"
        if key in self.cache:
            return self.cache[key]
        result = self.delegate.load_config(config_path=config_path)
        self.cache[key] = result
        return result

    def group_exists(self, config_path: str) -> bool:
        return self.delegate.group_exists(config_path=config_path)

    def config_exists(self, config_path: str) -> bool:
        return self.delegate.config_exists(config_path=config_path)

    def get_group_options(
        self, group_name: str, results_filter: Optional[ObjectType] = ObjectType.CONFIG
    ) -> List[str]:
        return self.delegate.get_group_options(
            group_name=group_name, results_filter=results_filter
        )

    def get_sources(self) -> List[ConfigSource]:
        return self.delegate.get_sources()
