"""Compose a config: resolve the defaults list, merge it, apply overrides.

Ported from ``hydra._internal.config_loader_impl`` (hydra-core, MIT). Three
caches sit on top, each keyed on exactly what can change its result:

``defaults list``
    Walking the Defaults List means loading every config in the tree to read
    its own ``defaults:`` and ``@package`` header. Only overrides that *select
    a config group* can change the outcome -- the ``++key=value`` overrides a
    sweep varies per point cannot -- so the list is memoized on the
    group-selecting subset.
``merged config``
    Given a defaults list, merging it is a pure function of that list plus the
    search path. Sweep points differing only in value overrides share one
    merge.
``file reads``
    In :mod:`hydra_fast._internal.config_source`, one parse per file per
    process.

Under ``validation="stat"`` (the default) the first two are revalidated
against the contributing files' ``stat()`` on every lookup, so editing a
config on disk invalidates exactly the entries that depend on it.
"""

from __future__ import annotations

import copy
import os
import sys
import warnings
from textwrap import dedent
from typing import Any, Dict, List, Optional, Tuple

from .. import _cache
from .._copy import fast_deepcopy
from ..container import _ABSENT, DictConfig, ListConfig, _select_raw
from ..core.config_store import generation as store_generation
from ..core.object_type import ObjectType
from ..errors import (
    ConfigAttributeError,
    ConfigCompositionException,
    ConfigKeyError,
    OmegaConfBaseException,
)
from ..grammar.interpolation import split_key
from ..grammar.override import Override, OverrideType, parse_overrides
from ..merge import merge_into
from ..omegaconf_api import OmegaConf
from .config_repository import CachingConfigRepository, ConfigRepository, IConfigRepository
from .config_search_path import ConfigSearchPath
from .config_source import ConfigResult
from .default_element import ResultDefault
from .defaults_list import create_defaults_list
from .fingerprint import (
    begin_composition,
    end_composition,
)
from .fingerprint import (
    contributing_files as _contributing_files,
)

__all__ = ["ConfigLoader", "RunMode"]


class RunMode:
    RUN = 1
    MULTIRUN = 2


class ConfigLoader:
    def __init__(
        self,
        config_search_path: ConfigSearchPath,
        cache_enabled: bool = True,
    ) -> None:
        self.config_search_path = config_search_path
        self.repository = ConfigRepository(config_search_path=config_search_path)
        self._active_repository: Optional[IConfigRepository] = None
        self.cache_enabled = cache_enabled

    # -- public ----------------------------------------------------------
    def load_configuration(
        self,
        config_name: Optional[str],
        overrides: List[str],
        run_mode: int = RunMode.RUN,
        from_shell: bool = True,
        validate_sweep_overrides: bool = True,
        with_hydra: bool = True,
    ) -> DictConfig:
        try:
            return self._load_configuration_impl(
                config_name=config_name,
                overrides=overrides,
                run_mode=run_mode,
                from_shell=from_shell,
                validate_sweep_overrides=validate_sweep_overrides,
                with_hydra=with_hydra,
            )
        except OmegaConfBaseException as exc:
            raise ConfigCompositionException(str(exc)).with_traceback(
                sys.exc_info()[2]
            ) from exc

    def get_group_options(
        self,
        group_name: str,
        results_filter: Optional[ObjectType] = ObjectType.CONFIG,
        config_name: Optional[str] = None,
        overrides: Optional[List[str]] = None,
    ) -> List[str]:
        if config_name is None and overrides is None and self._active_repository is not None:
            return self._active_repository.get_group_options(group_name, results_filter)
        parsed = parse_overrides(overrides or [], self)
        repo = CachingConfigRepository(self.repository)
        self._process_config_searchpath(config_name, parsed, repo)
        return repo.get_group_options(group_name, results_filter)

    def list_groups(self, parent_name: str) -> List[str]:
        return self.get_group_options(parent_name, results_filter=ObjectType.GROUP)

    def get_sources(self) -> List[Any]:
        return self.repository.get_sources()

    def ensure_main_config_source_available(self) -> None:
        for source in self.get_sources():
            # the schema source and hydra's own configs are always there
            if source.provider in ("hydra", "schema"):
                continue
            if not source.available():
                warnings.warn(
                    f"provider={source.provider}, path={source.path} is not available.",
                    UserWarning,
                    stacklevel=2,
                )

    # -- implementation --------------------------------------------------
    def _process_config_searchpath(
        self,
        config_name: Optional[str],
        parsed_overrides: List[Override],
        repo: CachingConfigRepository,
    ) -> None:
        """Honour ``hydra.searchpath``, from the primary config or the CLI."""
        if config_name is not None:
            loaded = repo.load_config(config_path=config_name)
            primary_config = loaded.config if loaded is not None else OmegaConf.create()
        else:
            primary_config = OmegaConf.create()

        if not OmegaConf.is_dict(primary_config):
            raise ConfigCompositionException(
                f"primary config '{config_name}' must be a DictConfig, got"
                f" {type(primary_config).__name__}"
            )

        def is_searchpath_override(override: Override) -> bool:
            return (
                override.type in (OverrideType.CHANGE, OverrideType.EXTEND_LIST)
                and override.package is None
                and split_key(override.key_or_group) == ["hydra", "searchpath"]
            )

        override_value = None
        for override in parsed_overrides:
            if is_searchpath_override(override):
                override_value = override.value()
                break

        if override_value is not None:
            provider = "hydra.searchpath in command-line"
            searchpath = override_value
        else:
            provider = "hydra.searchpath in main"
            searchpath = OmegaConf.select(primary_config, "hydra.searchpath")

        if searchpath is None:
            return

        def invalid() -> None:
            raise ConfigCompositionException(
                f"hydra.searchpath must be a list of strings. Got: {searchpath}"
            )

        if isinstance(searchpath, ListConfig):
            searchpath = OmegaConf.to_container(searchpath, resolve=True)
        if not isinstance(searchpath, (list, tuple)):
            invalid()
        for item in searchpath:
            if not isinstance(item, str):
                invalid()

        new_path = copy.deepcopy(self.config_search_path)
        schema = new_path.get_path().pop(-1)
        assert schema.provider == "schema"
        for item in searchpath:
            new_path.append(provider=provider, path=item)
        new_path.append("schema", "structured://")
        repo.initialize_sources(new_path)

        for source in repo.get_sources():
            if not source.available():
                warnings.warn(
                    f"provider={source.provider}, path={source.path} is not available.",
                    UserWarning,
                    stacklevel=2,
                )

    def _load_configuration_impl(
        self,
        config_name: Optional[str],
        overrides: List[str],
        run_mode: int,
        from_shell: bool,
        validate_sweep_overrides: bool,
        with_hydra: bool,
    ) -> DictConfig:
        self.ensure_main_config_source_available()
        parsed_overrides = parse_overrides(overrides, self)

        if validate_sweep_overrides:
            self.validate_sweep_overrides_legal(parsed_overrides, run_mode, from_shell)

        repo = CachingConfigRepository(self.repository)
        self._process_config_searchpath(config_name, parsed_overrides, repo)

        # The defaults-list cache and the compose cache validate against the
        # same set of files; scope them so the stat() walk happens once.
        begin_composition()
        try:
            defaults_list = create_defaults_list(
                repo=repo,
                config_name=config_name,
                overrides_list=parsed_overrides,
                prepend_hydra=with_hydra,
                skip_missing=run_mode == RunMode.MULTIRUN,
            )
            cfg = self._compose_config_from_defaults_list(defaults_list.defaults, repo)
        finally:
            end_composition()

        OmegaConf.set_struct(cfg, True)
        if with_hydra and "hydra" in cfg:
            OmegaConf.set_readonly(cfg.hydra, False)

        self._apply_overrides_to_config(defaults_list.config_overrides, cfg)

        if with_hydra and "hydra" in cfg:
            self._fill_hydra_node(cfg, config_name, parsed_overrides, defaults_list, repo)

        self._active_repository = repo
        return cfg

    # -- merge -----------------------------------------------------------
    def _compose_config_from_defaults_list(
        self, defaults: List[ResultDefault], repo: IConfigRepository
    ) -> DictConfig:
        """Merge every config in the defaults list, in order.

        Cached on the defaults list plus the search path: two sweep points
        that select the same groups merge to the same thing, however their
        ``key=value`` overrides differ.
        """
        key = (
            tuple(
                (d.config_path, d.package, d.parent, d.is_self, d.primary) for d in defaults
            ),
            tuple((s.scheme(), s.provider, s.path) for s in repo.get_sources()),
            store_generation(),
        )
        entry = _COMPOSE_CACHE.get(key) if self.cache_enabled else None
        if entry is not None:
            fingerprints, cached, cached_types = entry
            if fingerprints == _contributing_files(defaults, repo):
                _cache._counters["compose_hit"] += 1
                # The caller mutates this (struct flag, overrides, hydra
                # bookkeeping), so it cannot be shared.
                return DictConfig._hf_adopt(
                    fast_deepcopy(cached),
                    dict(cached_types) if cached_types else None,
                )

        _cache._counters["compose_miss"] += 1
        data: Dict[str, Any] = {}
        # Declared types accumulate across the defaults list: a structured
        # config in the list (a ConfigStore schema) governs the keys it
        # declares for the rest of the composition, so a later YAML file's
        # `port: "5432"` lands as an int.
        types: Optional[Dict] = None
        for default in defaults:
            loaded = self._load_single_config(default=default, repo=repo)
            body = loaded.config
            if not isinstance(body, DictConfig):
                raise ConfigCompositionException(
                    f"Config {default.config_path} must be a mapping, got "
                    f"{type(body).__name__}"
                )
            types = _combine_types(types, body._hf_root.types)
            try:
                merge_into(data, body._hf_container(), types)
            except OmegaConfBaseException as exc:
                raise ConfigCompositionException(
                    f"In '{default.config_path}': {type(exc).__name__} raised while "
                    f"composing config:\n{exc}"
                ).with_traceback(sys.exc_info()[2]) from exc

        if self.cache_enabled:
            _COMPOSE_CACHE[key] = (
                _contributing_files(defaults, repo),
                fast_deepcopy(data),
                dict(types) if types else None,
            )
        return DictConfig._hf_adopt(data, types)

    def _load_single_config(
        self, default: ResultDefault, repo: IConfigRepository
    ) -> ConfigResult:
        config_path = default.config_path
        assert config_path is not None
        loaded = repo.load_config(config_path=config_path)
        assert loaded is not None

        if not OmegaConf.is_config(loaded.config):
            raise ValueError(
                f"Config {config_path} must be a config, got {type(loaded.config).__name__}"
            )

        result = self._embed_result_config(loaded, default.package)
        if (
            not default.primary
            and config_path != "hydra/config"
            and isinstance(result.config, DictConfig)
            and OmegaConf.select(result.config, "hydra.searchpath") is not None
        ):
            raise ConfigCompositionException(
                f"In '{config_path}': Overriding hydra.searchpath is only supported"
                " from the primary config"
            )
        return result

    @staticmethod
    def _embed_result_config(
        loaded: ConfigResult, package_override: Optional[str]
    ) -> ConfigResult:
        """Nest a config under its ``@package``, if it has one."""
        package = loaded.header["package"]
        if package_override is not None:
            package = package_override
        if package is not None and package != "":
            nested: Dict[str, Any] = {}
            node = nested
            parts = package.split(".")
            for part in parts[:-1]:
                node[part] = {}
                node = node[part]
            node[parts[-1]] = loaded.config._hf_container()
            loaded = copy.copy(loaded)
            # The declared types are keyed by path, so mounting the config
            # under a package has to re-key them to match.
            loaded.config = DictConfig._hf_adopt(
                nested, _nest_types(loaded.config._hf_root.types, tuple(parts))
            )
        return loaded

    # -- overrides -------------------------------------------------------
    @staticmethod
    def _apply_overrides_to_config(overrides: List[Override], cfg: DictConfig) -> None:
        for override in overrides:
            if override.package is not None:
                raise ConfigCompositionException(
                    f"Override {override.input_line} looks like a config group"
                    f" override, but config group '{override.key_or_group}' does not"
                    " exist."
                )

            key = override.key_or_group
            value = override.value()
            try:
                if override.is_delete():
                    ConfigLoader._apply_delete(cfg, override, key, value)
                elif override.is_add():
                    if (
                        OmegaConf.select(cfg, key, throw_on_missing=False) is None
                        or isinstance(value, (dict, list))
                    ):
                        OmegaConf.update(cfg, key, value, merge=True, force_add=True)
                    else:
                        assert override.input_line is not None
                        raise ConfigCompositionException(
                            dedent(f"""\
                        Could not append to config. An item is already at '{override.key_or_group}'.
                        Either remove + prefix: '{override.input_line[1:]}'
                        Or add a second + to add or override '{override.key_or_group}': '+{override.input_line}'
                        """)
                        )
                elif override.is_force_add():
                    OmegaConf.update(cfg, key, value, merge=True, force_add=True)
                elif override.is_list_extend():
                    target = OmegaConf.select(cfg, key, throw_on_missing=True)
                    if not OmegaConf.is_list(target):
                        raise ConfigCompositionException(
                            "Could not append to config list. The existing value of"
                            f" '{override.key_or_group}' is {target} which is not a list."
                        )
                    target.extend(value)
                else:
                    try:
                        OmegaConf.update(cfg, key, value, merge=True)
                    except (ConfigAttributeError, ConfigKeyError) as exc:
                        raise ConfigCompositionException(
                            f"Could not override '{override.key_or_group}'."
                            f"\nTo append to your config use +{override.input_line}"
                        ) from exc
            except OmegaConfBaseException as exc:
                raise ConfigCompositionException(
                    f"Error merging override {override.input_line}"
                ).with_traceback(sys.exc_info()[2]) from exc

    @staticmethod
    def _apply_delete(cfg: DictConfig, override: Override, key: str, value: Any) -> None:
        parts = split_key(key)
        container = _select_raw(cfg._hf_root.data, tuple(parts[:-1])) if len(parts) > 1 else (
            cfg._hf_container()
        )
        last = parts[-1] if parts else None
        present = (
            isinstance(container, (dict, list))
            and last is not None
            and (
                last in container
                if isinstance(container, dict)
                else last.isdigit() and int(last) < len(container)
            )
        )
        if container is _ABSENT or not present:
            raise ConfigCompositionException(
                f"Could not delete from config. '{override.key_or_group}' does not exist."
            )
        current = container[last] if isinstance(container, dict) else container[int(last)]
        if override.value_type is not None and value != current:
            raise ConfigCompositionException(
                "Could not delete from config. The value of"
                f" '{override.key_or_group}' is {current} and not {value}."
            )
        if isinstance(container, dict):
            del container[last]
        else:
            del container[int(last)]

    @staticmethod
    def validate_sweep_overrides_legal(
        overrides: List[Override], run_mode: int, from_shell: bool
    ) -> None:
        for override in overrides:
            if override.is_sweep_override():
                if run_mode == RunMode.MULTIRUN:
                    if override.is_hydra_override():
                        raise ConfigCompositionException(
                            f"Sweeping over Hydra's configuration is not supported :"
                            f" '{override.input_line}'"
                        )
                elif run_mode == RunMode.RUN:
                    if override.value_type is not None and override.is_choice_sweep():
                        vals = "value1,value2"
                        if from_shell:
                            example_override = f"key=\\'{vals}\\'"
                        else:
                            example_override = f"key='{vals}'"
                        msg = dedent(f"""\
                            Ambiguous value for argument '{override.input_line}'
                            1. To use it as a list, use key=[value1,value2]
                            2. To use it as string, quote the value: {example_override}
                            3. To sweep over it, add --multirun to your command line""")
                        raise ConfigCompositionException(msg)
                    raise ConfigCompositionException(
                        f"Sweep parameters '{override.input_line}' requires --multirun"
                    )

    # -- hydra node ------------------------------------------------------
    def _fill_hydra_node(
        self,
        cfg: DictConfig,
        config_name: Optional[str],
        parsed_overrides: List[Override],
        defaults_list: Any,
        repo: IConfigRepository,
    ) -> None:
        from ..version import __version__

        hydra = cfg._hf_container().get("hydra")
        if not isinstance(hydra, dict):
            return

        overrides_node = hydra.setdefault("overrides", {})
        overrides_node.setdefault("hydra", [])
        overrides_node.setdefault("task", [])
        for override in parsed_overrides:
            target = "hydra" if override.is_hydra_override() else "task"
            overrides_node[target].append(override.input_line)

        runtime = hydra.setdefault("runtime", {})
        choices = runtime.setdefault("choices", {})
        choices.update(defaults_list.overrides.known_choices)
        runtime["version"] = __version__
        runtime["cwd"] = os.getcwd()
        runtime["config_sources"] = [
            {"path": source.path, "schema": source.scheme(), "provider": source.provider}
            for source in repo.get_sources()
        ]

        job = hydra.setdefault("job", {})
        for key in job.get("env_copy") or []:
            if key in os.environ:
                job.setdefault("env_set", {})[key] = os.environ[key]
        if "name" not in job or job.get("name") is None:
            job["name"] = _default_job_name(config_name)
        job["config_name"] = config_name


def _combine_types(first: Optional[Dict], second: Optional[Dict]) -> Optional[Dict]:
    from .._typing import combine

    return combine(second, first) if second else first


def _nest_types(types: Optional[Dict], prefix: Tuple[Any, ...]) -> Optional[Dict]:
    from .._typing import nest

    return nest(types, prefix)


def _default_job_name(config_name: Optional[str]) -> str:
    from .job_runtime import JobRuntime

    name = JobRuntime.instance().get("name")
    if name is not None:
        return str(name)
    if config_name is not None:
        return config_name
    return os.path.basename(sys.argv[0]).rsplit(".", 1)[0] if sys.argv else "app"


# ---------------------------------------------------------------------------
# caches
# ---------------------------------------------------------------------------
# Bounded: a sweep has a handful of distinct group selections, so this never
# evicts during one; the bound is for a process that keeps meeting new trees.
_COMPOSE_CACHE = _cache.new_cache("compose", maxsize=512)
