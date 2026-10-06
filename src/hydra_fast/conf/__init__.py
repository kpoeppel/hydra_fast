"""Hydra's own configuration schema (the ``hydra.*`` node).

Ported from ``hydra.conf`` (hydra-core, MIT). The YAML group configs beside
this module (``conf/hydra/output/default.yaml`` and friends) are hydra's own
files, copied verbatim, so ``hydra/job_logging=disabled`` and the rest resolve
to the same content they would under hydra.

The launcher and sweeper nodes are the basic ones only: hydra-fast composes
configs, it does not run jobs, so there is nothing for a real launcher plugin
to hook into. They are registered because defaults lists and
``hydra/launcher=...`` overrides refer to them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..container import MISSING
from ..core.config_store import ConfigStore

__all__ = ["HydraConf", "register_hydra_configs"]


@dataclass
class HelpConf:
    app_name: str = MISSING
    header: str = MISSING
    footer: str = MISSING
    template: str = MISSING


@dataclass
class HydraHelpConf:
    hydra_help: str = MISSING
    template: str = MISSING


@dataclass
class RunDir:
    dir: str = MISSING


@dataclass
class SweepDir:
    dir: str = MISSING
    subdir: str = MISSING


@dataclass
class OverridesConf:
    hydra: List[str] = field(default_factory=list)
    task: List[str] = field(default_factory=list)


@dataclass
class OverrideDirname:
    kv_sep: str = "="
    item_sep: str = ","
    exclude_keys: List[str] = field(default_factory=list)


@dataclass
class JobConfig:
    override_dirname: OverrideDirname = field(default_factory=OverrideDirname)


@dataclass
class JobConf:
    name: str = MISSING
    chdir: bool = False
    override_dirname: str = "${hydra_override_dirname:}"
    id: str = MISSING
    num: int = MISSING
    config_name: Optional[str] = MISSING
    env_set: Dict[str, str] = field(default_factory=dict)
    env_copy: List[str] = field(default_factory=list)
    config: JobConfig = field(default_factory=JobConfig)


@dataclass
class ConfigSourceInfo:
    path: str = MISSING
    schema: str = MISSING
    provider: str = MISSING


@dataclass
class RuntimeConf:
    version: str = MISSING
    version_base: str = MISSING
    cwd: str = MISSING
    config_sources: List[Any] = MISSING
    output_dir: str = MISSING
    choices: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BasicLauncherConf:
    _target_: str = "hydra._internal.core_plugins.basic_launcher.BasicLauncher"


@dataclass
class BasicSweeperConf:
    _target_: str = "hydra._internal.core_plugins.basic_sweeper.BasicSweeper"
    max_batch_size: Optional[int] = None
    params: Optional[Dict[str, str]] = None


@dataclass
class HydraConf:
    defaults: List[Any] = field(
        default_factory=lambda: [
            {"output": "default"},
            {"launcher": "basic"},
            {"sweeper": "basic"},
            {"help": "default"},
            {"hydra_help": "default"},
            {"hydra_logging": "default"},
            {"job_logging": "default"},
            {"callbacks": None},
            {"env": "default"},
        ]
    )

    mode: Optional[Any] = None
    searchpath: List[str] = field(default_factory=list)
    run: RunDir = field(default_factory=RunDir)
    sweep: SweepDir = field(default_factory=SweepDir)
    hydra_logging: Dict[str, Any] = MISSING
    job_logging: Dict[str, Any] = MISSING
    sweeper: Any = MISSING
    launcher: Any = MISSING
    callbacks: Dict[str, Any] = field(default_factory=dict)
    help: HelpConf = field(default_factory=HelpConf)
    hydra_help: HydraHelpConf = field(default_factory=HydraHelpConf)
    output_subdir: Optional[str] = ".hydra"
    overrides: OverridesConf = field(default_factory=OverridesConf)
    job: JobConf = field(default_factory=JobConf)
    runtime: RuntimeConf = field(default_factory=RuntimeConf)
    verbose: Any = False


_REGISTERED = False


def register_hydra_configs() -> None:
    """Register the ``hydra`` schema nodes. Idempotent."""
    global _REGISTERED
    if _REGISTERED:
        return
    store = ConfigStore.instance()
    store.store(group="hydra", name="config", node=HydraConf(), provider="hydra")
    store.store(
        group="hydra/launcher", name="basic", node=BasicLauncherConf(), provider="hydra"
    )
    store.store(group="hydra/sweeper", name="basic", node=BasicSweeperConf(), provider="hydra")
    _REGISTERED = True
