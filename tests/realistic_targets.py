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
    api.register_new_resolver("multiply", multiply, replace=True)
