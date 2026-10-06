# Ported from hydra.core.object_type (hydra-core, MIT).
from enum import Enum


class ObjectType(Enum):
    NOT_FOUND = 0
    CONFIG = 1
    GROUP = 2
