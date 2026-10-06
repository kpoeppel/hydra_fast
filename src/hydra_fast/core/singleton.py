# Ported from hydra.core.singleton (hydra-core, MIT).
from typing import Any, Dict


class Singleton(type):
    _instances: Dict[type, "Singleton"] = {}

    def __call__(cls, *args: Any, **kwargs: Any) -> Any:
        if cls not in cls._instances:
            cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]

    @staticmethod
    def instance(__class__: Any = None, *args: Any, **kwargs: Any) -> Any:
        if __class__ is None:
            return Singleton
        return __class__(*args, **kwargs)

    @staticmethod
    def get_state() -> Any:
        return {"instances": Singleton._instances}

    @staticmethod
    def set_state(state: Any) -> None:
        Singleton._instances = state["instances"]
