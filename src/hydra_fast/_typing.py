"""Runtime type validation for structured configs.

OmegaConf enforces a dataclass's annotations because every value is a typed
``Node`` that validates on assignment. hydra-fast stores plain data, so the
declared types live beside it instead: a ``{path tuple -> annotation}`` map
carried on the config root, consulted on write.

Putting it on writes rather than reads is what makes it free. A sweep does
~1350 reads per 7 writes, and the expensive merge is cached, so validation
costs nothing measurable (see ``bench/proto_typecheck.py``).

Two values bypass validation entirely, because composition produces them
constantly against typed fields:

* ``???`` (MISSING) -- the field is declared but unset;
* any interpolation string -- its type is only knowable once resolved.

The coercion rules are transcribed from omegaconf's typed nodes; see
``bench/oracle_typing.py``, which captures them from the real package and is
the test oracle.
"""

from __future__ import annotations

import dataclasses
import enum
import typing
from typing import Any, Dict, Optional, Tuple

from .errors import ValidationError

__all__ = [
    "TypeMap",
    "declared_types",
    "element_annotation",
    "validate",
]

TypeMap = Dict[Tuple[Any, ...], Any]

_NoneType = type(None)
_MISSING = "???"

try:  # pragma: no cover - attrs is optional
    import attr

    _HAS_ATTRS = True
except ImportError:  # pragma: no cover
    _HAS_ATTRS = False


# ---------------------------------------------------------------------------
# extracting declared types
# ---------------------------------------------------------------------------
def _is_structured(annotation: Any) -> bool:
    if not isinstance(annotation, type):
        return False
    if dataclasses.is_dataclass(annotation):
        return True
    return bool(_HAS_ATTRS and attr.has(annotation))


def _resolve_hints(cls: Any) -> Dict[str, Any]:
    """Annotations with string forms resolved, or ``{}`` if unresolvable.

    ``from __future__ import annotations`` makes every annotation a string;
    ``get_type_hints`` resolves them against the defining module. A class
    defined inside a function can reference names that are not reachable that
    way -- in that case there is nothing to validate against, so the field is
    treated as untyped rather than guessed at.
    """
    try:
        return typing.get_type_hints(cls)
    except Exception:  # noqa: BLE001 - unresolvable hints mean "no checking"
        return {}


def declared_types(cls: Any, prefix: Tuple[Any, ...] = ()) -> TypeMap:
    """Flatten a structured class into ``{path: annotation}``, recursively.

    The class is also recorded *at its own path* (``()`` for the root), which
    is what makes :func:`is_schema_backed` answerable -- omegaconf rejects
    keys absent from a schema, and that check needs to know which containers
    are schema-governed.
    """
    out: TypeMap = {prefix: cls}
    _collect(cls, prefix, out, set())
    return out


def is_schema_backed(types: Optional[TypeMap], path: Tuple[Any, ...]) -> bool:
    """True if the container at ``path`` is governed by a structured schema."""
    if not types:
        return False
    return _is_structured(strip_optional(types.get(path)))


def is_open_mapping(types: Optional[TypeMap], path: Tuple[Any, ...]) -> bool:
    """True if ``path`` is annotated as a plain mapping, e.g. ``Dict[str, int]``.

    Such a field accepts arbitrary keys even inside a struct config -- the
    annotation constrains the *values*, not the key set.
    """
    if not types or path not in types:
        return False
    annotation = strip_optional(types.get(path))
    if annotation in (dict, Dict):
        return True
    return typing.get_origin(annotation) in (dict, typing.Dict)


def known_fields(types: Optional[TypeMap], path: Tuple[Any, ...]) -> set:
    """Field names declared directly under ``path``."""
    if not types:
        return set()
    size = len(path)
    return {
        candidate[size]
        for candidate in types
        if len(candidate) == size + 1 and candidate[:size] == path
    }


def _collect(cls: Any, prefix: Tuple[Any, ...], out: TypeMap, seen: set) -> None:
    # A self-referential schema would otherwise recurse forever.
    if cls in seen:
        return
    seen = seen | {cls}

    hints = _resolve_hints(cls)
    if dataclasses.is_dataclass(cls):
        names = [field.name for field in dataclasses.fields(cls)]
    elif _HAS_ATTRS and attr.has(cls):  # pragma: no cover - needs attrs
        names = [field.name for field in attr.fields(cls)]
    else:
        return

    for name in names:
        annotation = hints.get(name)
        if annotation is None:
            continue
        path = prefix + (name,)
        out[path] = annotation
        inner = strip_optional(annotation)
        if _is_structured(inner):
            _collect(inner, path, out, seen)


def strip_optional(annotation: Any) -> Any:
    """``Optional[X]``/``X | None`` -> ``X``; anything else unchanged."""
    origin = typing.get_origin(annotation)
    if _is_union(origin):
        args = [arg for arg in typing.get_args(annotation) if arg is not _NoneType]
        if len(args) == 1:
            return args[0]
    return annotation


def _is_union(origin: Any) -> bool:
    if origin is typing.Union:
        return True
    # types.UnionType, for `int | None` written without typing.Optional
    return origin is not None and origin.__class__.__name__ == "UnionType" or (
        getattr(origin, "__name__", None) == "UnionType"
    )


def element_annotation(annotation: Any) -> Optional[Any]:
    """Element type of a ``List[X]``, for validating ``append``/``__setitem__``."""
    annotation = strip_optional(annotation)
    if typing.get_origin(annotation) in (list, typing.List):
        args = typing.get_args(annotation)
        return args[0] if args else None
    return None


def value_annotation(annotation: Any) -> Optional[Any]:
    """Value type of a ``Dict[K, V]``, for keys not named in the schema."""
    annotation = strip_optional(annotation)
    if typing.get_origin(annotation) in (dict, typing.Dict):
        args = typing.get_args(annotation)
        return args[1] if len(args) == 2 else None
    return None


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def _fail(value: Any, kind: str, key: Optional[str]) -> None:
    """Raise the bare message; the caller adds the full_key/object_type trailer
    via :func:`hydra_fast.errors.decorate`, which needs the config to describe."""
    raise ValidationError(
        f"Value '{value}' of type '{type(value).__name__}' could not be "
        f"converted to {kind}"
    )


def validate(value: Any, annotation: Any, key: Optional[str] = None) -> Any:
    """Coerce ``value`` to ``annotation``, or raise :class:`ValidationError`.

    Returns the coerced value -- ``"42"`` against ``int`` comes back as ``42``,
    which is the behaviour that matters: without it a YAML override silently
    leaves a string in an int field.
    """
    if annotation is None or annotation is typing.Any:
        return value

    # Bypasses: a missing marker and an interpolation have no type yet.
    if type(value) is str and (value == _MISSING or "${" in value):
        return value

    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)

    if _is_union(origin):
        if value is None:
            if _NoneType in args:
                return None
            _fail(value, _name(annotation), key)
        inner = [arg for arg in args if arg is not _NoneType]
        if len(inner) == 1:
            return validate(value, inner[0], key)
        for candidate in inner:  # a genuine union: first that fits wins
            try:
                return validate(value, candidate, key)
            except ValidationError:
                continue
        _fail(value, _name(annotation), key)

    if origin in (list, typing.List):
        if not isinstance(value, list):
            _fail(value, _name(annotation), key)
        if args:
            return [validate(item, args[0], key) for item in value]
        return value

    if origin in (dict, typing.Dict):
        if not isinstance(value, dict):
            _fail(value, _name(annotation), key)
        if len(args) == 2:
            return {
                validate(k, args[0], key): validate(v, args[1], key)
                for k, v in value.items()
            }
        return value

    if origin is not None:
        # Tuple[...], Set[...] and friends: shape-check only.
        return value

    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return _to_enum(value, annotation, key)

    if _is_structured(annotation):
        # omegaconf refuses to assign a bare mapping over a nested structured
        # field; merging into it is the supported route.
        _fail(value, annotation.__name__, key)

    if annotation is bool:
        return _to_bool(value, key)
    if annotation is int:
        return _to_int(value, key)
    if annotation is float:
        return _to_float(value, key)
    if annotation is str:
        return _to_str(value, key)

    return value


def _name(annotation: Any) -> str:
    return getattr(annotation, "__name__", None) or str(annotation)


def _to_int(value: Any, key: Optional[str]) -> int:
    # bool is a subclass of int, but omegaconf rejects it for an int field.
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        _fail(value, "Integer", key)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except ValueError:
        _fail(value, "Integer", key)
    raise AssertionError  # pragma: no cover - _fail always raises


def _to_float(value: Any, key: Optional[str]) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        _fail(value, "Float", key)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except ValueError:
        _fail(value, "Float", key)
    raise AssertionError  # pragma: no cover


def _to_str(value: Any, key: Optional[str]) -> str:
    # str(True) is 'True', matching omegaconf.
    if not isinstance(value, (str, int, float, bool)):
        _fail(value, "str", key)
    return str(value)


_TRUE = frozenset({"true", "yes", "on", "1"})
_FALSE = frozenset({"false", "no", "off", "0"})


def _to_bool(value: Any, key: Optional[str]) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
    _fail(value, "bool", key)
    raise AssertionError  # pragma: no cover


def _to_enum(value: Any, annotation: Any, key: Optional[str]) -> Any:
    if isinstance(value, annotation):
        return value
    if isinstance(value, str):
        name = value
        prefix = f"{annotation.__name__}."
        if name.startswith(prefix):  # `Color.GREEN` is accepted too
            name = name[len(prefix) :]
        try:
            return annotation[name]
        except KeyError:
            _fail(value, annotation.__name__, key)
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return annotation(value)
        except ValueError:
            _fail(value, annotation.__name__, key)
    _fail(value, annotation.__name__, key)
    raise AssertionError  # pragma: no cover


# ---------------------------------------------------------------------------
# map lookup
# ---------------------------------------------------------------------------
def lookup(types: Optional[TypeMap], path: Tuple[Any, ...]) -> Optional[Any]:
    """Annotation governing ``path``, falling back to a container's arg type.

    A key that is not named in the schema can still be typed: ``Dict[str, int]``
    governs every key under it, and ``List[int]`` every index.
    """
    if not types:
        return None
    annotation = types.get(path)
    if annotation is not None:
        return annotation
    if len(path) < 2:
        return None
    parent = types.get(path[:-1])
    if parent is None:
        return None
    last = path[-1]
    if isinstance(last, int):
        return element_annotation(parent)
    return value_annotation(parent)


def reroot(types: Optional[TypeMap], prefix: Tuple[Any, ...]) -> Optional[TypeMap]:
    """Re-key a map for a subtree, dropping entries outside it."""
    if not types:
        return None
    if not prefix:
        return dict(types)
    size = len(prefix)
    out = {
        path[size:]: annotation
        for path, annotation in types.items()
        if path[:size] == prefix and len(path) >= size
    }
    return out or None


def nest(types: Optional[TypeMap], prefix: Tuple[Any, ...]) -> Optional[TypeMap]:
    """Re-key a map as if its config were mounted under ``prefix``."""
    if not types:
        return None
    if not prefix:
        return dict(types)
    return {prefix + path: annotation for path, annotation in types.items()}


def combine(first: Optional[TypeMap], second: Optional[TypeMap]) -> Optional[TypeMap]:
    """Union of two maps; ``first`` wins on conflict (schema-first merging)."""
    if not first:
        return dict(second) if second else None
    if not second:
        return dict(first)
    merged = dict(second)
    merged.update(first)
    return merged
