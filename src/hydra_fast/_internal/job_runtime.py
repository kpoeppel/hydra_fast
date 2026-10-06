"""Per-process job metadata (the ``hydra.job.name`` default).

Ported from ``hydra.core.utils.JobRuntime`` (hydra-core, MIT), trimmed to what
composition needs.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..core.singleton import Singleton

__all__ = ["JobRuntime"]


class JobRuntime(metaclass=Singleton):
    conf: Dict[str, Any]

    @staticmethod
    def instance(*args: Any, **kwargs: Any) -> "JobRuntime":
        return Singleton.instance(JobRuntime, *args, **kwargs)  # type: ignore[return-value]

    def __init__(self) -> None:
        self.conf = {"name": None}

    def get(self, key: str) -> Optional[Any]:
        return self.conf.get(key)

    def set(self, key: str, value: Any) -> None:
        self.conf[key] = value
