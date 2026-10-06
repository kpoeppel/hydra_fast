"""Registry for configs declared in Python (structured configs).

Same API as ``hydra.core.config_store``. Nodes are stored as plain dicts, and
a generation counter is bumped on every ``store()`` so the composition caches
can tell when the store changed without having to hash it.
"""

from __future__ import annotations

import builtins
import copy
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..errors import ConfigCompositionException
from .object_type import ObjectType
from .singleton import Singleton

__all__ = ["ConfigNode", "ConfigStore", "ConfigStoreWithProvider", "ConfigLoadError"]


class ConfigLoadError(IOError, ConfigCompositionException):
    pass


# Bumped on every store(); the defaults-list and compose caches key on it so a
# config registered mid-process invalidates exactly what it should.
_GENERATION = [0]


def generation() -> int:
    return _GENERATION[0]


class ConfigStoreWithProvider:
    def __init__(self, provider: str) -> None:
        self.provider = provider

    def __enter__(self) -> "ConfigStoreWithProvider":
        return self

    def store(
        self,
        name: str,
        node: Any,
        group: Optional[str] = None,
        package: Optional[str] = None,
    ) -> None:
        ConfigStore.instance().store(
            group=group, name=name, node=node, package=package, provider=self.provider
        )

    def __exit__(self, *_exc: Any) -> None:
        return None


@dataclass
class ConfigNode:
    name: str
    node: Any
    group: Optional[str]
    package: Optional[str]
    provider: Optional[str]


class ConfigStore(metaclass=Singleton):
    repo: Dict[str, Any]

    @staticmethod
    def instance(*args: Any, **kwargs: Any) -> "ConfigStore":
        return Singleton.instance(ConfigStore, *args, **kwargs)  # type: ignore[return-value]

    def __init__(self) -> None:
        self.repo = {}

    def store(
        self,
        name: str,
        node: Any,
        group: Optional[str] = None,
        package: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> None:
        if group == "":
            group = None

        cur = self.repo
        if group is not None:
            for fragment in group.split("/"):
                if fragment not in cur:
                    cur[fragment] = {}
                cur = cur[fragment]

        if not name.endswith(".yaml"):
            name = f"{name}.yaml"
        assert isinstance(cur, dict)

        from ..omegaconf_api import OmegaConf

        cfg = OmegaConf.structured(node)
        cur[name] = ConfigNode(
            name=name, node=cfg, group=group, package=package, provider=provider
        )
        _GENERATION[0] += 1

    def load(self, config_path: str) -> ConfigNode:
        found = self._load(config_path)
        found = copy.copy(found)
        # Deep-copy the node: callers mutate what they get back, and the store
        # has to keep serving the original.
        found.node = found.node._hf_clone(deep=True)
        return found

    def _load(self, config_path: str) -> ConfigNode:
        index = config_path.rfind("/")
        if index == -1:
            found = self._open(config_path)
            if found is None:
                raise ConfigLoadError(f"Structured config not found {config_path}")
            assert isinstance(found, ConfigNode)
            return found
        path, name = config_path[:index], config_path[index + 1 :]
        group = self._open(path)
        if group is None or not isinstance(group, dict):
            raise ConfigLoadError(f"Structured config not found {config_path}")
        if name not in group:
            raise ConfigLoadError(f"Structured config {name} not found in {config_path}")
        found = group[name]
        assert isinstance(found, ConfigNode)
        return found

    def get_type(self, path: str) -> ObjectType:
        found = self._open(path)
        if found is None:
            return ObjectType.NOT_FOUND
        return ObjectType.GROUP if isinstance(found, dict) else ObjectType.CONFIG

    def list(self, path: str) -> builtins.list:
        found = self._open(path)
        if found is None:
            raise OSError(f"Path not found {path}")
        if not isinstance(found, dict):
            raise OSError(f"Path points to a file : {path}")
        return sorted(found.keys())

    def _open(self, path: str) -> Any:
        node: Any = self.repo
        for fragment in path.split("/"):
            if fragment == "":
                continue
            if isinstance(node, dict) and fragment in node:
                node = node[fragment]
            else:
                return None
        return node
