"""Targets for the ``instantiate`` tests, importable by name.

A separate module because ``_target_`` is a dotted path: both hydra and
hydra-fast have to import the same objects for a differential comparison to
mean anything.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Optional


class Plain:
    def __init__(self, a: int = 1, b: str = "x") -> None:
        self.a = a
        self.b = b

    def __repr__(self) -> str:
        return f"Plain(a={self.a!r}, b={self.b!r})"

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, Plain) and (self.a, self.b) == (other.a, other.b)


class Positional:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs

    def __repr__(self) -> str:
        return f"Positional(args={self.args!r}, kwargs={self.kwargs!r})"

    def __eq__(self, other: Any) -> bool:
        return (
            isinstance(other, Positional)
            and self.args == other.args
            and self.kwargs == other.kwargs
        )


class Nested:
    def __init__(self, inner: Any = None, name: str = "n") -> None:
        self.inner = inner
        self.name = name

    def __repr__(self) -> str:
        return f"Nested(inner={self.inner!r}, name={self.name!r})"

    def __eq__(self, other: Any) -> bool:
        return (
            isinstance(other, Nested) and self.inner == other.inner and self.name == other.name
        )


class HoldsContainers:
    """Records the *types* it was handed, which is what `_convert_` governs."""

    def __init__(self, mapping: Any = None, sequence: Any = None) -> None:
        self.mapping = mapping
        self.sequence = sequence

    def __repr__(self) -> str:
        return (
            f"HoldsContainers(mapping={type(self.mapping).__name__}:{self.mapping!r},"
            f" sequence={type(self.sequence).__name__}:{self.sequence!r})"
        )

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, HoldsContainers) and repr(self) == repr(other)


class Raises:
    def __init__(self, **_: Any) -> None:
        raise ValueError("target blew up")


def make(a: int = 1, b: int = 2) -> Dict[str, int]:
    return {"a": a, "b": b}


@dataclasses.dataclass
class Schema:
    x: int = 1
    y: str = "y"


@dataclasses.dataclass
class HasList:
    items: List[int] = dataclasses.field(default_factory=lambda: [1, 2])
    tag: Optional[str] = None


CONSTANT = 42
