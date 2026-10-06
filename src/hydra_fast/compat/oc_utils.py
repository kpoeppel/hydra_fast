"""Stand-ins for the ``omegaconf._utils`` helpers third-party code imports.

``omegaconf._utils`` is private and holds ~90 public names, most of them
stdlib re-exports or plumbing for omegaconf's own node graph. The ones here
are the subset that answers a question *about a value or an annotation* --
which is what code outside omegaconf actually reaches for, and the only part
that has a meaningful hydra-fast equivalent. Each is verified against the real
implementation by ``bench/oracle_internals.py``.

Everything absent is absent deliberately: it either describes omegaconf's
internal node graph, which hydra-fast does not have, or it is a re-exported
module. The audit lists the names so the gap is enumerable rather than vague.
"""

from __future__ import annotations

import dataclasses
import typing
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from ..container import MISSING, Container, DictConfig, ListConfig

__all__ = ["PROVIDED", "ValueKind"]


class ValueKind(Enum):
    """What a stored value *is*, before any resolution."""

    VALUE = 0
    MANDATORY_MISSING = 1
    INTERPOLATION = 2


def _unwrap(value: Any) -> Any:
    """The raw stored value behind a node proxy, if it is one."""
    from ..nodes import ValueNode

    if isinstance(value, ValueNode):
        return value._value()
    return value


def _is_missing_value(value: Any) -> bool:
    raw = _unwrap(value)
    return type(raw) is str and raw == MISSING


def _is_interpolation_string(value: str, strict_interpolation_validation: bool = False) -> bool:
    return "${" in value


def _is_interpolation(v: Any, strict_interpolation_validation: bool = False) -> bool:
    raw = _unwrap(v)
    if isinstance(raw, str):
        return _is_interpolation_string(raw, strict_interpolation_validation)
    return False


def _is_none(
    value: Any, resolve: bool = False, throw_on_resolution_failure: bool = True
) -> bool:
    return _unwrap(value) is None


def _is_special(value: Any) -> bool:
    """Missing or an interpolation -- the two states that are not a value."""
    return _is_missing_value(value) or _is_interpolation(value)


def get_value_kind(value: Any, strict_interpolation_validation: bool = False) -> ValueKind:
    if _is_missing_value(value):
        return ValueKind.MANDATORY_MISSING
    if _is_interpolation(value, strict_interpolation_validation):
        return ValueKind.INTERPOLATION
    return ValueKind.VALUE


def _get_value(value: Any) -> Any:
    """Unwrap a config or node to plain data, leaving primitives alone."""
    from ..omegaconf_api import OmegaConf

    if isinstance(value, Container):
        return OmegaConf.to_container(value, resolve=False)
    return _unwrap(value)


def _ensure_container(target: Any, flags: Optional[Dict[str, bool]] = None) -> Any:
    """``target`` as a config, creating one if it is plain data."""
    from .._structured import is_structured
    from ..omegaconf_api import OmegaConf

    if isinstance(target, Container):
        return target
    if is_structured(target) or isinstance(target, (dict, list)):
        return OmegaConf.create(target, flags=flags) if flags else OmegaConf.create(target)
    raise ValueError(
        "Invalid input. Supports one of "
        "[dict,list,DictConfig,ListConfig,dataclass,dataclass instance,attr class,"
        "attr class instance]"
    )


def is_primitive_dict(obj: Any) -> bool:
    return type(obj) in (dict, type({}.copy()))


def is_primitive_list(obj: Any) -> bool:
    return type(obj) in (list, tuple)


def is_primitive_container(obj: Any) -> bool:
    return is_primitive_dict(obj) or is_primitive_list(obj)


def is_dict_annotation(type_: Any) -> bool:
    origin = typing.get_origin(type_)
    return origin is dict or type_ is dict


def is_list_annotation(type_: Any) -> bool:
    origin = typing.get_origin(type_)
    return origin is list or type_ is list


def is_tuple_annotation(type_: Any) -> bool:
    origin = typing.get_origin(type_)
    return origin is tuple or type_ is tuple


def get_dict_key_value_types(ref_type: Any) -> Tuple[Any, Any]:
    args = typing.get_args(ref_type)
    if not args:
        return Any, Any
    key_type, value_type = args[0], args[1]
    return (
        Any if isinstance(key_type, typing.TypeVar) else key_type,
        Any if isinstance(value_type, typing.TypeVar) else value_type,
    )


def get_list_element_type(ref_type: Optional[type]) -> Any:
    args = typing.get_args(ref_type)
    if not args:
        return Any
    element = args[0]
    return Any if isinstance(element, typing.TypeVar) else element


def get_type_of(class_or_object: Any) -> type:
    return class_or_object if isinstance(class_or_object, type) else type(class_or_object)


def is_attr_class(obj: Any) -> bool:
    from .._structured import _HAS_ATTRS

    if not _HAS_ATTRS:
        return False
    import attr

    return attr.has(get_type_of(obj))


def is_dataclass(obj: Any) -> bool:
    return dataclasses.is_dataclass(obj)


def is_int(value: Any) -> bool:
    """Whether ``value`` *parses* as an int -- not whether it is one."""
    try:
        int(value)
        return True
    except (ValueError, TypeError):
        return False


def is_float(value: Any) -> bool:
    try:
        float(value)
        return True
    except (ValueError, TypeError):
        return False


def is_dict(obj: Any) -> bool:
    """A dict, a ``Dict[...]`` annotation, or a dict subclass."""
    return is_primitive_dict(obj) or is_dict_annotation(obj) or isinstance(obj, dict)


# omegaconf's implementations of these contradict their own docstrings. The
# docs say `list` and bare `typing.List` return False and only `List[T]`
# returns True; the code is `is_list_annotation(t) and get_list_element_type(t)
# is not None`, and the element type of a bare `List` is `Any`, not None -- so
# all three return True. Matched to the behaviour, not the prose, since that is
# what a caller observes. Do not "fix" these to the docstrings.
def is_generic_dict(type_: Any) -> bool:
    return is_dict_annotation(type_)


def is_generic_list(type_: Any) -> bool:
    return is_list_annotation(type_)


def is_union_annotation(type_: Any) -> bool:
    origin = typing.get_origin(type_)
    if origin is typing.Union:
        return True
    return getattr(origin, "__name__", "") == "UnionType"


def is_container_annotation(type_: Any) -> bool:
    return is_dict_annotation(type_) or is_list_annotation(type_)


def is_primitive_type_annotation(type_: Any) -> bool:
    """True for a scalar annotation: int, str, bool, float, bytes or an Enum."""
    import enum

    from .._typing import strip_optional

    inner = strip_optional(type_)
    if isinstance(inner, type) and issubclass(inner, enum.Enum):
        return True
    return inner in (int, float, bool, str, bytes, type(None))


def _is_missing_literal(value: Any) -> bool:
    return type(value) is str and value == MISSING


#: YAML 1.1 spellings PyYAML reads as booleans, which is why a config key
#: like `on:` or a value like `yes` needs quoting to stay a string.
_YAML_BOOLS = frozenset(
    {"y", "yes", "n", "no", "true", "false", "on", "off"}
)


def yaml_is_bool(value: str) -> bool:
    return str(value).lower() in _YAML_BOOLS


def split_key(key: str) -> List[str]:
    """Split a dotted key, honouring bracket segments -- ``a.b[0].c``."""
    from .._internal.config_loader import split_key as _split

    return list(_split(key))


#: The names installed onto the shim's ``omegaconf._utils``. Listed explicitly
#: rather than swept from ``dir()``, so adding a private helper here does not
#: silently widen the compatibility surface.
PROVIDED = (
    "ValueKind",
    "_ensure_container",
    "_get_value",
    "_is_interpolation",
    "_is_interpolation_string",
    "_is_missing_value",
    "_is_none",
    "_is_special",
    "get_dict_key_value_types",
    "get_list_element_type",
    "get_type_of",
    "get_value_kind",
    "is_attr_class",
    "is_dataclass",
    "is_dict_annotation",
    "is_list_annotation",
    "is_primitive_container",
    "is_primitive_dict",
    "is_primitive_list",
    "is_primitive_type_annotation",
    "is_tuple_annotation",
    "is_union_annotation",
    "is_container_annotation",
    "is_dict",
    "is_float",
    "is_generic_dict",
    "is_generic_list",
    "is_int",
    "_is_missing_literal",
    "yaml_is_bool",
    "split_key",
)

# Imported for re-export via PROVIDED; referenced here so linters see the use.
_ = (DictConfig, ListConfig)
