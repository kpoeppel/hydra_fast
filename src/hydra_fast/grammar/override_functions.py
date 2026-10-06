"""The functions callable inside an override value.

``key=choice(a,b)``, ``key=range(1,10)``, ``group=glob(*)``, ``key=int(1.5)``
and friends. Behaviour transcribed from
``hydra._internal.grammar.grammar_functions``.
"""

from __future__ import annotations

import builtins
import json
import random
from copy import copy
from typing import Any, Callable, Dict, List, Optional, Union

__all__ = [
    "apply_to_dict_values",
    "cast_bool",
    "cast_float",
    "cast_int",
    "cast_json_str",
    "cast_str",
    "choice",
    "extract_text",
    "extend_list",
    "glob",
    "interval",
    "range_",
    "shuffle",
    "sort",
    "tag",
]


def _types() -> Any:
    from .override import (
        ChoiceSweep,
        Glob,
        IntervalSweep,
        ListExtensionOverrideValue,
        QuotedString,
        RangeSweep,
        Sweep,
    )

    return (
        ChoiceSweep,
        Glob,
        IntervalSweep,
        ListExtensionOverrideValue,
        QuotedString,
        RangeSweep,
        Sweep,
    )


# ---------------------------------------------------------------------------
# sweeps
# ---------------------------------------------------------------------------
def choice(*args: Any) -> Any:
    ChoiceSweep = _types()[0]
    if len(args) == 0:
        raise ValueError("empty choice is not legal")
    if len(args) == 1 and isinstance(args[0], ChoiceSweep):
        first = args[0]
        if first.simple_form:
            first.simple_form = False
            return first
        raise ValueError("nesting choices is not supported")
    return ChoiceSweep(list=list(args))


def range_(
    start: Union[int, float],
    stop: Optional[Union[int, float]] = None,
    step: Union[int, float] = 1,
) -> Any:
    RangeSweep = _types()[5]
    if stop is None:
        stop = start
        start = 0
    return RangeSweep(start=start, stop=stop, step=step)


def interval(start: Union[int, float], end: Union[int, float]) -> Any:
    IntervalSweep = _types()[2]
    return IntervalSweep(start=float(start), end=float(end))


def tag(*args: Any, sweep: Any = None) -> Any:
    Sweep = _types()[6]
    if len(args) < 1:
        raise ValueError("Not enough arguments to tag, must take at least a sweep")
    if sweep is not None:
        return tag(*(list(args) + [sweep]))
    last = args[-1]
    if isinstance(last, Sweep):
        tags = set()
        for item in args[:-1]:
            if not isinstance(item, str):
                raise ValueError(
                    f"tag arguments type must be string, got {type(item).__name__}"
                )
            tags.add(item)
        last.tags = tags
        return last
    raise ValueError(
        "Last argument to tag() must be a choice(), range() or interval(), "
        f"got {type(last).__name__}"
    )


def _list_to_simple_choice(*args: Any) -> Any:
    ChoiceSweep = _types()[0]
    return ChoiceSweep(list=list(args), simple_form=True)


def shuffle(*args: Any, sweep: Any = None, list: Any = None) -> Any:  # noqa: A002
    ChoiceSweep, _, _, _, _, RangeSweep, _ = _types()
    if list is not None:
        return shuffle(list)
    if sweep is not None:
        return shuffle(sweep)
    if len(args) == 1:
        arg = args[0]
        if isinstance(arg, (ChoiceSweep, RangeSweep)):
            out = copy(arg)
            out.shuffle = True
            return out
        if isinstance(arg, builtins.list):
            items = copy(arg)
            random.shuffle(items)
            return items
        return [arg]
    simple = _list_to_simple_choice(*args)
    simple.shuffle = True
    return simple


def sort(*args: Any, sweep: Any = None, list: Any = None, reverse: bool = False) -> Any:  # noqa: A002
    ChoiceSweep, _, _, _, _, RangeSweep, _ = _types()
    if list is not None:
        return sort(list, reverse=reverse)
    if sweep is not None:
        return _sort_sweep(sweep, reverse)
    if len(args) == 1:
        arg = args[0]
        if isinstance(arg, (ChoiceSweep, RangeSweep)):
            return _sort_sweep(arg, reverse)
        if isinstance(arg, builtins.list):
            return sorted(arg, reverse=reverse)
        return arg
    return _sort_sweep(_list_to_simple_choice(*args), reverse)


def _sort_sweep(sweep: Any, reverse: bool) -> Any:
    ChoiceSweep, _, _, _, _, RangeSweep, _ = _types()
    out = copy(sweep)
    if isinstance(out, ChoiceSweep):
        out.list = sorted(out.list, reverse=reverse)
        return out
    if isinstance(out, RangeSweep):
        assert out.start is not None and out.stop is not None
        if not reverse:
            if out.step < 0:
                out.start, out.stop = out.stop + abs(out.step), out.start + abs(out.step)
                out.step = -out.step
        else:
            if out.step > 0:
                out.start, out.stop = out.stop - out.step, out.start - out.step
                out.step = -out.step
        return out
    raise TypeError(f"Invalid sweep type: {type(sweep).__name__}")


def glob(include: Union[List[str], str], exclude: Any = None) -> Any:
    Glob = _types()[1]
    if isinstance(include, str):
        include = [include]
    if exclude is None:
        exclude = []
    elif isinstance(exclude, str):
        exclude = [exclude]
    return Glob(include=include, exclude=exclude)


def extend_list(*args: Any) -> Any:
    ListExtensionOverrideValue = _types()[3]
    return ListExtensionOverrideValue(values=list(args))


# ---------------------------------------------------------------------------
# casts
#
# Mirrors hydra's implementations closely, including letting Python's own
# errors through: `int(float("nan"))` must surface as the ValueError hydra
# reports, and the parser wraps it with the source text (see
# `_OvParser.parse_function`). Pre-validating here would produce a different
# exception type and a different message.
# ---------------------------------------------------------------------------
def _normalize_cast_value(*args: Any, value: Any = None) -> Any:
    if len(args) > 0 and value is not None:
        raise TypeError("cannot use both position and named arguments")
    if value is not None:
        return value
    if len(args) == 0:
        raise TypeError("No positional args or value specified")
    if len(args) == 1:
        return args[0]
    return _list_to_simple_choice(*args)


def apply_to_dict_values(
    value: Dict[Any, Any], function: Callable[..., Any]
) -> Dict[Any, Any]:
    return {key: function(item) for key, item in value.items()}


def cast_choice(value: Any, function: Callable[..., Any]) -> Any:
    ChoiceSweep = _types()[0]
    return ChoiceSweep(
        list=[function(item) for item in value.list],
        simple_form=value.simple_form,
        tags=copy(value.tags),
    )


def cast_interval(value: Any, function: Callable[..., Any]) -> Any:
    IntervalSweep = _types()[2]
    return IntervalSweep(
        start=function(value.start), end=function(value.end), tags=copy(value.tags)
    )


def cast_range(value: Any, function: Callable[..., Any]) -> Any:
    """A range is numeric, so only int/float casts make sense for it."""
    if function not in (cast_float, cast_int):
        raise ValueError("Range can only be cast to int or float")
    RangeSweep = _types()[5]
    return RangeSweep(
        start=function(value.start),
        stop=function(value.stop),
        step=function(value.step),
    )


def cast_int(*args: Any, value: Any = None) -> Any:
    ChoiceSweep, _, IntervalSweep, _, QuotedString, RangeSweep, _ = _types()
    value = _normalize_cast_value(*args, value=value)
    if isinstance(value, QuotedString):
        return cast_int(value.text)
    if isinstance(value, dict):
        return apply_to_dict_values(value, cast_int)
    if isinstance(value, builtins.list):
        return builtins.list(map(cast_int, value))
    if isinstance(value, ChoiceSweep):
        return cast_choice(value, cast_int)
    if isinstance(value, RangeSweep):
        return cast_range(value, cast_int)
    if isinstance(value, IntervalSweep):
        return cast_interval(value, cast_int)
    assert isinstance(value, (int, float, bool, str))
    return int(value)


def cast_float(*args: Any, value: Any = None) -> Any:
    ChoiceSweep, _, IntervalSweep, _, QuotedString, RangeSweep, _ = _types()
    value = _normalize_cast_value(*args, value=value)
    if isinstance(value, QuotedString):
        return cast_float(value.text)
    if isinstance(value, dict):
        return apply_to_dict_values(value, cast_float)
    if isinstance(value, builtins.list):
        return builtins.list(map(cast_float, value))
    if isinstance(value, ChoiceSweep):
        return cast_choice(value, cast_float)
    if isinstance(value, RangeSweep):
        return cast_range(value, cast_float)
    if isinstance(value, IntervalSweep):
        return cast_interval(value, cast_float)
    assert isinstance(value, (int, float, bool, str))
    return float(value)


def cast_str(*args: Any, value: Any = None) -> Any:
    ChoiceSweep, _, IntervalSweep, _, QuotedString, RangeSweep, _ = _types()
    value = _normalize_cast_value(*args, value=value)
    if isinstance(value, QuotedString):
        return cast_str(value.text)
    if isinstance(value, dict):
        return apply_to_dict_values(value, cast_str)
    if isinstance(value, builtins.list):
        return builtins.list(map(cast_str, value))
    if isinstance(value, ChoiceSweep):
        return cast_choice(value, cast_str)
    if isinstance(value, RangeSweep):
        return cast_range(value, cast_str)
    if isinstance(value, IntervalSweep):
        raise ValueError("Intervals cannot be cast to str")
    assert isinstance(value, (int, float, bool, str))
    # bools render lowercase, so the result reads back as a bool
    return str(value).lower() if isinstance(value, bool) else str(value)


def cast_bool(*args: Any, value: Any = None) -> Any:
    ChoiceSweep, _, IntervalSweep, _, QuotedString, RangeSweep, _ = _types()
    value = _normalize_cast_value(*args, value=value)
    if isinstance(value, QuotedString):
        return cast_bool(value.text)
    if isinstance(value, dict):
        return apply_to_dict_values(value, cast_bool)
    if isinstance(value, builtins.list):
        return builtins.list(map(cast_bool, value))
    if isinstance(value, ChoiceSweep):
        return cast_choice(value, cast_bool)
    if isinstance(value, RangeSweep):
        return cast_range(value, cast_bool)
    if isinstance(value, IntervalSweep):
        raise ValueError("Intervals cannot be cast to bool")
    if isinstance(value, str):
        lowered = value.lower()
        if lowered == "false":
            return False
        if lowered == "true":
            return True
        raise ValueError(f"Cannot cast '{value}' to bool")
    return bool(value)


def extract_text(*args: Any, value: Any = None) -> Any:
    ChoiceSweep, _, _, _, QuotedString, RangeSweep, _ = _types()
    value = _normalize_cast_value(*args, value=value)
    if isinstance(value, QuotedString):
        return value.text
    if isinstance(value, dict):
        return apply_to_dict_values(value, extract_text)
    if isinstance(value, builtins.list):
        return builtins.list(map(extract_text, value))
    if isinstance(value, ChoiceSweep):
        return cast_choice(value, extract_text)
    if isinstance(value, RangeSweep):
        return cast_range(value, extract_text)
    return value


def cast_json_str(*args: Any, value: Any = None) -> Any:
    ChoiceSweep, _, IntervalSweep, _, QuotedString, RangeSweep, _ = _types()
    value = _normalize_cast_value(*args, value=value)
    json_val = value
    if isinstance(value, QuotedString):
        json_val = value.text
    if isinstance(value, dict):
        json_val = apply_to_dict_values(value, extract_text)
    elif isinstance(value, builtins.list):
        json_val = builtins.list(map(extract_text, value))
    elif isinstance(value, ChoiceSweep):
        return cast_choice(cast_choice(value, extract_text), json.dumps)
    elif isinstance(value, RangeSweep):
        return cast_range(cast_range(value, extract_text), json.dumps)
    elif isinstance(value, IntervalSweep):
        raise ValueError("Intervals cannot be cast to json_str")
    return json.dumps(json_val)
