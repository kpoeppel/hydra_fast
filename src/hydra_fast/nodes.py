"""``ValueNode`` proxies, for code that reaches past the public API.

OmegaConf stores every value as a ``Node`` object, so libraries sometimes use
that layer: ``cfg._get_node("a")._value()``, ``isinstance(node, IntegerNode)``,
``node._get_full_key("")``. hydra-fast stores plain data and has no such
objects, so they are synthesized on demand here.

The proxies are **live views**, not snapshots: each holds ``(root, path)`` and
reads or writes storage through it, so ``node._set_value(5)`` updates the
config and a later ``node._value()`` sees any change made elsewhere. They are
also **identity-stable** -- ``cfg._get_node("a") is cfg._get_node("a")`` holds,
as it does in omegaconf -- via a per-root cache keyed on path.

Nothing in hydra-fast itself uses these; they exist purely for compatibility,
so the cost is paid only by code that asks. The typed subclass is chosen from
the declared-type map when the config has a schema, matching omegaconf's
``IntegerNode``/``StringNode``/… and falling back to ``AnyNode``.
"""

from __future__ import annotations

import enum
import typing
from typing import Any, Dict, Optional, Tuple

__all__ = [
    "AnyNode",
    "BooleanNode",
    "EnumNode",
    "FloatNode",
    "IntegerNode",
    "StringNode",
    "ValueNode",
    "node_for",
]

MISSING = "???"


class _NodeMetadata:
    """Stand-in for omegaconf's ``Metadata``, with the fields people read."""

    __slots__ = (
        "object_type",
        "key",
        "ref_type",
        "element_type",
        "optional",
        "key_type",
        "flags",
        "flags_root",
        "resolver_cache",
        "type_hint",
    )

    def __init__(
        self,
        key: Any = None,
        object_type: Any = None,
        ref_type: Any = typing.Any,
        element_type: Any = typing.Any,
        optional: bool = True,
        key_type: Any = typing.Any,
        flags: Optional[Dict[str, bool]] = None,
    ) -> None:
        self.key = key
        self.object_type = object_type
        self.ref_type = ref_type
        self.element_type = element_type
        self.optional = optional
        self.key_type = key_type
        self.flags = flags if flags is not None else {}
        self.flags_root = False
        self.resolver_cache: Dict[Any, Any] = {}
        self.type_hint = ref_type

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Metadata(key={self.key!r}, object_type={self.object_type!r})"


class ValueNode:
    """A live handle on one scalar in a config."""

    __slots__ = ("_hf_root", "_hf_path", "_metadata")

    def __init__(self, root: Any, path: Tuple[Any, ...], ref_type: Any = typing.Any) -> None:
        self._hf_root = root
        self._hf_path = path
        self._metadata = _NodeMetadata(
            key=path[-1] if path else None,
            ref_type=ref_type,
            optional=_is_optional(ref_type),
        )

    # -- reading ---------------------------------------------------------
    def _raw(self) -> Any:
        from .container import _ABSENT, _select_raw

        value = _select_raw(self._hf_root.data, self._hf_path)
        return None if value is _ABSENT else value

    def _value(self) -> Any:
        """The stored value, unresolved -- an interpolation stays a string."""
        return self._raw()

    def _set_value(self, value: Any, flags: Any = None) -> None:
        """Write through to the config, with the same validation as assignment."""
        from .container import DictConfig, ListConfig, _select_raw

        parent_path = self._hf_path[:-1]
        parent = _select_raw(self._hf_root.data, parent_path)
        key = self._hf_path[-1]
        view_type = ListConfig if isinstance(parent, list) else DictConfig
        view = view_type._hf_view(self._hf_root, parent_path)
        if isinstance(parent, list):
            view[int(key)] = value
        else:
            view[key] = value

    def _is_missing(self) -> bool:
        value = self._raw()
        return type(value) is str and value == MISSING

    def _is_interpolation(self) -> bool:
        value = self._raw()
        return type(value) is str and "${" in value

    def _is_none(self) -> bool:
        return self._raw() is None

    def _is_optional(self) -> bool:
        return self._metadata.optional

    def _key(self) -> Any:
        return self._hf_path[-1] if self._hf_path else None

    def _get_full_key(self, key: Any = None) -> str:
        parts = list(self._hf_path)
        if key not in (None, ""):
            parts.append(key)
        return ".".join(str(part) for part in parts)

    def _get_node_flag(self, flag: str) -> Optional[bool]:
        return self._hf_root.flags.get(self._hf_path, {}).get(flag)

    def _get_flag(self, flag: str) -> Optional[bool]:
        from .container import DictConfig

        return DictConfig._hf_view(self._hf_root, self._hf_path[:-1])._get_flag(flag)

    def _set_flag(self, flag: Any, value: Any) -> "ValueNode":
        names = [flag] if isinstance(flag, str) else list(flag)
        values = [value] if not isinstance(flag, (list, tuple)) else list(value)
        store = self._hf_root.flags.setdefault(self._hf_path, {})
        for name, val in zip(names, values, strict=False):
            if val is None:
                store.pop(name, None)
            else:
                store[name] = val
        return self

    def _get_parent(self) -> Any:
        from .container import DictConfig, ListConfig, _select_raw

        parent_path = self._hf_path[:-1]
        parent = _select_raw(self._hf_root.data, parent_path)
        view_type = ListConfig if isinstance(parent, list) else DictConfig
        return view_type._hf_view(self._hf_root, parent_path)

    # -- behaving like the value ------------------------------------------
    def __eq__(self, other: Any) -> Any:
        if isinstance(other, ValueNode):
            return self._raw() == other._raw()
        return self._raw() == other

    def __ne__(self, other: Any) -> Any:
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self) -> int:
        value = self._raw()
        try:
            return hash(value)
        except TypeError:  # pragma: no cover - unhashable stored value
            return id(self)

    def __bool__(self) -> bool:
        return bool(self._raw())

    def __str__(self) -> str:
        return str(self._raw())

    def __repr__(self) -> str:
        return repr(self._raw())


class AnyNode(ValueNode):
    __slots__ = ()


class IntegerNode(ValueNode):
    __slots__ = ()


class FloatNode(ValueNode):
    __slots__ = ()


class StringNode(ValueNode):
    __slots__ = ()


class BooleanNode(ValueNode):
    __slots__ = ()


class BytesNode(ValueNode):
    __slots__ = ()


class EnumNode(ValueNode):
    __slots__ = ()


_BY_TYPE = {
    int: IntegerNode,
    float: FloatNode,
    str: StringNode,
    bool: BooleanNode,
    bytes: BytesNode,
}


def _is_optional(ref_type: Any) -> bool:
    if ref_type is typing.Any or ref_type is None:
        return True
    origin = typing.get_origin(ref_type)
    if origin is typing.Union or getattr(origin, "__name__", "") == "UnionType":
        return type(None) in typing.get_args(ref_type)
    return False


def _node_class(annotation: Any) -> type:
    """The omegaconf node class a declared annotation would produce."""
    from ._typing import strip_optional

    inner = strip_optional(annotation)
    found = _BY_TYPE.get(inner)
    if found is not None:
        return found
    if isinstance(inner, type) and issubclass(inner, enum.Enum):
        return EnumNode
    return AnyNode


def node_for(root: Any, path: Tuple[Any, ...]) -> ValueNode:
    """A live, identity-stable node proxy for the scalar at ``path``."""
    cache = root.node_cache
    found = cache.get(path)
    if found is not None:
        return found

    annotation = None
    if root.types is not None:
        from ._typing import lookup

        annotation = lookup(root.types, path)
    node = _node_class(annotation)(root, path, annotation or typing.Any)
    cache[path] = node
    return node
