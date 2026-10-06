"""``DictConfig``/``ListConfig`` as thin views over plain dicts and lists.

OmegaConf wraps every single value in a ``Node`` object carrying its own
metadata and parent pointer. That is what makes it flexible -- and it is also
why merging, copying and walking a config are all expensive: a 600-key config
is 600+ Python objects to allocate, link and later deep-copy.

Here the storage *is* a plain ``dict``/``list`` tree. ``DictConfig`` is a view:
three slots (root, path, and a cached reference to its own container). Creating
one is nearly free, merging is a dict walk, and ``copy.deepcopy`` copies plain
data instead of an object graph.

Interpolation strings stay in storage as plain ``str`` and are resolved on
access, through the compiled closures in :mod:`hydra_fast.grammar`.
"""

from __future__ import annotations

import copy
from collections.abc import MutableMapping, MutableSequence
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union

from ._copy import fast_deepcopy
from .errors import (
    ConfigAttributeError,
    ConfigIndexError,
    ConfigKeyError,
    ConfigTypeError,
    InterpolationKeyError,
    InterpolationResolutionError,
    InterpolationToMissingValueError,
    MissingMandatoryValue,
    ReadonlyConfigError,
    UnsupportedInterpolationType,
    ValidationError,
    decorate,
)
from .grammar import interpolation as _ip

__all__ = ["DictConfig", "ListConfig", "Container", "MISSING", "Node"]

MISSING = "???"

_PRIMITIVES = (int, float, str, bool, type(None))


class _MissingType:
    """Marker for ``OmegaConf.MISSING`` assignment."""

    _instance = None

    def __new__(cls) -> "_MissingType":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "'???'"

    def __str__(self) -> str:
        return MISSING

    def __bool__(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# root: shared mutable state behind every view
# ---------------------------------------------------------------------------
class _Root:
    """Storage plus whole-config state shared by all views of one config."""

    __slots__ = (
        "data",
        "flags",
        "resolver_cache",
        "types",
        "interp_base",
        "node_cache",
    )

    def __init__(self, data: Any, types: Optional[Dict] = None) -> None:
        self.data = data
        # path tuple -> {flag name: bool|None}; inherited by walking up
        self.flags: Dict[Tuple[Any, ...], Dict[str, Optional[bool]]] = {}
        # for resolvers registered with use_cache=True
        self.resolver_cache: Dict[Tuple[Any, ...], Any] = {}
        # path tuple -> declared annotation, for structured configs. None when
        # the config has no schema, which is the common case and must cost
        # nothing: every write path checks `is None` first.
        self.types: Optional[Dict[Tuple[Any, ...], Any]] = types
        # Set when this container's absolute `${a.b}` references belong to
        # another config -- `oc.dict.values` returns a standalone list of
        # references into the config it was called on. None for everything
        # else, which is the overwhelming majority.
        self.interp_base: Optional["_Root"] = None
        # Lazily populated ValueNode proxies, so `_get_node(k)` returns the
        # same object each time as omegaconf's does. Empty unless something
        # actually asks for a node.
        self.node_cache: Dict[Tuple[Any, ...], Any] = {}


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------
class _Ctx:
    """Evaluation context handed to a compiled interpolation closure.

    Errors raised during resolution carry the location of the value that
    *held* the interpolation, which is what omegaconf reports and what the
    reader needs in order to find it.
    """

    __slots__ = ("root", "path", "memo", "active", "origin")

    def __init__(
        self,
        root: _Root,
        path: Tuple[Any, ...],
        memo: Optional[Dict] = None,
        active: Optional[set] = None,
    ) -> None:
        self.root = root
        self.path = path
        self.memo = memo if memo is not None else {}
        self.active = active if active is not None else set()
        self.origin: Optional[Tuple[Any, ...]] = None

    def _at(self, exc: Any) -> Any:
        full_key, object_type = _describe(self.root, self.path)
        return decorate(exc, full_key, object_type)

    # -- node interpolation ---------------------------------------------
    def node(self, dots: int, parts: Tuple[str, ...], spelling: str) -> Any:
        root = self.root
        if dots:
            # `${.b}` is relative to the container holding the value, so each
            # dot strips one component off the value's own path.
            if dots > len(self.path):
                raise self._at(
                    InterpolationKeyError(f"Interpolation key '{spelling}' not found")
                )
            base_path = self.path[: len(self.path) - dots]
        else:
            base_path = ()
            if root.interp_base is not None:
                root = root.interp_base

        target = base_path + tuple(parts)
        try:
            value = _select_raw(root.data, target)
        except (KeyError, IndexError, TypeError):
            raise self._at(
                InterpolationKeyError(f"Interpolation key '{spelling}' not found")
            ) from None
        if value is _ABSENT:
            raise self._at(
                InterpolationKeyError(f"Interpolation key '{spelling}' not found")
            )

        resolved = _resolve_inner(root, target, value, self.memo, self.active)
        if _is_missing_value(resolved):
            raise self._at(
                InterpolationToMissingValueError(
                    f"MissingMandatoryValue while resolving interpolation: "
                    f"Interpolation key '{spelling}' is missing"
                )
            )
        self.origin = target if root is self.root else None
        if type(resolved) is dict:
            # A node reference to a container yields a *view*, not the raw
            # dict: resolvers receive `${some.node}` as a DictConfig and call
            # OmegaConf APIs on it. Concatenation unwraps again via to_plain.
            return DictConfig._hf_view(root, target)
        if type(resolved) is list:
            return ListConfig._hf_view(root, target)
        return resolved

    # -- resolver interpolation -----------------------------------------
    def resolver(self, name: str, args: Tuple[Any, ...], args_str: Tuple[str, ...]) -> Any:
        from .resolvers import lookup_resolver

        entry = lookup_resolver(name)
        if entry is None:
            raise self._at(
                UnsupportedInterpolationType(f"Unsupported interpolation type {name}")
            )
        func, use_cache, needs_parent, needs_node, needs_root = entry

        # Keyed on the argument *spellings*, as OmegaConf does: `${f:${x}}` is
        # one cache entry regardless of what `x` holds.
        key = (name, args_str)
        if use_cache:
            cached = self.root.resolver_cache.get(key, _ABSENT)
            if cached is not _ABSENT:
                self.origin = None
                return cached

        kwargs: Dict[str, Any] = {}
        if needs_parent:
            kwargs["_parent_"] = _wrap(self.root, self.path[:-1])
        if needs_node:
            kwargs["_node_"] = _wrap(self.root, self.path)
        if needs_root:
            kwargs["_root_"] = _wrap(self.root, ())

        try:
            value = func(*args, **kwargs) if kwargs else func(*args)
        except (
            InterpolationResolutionError,
            UnsupportedInterpolationType,
            MissingMandatoryValue,
        ):
            raise
        except Exception as exc:
            raise self._at(
                InterpolationResolutionError(
                    f"{type(exc).__name__} raised while resolving interpolation: {exc}"
                )
            ) from exc

        if use_cache:
            self.root.resolver_cache[key] = value
        self.origin = None
        return value


_ABSENT = object()


def _select_raw(data: Any, parts: Sequence[Any]) -> Any:
    """Walk plain storage along ``parts``; ``_ABSENT`` if the path is absent."""
    node = data
    for part in parts:
        if isinstance(node, dict):
            if part in node:
                node = node[part]
                continue
            # YAML keys are typed, so a path segment spelled "0" or "true" may
            # need coercing before it matches.
            coerced = _coerce_key(part, node)
            if coerced is _ABSENT:
                return _ABSENT
            node = node[coerced]
        elif isinstance(node, list):
            try:
                index = int(part)
            except (TypeError, ValueError):
                return _ABSENT
            if -len(node) <= index < len(node):
                node = node[index]
            else:
                return _ABSENT
        else:
            return _ABSENT
    return node


def _coerce_key(part: Any, node: dict) -> Any:
    if not isinstance(part, str):
        return _ABSENT
    for caster in (int, _as_bool):
        try:
            candidate = caster(part)
        except (TypeError, ValueError):
            continue
        if candidate in node:
            return candidate
    return _ABSENT


def _as_bool(text: str) -> bool:
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    raise ValueError(text)


def _is_missing_value(value: Any) -> bool:
    return value is MISSING or (type(value) is str and value == MISSING)


def _needs_resolve(value: Any) -> bool:
    return type(value) is str and "${" in value


def _resolve_inner(
    root: _Root,
    path: Tuple[Any, ...],
    value: Any,
    memo: Dict,
    active: set,
) -> Any:
    """Resolve one stored value, following chained interpolations."""
    return _resolve_inner2(root, path, value, memo, active)[0]


def _resolve_inner2(
    root: _Root,
    path: Tuple[Any, ...],
    value: Any,
    memo: Dict,
    active: set,
) -> Tuple[Any, Optional[Tuple[Any, ...]]]:
    """As :func:`_resolve_inner`, also reporting where the value came from.

    The origin is the storage path of the referenced node when the whole value
    was a single node reference. Callers use it to keep resolving the
    referenced subtree in *its* own context -- ``${a}`` pointing at a subtree
    full of ``${.sibling}`` references must resolve them relative to ``a``.
    """
    if not _needs_resolve(value):
        return value, None

    key = (path, value)
    hit = memo.get(key, _ABSENT)
    if hit is not _ABSENT:
        return hit  # type: ignore[return-value]
    if key in active:
        raise InterpolationResolutionError(
            f"Recursive interpolation detected while resolving '{value}'"
        )

    active.add(key)
    try:
        compiled = _ip.compile_text(value)
        ctx = _Ctx(root, path, memo, active)
        result = compiled(ctx)
        origin = ctx.origin
    finally:
        active.discard(key)

    memo[key] = (result, origin)
    return result, origin


def _resolve_at(root: _Root, path: Tuple[Any, ...], value: Any) -> Tuple[Any, Optional[Tuple]]:
    """Resolve a value for an accessor; also report where it came from.

    The origin path lets the caller return a live view of the referenced node
    (``cfg.alias`` where ``alias: ${real}`` gives a view of ``real``), which is
    what OmegaConf does.
    """
    if not _needs_resolve(value):
        return value, None
    compiled = _ip.compile_text(value)
    ctx = _Ctx(root, path)
    result = compiled(ctx)
    return result, ctx.origin


# late-bound hook used by compiled closures to stringify container values
def _to_plain(value: Any) -> Any:
    if isinstance(value, (DictConfig, ListConfig)):
        return value._hf_container()
    return value


_ip.to_plain = _to_plain  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------
def _format_key(path: Tuple[Any, ...]) -> str:
    """``a.b[0].c`` -- list indices in brackets, as omegaconf renders them."""
    out = ""
    for part in path:
        if isinstance(part, int):
            out += f"[{part}]"
        elif out:
            out += f".{part}"
        else:
            out = str(part)
    return out


def _describe(root: _Root, path: Tuple[Any, ...]) -> Tuple[str, str]:
    """``(full_key, object_type)`` for an error about ``path``.

    ``object_type`` names the *container* holding the key -- the schema class
    when that container is schema-backed, otherwise ``dict``/``list``.
    """
    full_key = _format_key(path)
    parent = path[:-1]
    if root.types is not None:
        from ._typing import is_schema_backed, strip_optional

        if is_schema_backed(root.types, parent):
            annotation = strip_optional(root.types.get(parent))
            return full_key, getattr(annotation, "__name__", "dict")
    node = _select_raw(root.data, parent)
    return full_key, "list" if isinstance(node, list) else "dict"


def _struct_message(root: _Root, path: Tuple[Any, ...], key: Any) -> str:
    """omegaconf names the schema when there is one, else says "struct"."""
    if root.types is not None:
        from ._typing import is_schema_backed, strip_optional

        parent = path[:-1]
        if is_schema_backed(root.types, parent):
            annotation = strip_optional(root.types.get(parent))
            name = getattr(annotation, "__name__", None)
            if name:
                return f"Key '{key}' not in '{name}'"
    return f"Key '{key}' is not in struct"


def make_reference_list(items: List[str], base: _Root) -> "ListConfig":
    """A standalone list of interpolation strings resolving against ``base``.

    Used by ``oc.dict.values``, which omegaconf returns as live references
    rather than resolved values, so later edits to the source show through.
    """
    root = _Root(list(items))
    root.interp_base = base
    return ListConfig._hf_view(root, ())


def _wrap(root: _Root, path: Tuple[Any, ...]) -> Any:
    node = _select_raw(root.data, path)
    if isinstance(node, dict):
        return DictConfig._hf_view(root, path)
    if isinstance(node, list):
        return ListConfig._hf_view(root, path)
    return node


class Node:
    """Base of the view hierarchy; present so ``isinstance`` checks work."""

    __slots__ = ()


class Container(Node):
    """Common behaviour for :class:`DictConfig` and :class:`ListConfig`."""

    __slots__ = ("_hf_root", "_hf_path")

    _hf_root: _Root
    _hf_path: Tuple[Any, ...]

    # -- construction ----------------------------------------------------
    @classmethod
    def _hf_view(cls, root: _Root, path: Tuple[Any, ...]) -> Any:
        view = object.__new__(cls)
        object.__setattr__(view, "_hf_root", root)
        object.__setattr__(view, "_hf_path", path)
        return view

    @classmethod
    def _hf_adopt(cls, data: Any, types: Optional[Dict] = None) -> Any:
        """Wrap already-plain storage without copying it.

        The caller hands over ownership: composition builds plain dicts and
        wraps them at the end, and copying there would undo the point.
        """
        return cls._hf_view(_Root(data, types), ())

    # -- storage ---------------------------------------------------------
    def _hf_container(self) -> Any:
        """The plain dict/list this view points at."""
        node = _select_raw(self._hf_root.data, self._hf_path)
        if node is _ABSENT:
            raise ConfigKeyError(f"Node '{self._hf_key_path()}' no longer exists")
        return node

    def _hf_key_path(self) -> str:
        return _format_key(self._hf_path)

    # -- omegaconf-internal surface --------------------------------------
    @property
    def _metadata(self) -> Any:
        """Stand-in for omegaconf's container ``Metadata``."""
        from .nodes import _NodeMetadata

        node = _select_raw(self._hf_root.data, self._hf_path)
        object_type: Any = list if isinstance(node, list) else dict
        if self._hf_root.types is not None:
            from ._typing import is_schema_backed, strip_optional

            if is_schema_backed(self._hf_root.types, self._hf_path):
                object_type = strip_optional(self._hf_root.types.get(self._hf_path))
        return _NodeMetadata(
            key=self._hf_path[-1] if self._hf_path else None,
            object_type=object_type,
            flags=self._hf_root.flags.get(self._hf_path, {}),
        )

    def _key(self) -> Any:
        return self._hf_path[-1] if self._hf_path else None

    def _get_full_key(self, key: Any = None) -> str:
        parts = list(self._hf_path)
        if key not in (None, ""):
            parts.append(key)
        return _format_key(tuple(parts))

    def _get_parent(self) -> Any:
        if not self._hf_path:
            return None
        return _wrap(self._hf_root, self._hf_path[:-1])

    def _get_root(self) -> Any:
        return _wrap(self._hf_root, ())

    def _is_missing(self) -> bool:
        return False

    def _is_interpolation(self) -> bool:
        return False

    def _is_none(self) -> bool:
        return False

    def resolve_parse_tree(
        self,
        parse_tree: Any,
        node: Any = None,
        memo: Any = None,
        key: Any = None,
    ) -> Any:
        """Evaluate a parsed interpolation against this config.

        Compatible with omegaconf's method of the same name. ``parse_tree`` is
        whatever :func:`hydra_fast.grammar.interpolation.parse` returned -- a
        compiled closure rather than an ANTLR tree, but used the same way:
        parse once, resolve against as many configs as you like.
        """
        path = self._hf_path
        if node is not None and hasattr(node, "_hf_path"):
            path = node._hf_path
        if key is not None:
            path = path + (key,)
        ctx = _Ctx(self._hf_root, path)
        return parse_tree(ctx)

    # -- flags -----------------------------------------------------------
    def _set_flag(
        self,
        flags: Union[str, Sequence[str]],
        values: Union[Optional[bool], Sequence[Optional[bool]]],
    ) -> Any:
        if isinstance(flags, str):
            flags, values = [flags], [values]  # type: ignore[list-item]
        assert len(flags) == len(values)  # type: ignore[arg-type]
        store = self._hf_root.flags.setdefault(self._hf_path, {})
        for flag, value in zip(flags, values, strict=False):  # type: ignore[arg-type]
            if value is None:
                store.pop(flag, None)
            else:
                store[flag] = value
        return self

    def _get_node_flag(self, flag: str) -> Optional[bool]:
        return self._hf_root.flags.get(self._hf_path, {}).get(flag)

    def _get_flag(self, flag: str) -> Optional[bool]:
        """Flags are inherited from ancestors, nearest wins."""
        flags = self._hf_root.flags
        path = self._hf_path
        for stop in range(len(path), -1, -1):
            value = flags.get(path[:stop], {}).get(flag)
            if value is not None:
                return value
        return None

    def _hf_check_writable(self, key: Any = None, message: Optional[str] = None) -> None:
        if self._get_flag("readonly"):
            path = self._hf_path if key is None else self._hf_path + (key,)
            full_key, object_type = _describe(self._hf_root, path)
            raise decorate(
                ReadonlyConfigError(
                    message or "Cannot change read-only config container"
                ),
                full_key,
                object_type,
            )

    def _hf_struct_error(self, exc_type: Any, key: Any) -> Exception:
        """Build omegaconf's struct-violation error for ``key``."""
        path = self._hf_path + (key,)
        full_key, object_type = _describe(self._hf_root, path)
        message = _struct_message(self._hf_root, path, key)
        return decorate(exc_type(message), full_key, object_type)

    def _hf_coerce(self, path: Tuple[Any, ...], value: Any) -> Any:
        """Validate/coerce against the declared type for ``path``, if any.

        The ``is None`` short-circuit keeps schema-less configs -- almost all
        of them -- on exactly the path they were on before.
        """
        types = self._hf_root.types
        if types is None:
            return value
        from ._typing import lookup, validate

        annotation = lookup(types, path)
        if annotation is None:
            return value
        from .errors import ValidationError as _VE

        try:
            return validate(value, annotation)
        except _VE as exc:
            full_key, object_type = _describe(self._hf_root, path)
            raise decorate(exc, full_key, object_type) from None

    # -- dunder ----------------------------------------------------------
    def __len__(self) -> int:
        return len(self._hf_container())

    def __bool__(self) -> bool:
        return bool(self._hf_container())

    def __copy__(self) -> Any:
        return self._hf_clone(deep=False)

    def __deepcopy__(self, memo: Optional[Dict] = None) -> Any:
        return self._hf_clone(deep=True)

    def _hf_clone(self, deep: bool) -> Any:
        data = self._hf_container()
        data = fast_deepcopy(data) if deep else copy.copy(data)
        from ._typing import reroot

        root = _Root(data, reroot(self._hf_root.types, self._hf_path))
        # Re-root the flags that applied to this subtree.
        prefix = self._hf_path
        size = len(prefix)
        for path, store in self._hf_root.flags.items():
            if path[:size] == prefix:
                root.flags[path[size:]] = dict(store)
        return type(self)._hf_view(root, ())

    def copy(self) -> Any:
        return self._hf_clone(deep=True)

    def __repr__(self) -> str:
        return repr(self._hf_container())

    def __str__(self) -> str:
        return str(self._hf_container())

    # -- resolution helpers ---------------------------------------------
    def _hf_get(self, path: Tuple[Any, ...], value: Any, *, resolve: bool = True) -> Any:
        """Turn stored ``value`` at ``path`` into what an accessor returns."""
        if _is_missing_value(value):
            full_key, object_type = _describe(self._hf_root, path)
            raise decorate(
                MissingMandatoryValue(f"Missing mandatory value: {full_key}"),
                full_key,
                object_type,
            )
        if resolve and _needs_resolve(value):
            resolved, origin = _resolve_at(self._hf_root, path, value)
            if isinstance(resolved, Container):
                return resolved
            if isinstance(resolved, dict):
                if origin is not None and _select_raw(self._hf_root.data, origin) is resolved:
                    return DictConfig._hf_view(self._hf_root, origin)
                return DictConfig(resolved)
            if isinstance(resolved, list):
                if origin is not None and _select_raw(self._hf_root.data, origin) is resolved:
                    return ListConfig._hf_view(self._hf_root, origin)
                return ListConfig(resolved)
            return resolved
        if isinstance(value, dict):
            return DictConfig._hf_view(self._hf_root, path)
        if isinstance(value, list):
            return ListConfig._hf_view(self._hf_root, path)
        return value


def _coerce_assigned(value: Any) -> Any:
    """Convert an assigned value into plain storage.

    Interpolation strings are syntax-checked here rather than on first read,
    because that is where omegaconf checks them: ``create({"a": "${bad("})``
    raises immediately, which puts the error next to the input that caused it.
    The check is a cached compile, and this function already walks the whole
    value, so it costs a dict lookup per distinct string.
    """
    if isinstance(value, (DictConfig, ListConfig)):
        return fast_deepcopy(value._hf_container())
    if isinstance(value, _MissingType):
        return MISSING
    if isinstance(value, dict):
        return {key: _coerce_assigned(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_coerce_assigned(item) for item in value]
    if type(value) is str:
        if "${" in value:
            _ip.compile_text(value)  # raises GrammarParseError if malformed
        return value
    if isinstance(value, _PRIMITIVES):
        return value
    if isinstance(value, bytes):
        return value
    # Enums, Paths and arbitrary objects are stored as-is; OmegaConf keeps
    # enums and paths too, and refuses the rest only on validation.
    return value


class DictConfig(Container):
    """Mapping view over a plain ``dict``."""

    __slots__ = ()

    def __init__(
        self,
        content: Any = None,
        parent: Any = None,
        flags: Optional[Dict[str, bool]] = None,
        key: Any = None,
    ) -> None:
        if isinstance(content, DictConfig):
            data = copy.deepcopy(content._hf_container())
        elif content is None:
            data = {}
        elif isinstance(content, str):
            # a lone interpolation string standing in for a whole node
            data = content  # type: ignore[assignment]
        elif isinstance(content, dict):
            data = {key_: _coerce_assigned(value) for key_, value in content.items()}
        else:
            from ._structured import is_structured, structured_to_plain

            if is_structured(content):
                data = structured_to_plain(content)
            else:
                raise ValidationError(
                    f"Unsupported value type for DictConfig: {type(content).__name__}"
                )
        object.__setattr__(self, "_hf_root", _Root(data))
        object.__setattr__(self, "_hf_path", ())
        if flags:
            self._set_flag(list(flags.keys()), list(flags.values()))

    # -- reads -----------------------------------------------------------
    def __getattr__(self, name: str) -> Any:
        if name.startswith("_hf_") or name.startswith("__"):
            raise AttributeError(name)
        data = self._hf_container()
        try:
            value = data[name]
        except KeyError:
            if self._get_flag("struct") is False:
                return None
            raise self._hf_struct_error(ConfigAttributeError, name) from None
        except TypeError:
            raise ConfigAttributeError(f"Key '{name}' not accessible") from None
        return self._hf_get(self._hf_path + (name,), value)

    def __getitem__(self, key: Any) -> Any:
        data = self._hf_container()
        try:
            value = data[key]
        except KeyError:
            if self._get_flag("struct") is False:
                return None
            raise self._hf_struct_error(ConfigKeyError, key) from None
        except TypeError as exc:
            raise ConfigTypeError(str(exc)) from None
        return self._hf_get(self._hf_path + (key,), value)

    def get(self, key: Any, default_value: Any = _ABSENT) -> Any:
        data = self._hf_container()
        if key not in data:
            if default_value is not _ABSENT:
                return default_value
            if self._get_flag("struct"):
                raise self._hf_struct_error(ConfigAttributeError, key)
            return None
        return self._hf_get(self._hf_path + (key,), data[key])

    def _get_node(self, key: Any, validate_access: bool = True, default_value: Any = _ABSENT) -> Any:
        """The node at ``key``: a container view, or a live ``ValueNode``.

        OmegaConf-internal API. Scalars come back wrapped because callers do
        ``node._value()`` / ``isinstance(node, IntegerNode)``; see
        :mod:`hydra_fast.nodes`.
        """
        data = self._hf_container()
        if key not in data:
            if default_value is not _ABSENT:
                return default_value
            if validate_access and self._get_flag("struct"):
                raise self._hf_struct_error(ConfigAttributeError, key)
            return None
        path = self._hf_path + (key,)
        value = data[key]
        if isinstance(value, dict):
            return DictConfig._hf_view(self._hf_root, path)
        if isinstance(value, list):
            return ListConfig._hf_view(self._hf_root, path)
        from .nodes import node_for

        return node_for(self._hf_root, path)

    def _hf_join(self, key: Any) -> str:
        return _format_key(self._hf_path + (key,))

    # -- writes ----------------------------------------------------------
    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_hf_"):
            object.__setattr__(self, name, value)
            return
        self._hf_set(name, value, attribute=True)

    def __setitem__(self, key: Any, value: Any) -> None:
        self._hf_set(key, value, attribute=False)

    def _hf_set(self, key: Any, value: Any, attribute: bool) -> None:
        self._hf_check_writable(key)
        data = self._hf_container()
        if key not in data and self._get_flag("struct"):
            error = ConfigAttributeError if attribute else ConfigKeyError
            raise self._hf_struct_error(error, key)
        plain = _coerce_assigned(value)
        data[key] = self._hf_coerce(self._hf_path + (key,), plain)

    def __delitem__(self, key: Any) -> None:
        self._hf_check_writable(key)
        if self._get_flag("struct"):
            raise ConfigTypeError(
                f"DictConfig in struct mode does not support deletion of key '{key}'"
            )
        try:
            del self._hf_container()[key]
        except KeyError:
            raise ConfigKeyError(f"Key '{key}' does not exist") from None

    def __delattr__(self, name: str) -> None:
        self.__delitem__(name)

    def pop(self, key: Any, default: Any = _ABSENT) -> Any:
        self._hf_check_writable(key)
        if self._get_flag("struct"):
            raise ConfigTypeError(
                f"DictConfig in struct mode does not support pop of key '{key}'"
            )
        data = self._hf_container()
        if key not in data:
            if default is not _ABSENT:
                return default
            full_key, object_type = _describe(self._hf_root, self._hf_path + (key,))
            raise decorate(
                ConfigKeyError(f"Key not found: '{key}'"), full_key, object_type
            )
        value = self._hf_get(self._hf_path + (key,), data[key])
        if isinstance(value, Container):
            value = value._hf_clone(deep=True)
        del data[key]
        return value

    def setdefault(self, key: Any, default: Any = None) -> Any:
        data = self._hf_container()
        if key in data:
            return self._hf_get(self._hf_path + (key,), data[key])
        self._hf_set(key, default, attribute=False)
        return self._hf_get(self._hf_path + (key,), data[key])

    def update(self, *args: Any, **kwargs: Any) -> None:
        other: Dict[Any, Any] = {}
        if args:
            source = args[0]
            if isinstance(source, DictConfig):
                source = source._hf_container()
            other.update(source)
        other.update(kwargs)
        for key, value in other.items():
            self._hf_set(key, value, attribute=False)

    def clear(self) -> None:
        self._hf_check_writable()
        self._hf_container().clear()

    # -- iteration -------------------------------------------------------
    def keys(self) -> Any:
        return self._hf_container().keys()

    def __iter__(self) -> Iterator[Any]:
        return iter(self._hf_container())

    def __contains__(self, key: Any) -> bool:
        # A `???` value reads as absent, matching omegaconf.
        data = self._hf_container()
        return key in data and not _is_missing_value(data[key])

    def values(self) -> List[Any]:
        return [self[key] for key in self._hf_container()]

    def items(self) -> List[Tuple[Any, Any]]:
        return [(key, self[key]) for key in self._hf_container()]

    # -- comparison ------------------------------------------------------
    def __eq__(self, other: Any) -> bool:
        if isinstance(other, DictConfig):
            return self._hf_container() == other._hf_container()
        if isinstance(other, dict):
            return self._hf_container() == _coerce_assigned(other)
        return NotImplemented

    def __ne__(self, other: Any) -> bool:
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    __hash__ = None  # type: ignore[assignment]


class ListConfig(Container):
    """Sequence view over a plain ``list``."""

    __slots__ = ()

    def __init__(
        self,
        content: Any = None,
        parent: Any = None,
        flags: Optional[Dict[str, bool]] = None,
        key: Any = None,
    ) -> None:
        if isinstance(content, ListConfig):
            data = copy.deepcopy(content._hf_container())
        elif content is None:
            data = []
        elif isinstance(content, str):
            data = content  # type: ignore[assignment]
        elif isinstance(content, (list, tuple)):
            data = [_coerce_assigned(item) for item in content]
        else:
            raise ValidationError(
                f"Unsupported value type for ListConfig: {type(content).__name__}"
            )
        object.__setattr__(self, "_hf_root", _Root(data))
        object.__setattr__(self, "_hf_path", ())
        if flags:
            self._set_flag(list(flags.keys()), list(flags.values()))

    def __getitem__(self, index: Any) -> Any:
        data = self._hf_container()
        if isinstance(index, slice):
            return [
                self._hf_get(self._hf_path + (i,), data[i])
                for i in range(*index.indices(len(data)))
            ]
        try:
            value = data[index]
        except IndexError:
            raise ConfigIndexError(f"list index out of range: {index}") from None
        except TypeError:
            try:
                index = int(index)
            except (TypeError, ValueError):
                raise ConfigTypeError(
                    f"ListConfig indices must be integers, not {type(index).__name__}"
                ) from None
            value = data[index]
        if index < 0:
            index += len(data)
        return self._hf_get(self._hf_path + (index,), value)

    def __setitem__(self, index: Any, value: Any) -> None:
        self._hf_check_writable(index, "ListConfig is read-only")
        plain = _coerce_assigned(value)
        if isinstance(index, int):
            plain = self._hf_coerce(self._hf_path + (index,), plain)
        self._hf_container()[index] = plain

    def __delitem__(self, index: Any) -> None:
        self._hf_check_writable(
            index if isinstance(index, int) else None,
            "Cannot delete item from read-only ListConfig",
        )
        del self._hf_container()[index]

    def __iter__(self) -> Iterator[Any]:
        data = self._hf_container()
        base = self._hf_path
        for index in range(len(data)):
            yield self._hf_get(base + (index,), data[index])

    def __contains__(self, item: Any) -> bool:
        return any(value == item for value in self)

    def append(self, item: Any) -> None:
        self._hf_check_writable(len(self._hf_container()), "ListConfig is read-only")
        data = self._hf_container()
        self._hf_container().append(
            self._hf_coerce(self._hf_path + (len(data),), _coerce_assigned(item))
        )

    def extend(self, items: Any) -> None:
        for item in items:
            self.append(item)

    def insert(self, index: int, item: Any) -> None:
        self._hf_check_writable(index, "Cannot insert into a read-only ListConfig")
        self._hf_container().insert(
            index, self._hf_coerce(self._hf_path + (index,), _coerce_assigned(item))
        )

    def pop(self, index: int = -1) -> Any:
        self._hf_check_writable(index, "Cannot pop from read-only ListConfig")
        data = self._hf_container()
        if index < 0:
            index += len(data)
        value = self._hf_get(self._hf_path + (index,), data[index])
        if isinstance(value, Container):
            value = value._hf_clone(deep=True)
        del data[index]
        return value

    def remove(self, item: Any) -> None:
        self._hf_check_writable()
        data = self._hf_container()
        for index, value in enumerate(data):
            if value == item or self._hf_get(self._hf_path + (index,), value) == item:
                del data[index]
                return
        raise ValueError(f"Item not found: {item}")

    def index(self, item: Any, *args: Any) -> int:
        for index, value in enumerate(self):
            if value == item:
                return index
        raise ValueError(f"Item not found: {item}")

    def count(self, item: Any) -> int:
        return sum(1 for value in self if value == item)

    def clear(self) -> None:
        self._hf_check_writable()
        self._hf_container().clear()

    def sort(self, key: Any = None, reverse: bool = False) -> None:
        self._hf_check_writable()
        self._hf_container().sort(key=key, reverse=reverse)

    def _get_node(self, index: Any, validate_access: bool = True) -> Any:
        data = self._hf_container()
        path = self._hf_path + (index,)
        value = data[index]
        if isinstance(value, dict):
            return DictConfig._hf_view(self._hf_root, path)
        if isinstance(value, list):
            return ListConfig._hf_view(self._hf_root, path)
        from .nodes import node_for

        return node_for(self._hf_root, path)

    def __add__(self, other: Any) -> "ListConfig":
        if isinstance(other, ListConfig):
            other = other._hf_container()
        return ListConfig(self._hf_container() + list(other))

    def __radd__(self, other: Any) -> "ListConfig":
        # `plain_list + cfg_list`: list.__add__ refuses a non-list, so without
        # this the expression fails instead of concatenating.
        if isinstance(other, ListConfig):
            other = other._hf_container()
        return ListConfig(list(other) + self._hf_container())

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, ListConfig):
            return self._hf_container() == other._hf_container()
        if isinstance(other, (list, tuple)):
            return self._hf_container() == _coerce_assigned(list(other))
        return NotImplemented

    def __ne__(self, other: Any) -> bool:
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    __hash__ = None  # type: ignore[assignment]


# Both containers implement their protocol in full -- DictConfig has
# __getitem__/__setitem__/__delitem__/__iter__/__len__/__contains__ plus
# keys/values/items/get/pop/setdefault/update/clear, ListConfig the
# MutableSequence equivalents -- but they derive from Node rather than
# collections.abc, so `isinstance(cfg, Mapping)` was False.
#
# omegaconf's containers inherit MutableMapping / MutableSequence, so code
# written against omegaconf branches on those ABCs. A custom resolver doing
# `isinstance(arg, Mapping)` over its arguments silently skipped every
# DictConfig it was handed and returned an empty result -- no error, just a
# wrong answer. Registering is what keeps that code working while leaving the
# two-slot layout, the MRO and the metaclass untouched: it adds no mixin
# methods and costs nothing at runtime.
MutableMapping.register(DictConfig)
MutableSequence.register(ListConfig)
