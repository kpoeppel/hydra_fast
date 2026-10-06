"""Ordered list of places configs are looked up.

Ported from ``hydra.core.config_search_path`` and
``hydra._internal.config_search_path_impl`` (hydra-core, MIT); pure list
bookkeeping, unchanged in behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, MutableSequence, Optional, Union

__all__ = ["ConfigSearchPath", "SearchPathElement", "SearchPathQuery"]


class SearchPathElement:
    def __init__(self, provider: str, search_path: str) -> None:
        self.provider = provider
        self.path = search_path

    def __str__(self) -> str:
        return repr(self)

    def __repr__(self) -> str:
        return f"provider={self.provider}, path={self.path}"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SearchPathElement):
            return NotImplemented
        return self.provider == other.provider and self.path == other.path

    def __hash__(self) -> int:
        return hash((self.provider, self.path))


@dataclass
class SearchPathQuery:
    provider: Optional[str] = None
    path: Optional[str] = None


class ConfigSearchPath:
    config_search_path: List[SearchPathElement]

    def __init__(self) -> None:
        self.config_search_path = []

    def get_path(self) -> MutableSequence[SearchPathElement]:
        return self.config_search_path

    def find_last_match(self, reference: SearchPathQuery) -> int:
        return self.find_match(reference, reverse=True)

    def find_first_match(self, reference: SearchPathQuery) -> int:
        return self.find_match(reference, reverse=False)

    def find_match(self, reference: SearchPathQuery, reverse: bool) -> int:
        path = self.config_search_path
        indices = reversed(range(len(path))) if reverse else range(len(path))
        for index in indices:
            element = path[index]
            has_provider = reference.provider is not None
            has_path = reference.path is not None
            if has_provider and has_path:
                if reference.provider == element.provider and reference.path == element.path:
                    return index
            elif has_provider:
                if reference.provider == element.provider:
                    return index
            elif has_path:
                if reference.path == element.path:
                    return index
            else:
                raise ValueError("SearchPathQuery must specify provider and/or path")
        return -1

    def append(
        self, provider: str, path: str, anchor: Optional[Union[SearchPathQuery, str]] = None
    ) -> None:
        if anchor is None:
            self.config_search_path.append(SearchPathElement(provider, path))
            return
        if isinstance(anchor, str):
            anchor = SearchPathQuery(anchor, None)
        index = self.find_last_match(anchor)
        if index != -1:
            self.config_search_path.insert(index + 1, SearchPathElement(provider, path))
        else:
            self.append(provider, path, anchor=None)

    def prepend(
        self, provider: str, path: str, anchor: Optional[Union[SearchPathQuery, str]] = None
    ) -> None:
        if anchor is None:
            self.config_search_path.insert(0, SearchPathElement(provider, path))
            return
        if isinstance(anchor, str):
            anchor = SearchPathQuery(anchor, None)
        index = self.find_first_match(anchor)
        if index > 0:
            self.config_search_path.insert(index, SearchPathElement(provider, path))
        else:
            self.prepend(provider, path, None)

    def __str__(self) -> str:
        return str(self.config_search_path)


# hydra spells the implementation class separately; keep the alias so code
# written against either name works.
ConfigSearchPathImpl = ConfigSearchPath
