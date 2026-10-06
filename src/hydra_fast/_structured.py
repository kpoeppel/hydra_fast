"""Dataclass / attrs support.

Structured configs are flattened to plain data at creation time. That gives up
OmegaConf's runtime type *enforcement* on later assignment, but keeps the part
configs actually rely on: a dataclass as a schema you merge YAML onto, with
defaults, nesting and ``MISSING`` preserved.

:func:`get_structured_type` records the originating class so
``OmegaConf.to_object`` can rebuild instances.
"""

from __future__ import annotations

import dataclasses
import enum
from typing import Any, Dict, Optional, Tuple

from .container import MISSING
from .errors import ValidationError

__all__ = [
    "get_structured_type",
    "is_structured",
    "structured_to_plain",
    "type_map_for",
]

# id(plain dict) -> originating class, for to_object()
_ORIGIN: Dict[int, Any] = {}

try:  # pragma: no cover - attrs is optional
    import attr

    _HAS_ATTRS = True
except ImportError:  # pragma: no cover
    _HAS_ATTRS = False


def is_structured(value: Any) -> bool:
    """True for a dataclass/attrs class or instance."""
    if isinstance(value, type):
        return dataclasses.is_dataclass(value) or (_HAS_ATTRS and attr.has(value))
    cls = type(value)
    if dataclasses.is_dataclass(cls):
        return True
    return bool(_HAS_ATTRS and attr.has(cls))


def get_structured_type(data: Any) -> Optional[Any]:
    return _ORIGIN.get(id(data))


def type_map_for(value: Any) -> Optional[Dict[Tuple[Any, ...], Any]]:
    """Declared annotations for a structured class or instance.

    Keyed by *path tuple*, not field name -- a nested dataclass contributes
    ``("db", "port")`` -- which is what lets one map cover a whole tree.
    Returned separately from the flattened data so the config root can carry
    it; see :mod:`hydra_fast._typing`.
    """
    from ._typing import declared_types

    cls = value if isinstance(value, type) else type(value)
    if not is_structured(cls):
        return None
    return declared_types(cls) or None


def _field_value(value: Any) -> Any:
    if value is dataclasses.MISSING:
        return MISSING
    if isinstance(value, str) and value == "???":
        return MISSING
    if isinstance(value, enum.Enum):
        return value
    if is_structured(value):
        return structured_to_plain(value)
    if isinstance(value, dict):
        return {key: _field_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_field_value(item) for item in value]
    return value


def structured_to_plain(value: Any) -> Dict[str, Any]:
    """Flatten a dataclass/attrs class or instance into a plain dict."""
    cls = value if isinstance(value, type) else type(value)

    if dataclasses.is_dataclass(cls):
        out: Dict[str, Any] = {}
        for field in dataclasses.fields(cls):
            if not field.init and not hasattr(value, field.name):
                continue
            if isinstance(value, type):
                if field.default is not dataclasses.MISSING:
                    item = field.default
                elif field.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
                    item = field.default_factory()  # type: ignore[misc]
                else:
                    item = MISSING
            else:
                item = getattr(value, field.name, MISSING)
            out[field.name] = _field_value(item)
        _ORIGIN[id(out)] = cls
        return out

    if _HAS_ATTRS and attr.has(cls):  # pragma: no cover - exercised only with attrs
        out = {}
        for field in attr.fields(cls):
            if isinstance(value, type):
                item = field.default if field.default is not attr.NOTHING else MISSING
                if isinstance(item, attr.Factory):  # type: ignore[arg-type]
                    item = item.factory()  # type: ignore[attr-defined]
            else:
                item = getattr(value, field.name, MISSING)
            out[field.name] = _field_value(item)
        _ORIGIN[id(out)] = cls
        return out

    raise ValidationError(f"Unsupported structured type: {cls.__name__}")
