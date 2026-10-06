"""Targets and resolvers for the realistic config tree.

Separate module because `_target_` is a dotted path and `${multiply:...}` is
a resolver registered by name: both are looked up by string from the YAML, so
they have to live somewhere importable.
"""

from __future__ import annotations

from typing import Any


class Sink:
    """Stands in for whatever a pipeline actually writes through."""

    def __init__(self, root: str, compression: str = "none") -> None:
        self.root = root
        self.compression = compression

    def __repr__(self) -> str:
        return f"Sink(root={self.root!r}, compression={self.compression!r})"

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, Sink) and (self.root, self.compression) == (
            other.root,
            other.compression,
        )


def multiply(a: Any, b: Any) -> int:
    """A custom resolver, as every real tree has a few of."""
    return int(a) * int(b)


def register(api: Any) -> None:
    """Register the resolvers on `api`, which is an OmegaConf-like module.

    Takes the API rather than importing one, so the same tree can be composed
    by hydra-fast and by real omegaconf for a differential comparison.
    """
    for name, fn in RESOLVERS().items():
        api.register_new_resolver(name, fn, replace=True)


# ---------------------------------------------------------------------------
# A spread of resolver shapes. A real tree registers dozens, and they are the
# part most likely to interact badly with a reimplementation: variadic ones,
# ones taking containers, ones that return containers, and ones that read the
# config they are called from.
# ---------------------------------------------------------------------------
def join_path(*parts: Any) -> str:
    """Variadic, string-producing. Keeps a leading slash on the first part."""
    pieces = [str(part) for part in parts if str(part)]
    if not pieces:
        return ""
    head, *rest = pieces
    joined = "/".join([head.rstrip("/"), *(piece.strip("/") for piece in rest)])
    return joined or "/"


def first_set(*values: Any) -> Any:
    """Variadic with a short-circuit: the first argument that is truthy."""
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def to_list(value: Any) -> list:
    """Returns a container, which has to survive being put back in a config."""
    if isinstance(value, str):
        return [part for part in value.split(",") if part]
    return list(value)


def total(values: Any) -> int:
    """Takes a container."""
    return sum(int(v) for v in values)


def pad(value: Any, width: Any = 3) -> str:
    """Has a default argument, so it is callable with one or two."""
    return str(value).zfill(int(width))


def RESOLVERS() -> dict:
    return {
        "multiply": multiply,
        "join_path": join_path,
        "first_set": first_set,
        "to_list": to_list,
        "total": total,
        "pad": pad,
    }
