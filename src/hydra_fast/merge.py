"""Merge semantics, matching ``OmegaConf.merge`` on plain storage.

The rules (verified against omegaconf 2.3):

* dict into dict merges key by key, recursing where both sides are dicts;
* a list in the source **replaces** the destination list outright;
* ``???`` (MISSING) in the source is skipped, leaving the destination intact;
* ``None`` in the source *does* overwrite;
* any type mismatch is a replacement, in either direction.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ._copy import fast_deepcopy
from .container import MISSING, Container, DictConfig, ListConfig
from .errors import ConfigKeyError

__all__ = ["merge_into", "merge_configs"]


def _is_missing(value: Any) -> bool:
    return type(value) is str and value == MISSING


def merge_into(
    dest: Dict[str, Any],
    src: Dict[str, Any],
    types: Optional[Dict] = None,
    prefix: Tuple[Any, ...] = (),
    struct: bool = False,
) -> None:
    """Merge plain ``src`` into plain ``dest``, in place.

    When ``types`` is given, each merged leaf is validated and coerced against
    the schema. That is what makes ``merge(schema, yaml)`` put an ``int`` in an
    ``int`` field rather than leaving the string YAML gave us -- the one place
    where skipping validation produced a *wrong value* rather than just a
    missing error.

    ``types`` is ``None`` for every schema-less config, which is the common
    case, so the cost there is one ``is None`` test per call.
    """
    if types is None and not struct:
        _merge_plain(dest, src)
        return

    from ._typing import is_open_mapping, is_schema_backed, known_fields

    schema_backed = is_schema_backed(types, prefix) if types else False
    # Always a set: `known_fields` returns an empty one when there is no
    # schema, and it is only consulted when `schema_backed` anyway.
    declared = known_fields(types, prefix) if schema_backed else set()
    # A `Dict[K, V]`-annotated field takes any key; the annotation governs the
    # values. So neither the schema nor struct mode closes it.
    open_mapping = is_open_mapping(types, prefix) if types else False

    for key, src_value in src.items():
        # A key the destination does not already have is only acceptable if
        # the schema declares it. Schema-backed and plain struct mode differ
        # only in the wording omegaconf uses.
        if key not in dest and (schema_backed or struct) and not open_mapping:
            undeclared = key not in declared if schema_backed else True
            if undeclared:
                from .errors import decorate

                if schema_backed:
                    assert types is not None  # implied by schema_backed
                    name = _schema_name(types, prefix)
                    message = f"Key '{key}' not in '{name}'"
                else:
                    name = "dict"
                    message = f"Key '{key}' is not in struct"
                raise decorate(
                    ConfigKeyError(message),
                    ".".join(str(part) for part in prefix + (key,)),
                    name,
                )
        if _is_missing(src_value):
            if key not in dest:
                dest[key] = MISSING
            continue
        path = prefix + (key,)
        if isinstance(src_value, dict):
            dest_value = dest.get(key)
            if not isinstance(dest_value, dict):
                # A dict replaces whatever was there, including a scalar.
                dest_value = {}
                dest[key] = dest_value
            # Always recurse rather than validating the mapping as a whole:
            # merging a dict *into* a nested structured field is legal (and is
            # how schemas get filled in), while assigning one over it is not.
            # Recursion also means each leaf meets its own annotation.
            merge_into(dest_value, src_value, types, path, struct)
            continue
        value = fast_deepcopy(src_value) if isinstance(src_value, list) else src_value
        dest[key] = _validated(value, types, path) if types else value


def _schema_name(types: Dict, path: Tuple[Any, ...]) -> str:
    from ._typing import strip_optional

    annotation = strip_optional(types.get(path))
    return getattr(annotation, "__name__", "config")


def _validated(value: Any, types: Dict, path: Tuple[Any, ...]) -> Any:
    from ._typing import lookup, validate
    from .errors import ValidationError, decorate

    annotation = lookup(types, path)
    if annotation is None:
        return value
    try:
        return validate(value, annotation)
    except ValidationError as exc:
        raise decorate(
            exc,
            ".".join(str(part) for part in path),
            _schema_name(types, path[:-1]),
        ) from None


def _merge_plain(dest: Dict[str, Any], src: Dict[str, Any]) -> None:
    """The schema-less path, unchanged and untouched by validation."""
    for key, src_value in src.items():
        if _is_missing(src_value):
            if key not in dest:
                dest[key] = MISSING
            continue
        if isinstance(src_value, dict):
            dest_value = dest.get(key)
            if isinstance(dest_value, dict):
                _merge_plain(dest_value, src_value)
            else:
                dest[key] = fast_deepcopy(src_value)
            continue
        dest[key] = fast_deepcopy(src_value) if isinstance(src_value, list) else src_value


def _types_of(value: Any) -> Optional[Dict]:
    """Declared-type map of a config or structured value, if it has one."""
    if isinstance(value, Container):
        from ._typing import reroot

        return reroot(value._hf_root.types, value._hf_path)
    from ._structured import is_structured, type_map_for

    if is_structured(value):
        return type_map_for(value)
    return None


def _combine(first: Optional[Dict], second: Optional[Dict]) -> Optional[Dict]:
    from ._typing import combine

    return combine(first, second)


def _as_plain(value: Any) -> Any:
    from ._structured import is_structured, structured_to_plain

    if isinstance(value, Container):
        return value._hf_container()
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (list, tuple)):
        return list(value)
    if is_structured(value):
        return structured_to_plain(value)
    return value


def merge_configs(*configs: Any) -> Any:
    """``OmegaConf.merge`` -- never mutates its inputs."""
    if not configs:
        raise ValueError("merge requires at least one config")

    first = _as_plain(configs[0])
    if isinstance(first, list):
        result: Any = fast_deepcopy(first)
        for other in configs[1:]:
            other_plain = _as_plain(other)
            if not isinstance(other_plain, list):
                raise ValueError(
                    f"Cannot merge {type(other_plain).__name__} into a list config"
                )
            result = fast_deepcopy(other_plain)
        merged = ListConfig._hf_adopt(result)
    else:
        if not isinstance(first, dict):
            raise ValueError(f"Cannot merge a config of type {type(first).__name__}")
        result = fast_deepcopy(first)
        # Schema-first: the type map of the leftmost config governs, which is
        # how `merge(schema, overrides...)` is meant to read.
        types = _types_of(configs[0])
        # struct mode is inherited, so the destination's flag governs the whole
        # merge: a key absent from a struct config is an error at any depth.
        struct = bool(isinstance(configs[0], Container) and configs[0]._get_flag("struct"))
        for other in configs[1:]:
            other_plain = _as_plain(other)
            if isinstance(other_plain, list):
                raise ValueError("Cannot merge a list config into a dict config")
            if not isinstance(other_plain, dict):
                raise ValueError(f"Cannot merge a config of type {type(other_plain).__name__}")
            types = _combine(types, _types_of(other))
            merge_into(result, other_plain, types, struct=struct)
        merged = DictConfig._hf_adopt(result, types)

    # Flags follow the first config, as OmegaConf's do.
    source = configs[0]
    if isinstance(source, Container):
        prefix = source._hf_path
        size = len(prefix)
        for path, store in source._hf_root.flags.items():
            if path[:size] == prefix:
                merged._hf_root.flags[path[size:]] = dict(store)
    return merged
