"""Resolver registry and OmegaConf's built-in resolvers.

A registry entry is a 5-tuple -- ``(func, use_cache, pass_parent, pass_node,
pass_root)`` -- computed once at registration from the function's signature.
The hot path then needs no ``inspect`` and no per-call signature test.
"""

from __future__ import annotations

import inspect
import os
import re
import warnings
from typing import Any, Callable, Dict, Optional, Tuple

from .errors import (
    ConfigKeyError,
    InterpolationResolutionError,
    UnsupportedInterpolationType,
    ValidationError,
)

__all__ = [
    "clear_resolver",
    "clear_resolvers",
    "has_resolver",
    "lookup_resolver",
    "register_resolver",
]

# name -> (func, use_cache, pass_parent, pass_node, pass_root)
Entry = Tuple[Callable[..., Any], bool, bool, bool, bool]
_RESOLVERS: Dict[str, Entry] = {}


def lookup_resolver(name: str) -> Optional[Entry]:
    return _RESOLVERS.get(name)


def has_resolver(name: str) -> bool:
    return name in _RESOLVERS


def register_resolver(
    name: str,
    resolver: Callable[..., Any],
    *,
    replace: bool = False,
    use_cache: bool = False,
) -> None:
    if not callable(resolver):
        raise TypeError("resolver must be callable")
    if not name:
        raise ValueError("cannot use an empty resolver name")
    if not replace and name in _RESOLVERS:
        raise ValueError(f"resolver '{name}' is already registered")

    try:
        sig: Optional[inspect.Signature] = inspect.signature(resolver)
    except (TypeError, ValueError):
        sig = None

    def wants(special: str) -> bool:
        present = sig is not None and special in sig.parameters
        if present and use_cache:
            raise ValueError(
                f"use_cache=True is incompatible with functions that receive the {special}"
            )
        return present

    _RESOLVERS[name] = (
        resolver,
        use_cache,
        wants("_parent_"),
        wants("_node_"),
        wants("_root_"),
    )


def clear_resolver(name: str) -> bool:
    return _RESOLVERS.pop(name, None) is not None


def clear_resolvers() -> None:
    """Drop user resolvers, keeping the built-ins (OmegaConf's behaviour)."""
    _RESOLVERS.clear()
    register_builtin_resolvers()


# ---------------------------------------------------------------------------
# built-ins
# ---------------------------------------------------------------------------
_NOT_FOUND = object()


def _oc_env(key: str, default: Any = _NOT_FOUND) -> Any:
    """``${oc.env:NAME}`` / ``${oc.env:NAME,default}``.

    Values always come back as strings when read from the environment -- the
    environment has no types -- which is what OmegaConf does too.
    """
    try:
        return os.environ[key]
    except KeyError:
        if default is not _NOT_FOUND:
            return str(default) if default is not None else None
        raise InterpolationResolutionError(f"Environment variable '{key}' not found") from None


def _oc_select(key: str, default: Any = _NOT_FOUND, *, _parent_: Any = None) -> Any:
    """``${oc.select:a.b}`` -- like a node reference but tolerates absence.

    Resolved against ``_parent_`` with ``absolute_key=True``, as omegaconf
    does: a bare key is absolute from the root, a leading ``.`` makes it
    relative to the node holding the interpolation.
    """
    from .omegaconf_api import OmegaConf

    try:
        return OmegaConf.select(
            _parent_,
            key,
            default=None if default is _NOT_FOUND else default,
            absolute_key=True,
        )
    except Exception:  # noqa: BLE001 - absence is the normal case here
        return None if default is _NOT_FOUND else default


def _oc_dict_keys(key: str, *, _parent_: Any = None) -> Any:
    from .omegaconf_api import OmegaConf

    node = _dict_arg(key, _parent_, "oc.dict.keys")
    return OmegaConf.create(list(node.keys()))


_SIMPLE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _oc_dict_values(key: str, *, _parent_: Any = None) -> Any:
    """``${oc.dict.values:x}`` -- the values of dict ``x``, in key order.

    Returns *references* (``${x.a}``, ``${x.b}``) rather than resolved values,
    matching omegaconf: a list retained from this resolver keeps tracking the
    source, so a later edit to ``x`` shows through.

    A key that is not a plain identifier cannot be spelled unambiguously in a
    reference, so those entries fall back to their resolved value. Mixing is
    harmless -- the list is heterogeneous either way.
    """
    from .container import Container, make_reference_list

    node = _dict_arg(key, _parent_, "oc.dict.values")
    path = node._hf_path
    referencable = all(_SIMPLE_KEY.match(str(part)) for part in path)

    items = []
    for name in node:
        if referencable and _SIMPLE_KEY.match(str(name)):
            dotted = ".".join(str(part) for part in (*path, name))
            items.append(f"${{{dotted}}}")
        else:
            value = node[name]
            items.append(value._hf_container() if isinstance(value, Container) else value)
    return make_reference_list(items, node._hf_root)


def _dict_arg(key: Any, parent: Any, who: str) -> Any:
    from .container import DictConfig
    from .omegaconf_api import OmegaConf

    if isinstance(key, DictConfig):
        return key
    if not isinstance(key, str):
        raise TypeError(
            f"`{who}` requires a string as input, but obtained `{key}` "
            f"of type: {type(key).__name__}"
        )
    node = OmegaConf.select(parent, key, absolute_key=True, throw_on_missing=True)
    if node is None:
        raise ConfigKeyError(f"Key not found: '{key}'")
    if not isinstance(node, DictConfig):
        raise TypeError(f"`{who}` cannot be applied to objects of type: {type(node).__name__}")
    return node


def _oc_create(value: Any, *, _parent_: Any = None) -> Any:
    """``${oc.create:...}`` -- build a config node from a value or YAML text."""
    from .container import Container
    from .omegaconf_api import OmegaConf

    if isinstance(value, Container):
        value = value._hf_container()
    return OmegaConf.create(value)


def _oc_decode(value: Optional[str]) -> Any:
    """``${oc.decode:'...'}`` -- parse a string with the grammar's value rules."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(
            f"TypeError while evaluating 'oc.decode': argument must be a string or None, "
            f"got {type(value).__name__}"
        )
    from .grammar.interpolation import compile_single_element

    compiled = compile_single_element(value)
    result = compiled(_NULL_CTX)
    from .omegaconf_api import OmegaConf

    if isinstance(result, (dict, list)):
        return OmegaConf.create(result)
    return result


def _oc_deprecated(key: str, message: Optional[str] = None, *, _parent_: Any = None) -> Any:
    """``${oc.deprecated:new_key}`` -- read ``new_key`` and warn."""
    from .omegaconf_api import OmegaConf

    if not isinstance(key, str):
        raise ValidationError("oc.deprecated: interpolation key must be a string")
    if message is None:
        message = "'$OLD_KEY' is deprecated. Change your code and config to use '$NEW_KEY'"
    value = OmegaConf.select(_parent_, key, absolute_key=False, throw_on_missing=True)
    if value is None:
        raise ConfigKeyError(f"Key not found: '{key}'")
    warnings.warn(
        message.replace("$NEW_KEY", key).replace("$OLD_KEY", "<unknown>"),
        UserWarning,
        stacklevel=2,
    )
    return value


class _NullCtx:
    """Context for interpolation-free evaluation (``oc.decode`` arguments)."""

    def node(self, dots: int, parts: Tuple[str, ...], spelling: str) -> Any:
        raise UnsupportedInterpolationType(
            "node interpolations are not supported in this context"
        )

    def resolver(self, name: str, args: Tuple, args_str: Tuple) -> Any:
        raise UnsupportedInterpolationType(
            f"resolver interpolations are not supported in this context: {name}"
        )


_NULL_CTX = _NullCtx()


def register_builtin_resolvers() -> None:
    register_resolver("oc.env", _oc_env, replace=True)
    register_resolver("oc.select", _oc_select, replace=True)
    register_resolver("oc.dict.keys", _oc_dict_keys, replace=True)
    register_resolver("oc.dict.values", _oc_dict_values, replace=True)
    register_resolver("oc.create", _oc_create, replace=True)
    register_resolver("oc.decode", _oc_decode, replace=True)
    register_resolver("oc.deprecated", _oc_deprecated, replace=True)


register_builtin_resolvers()
