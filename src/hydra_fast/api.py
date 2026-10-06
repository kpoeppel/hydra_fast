"""The hydra-facing API: ``compose``, ``initialize*``, ``main``.

Signatures and behaviour mirror ``hydra.compose`` / ``hydra.initialize*`` /
``hydra.main``, so existing call sites work unchanged.
"""

from __future__ import annotations

import functools
import inspect
import os
import sys
from typing import Any, Callable, List, Optional, Sequence

from ._internal.config_loader import ConfigLoader, RunMode
from ._internal.config_search_path import ConfigSearchPath
from ._internal.job_runtime import JobRuntime
from .container import DictConfig
from .core.singleton import Singleton
from .errors import HydraException
from .omegaconf_api import open_dict
from .version import setbase

__all__ = [
    "GlobalHydra",
    "compose",
    "initialize",
    "initialize_config_dir",
    "initialize_config_module",
    "main",
]


# ---------------------------------------------------------------------------
# search path construction
# ---------------------------------------------------------------------------
def create_config_search_path(search_path_dir: Optional[str]) -> ConfigSearchPath:
    return create_config_search_path_from_sources(
        [] if search_path_dir is None else [search_path_dir]
    )


def create_config_search_path_from_sources(sources: Sequence[str]) -> ConfigSearchPath:
    from .conf import register_hydra_configs

    register_hydra_configs()

    search_path = ConfigSearchPath()
    search_path.append("hydra", "pkg://hydra_fast.conf")
    for source in sources:
        search_path.append("main", source)
    search_path.append("schema", "structured://")
    return search_path


# ---------------------------------------------------------------------------
# global state
# ---------------------------------------------------------------------------
class GlobalHydra(metaclass=Singleton):
    config_loader: Optional[ConfigLoader]

    @staticmethod
    def instance(*args: Any, **kwargs: Any) -> "GlobalHydra":
        return Singleton.instance(GlobalHydra, *args, **kwargs)  # type: ignore[return-value]

    def __init__(self) -> None:
        self.config_loader = None

    def initialize(self, config_loader: ConfigLoader) -> None:
        self.config_loader = config_loader

    def is_initialized(self) -> bool:
        return self.config_loader is not None

    def clear(self) -> None:
        self.config_loader = None


def _snapshot() -> Any:
    gh = GlobalHydra.instance()
    return gh.config_loader


def _restore(loader: Any) -> None:
    GlobalHydra.instance().config_loader = loader


# ---------------------------------------------------------------------------
# initialization context managers
# ---------------------------------------------------------------------------
class _InitBase:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: Any) -> None:
        _restore(self._backup)


class initialize_config_dir(_InitBase):
    """Initialize with an absolute filesystem directory on the search path."""

    def __init__(
        self,
        config_dir: str,
        job_name: str = "app",
        version_base: Any = None,
    ) -> None:
        self._backup = _snapshot()
        setbase(version_base)
        if not os.path.isabs(config_dir):
            raise HydraException(
                "initialize_config_dir() requires an absolute config_dir as input"
            )
        JobRuntime.instance().set("name", job_name)
        search_path = create_config_search_path(config_dir)
        GlobalHydra.instance().initialize(ConfigLoader(config_search_path=search_path))

    def __repr__(self) -> str:
        return "hydra_fast.initialize_config_dir()"


class initialize_config_module(_InitBase):
    """Initialize with an importable module on the search path."""

    def __init__(
        self,
        config_module: str,
        job_name: str = "app",
        version_base: Any = None,
    ) -> None:
        self._backup = _snapshot()
        setbase(version_base)
        JobRuntime.instance().set("name", job_name)
        search_path = create_config_search_path(f"pkg://{config_module}")
        GlobalHydra.instance().initialize(ConfigLoader(config_search_path=search_path))

    def __repr__(self) -> str:
        return "hydra_fast.initialize_config_module()"


class initialize(_InitBase):
    """Initialize with a path relative to the caller's module."""

    def __init__(
        self,
        config_path: Optional[str] = None,
        job_name: Optional[str] = None,
        caller_stack_depth: int = 1,
        version_base: Any = None,
    ) -> None:
        self._backup = _snapshot()
        setbase(version_base)
        if config_path is not None and os.path.isabs(config_path):
            raise HydraException(
                "config_path in initialize() must be relative. "
                "Use initialize_config_dir() for an absolute path."
            )

        calling_file, calling_module = _detect_calling_file_or_module(caller_stack_depth + 1)
        if job_name is None:
            job_name = _detect_task_name(calling_file, calling_module)
        JobRuntime.instance().set("name", job_name)

        if config_path is None:
            search_path = create_config_search_path(None)
        elif calling_module is not None:
            module = calling_module.rsplit(".", 1)[0] if "." in calling_module else ""
            parts = [part for part in config_path.split("/") if part not in ("", ".")]
            target = ".".join(filter(None, [module, *parts]))
            search_path = create_config_search_path(f"pkg://{target}")
        else:
            base = os.path.dirname(os.path.abspath(calling_file or sys.argv[0]))
            search_path = create_config_search_path(
                os.path.realpath(os.path.join(base, config_path))
            )

        GlobalHydra.instance().initialize(ConfigLoader(config_search_path=search_path))

    def __repr__(self) -> str:
        return "hydra_fast.initialize()"


def _detect_calling_file_or_module(stack_depth: int):
    stack = inspect.stack()
    frame = stack[stack_depth]
    module = frame.frame.f_globals.get("__name__")
    filename = frame.filename
    if module == "__main__" or module is None:
        return filename, None
    return filename, module


def _detect_task_name(calling_file: Optional[str], calling_module: Optional[str]) -> str:
    if calling_module is not None:
        return calling_module.rsplit(".", 1)[-1]
    if calling_file is not None:
        return os.path.basename(calling_file).rsplit(".", 1)[0]
    return "app"


# ---------------------------------------------------------------------------
# compose
# ---------------------------------------------------------------------------
def compose(
    config_name: Optional[str] = None,
    overrides: Optional[List[str]] = None,
    return_hydra_config: bool = False,
    strict: Any = None,
) -> DictConfig:
    """Compose a config. Requires one of the ``initialize*`` helpers first.

    :param config_name: config name, usually the file name without ``.yaml``
    :param overrides: command-line style overrides
    :param return_hydra_config: keep the ``hydra`` node in the result
    """
    if overrides is None:
        overrides = []

    gh = GlobalHydra.instance()
    if not gh.is_initialized():
        raise HydraException(
            "GlobalHydra is not initialized, use @hydra_fast.main() or call one of "
            "the hydra_fast initialization methods first"
        )
    assert gh.config_loader is not None

    cfg = gh.config_loader.load_configuration(
        config_name=config_name,
        overrides=overrides,
        run_mode=RunMode.RUN,
        from_shell=False,
        # The hydra node is composed only when asked for. hydra composes it
        # always and deletes it again, which costs a merge of its whole schema
        # on every call.
        with_hydra=return_hydra_config,
    )

    if not return_hydra_config and "hydra" in cfg:
        with open_dict(cfg):
            del cfg["hydra"]
    return cfg


# ---------------------------------------------------------------------------
# main decorator
# ---------------------------------------------------------------------------
def main(
    config_path: Optional[str] = None,
    config_name: Optional[str] = None,
    version_base: Any = None,
) -> Callable[[Callable], Callable]:
    """Decorator that composes a config from ``sys.argv`` and calls the task."""

    def decorate(task_function: Callable) -> Callable:
        @functools.wraps(task_function)
        def wrapper(cfg_passthrough: Optional[DictConfig] = None) -> Any:
            if cfg_passthrough is not None:
                return task_function(cfg_passthrough)

            args = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
            with initialize(
                config_path=config_path,
                job_name=_detect_task_name(
                    inspect.getfile(task_function), task_function.__module__
                ),
                caller_stack_depth=2,
                version_base=version_base,
            ):
                cfg = compose(config_name=config_name, overrides=args)
                return task_function(cfg)

        return wrapper

    return decorate
