"""The ``OmegaConf`` facade.

Same static-method surface as ``omegaconf.OmegaConf``, implemented against
plain storage. The hot method is :meth:`OmegaConf.to_container` with
``resolve=True``, which walks the plain tree directly and resolves each
interpolation through a compiled closure, memoized for the duration of the
walk.
"""

from __future__ import annotations

import copy
import io
import pathlib
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from . import resolvers as _resolvers
from ._yaml import load_yaml_file, load_yaml_str, yaml_dump
from .container import (
    _ABSENT,
    MISSING,
    Container,
    DictConfig,
    ListConfig,
    _coerce_assigned,
    _Ctx,
    _is_missing_value,
    _needs_resolve,
    _resolve_inner,
    _resolve_inner2,
    _select_raw,
)
from .errors import (
    ConfigAttributeError,
    ConfigIndexError,
    InterpolationResolutionError,
    MissingMandatoryValue,
    ReadonlyConfigError,
    ValidationError,
)
from .grammar.interpolation import compile_text, split_key
from .merge import merge_configs

__all__ = [
    "OmegaConf",
    "SCMode",
    "SI",
    "II",
    "MISSING",
    "flag_override",
    "open_dict",
    "read_write",
]


class SCMode(Enum):
    """How ``to_container`` should render structured-config nodes."""

    DICT = 1
    """Plain dicts (the default)."""
    DICT_CONFIG = 2
    """Leave them as ``DictConfig``."""
    INSTANTIATE = 3
    """Rebuild instances of the originating dataclass."""


def SI(interpolation: str) -> Any:
    """``SI("${a.b}")`` -- spell an interpolation in Python code."""
    return interpolation


def II(interpolation: str) -> Any:
    """``II("a.b")`` -- shorthand for ``SI("${a.b}")``."""
    return f"${{{interpolation}}}"


class OmegaConf:
    """Static API over :class:`~hydra_fast.container.DictConfig`."""

    # -- construction ----------------------------------------------------
    @staticmethod
    def create(obj: Any = None, parent: Any = None, flags: Optional[Dict] = None) -> Any:
        from ._structured import is_structured, structured_to_plain

        if obj is None:
            return DictConfig._hf_adopt({})
        if isinstance(obj, Container):
            return obj._hf_clone(deep=True)
        if isinstance(obj, str):
            data = load_yaml_str(obj)
            if isinstance(data, list):
                return ListConfig._hf_adopt(data)
            if not isinstance(data, dict):
                raise ValidationError(
                    f"Unexpected YAML content type: {type(data).__name__}"
                )
            return DictConfig._hf_adopt(data)
        if isinstance(obj, dict):
            return DictConfig._hf_adopt(
                {key: _coerce_assigned(value) for key, value in obj.items()}
            )
        if isinstance(obj, (list, tuple)):
            return ListConfig._hf_adopt([_coerce_assigned(item) for item in obj])
        if is_structured(obj):
            from ._structured import type_map_for

            return DictConfig._hf_adopt(structured_to_plain(obj), type_map_for(obj))
        raise ValidationError(f"Unsupported type for create: {type(obj).__name__}")

    @staticmethod
    def structured(obj: Any, parent: Any = None, flags: Optional[Dict] = None) -> Any:
        cfg = OmegaConf.create(obj, parent, flags)
        cfg._set_flag("struct", True)
        return cfg

    # -- io --------------------------------------------------------------
    @staticmethod
    def load(file_: Union[str, pathlib.Path, io.IOBase]) -> Any:
        if isinstance(file_, (str, pathlib.Path)):
            data = load_yaml_file(str(file_))
            # The cache hands out a shared object; copy before the caller can
            # mutate it.
            data = copy.deepcopy(data)
        else:
            data = load_yaml_str(file_.read())  # type: ignore[union-attr]
        if isinstance(data, list):
            return ListConfig._hf_adopt(data)
        if data is None:
            return DictConfig._hf_adopt({})
        if not isinstance(data, dict):
            raise ValidationError(f"Invalid yaml content type: {type(data).__name__}")
        return DictConfig._hf_adopt(data)

    @staticmethod
    def save(
        config: Any, f: Union[str, pathlib.Path, io.IOBase], resolve: bool = False
    ) -> None:
        text = OmegaConf.to_yaml(config, resolve=resolve)
        if isinstance(f, (str, pathlib.Path)):
            with open(f, "w", encoding="utf-8") as handle:
                handle.write(text)
        else:
            if isinstance(f, (io.RawIOBase, io.BufferedIOBase)):
                f.write(text.encode("utf-8"))  # type: ignore[arg-type]
            else:
                f.write(text)  # type: ignore[arg-type]

    @staticmethod
    def to_yaml(cfg: Any, *, resolve: bool = False, sort_keys: bool = False) -> str:
        container = OmegaConf.to_container(cfg, resolve=resolve, enum_to_str=True)
        return yaml_dump(container, sort_keys=sort_keys)

    # -- conversion ------------------------------------------------------
    @staticmethod
    def to_container(
        cfg: Any,
        *,
        resolve: bool = False,
        throw_on_missing: bool = False,
        enum_to_str: bool = False,
        structured_config_mode: "SCMode" = None,  # type: ignore[assignment]
    ) -> Any:
        if not isinstance(cfg, Container):
            raise ValueError(f"Invalid config type: {type(cfg).__name__}")
        if structured_config_mode is None:
            structured_config_mode = SCMode.DICT
        if structured_config_mode == SCMode.DICT_CONFIG:
            # Nothing to convert: this config already *is* the DictConfig the
            # caller is asking for.
            return cfg

        root = cfg._hf_root
        data = cfg._hf_container()
        memo: Dict = {}
        active: set = set()
        container = _to_container(
            root, cfg._hf_path, data, resolve, throw_on_missing, enum_to_str, memo, active
        )
        if structured_config_mode == SCMode.INSTANTIATE:
            return _instantiate_structured(container, data)
        return container

    @staticmethod
    def to_object(cfg: Any) -> Any:
        """Like ``to_container`` but rebuilds dataclass instances."""
        return OmegaConf.to_container(
            cfg,
            resolve=True,
            throw_on_missing=True,
            structured_config_mode=SCMode.INSTANTIATE,
        )

    # -- resolution ------------------------------------------------------
    @staticmethod
    def resolve(cfg: Any) -> None:
        """Replace every interpolation in place with its resolved value."""
        if not isinstance(cfg, Container):
            raise ValueError(f"Invalid config type: {type(cfg).__name__}")
        root = cfg._hf_root
        memo: Dict = {}
        active: set = set()
        _resolve_in_place(root, cfg._hf_path, cfg._hf_container(), memo, active)

    # -- selection / update ----------------------------------------------
    @staticmethod
    def select(
        cfg: Any,
        key: str,
        *,
        default: Any = _ABSENT,
        absolute_key: bool = False,
        throw_on_resolution_failure: bool = True,
        throw_on_missing: bool = False,
    ) -> Any:
        if cfg is None:
            return None if default is _ABSENT else default
        if not isinstance(cfg, Container):
            raise ValueError(f"Invalid config type: {type(cfg).__name__}")

        root = cfg._hf_root
        if key.startswith("."):
            stripped = key.lstrip(".")
            dots = len(key) - len(stripped)
            base = cfg._hf_path[: len(cfg._hf_path) - (dots - 1)] if dots > 1 else cfg._hf_path
            parts = tuple(split_key(stripped))
        elif absolute_key:
            base, parts = (), tuple(split_key(key))
        else:
            base, parts = cfg._hf_path, tuple(split_key(key))

        if not parts:
            return cfg

        target = base + parts
        value = _select_raw(root.data, target)
        if value is _ABSENT:
            return None if default is _ABSENT else default
        if _is_missing_value(value):
            if throw_on_missing:
                raise MissingMandatoryValue(
                    f"Missing mandatory value: {'.'.join(str(p) for p in target)}"
                )
            return None if default is _ABSENT else default

        if _needs_resolve(value):
            try:
                resolved, origin = _resolve_for_select(root, target, value)
            except InterpolationResolutionError:
                if throw_on_resolution_failure:
                    raise
                return None if default is _ABSENT else default
            if isinstance(resolved, Container):
                return resolved
            if isinstance(resolved, dict):
                if origin is not None and _select_raw(root.data, origin) is resolved:
                    return DictConfig._hf_view(root, origin)
                return OmegaConf.create(resolved)
            if isinstance(resolved, list):
                if origin is not None and _select_raw(root.data, origin) is resolved:
                    return ListConfig._hf_view(root, origin)
                return OmegaConf.create(resolved)
            return resolved

        if isinstance(value, dict):
            return DictConfig._hf_view(root, target)
        if isinstance(value, list):
            return ListConfig._hf_view(root, target)
        return value

    @staticmethod
    def update(
        cfg: Any,
        key: str,
        value: Any = None,
        *,
        merge: bool = True,
        force_add: bool = False,
    ) -> None:
        if not isinstance(cfg, Container):
            raise ValueError(f"Invalid config type: {type(cfg).__name__}")
        parts = split_key(key)
        if not parts:
            raise ValueError("Key cannot be empty")

        data = cfg._hf_container()
        struct = cfg._get_flag("struct")
        if cfg._get_flag("readonly"):
            from .container import _describe
            from .errors import decorate

            full_key, object_type = _describe(cfg._hf_root, tuple(parts))
            raise decorate(
                ReadonlyConfigError("Cannot change read-only config container"),
                full_key,
                object_type,
            )

        # The path is accumulated as it is walked rather than sliced out of
        # `parts`, because a list index has to enter it as an int: that is what
        # makes an error about it render as `a[2].b` instead of `a.2.b`.
        walked: Tuple[Any, ...] = ()
        for part in parts[:-1]:
            if isinstance(data, list):
                position = _descent_index(part)
                _check_list_index(cfg, data, walked, position)
                walked += (position,)
                nxt = data[position]
                if not isinstance(nxt, (dict, list)):
                    # A scalar standing where the path continues is replaced by
                    # a mapping, which is what the dict branch below does and
                    # what omegaconf does for a list element too. Descending
                    # into it unconditionally made `data` the scalar itself, so
                    # the next iteration (or the key check further down) failed
                    # with a raw TypeError/AttributeError out of `in` or `.get`.
                    nxt = {}
                    data[position] = nxt
                data = nxt
                continue
            nxt = data.get(part, _ABSENT)
            walked += (part,)
            if nxt is _ABSENT or not isinstance(nxt, (dict, list)):
                if not force_add and struct and nxt is _ABSENT:
                    raise _struct_error(cfg, walked)
                nxt = {}
                data[part] = nxt
            data = nxt

        last = parts[-1]
        plain = _coerce_assigned(value)
        if isinstance(data, list):
            # int() directly, not _descent_index: omegaconf lets the built-in
            # ValueError out for a non-numeric index in the ASSIGNMENT position
            # and only raises its own TypeError while descending. Matching that
            # asymmetry is deliberate.
            index = int(last)
            _check_list_index(cfg, data, walked, index)
            data[index] = _coerce_at(cfg, walked + (index,), plain)
            return
        if last not in data and not force_add and struct:
            raise _struct_error(cfg, walked + (last,))
        existing = data.get(last)
        if merge and isinstance(existing, dict) and isinstance(plain, dict):
            from .merge import merge_into

            merge_into(existing, plain, cfg._hf_root.types, tuple(parts))
        else:
            data[last] = _coerce_at(cfg, tuple(parts), plain)

    # -- predicates ------------------------------------------------------
    @staticmethod
    def is_missing(cfg: Any, key: Any) -> bool:
        if not isinstance(cfg, Container):
            return False
        data = cfg._hf_container()
        try:
            value = data[key] if not isinstance(data, list) else data[int(key)]
        except (KeyError, IndexError, TypeError, ValueError):
            return False
        return _is_missing_value(value)

    @staticmethod
    def is_interpolation(node: Any, key: Any = None) -> bool:
        if key is None:
            return False
        if not isinstance(node, Container):
            return False
        data = node._hf_container()
        try:
            value = data[key] if not isinstance(data, list) else data[int(key)]
        except (KeyError, IndexError, TypeError, ValueError):
            return False
        return _needs_resolve(value)

    @staticmethod
    def is_none(cfg: Any, key: Any = None) -> bool:
        if key is None:
            return cfg is None
        if not isinstance(cfg, Container):
            return False
        try:
            return cfg[key] is None
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def is_config(obj: Any) -> bool:
        return isinstance(obj, Container)

    @staticmethod
    def is_dict(obj: Any) -> bool:
        return isinstance(obj, DictConfig)

    @staticmethod
    def is_list(obj: Any) -> bool:
        return isinstance(obj, ListConfig)

    # -- flags -----------------------------------------------------------
    @staticmethod
    def set_struct(conf: Any, value: Optional[bool]) -> None:
        conf._set_flag("struct", value)

    @staticmethod
    def set_readonly(conf: Any, value: Optional[bool]) -> None:
        conf._set_flag("readonly", value)

    @staticmethod
    def is_readonly(conf: Any) -> Optional[bool]:
        return conf._get_flag("readonly")

    @staticmethod
    def is_struct(conf: Any) -> Optional[bool]:
        return conf._get_flag("struct")

    @staticmethod
    def get_type(obj: Any, key: Optional[str] = None) -> Any:
        if key is not None:
            obj = OmegaConf.select(obj, key)
        if isinstance(obj, DictConfig):
            from ._structured import get_structured_type

            return get_structured_type(obj._hf_container()) or dict
        if isinstance(obj, ListConfig):
            return list
        return type(obj)

    # -- merge -----------------------------------------------------------
    @staticmethod
    def merge(*others: Any) -> Any:
        return merge_configs(*others)

    @staticmethod
    def unsafe_merge(*others: Any) -> Any:
        return merge_configs(*others)

    @staticmethod
    def masked_copy(conf: Any, keys: Union[str, List[str]]) -> Any:
        if isinstance(keys, str):
            keys = [keys]
        data = conf._hf_container()
        return DictConfig._hf_adopt(
            {key: copy.deepcopy(data[key]) for key in keys if key in data}
        )

    # -- dotlist / cli ----------------------------------------------------
    @staticmethod
    def from_dotlist(dotlist: List[str]) -> Any:
        cfg = OmegaConf.create({})
        cfg.merge_with_dotlist(dotlist)
        return cfg

    @staticmethod
    def from_cli(args_list: Optional[List[str]] = None) -> Any:
        import sys

        if args_list is None:
            args_list = sys.argv[1:]
        return OmegaConf.from_dotlist(args_list)

    # -- resolvers --------------------------------------------------------
    @staticmethod
    def register_new_resolver(
        name: str,
        resolver: Callable[..., Any],
        *,
        replace: bool = False,
        use_cache: bool = False,
    ) -> None:
        _resolvers.register_resolver(name, resolver, replace=replace, use_cache=use_cache)

    # 2.4 spelling
    register_resolver = register_new_resolver

    @staticmethod
    def has_resolver(name: str) -> bool:
        return _resolvers.has_resolver(name)

    @staticmethod
    def clear_resolver(name: str) -> bool:
        return _resolvers.clear_resolver(name)

    @staticmethod
    def clear_resolvers() -> None:
        _resolvers.clear_resolvers()

    @staticmethod
    def clear_cache(conf: Any) -> None:
        conf._hf_root.resolver_cache.clear()

    @staticmethod
    def get_cache(conf: Any) -> Dict:
        return conf._hf_root.resolver_cache

    @staticmethod
    def set_cache(conf: Any, cache: Dict) -> None:
        conf._hf_root.resolver_cache.clear()
        conf._hf_root.resolver_cache.update(cache)

    # -- misc -------------------------------------------------------------
    @staticmethod
    def missing_keys(cfg: Any) -> set:
        found: set = set()

        def walk(data: Any, prefix: str) -> None:
            if isinstance(data, dict):
                for key, value in data.items():
                    path = f"{prefix}.{key}" if prefix else str(key)
                    if _is_missing_value(value):
                        found.add(path)
                    else:
                        walk(value, path)
            elif isinstance(data, list):
                for index, value in enumerate(data):
                    path = f"{prefix}[{index}]"
                    if _is_missing_value(value):
                        found.add(path)
                    else:
                        walk(value, path)

        walk(cfg._hf_container(), "")
        return found


# ---------------------------------------------------------------------------
# dotlist merging (method on DictConfig, as in omegaconf)
# ---------------------------------------------------------------------------
def _merge_with_dotlist(self: Any, dotlist: Sequence[str]) -> None:
    from ._yaml import yaml_type_of

    for entry in dotlist:
        if not isinstance(entry, str):
            raise ValueError("Input list must be a list of strings")
        index = entry.find("=")
        if index == -1:
            key, value = entry, None
        else:
            key, raw = entry[:index], entry[index + 1 :]
            value = yaml_type_of(raw)
        # force_add only when the config is not in struct mode: a dotlist
        # naming an undeclared key must raise there, as it does in omegaconf.
        OmegaConf.update(
            self, key, value, merge=True, force_add=not self._get_flag("struct")
        )


def _merge_with(self: Any, *others: Any) -> None:
    """In-place merge.

    Unlike ``OmegaConf.merge`` this mutates, so a read-only config refuses it
    -- but only if the merge would actually change something. Merging an empty
    config is a no-op and is allowed, as it is in omegaconf.
    """
    merged = merge_configs(self, *others)
    result = merged._hf_container()
    data = self._hf_container()
    if result == data:
        return
    self._hf_check_writable()
    if isinstance(data, dict):
        data.clear()
        data.update(result)
    else:
        data[:] = result


DictConfig.merge_with_dotlist = _merge_with_dotlist  # type: ignore[attr-defined]
DictConfig.merge_with = _merge_with  # type: ignore[attr-defined]
ListConfig.merge_with = _merge_with  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# to_container / resolve walkers
# ---------------------------------------------------------------------------
def _to_container(
    root: Any,
    path: Tuple[Any, ...],
    data: Any,
    resolve: bool,
    throw_on_missing: bool,
    enum_to_str: bool,
    memo: Dict,
    active: set,
) -> Any:
    """Walk plain storage, resolving as we go. This is the hot path."""
    if isinstance(data, dict):
        out: Dict[Any, Any] = {}
        for key, value in data.items():
            child = path + (key,)
            out[key] = _to_container_value(
                root, child, value, resolve, throw_on_missing, enum_to_str, memo, active
            )
        return out
    if isinstance(data, list):
        return [
            _to_container_value(
                root,
                path + (index,),
                value,
                resolve,
                throw_on_missing,
                enum_to_str,
                memo,
                active,
            )
            for index, value in enumerate(data)
        ]
    return data


def _to_container_value(
    root: Any,
    path: Tuple[Any, ...],
    value: Any,
    resolve: bool,
    throw_on_missing: bool,
    enum_to_str: bool,
    memo: Dict,
    active: set,
) -> Any:
    if _is_missing_value(value):
        if throw_on_missing:
            raise MissingMandatoryValue(
                f"Missing mandatory value: {'.'.join(str(p) for p in path)}"
            )
        return MISSING
    if isinstance(value, (dict, list)):
        return _to_container(
            root, path, value, resolve, throw_on_missing, enum_to_str, memo, active
        )
    if resolve and _needs_resolve(value):
        resolved, origin = _resolve_inner2(root, path, value, memo, active)
        if isinstance(resolved, Container):
            origin = resolved._hf_path if resolved._hf_root is root else None
            resolved = resolved._hf_container()
        if isinstance(resolved, (dict, list)):
            # A resolved reference can itself hold interpolations, and those
            # resolve relative to where they live, not where we are.
            return _to_container(
                root,
                origin if origin is not None else path,
                resolved,
                resolve,
                throw_on_missing,
                enum_to_str,
                memo,
                active,
            )
        value = resolved
    if enum_to_str and isinstance(value, Enum):
        # omegaconf renders the member name alone, so `to_yaml` emits
        # `c: RED` rather than `c: Color.RED` and the YAML stays loadable
        # against the enum's own values.
        return value.name
    return value


def _instantiate_structured(container: Any, storage: Any) -> Any:
    """Rebuild dataclass instances for nodes that came from a structured config.

    Walks the converted container alongside the original storage, because the
    originating class is recorded against the storage dict (see
    :mod:`hydra_fast._structured`).
    """
    from ._structured import get_structured_type

    if isinstance(container, dict) and isinstance(storage, dict):
        built = {
            key: _instantiate_structured(value, storage.get(key))
            for key, value in container.items()
        }
        origin = get_structured_type(storage)
        if origin is None:
            return built
        try:
            return origin(**built)
        except TypeError:
            # A config that gained keys the dataclass does not accept cannot
            # be instantiated; hand back the dict rather than failing.
            return built
    if isinstance(container, list) and isinstance(storage, list):
        return [
            _instantiate_structured(value, storage[index] if index < len(storage) else None)
            for index, value in enumerate(container)
        ]
    return container


def _descent_index(part: Any) -> int:
    """A list index from a key segment, while walking *into* a config.

    omegaconf reports a non-integer index hit on the way down as a plain
    ``TypeError`` naming the offending segment, not as the ``ValueError``
    ``int()`` would raise.
    """
    try:
        return int(part)
    except (TypeError, ValueError):
        raise TypeError(
            f"Index '{part}' ({type(part).__name__}) is not an int"
        ) from None


def _check_list_index(
    cfg: Any, data: List[Any], prefix: Tuple[Any, ...], position: int
) -> None:
    """Reject an out-of-range list index the way omegaconf does.

    A bare ``IndexError`` would escape otherwise. omegaconf raises a decorated
    ``ConfigIndexError`` carrying ``full_key``/``object_type``, and its message
    is "list index out of range" in both positions -- including when assigning,
    where the built-in says "list assignment index out of range".
    """
    if -len(data) <= position < len(data):
        return
    from .container import _describe
    from .errors import decorate

    full_key, object_type = _describe(cfg._hf_root, prefix + (position,))
    raise decorate(
        ConfigIndexError("list index out of range"), full_key, object_type
    )


def _struct_error(cfg: Any, path: Tuple[Any, ...]) -> Exception:
    """omegaconf's struct-violation error, raised from the update path."""
    from .container import _describe, _struct_message
    from .errors import decorate

    full_key, object_type = _describe(cfg._hf_root, path)
    return decorate(
        ConfigAttributeError(_struct_message(cfg._hf_root, path, path[-1])),
        full_key,
        object_type,
    )


def _coerce_at(cfg: Any, path: Tuple[Any, ...], value: Any) -> Any:
    """Validate a value written at an absolute path in ``cfg``'s root."""
    types = cfg._hf_root.types
    if types is None:
        return value
    from ._typing import lookup, validate

    annotation = lookup(types, path)
    if annotation is None:
        return value
    return validate(value, annotation, ".".join(str(p) for p in path))


def _resolve_for_select(root: Any, path: Tuple[Any, ...], value: Any) -> Tuple[Any, Any]:
    compiled = compile_text(value)
    ctx = _Ctx(root, path)
    return compiled(ctx), ctx.origin


def _resolve_in_place(
    root: Any, path: Tuple[Any, ...], data: Any, memo: Dict, active: set
) -> None:
    if isinstance(data, dict):
        for key, value in list(data.items()):
            child = path + (key,)
            if isinstance(value, (dict, list)):
                _resolve_in_place(root, child, value, memo, active)
            elif _needs_resolve(value):
                resolved = _resolve_inner(root, child, value, memo, active)
                if isinstance(resolved, Container):
                    resolved = resolved._hf_container()
                data[key] = copy.deepcopy(resolved) if isinstance(
                    resolved, (dict, list)
                ) else resolved
    elif isinstance(data, list):
        for index, value in enumerate(data):
            child = path + (index,)
            if isinstance(value, (dict, list)):
                _resolve_in_place(root, child, value, memo, active)
            elif _needs_resolve(value):
                resolved = _resolve_inner(root, child, value, memo, active)
                if isinstance(resolved, Container):
                    resolved = resolved._hf_container()
                data[index] = copy.deepcopy(resolved) if isinstance(
                    resolved, (dict, list)
                ) else resolved


# ---------------------------------------------------------------------------
# context managers
# ---------------------------------------------------------------------------
class flag_override:
    """Temporarily set flags on a config node."""

    def __init__(
        self,
        config: Any,
        names: Union[str, Sequence[str]],
        values: Union[Optional[bool], Sequence[Optional[bool]]],
    ) -> None:
        if isinstance(names, str):
            names = [names]
            values = [values]  # type: ignore[list-item]
        self.config = config
        self.names = list(names)
        self.values = list(values)  # type: ignore[arg-type]
        self.previous: List[Optional[bool]] = []

    def __enter__(self) -> Any:
        self.previous = [self.config._get_node_flag(name) for name in self.names]
        self.config._set_flag(self.names, self.values)
        return self.config

    def __exit__(self, *_exc: Any) -> bool:
        self.config._set_flag(self.names, self.previous)
        return False


class read_write:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.previous: Optional[bool] = None

    def __enter__(self) -> Any:
        self.previous = self.config._get_node_flag("readonly")
        self.config._set_flag("readonly", False)
        return self.config

    def __exit__(self, *_exc: Any) -> bool:
        self.config._set_flag("readonly", self.previous)
        return False


class open_dict:
    """Temporarily disable struct mode, so new keys can be added."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.previous: Optional[bool] = None

    def __enter__(self) -> Any:
        self.previous = self.config._get_node_flag("struct")
        self.config._set_flag("struct", False)
        return self.config

    def __exit__(self, *_exc: Any) -> bool:
        self.config._set_flag("struct", self.previous)
        return False
