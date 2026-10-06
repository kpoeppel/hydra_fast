"""Typed dispatch for functions called inside an override value.

Ported from ``hydra._internal.grammar.functions`` (hydra-core, MIT). The
registry binds a call to the target's signature and type-checks each argument
against its annotation, so ``choice()``/``range()``/``glob()`` report the same
errors hydra does for a wrong argument type.

Kept separate from :mod:`hydra_fast.grammar.override_functions`, which holds
the function bodies: this module is the *calling convention*.
"""

from __future__ import annotations

import inspect
import typing
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

from ..errors import HydraException

__all__ = ["FunctionCall", "Functions", "is_type_matching", "type_str"]


@dataclass
class FunctionCall:
    name: str
    args: List[Any] = field(default_factory=list)
    kwargs: Dict[str, Any] = field(default_factory=dict)


def type_str(annotation: Any) -> str:
    """Render an annotation the way hydra's error messages do."""
    if annotation is type(None):
        return "NoneType"
    if annotation is Any:
        return "Any"
    name = getattr(annotation, "__name__", None)
    origin = typing.get_origin(annotation)
    if origin is None:
        return name or str(annotation).replace("typing.", "")
    args = typing.get_args(annotation)
    if _is_union(origin):
        return "Union[" + ", ".join(type_str(arg) for arg in args) + "]"
    base = getattr(origin, "__name__", str(origin)).capitalize()
    if base == "List":
        return f"List[{', '.join(type_str(a) for a in args)}]"
    if base == "Dict":
        return f"Dict[{', '.join(type_str(a) for a in args)}]"
    return str(annotation).replace("typing.", "")


def _is_union(origin: Any) -> bool:
    return origin is typing.Union or getattr(origin, "__name__", "") == "UnionType"


def is_type_matching(value: Any, annotation: Any) -> bool:
    """Does ``value`` satisfy ``annotation``? Containers are checked shallowly.

    Matches hydra's ``_internal.grammar.utils.is_type_matching``: element types
    of ``List[X]``/``Dict[K, V]`` are deliberately ignored, so a ``list`` of
    anything satisfies ``List[int]`` here. Argument *values* get validated by
    the function bodies.
    """
    if annotation is Any or annotation is inspect.Signature.empty:
        return True

    origin = typing.get_origin(annotation)
    if _is_union(origin):
        return any(is_type_matching(value, arg) for arg in typing.get_args(annotation))
    if origin in (list, typing.List):
        return isinstance(value, list)
    if origin in (dict, typing.Dict):
        return isinstance(value, dict)
    if origin is not None:
        return True

    if annotation in (int, float, bool, str):
        # exact type, so a bool does not pass for an int
        return type(value) is annotation
    try:
        return isinstance(value, annotation)
    except TypeError:  # pragma: no cover - exotic annotation
        return True


@dataclass
class Functions:
    definitions: Dict[str, inspect.Signature] = field(default_factory=dict)
    functions: Dict[str, Callable[..., Any]] = field(default_factory=dict)

    def register(self, name: str, func: Callable[..., Any]) -> None:
        if name in self.definitions:
            raise HydraException(f"Function named '{name}' is already registered")
        self.definitions[name] = inspect.signature(func)
        self.functions[name] = func

    def eval(self, func: FunctionCall) -> Any:
        from .override import QuotedString

        if func.name not in self.definitions:
            raise HydraException(
                f"Unknown function '{func.name}'"
                f"\nAvailable: {','.join(sorted(self.definitions.keys()))}\n"
            )
        signature = self.definitions[func.name]

        args = [
            arg.text if isinstance(arg, QuotedString) else arg for arg in func.args
        ]
        kwargs = {
            key: value.text if isinstance(value, QuotedString) else value
            for key, value in func.kwargs.items()
        }

        bound = signature.bind(*args, **kwargs)

        for name, value in bound.arguments.items():
            expected = signature.parameters[name].annotation
            kind = signature.parameters[name].kind
            if kind == inspect.Parameter.VAR_POSITIONAL:
                for index, item in enumerate(value):
                    if not is_type_matching(item, expected):
                        raise TypeError(
                            f"mismatch type argument {name}[{index}]:"
                            f" {type_str(type(item))} is incompatible with"
                            f" {type_str(expected)}"
                        )
            elif kind == inspect.Parameter.VAR_KEYWORD:
                continue
            elif not is_type_matching(value, expected):
                raise TypeError(
                    f"mismatch type argument {name}:"
                    f" {type_str(type(value))} is incompatible with {type_str(expected)}"
                )

        return self.functions[func.name](*bound.args, **bound.kwargs)
