"""``instantiate`` and the ``_target_`` protocol, matching ``hydra.utils``.

This is a *post-composition* operation: it takes a config that has already
been composed and resolved, reads ``_target_`` as an ordinary string key, and
builds objects from it. Nothing here runs during composition, merging or
interpolation, so a config tree that never calls ``instantiate`` pays nothing
for its existence -- see ``bench/bench_instantiate.py``.

The special keys, all optional except the first:

``_target_``
    Dotted path to the class or callable to build.
``_args_``
    Positional arguments. Arguments passed at the call site *replace* these
    rather than extending them, which is what hydra does.
``_recursive_``
    Build nested ``_target_`` configs too. ``True`` by default.
``_convert_``
    What non-target containers look like when they reach the target:
    ``"none"`` (``DictConfig``/``ListConfig``, the default), ``"partial"``
    (plain, except structured configs), ``"object"`` (plain, structured
    configs become dataclass instances), ``"all"`` (plain throughout).
``_partial_``
    Return a ``functools.partial`` instead of calling. ``False`` by default.

A config *without* ``_target_`` is not an error: its values are instantiated
recursively and the container itself is returned, converted per ``_convert_``.
"""

from __future__ import annotations

import functools
from enum import Enum
from typing import Any, Callable, Dict, List, Tuple

from .errors import InstantiationException

__all__ = [
    "ConvertMode",
    "call",
    "get_class",
    "get_method",
    "get_object",
    "get_static_method",
    "instantiate",
]

_TARGET = "_target_"
_ARGS = "_args_"
_RECURSIVE = "_recursive_"
_CONVERT = "_convert_"
_PARTIAL = "_partial_"

#: Keys the protocol owns; never passed through to the target as kwargs.
_RESERVED = frozenset({_TARGET, _ARGS, _RECURSIVE, _CONVERT, _PARTIAL})


class ConvertMode(str, Enum):
    """How containers are converted before reaching the target."""

    NONE = "none"
    PARTIAL = "partial"
    OBJECT = "object"
    ALL = "all"


# ---------------------------------------------------------------------------
# importing a target
# ---------------------------------------------------------------------------
def _locate(path: str) -> Any:
    """Import the object a dotted path names.

    Walks left to right, importing as far as it can and then using
    ``getattr``, so ``pkg.module.Class.attr`` resolves even though only
    ``pkg.module`` is importable.
    """
    if not path:
        raise InstantiationException("Empty path")
    parts = path.split(".")
    for part in parts:
        if not part:
            raise InstantiationException(
                f"Error loading '{path}': invalid dotstring."
                " Relative imports are not supported."
            )

    import importlib

    found: Any = None
    consumed = 0
    for index in range(len(parts)):
        candidate = ".".join(parts[: index + 1])
        try:
            found = importlib.import_module(candidate)
            consumed = index + 1
        except ImportError:
            break
    if found is None:
        raise InstantiationException(
            f"Error loading '{path}':\nModuleNotFoundError: No module named '{parts[0]}'"
        )

    for part in parts[consumed:]:
        try:
            found = getattr(found, part)
        except AttributeError as exc:
            raise InstantiationException(f"Error loading '{path}':\n{exc}") from exc
    return found


def get_object(path: str) -> Any:
    """The object a dotted path names, whatever kind it is."""
    return _locate(path)


def get_class(path: str) -> type:
    found = _locate(path)
    if not isinstance(found, type):
        raise InstantiationException(
            f"Located non-class of type '{type(found).__name__}' while loading '{path}'"
        )
    return found


def get_method(path: str) -> Callable[..., Any]:
    found = _locate(path)
    if not callable(found):
        raise InstantiationException(
            f"Located non-callable of type '{type(found).__name__}' while loading '{path}'"
        )
    return found


#: hydra exposes this name as well; same meaning.
get_static_method = get_method


# ---------------------------------------------------------------------------
# conversion
# ---------------------------------------------------------------------------
def _is_container(value: Any) -> bool:
    from .container import Container

    return isinstance(value, Container)


def _convert_value(value: Any, mode: ConvertMode) -> Any:
    """Apply ``_convert_`` to a value that is *not* being instantiated."""
    if mode is ConvertMode.NONE or not _is_container(value):
        return value

    from .omegaconf_api import OmegaConf, SCMode

    if mode is ConvertMode.ALL:
        return OmegaConf.to_container(value, resolve=True)
    if mode is ConvertMode.OBJECT:
        return OmegaConf.to_container(
            value, resolve=True, structured_config_mode=SCMode.INSTANTIATE
        )
    # PARTIAL: plain containers, but structured configs stay as configs.
    return OmegaConf.to_container(
        value, resolve=True, structured_config_mode=SCMode.DICT_CONFIG
    )


# ---------------------------------------------------------------------------
# instantiate
# ---------------------------------------------------------------------------
def _resolve_flags(
    config: Any, overrides: Dict[str, Any], recursive: bool, convert: ConvertMode
) -> Tuple[bool, ConvertMode, bool]:
    """``(recursive, convert, partial)`` for this node.

    A flag given at the call site wins over one in the config, and a flag in
    the config wins over what was inherited from the parent -- which is how
    `_convert_` set on the root reaches a nested target.
    """
    if _RECURSIVE in overrides:
        recursive = bool(overrides[_RECURSIVE])
    elif _is_container(config) and _RECURSIVE in config:
        recursive = bool(config[_RECURSIVE])

    if _CONVERT in overrides:
        convert = ConvertMode(overrides[_CONVERT])
    elif _is_container(config) and _CONVERT in config:
        convert = ConvertMode(config[_CONVERT])

    partial = False
    if _PARTIAL in overrides:
        partial = bool(overrides[_PARTIAL])
    elif _is_container(config) and _PARTIAL in config:
        partial = bool(config[_PARTIAL])

    return recursive, convert, partial


def _instantiate_node(node: Any, recursive: bool, convert: ConvertMode) -> Any:
    """Walk a composed value, building anything that carries ``_target_``."""
    from .container import DictConfig, ListConfig

    if isinstance(node, ListConfig):
        items = [_instantiate_node(item, recursive, convert) for item in node]
        return _rewrap(items) if convert is ConvertMode.NONE else items

    if not isinstance(node, DictConfig):
        return node

    has_target = _TARGET in node
    if has_target:
        return _build(node, {}, (), recursive, convert)

    if not recursive:
        return _convert_value(node, convert)

    built = {key: _instantiate_node(node[key], recursive, convert) for key in node}
    return _rewrap(built) if convert is ConvertMode.NONE else built


def _rewrap(built: Any) -> Any:
    """A container of the same kind, holding already-instantiated values.

    With ``_convert_="none"`` hydra hands back a ``DictConfig``/``ListConfig``
    that *contains* the objects it built, rather than a plain dict or list, so
    the container type survives instantiation. hydra-fast stores plain data,
    so an object simply sits in it -- no `allow_objects` flag needed.
    """
    from .omegaconf_api import OmegaConf

    return OmegaConf.create(built)


def _build(
    config: Any,
    overrides: Dict[str, Any],
    args: Tuple[Any, ...],
    recursive: bool,
    convert: ConvertMode,
) -> Any:
    """Call the target named by ``config[_target_]``."""
    recursive, convert, partial = _resolve_flags(config, overrides, recursive, convert)

    target_path = config[_TARGET]
    if not isinstance(target_path, str):
        if callable(target_path):
            target = target_path
        else:
            raise InstantiationException(
                f"Unsupported target type: {type(target_path).__name__}."
                " value: {target_path}"
            )
    else:
        target = _locate(target_path)

    # Call-site positional arguments *replace* `_args_` rather than extending
    # it, which is what hydra does -- `_args_: [1]` called with `(2, 3)` passes
    # `(2, 3)`, not `(1, 2, 3)`.
    positional: List[Any] = list(args)
    if not positional and _ARGS in config:
        positional = [
            _instantiate_node(item, recursive, convert)
            if recursive
            else _convert_value(item, convert)
            for item in config[_ARGS]
        ]

    kwargs: Dict[str, Any] = {}
    for key in config:
        if key in _RESERVED:
            continue
        value = config[key]
        kwargs[str(key)] = (
            _instantiate_node(value, recursive, convert)
            if recursive
            else _convert_value(value, convert)
        )
    for key, value in overrides.items():
        if key in _RESERVED:
            continue
        kwargs[key] = value

    if partial:
        return functools.partial(target, *positional, **kwargs)
    try:
        return target(*positional, **kwargs)
    except InstantiationException:
        raise
    except Exception as exc:
        raise InstantiationException(
            f"Error in call to target '{_describe_target(target)}':\n"
            f"{type(exc).__name__}({exc})"
        ) from exc


def _describe_target(target: Any) -> str:
    module = getattr(target, "__module__", None)
    name = getattr(target, "__qualname__", None) or getattr(target, "__name__", None)
    return f"{module}.{name}" if module and name else repr(target)


def instantiate(config: Any, *args: Any, **kwargs: Any) -> Any:
    """Build the object a config describes.

    ``config`` may carry ``_target_``, or be a container whose values do.
    Positional arguments replace ``_args_``; keyword arguments override config
    keys of the same name.
    """
    if config is None:
        return None

    from .container import DictConfig, ListConfig
    from .omegaconf_api import OmegaConf

    if isinstance(config, (dict, list)) and not _is_container(config):
        config = OmegaConf.create(config)

    recursive = True
    convert = ConvertMode.NONE

    if isinstance(config, ListConfig):
        if args or kwargs:
            raise InstantiationException(
                "Positional and keyword arguments are not supported for a list config"
            )
        recursive, convert, _ = _resolve_flags(config, kwargs, recursive, convert)
        return _instantiate_node(config, recursive, convert)

    if not isinstance(config, DictConfig):
        # A primitive at the top level is an error even with no arguments --
        # there is nothing to build and nothing to recurse into.
        raise InstantiationException(
            f"Cannot instantiate config of type {type(config).__name__}."
            " Top level config must be an OmegaConf DictConfig/ListConfig object,"
            " a plain dict/list, or a Structured Config class or instance."
        )

    if _TARGET not in config:
        if args:
            raise InstantiationException(
                "Positional arguments are only supported for a config with _target_"
            )
        recursive, convert, _ = _resolve_flags(config, kwargs, recursive, convert)
        return _instantiate_node(config, recursive, convert)

    return _build(config, kwargs, args, recursive, convert)


#: hydra's historical name for the same function.
call = instantiate
