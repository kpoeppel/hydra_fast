"""A fast deep copy for plain config data.

``copy.deepcopy`` is general: it keeps a memo of everything it has seen so
shared and cyclic references survive, and dispatches through
``__deepcopy__``/``__reduce_ex__`` per object. Config storage needs none of
that -- it is dicts, lists and immutable scalars -- and the copy happens on
the hot path, once per cache hit.

:func:`fast_deepcopy` handles that shape directly and falls back to
``copy.deepcopy`` for anything else (enums, paths, arbitrary objects a user
assigned into a config).
"""

from __future__ import annotations

import copy
from typing import Any

__all__ = ["fast_deepcopy"]

# Scalars that are immutable, so sharing them is indistinguishable from
# copying them.
_IMMUTABLE = (str, int, float, bool, bytes, type(None))


def fast_deepcopy(value: Any) -> Any:
    """Deep-copy plain config data; delegate anything unusual."""
    kind = type(value)
    if kind is dict:
        return {key: fast_deepcopy(item) for key, item in value.items()}
    if kind is list:
        return [fast_deepcopy(item) for item in value]
    if kind in _IMMUTABLE:
        return value
    if isinstance(value, _IMMUTABLE):
        # a str/int subclass: immutable in practice, but keep the instance
        return value
    if kind is tuple:
        return tuple(fast_deepcopy(item) for item in value)
    return copy.deepcopy(value)
