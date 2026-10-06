"""Config sources: where configs are read from, and the caching layer.

This is where the "read each file once per process" promise is kept.
:class:`FileConfigSource.load_config` goes through :mod:`hydra_fast._cache`
for three things, each keyed on file identity:

* the raw text (so the ``# @package`` header scan is free on a repeat);
* the parsed YAML (the expensive part);
* the ``defaults:`` list parsed into ``InputDefault`` objects.

A sweep that composes one tree 500 times reads and parses each of its ~90
YAML files exactly once, instead of 45,000 times.

Cached parse results are *shared*, so :meth:`load_config` hands back a deep
copy of the config body -- composition merges into it.
"""

from __future__ import annotations

import builtins
import copy
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .. import _cache
from .._copy import fast_deepcopy
from .._yaml import load_yaml_file, load_yaml_str
from ..container import DictConfig, ListConfig
from ..core.object_type import ObjectType
from ..errors import ConfigCompositionException, HydraException
from .default_element import ConfigDefault, GroupDefault, InputDefault

__all__ = [
    "ConfigLoadError",
    "ConfigResult",
    "ConfigSource",
    "FileConfigSource",
    "ImportlibResourcesConfigSource",
    "StructuredConfigSource",
    "create_config_source",
]


class ConfigLoadError(HydraException, IOError):
    pass


@dataclass
class ConfigResult:
    provider: str
    path: str
    config: Any
    header: Dict[str, Optional[str]]
    defaults_list: Optional[List[InputDefault]] = None
    is_schema_source: bool = False


_HEADER_LINE = re.compile(r"^\s*#\s*@")


class ConfigSource:
    provider: str
    path: str

    def __init__(self, provider: str, path: str) -> None:
        if not path.startswith(self.scheme()):
            raise ValueError("Invalid path")
        self.provider = provider
        self.path = path[len(self.scheme() + "://") :]

    @staticmethod
    def scheme() -> str:
        raise NotImplementedError

    def load_config(self, config_path: str) -> ConfigResult:
        raise NotImplementedError

    def exists(self, config_path: str) -> bool:
        return self.is_group(config_path) or self.is_config(config_path)

    def is_group(self, config_path: str) -> bool:
        raise NotImplementedError

    def is_config(self, config_path: str) -> bool:
        raise NotImplementedError

    def available(self) -> bool:
        raise NotImplementedError

    def list(self, config_path: str, results_filter: Optional[ObjectType]) -> builtins.list:
        raise NotImplementedError

    def full_path(self) -> str:
        return f"{self.scheme()}://{self.path}"

    def __str__(self) -> str:
        return repr(self)

    def __repr__(self) -> str:
        return f"provider={self.provider}, path={self.scheme()}://{self.path}"

    def _list_add_result(
        self,
        files: builtins.list,
        file_path: str,
        file_name: str,
        results_filter: Optional[ObjectType],
    ) -> None:
        filtered = ["__pycache__", "__init__.py"]
        if (
            self.is_group(file_path)
            and (results_filter is None or results_filter == ObjectType.GROUP)
            and file_name not in filtered
        ):
            files.append(file_name)
        if (
            self.is_config(file_path)
            and file_name not in filtered
            and (results_filter is None or results_filter == ObjectType.CONFIG)
        ):
            last_dot = file_name.rfind(".")
            if last_dot != -1:
                file_name = file_name[:last_dot]
            files.append(file_name)

    @staticmethod
    def _normalize_file_name(filename: str) -> str:
        if filename.endswith(".yml"):
            raise ConfigLoadError(
                "Unsupported config file extension '.yml'. "
                "Hydra config files must use the '.yaml' extension."
            )
        if not filename.endswith(".yaml"):
            filename += ".yaml"
        return filename

    @staticmethod
    def _get_header_dict(config_text: str) -> Dict[str, Optional[str]]:
        result: Dict[str, Optional[str]] = {}
        for line in config_text.splitlines():
            line = line.strip()
            if not line:
                continue
            if _HEADER_LINE.match(line):
                line = line.lstrip("#").strip()
                splits = [part for part in line.split(" ") if part]
                if len(splits) < 2:
                    raise ValueError(f"Expected header format: KEY VALUE, got '{line}'")
                if len(splits) > 2:
                    raise ValueError(f"Too many components in '{line}'")
                key, value = splits[0].strip(), splits[1].strip()
                if key.startswith("@"):
                    result[key[1:]] = value
            else:
                break
        if "package" not in result:
            result["package"] = None
        return result


# ---------------------------------------------------------------------------
# defaults list parsing
# ---------------------------------------------------------------------------
def _parse_defaults(data: Any, config_path: str) -> Optional[List[InputDefault]]:
    """Turn a ``defaults:`` block of plain data into ``InputDefault`` objects."""
    if not isinstance(data, dict):
        return None
    raw = data.get("defaults")
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ConfigCompositionException(
            f"Invalid defaults list in '{config_path}', expected a list, "
            f"got {type(raw).__name__}"
        )

    out: List[InputDefault] = []
    for index, item in enumerate(raw):
        out.append(_parse_default_item(item, index, config_path))
    return out


_KNOWN_KEYWORDS = ("optional", "override")


def _split_group(group_with_package: str):
    """``group@pkg`` / ``group@pkg1:pkg2`` -> (group, package, package2)."""
    index = group_with_package.find("@")
    if index == -1:
        return group_with_package, None, None
    group = group_with_package[:index]
    package = group_with_package[index + 1 :]
    package2 = None
    colon = package.find(":")
    if colon != -1:
        package2 = package[colon + 1 :]
        package = package[:colon]
    return group, package, package2


def _extract_keywords(config_path: str, group: str):
    """``"optional db"`` -> (group="db", optional=True, override=False)."""
    elements = group.split()
    if not elements:
        raise ValueError(f"In {config_path}: Missing group name in defaults list")
    name = elements[-1]
    optional = False
    override = False
    for keyword in elements[:-1]:
        if keyword not in _KNOWN_KEYWORDS:
            raise ValueError(
                f"In {config_path}: Unsupported keyword '{keyword}' in defaults list"
            )
        if keyword == "optional":
            optional = True
        else:
            override = True
    if optional and override:
        raise ValueError(
            f"In {config_path}: 'optional' and 'override' keywords "
            "cannot be combined in defaults list"
        )
    return name, optional, override


def _parse_default_item(item: Any, index: int, config_path: str) -> InputDefault:
    if isinstance(item, str):
        path, package, _ = _split_group(item)
        return ConfigDefault(path=path, package=package)

    if isinstance(item, dict):
        keys = list(item.keys())
        if len(keys) > 1:
            raise ValueError(f"In {config_path}: Too many keys in default item {item}")
        if len(keys) == 0:
            raise ValueError(f"In {config_path}: Missing group name in {item}")

        key = keys[0]
        config_group, package, _package2 = _split_group(str(key))
        group, optional, override = _extract_keywords(config_path, config_group)

        value = item[key]
        if value is not None and not isinstance(value, (str, list)):
            raise ValueError(
                f"Unsupported item value in defaults : {type(value).__name__}."
                " Supported: string or list"
            )
        if isinstance(value, list):
            options = []
            for option in value:
                if not isinstance(option, str):
                    raise ValueError(
                        f"Unsupported item value in defaults : {type(option).__name__},"
                        " nested list items must be strings"
                    )
                options.append(option)
            value = options

        # NOTE: hydra 1.4-dev rewrites `db: variants/mysql` into group
        # `db/variants` / option `mysql`, which moves the config's package from
        # `db` to `db.variants`. hydra 1.3 -- the compatibility target -- leaves
        # the slash in the value and keeps the package at `db`. We match 1.3;
        # see docs/compatibility.md.
        return GroupDefault(
            group=group,
            value=value,
            package=package,
            optional=optional,
            override=override,
        )

    raise ValueError(f"Unsupported type in defaults : {type(item).__name__}")


@_cache.memoize("source_defaults")
def _cached_defaults(path: str, fingerprint: Any) -> Optional[List[InputDefault]]:
    """Parse one file's defaults list, memoized on (path, file identity).

    ``fingerprint`` is only in the key -- it is what makes the entry go stale
    when the file is edited.
    """
    data = load_yaml_file(path)
    return _parse_defaults(data, path)


def _fingerprint_for(path: str) -> Any:
    if _cache.get_validation() == "never":
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size, stat.st_ino)


# ---------------------------------------------------------------------------
# file source
# ---------------------------------------------------------------------------
class FileConfigSource(ConfigSource):
    def __init__(self, provider: str, path: str) -> None:
        if path.find("://") == -1:
            path = f"{self.scheme()}://{path}"
        super().__init__(provider, path)

    @staticmethod
    def scheme() -> str:
        return "file"

    def _full_path(self, config_path: str) -> str:
        from .fingerprint import resolve_path

        return resolve_path(self.path, config_path)

    def load_config(self, config_path: str) -> ConfigResult:
        normalized = self._normalize_file_name(config_path)
        full_path = self._full_path(normalized)
        if not os.path.exists(full_path):
            raise ConfigLoadError(f"Config not found : {full_path}")

        fingerprint = _fingerprint_for(full_path)
        text = _cache.read_text(full_path)
        header = _cached_header(text)
        data = load_yaml_file(full_path)
        defaults_list = _cached_defaults(full_path, fingerprint)

        # The cached dict is shared; composition mutates what it gets, and the
        # `defaults` key is consumed separately, so hand over a copy without it.
        body = (
            {key: value for key, value in data.items() if key != "defaults"}
            if isinstance(data, dict)
            else data
        )
        config = (
            DictConfig._hf_adopt(fast_deepcopy(body))
            if isinstance(body, dict)
            else ListConfig._hf_adopt(fast_deepcopy(body))
        )

        return ConfigResult(
            config=config,
            path=f"{self.scheme()}://{self.path}",
            provider=self.provider,
            header=dict(header),
            defaults_list=copy.deepcopy(defaults_list) if defaults_list else defaults_list,
        )

    def available(self) -> bool:
        return self.is_group("")

    def is_group(self, config_path: str) -> bool:
        from .fingerprint import path_kind

        return path_kind(self._full_path(config_path)) == "dir"

    def is_config(self, config_path: str) -> bool:
        from .fingerprint import path_kind

        try:
            normalized = self._normalize_file_name(config_path)
        except ConfigLoadError:
            raise
        return path_kind(self._full_path(normalized)) == "file"

    def list(self, config_path: str, results_filter: Optional[ObjectType]) -> builtins.list:
        files: List[str] = []
        full_path = self._full_path(config_path)
        for name in _cache.listdir(full_path):
            self._list_add_result(
                files=files,
                file_path=os.path.join(config_path, name),
                file_name=name,
                results_filter=results_filter,
            )
        return sorted(set(files))


@_cache.memoize("source_header")
def _cached_header(text: str) -> Dict[str, Optional[str]]:
    """Parse the ``# @package`` header. Keyed on the text, which is itself cached."""
    # Only the leading comment block can hold a header, so a short prefix is
    # all that needs scanning -- matching hydra's 512-byte read.
    return ConfigSource._get_header_dict(text[:512])


# ---------------------------------------------------------------------------
# structured (ConfigStore) source
# ---------------------------------------------------------------------------
class StructuredConfigSource(ConfigSource):
    def __init__(self, provider: str, path: str) -> None:
        if path.find("://") == -1:
            path = f"{self.scheme()}://{path}"
        super().__init__(provider, path)

    @staticmethod
    def scheme() -> str:
        return "structured"

    def load_config(self, config_path: str) -> ConfigResult:
        from ..core.config_store import ConfigStore

        normalized = self._normalize_file_name(config_path)
        found = ConfigStore.instance().load(config_path=normalized)
        provider = found.provider if found.provider is not None else self.provider
        node = found.node
        defaults_list = None
        if isinstance(node, DictConfig):
            data = node._hf_container()
            if "defaults" in data:
                defaults_list = _parse_defaults(data, config_path)
                data = {key: value for key, value in data.items() if key != "defaults"}
                node = DictConfig._hf_adopt(data)
        return ConfigResult(
            config=node,
            path=f"{self.scheme()}://{self.path}",
            provider=provider,
            header={"package": found.package},
            defaults_list=defaults_list,
        )

    def available(self) -> bool:
        return True

    def is_group(self, config_path: str) -> bool:
        from ..core.config_store import ConfigStore

        return ConfigStore.instance().get_type(config_path.rstrip("/")) == ObjectType.GROUP

    def is_config(self, config_path: str) -> bool:
        from ..core.config_store import ConfigStore

        filename = self._normalize_file_name(config_path.rstrip("/"))
        return ConfigStore.instance().get_type(filename) == ObjectType.CONFIG

    def list(self, config_path: str, results_filter: Optional[ObjectType]) -> builtins.list:
        from ..core.config_store import ConfigStore

        out: List[str] = []
        for name in ConfigStore.instance().list(config_path):
            self._list_add_result(
                files=out,
                file_path=f"{config_path}/{name}",
                file_name=name,
                results_filter=results_filter,
            )
        return sorted(set(out))


# ---------------------------------------------------------------------------
# importlib.resources (pkg://) source
# ---------------------------------------------------------------------------
_pkg_roots = _cache.new_cache("pkg_root", maxsize=256)
_pkg_kind_cache = _cache.new_cache("pkg_kind", maxsize=1 << 16)


def _package_root(module: str) -> Any:
    """``importlib.resources.files(module)``, once per module.

    The call walks the import system and builds a Traversable; doing it per
    existence check made it the single most expensive step of a cached
    composition.
    """
    if not module:
        raise ConfigLoadError("Empty package path")
    try:
        return _pkg_roots[module]
    except KeyError:
        pass
    from importlib.resources import files

    root = files(module)
    _pkg_roots[module] = root
    return root


class ImportlibResourcesConfigSource(ConfigSource):
    @staticmethod
    def scheme() -> str:
        return "pkg"

    def __init__(self, provider: str, path: str) -> None:
        if path.find("://") == -1:
            path = f"{self.scheme()}://{path}"
        super().__init__(provider, path)

    def _traversable(self, config_path: str = "") -> Any:
        base = _package_root(self.path.replace("/", "."))
        if config_path:
            for part in config_path.split("/"):
                if part:
                    base = base.joinpath(part)
        return base

    def load_config(self, config_path: str) -> ConfigResult:
        normalized = self._normalize_file_name(config_path)
        try:
            resource = self._traversable(normalized)
            text = resource.read_text(encoding="utf-8")
        except (FileNotFoundError, ModuleNotFoundError, IsADirectoryError, OSError) as exc:
            raise ConfigLoadError(f"Config not found : {normalized}") from exc

        header = _cached_header(text)
        data = load_yaml_str(text)
        defaults_list = _parse_defaults(data, config_path)
        body = (
            {key: value for key, value in data.items() if key != "defaults"}
            if isinstance(data, dict)
            else data
        )
        config = (
            DictConfig._hf_adopt(body)
            if isinstance(body, dict)
            else ListConfig._hf_adopt(body)
        )
        return ConfigResult(
            config=config,
            path=f"{self.scheme()}://{self.path}",
            provider=self.provider,
            header=dict(header),
            defaults_list=defaults_list,
        )

    def available(self) -> bool:
        try:
            self._traversable()
        except (ModuleNotFoundError, ImportError, TypeError, ConfigLoadError):
            return False
        return True

    def is_group(self, config_path: str) -> bool:
        return self._kind(config_path) == "dir"

    def is_config(self, config_path: str) -> bool:
        return self._kind(self._normalize_file_name(config_path)) == "file"

    def _kind(self, config_path: str) -> Optional[str]:
        """``"file"``, ``"dir"`` or ``None``, cached.

        A package's contents are fixed once it is importable, so unlike the
        filesystem source this needs no revalidation. Worth caching: resolving
        a defaults list asks this for every candidate on every search path, and
        the ``pkg://hydra_fast.conf`` source is first in line.
        """
        key = (self.path, config_path)
        try:
            kind = _pkg_kind_cache[key]
        except KeyError:
            pass
        else:
            _cache._counters["pkg_kind_hit"] += 1
            return kind

        _cache._counters["pkg_kind_miss"] += 1
        try:
            node = self._traversable(config_path)
            kind = "dir" if node.is_dir() else "file" if node.is_file() else None
        except (FileNotFoundError, ModuleNotFoundError, OSError, ConfigLoadError, TypeError):
            kind = None
        _pkg_kind_cache[key] = kind
        return kind

    def list(self, config_path: str, results_filter: Optional[ObjectType]) -> builtins.list:
        out: List[str] = []
        try:
            base = self._traversable(config_path)
            names = sorted(item.name for item in base.iterdir())
        except (FileNotFoundError, ModuleNotFoundError, OSError, ConfigLoadError):
            return []
        for name in names:
            self._list_add_result(
                files=out,
                file_path=f"{config_path}/{name}" if config_path else name,
                file_name=name,
                results_filter=results_filter,
            )
        return sorted(set(out))


_SOURCES = {
    "file": FileConfigSource,
    "structured": StructuredConfigSource,
    "pkg": ImportlibResourcesConfigSource,
}


def create_config_source(provider: str, path: str) -> ConfigSource:
    scheme = path.split("://", 1)[0] if "://" in path else "file"
    source_type = _SOURCES.get(scheme)
    if source_type is None:
        raise ConfigCompositionException(f"No config source registered for schema {scheme}")
    return source_type(provider=provider, path=path)
