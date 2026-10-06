"""YAML loading with OmegaConf's typing rules, backed by libyaml when present.

OmegaConf customises PyYAML in three ways that change what values you get, so
they are reproduced exactly here:

* an extra ``float`` implicit resolver, so ``2.5e-4`` and ``1e5`` (no decimal
  point, which YAML 1.1 requires) load as floats rather than strings;
* no ``timestamp`` resolver, so ``2024-01-02`` stays a ``str``;
* ``pathlib`` constructors, so configs written by ``OmegaConf.save`` round-trip.

Loading goes through ``CSafeLoader`` when PyYAML was built with libyaml --
roughly 14x faster than the pure-Python scanner, and verified to agree with it
on the resolver set above.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any, Dict, List, Optional

import yaml

from . import _cache
from .errors import ValidationError

__all__ = ["get_yaml_loader", "load_yaml_file", "load_yaml_str", "yaml_dump"]

try:  # pragma: no cover - depends on how PyYAML was built
    from yaml import CSafeLoader as _BaseLoader

    HAS_LIBYAML = True
except ImportError:  # pragma: no cover
    from yaml import SafeLoader as _BaseLoader  # type: ignore[assignment]

    HAS_LIBYAML = False

try:  # pragma: no cover
    from yaml import CSafeDumper as _BaseDumper

    HAS_LIBYAML_DUMPER = True
except ImportError:  # pragma: no cover
    from yaml import SafeDumper as _BaseDumper  # type: ignore[assignment]

    HAS_LIBYAML_DUMPER = False


# OmegaConf's float pattern: like YAML 1.1's but with an exponent-without-dot
# alternative on the second line.
_FLOAT_RE = re.compile(
    """^(?:
     [-+]?[0-9]+(?:_[0-9]+)*\\.[0-9_]*(?:[eE][-+]?[0-9]+)?
    |[-+]?[0-9]+(?:_[0-9]+)*(?:[eE][-+]?[0-9]+)
    |\\.[0-9]+(?:_[0-9]+)*(?:[eE][-+][0-9]+)?
    |[-+]?[0-9]+(?:_[0-9]+)*(?::[0-5]?[0-9])+\\.[0-9_]*
    |[-+]?\\.(?:inf|Inf|INF)
    |\\.(?:nan|NaN|NAN))$""",
    re.X,
)


def _build_loader() -> Any:
    class HydraFastLoader(_BaseLoader):  # type: ignore[misc,valid-type]
        pass

    HydraFastLoader.add_implicit_resolver(
        "tag:yaml.org,2002:float", _FLOAT_RE, list("-+0123456789.")
    )
    HydraFastLoader.yaml_implicit_resolvers = {
        key: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
        for key, resolvers in HydraFastLoader.yaml_implicit_resolvers.items()
    }

    for name, factory in (
        ("pathlib.Path", pathlib.Path),
        ("pathlib.PosixPath", pathlib.PosixPath),
        ("pathlib.WindowsPath", pathlib.WindowsPath),
        # Python 3.13+ moved the concrete classes into pathlib._local
        ("pathlib._local.Path", pathlib.Path),
        ("pathlib._local.PosixPath", pathlib.PosixPath),
        ("pathlib._local.WindowsPath", pathlib.WindowsPath),
    ):
        HydraFastLoader.add_constructor(
            f"tag:yaml.org,2002:python/object/apply:{name}",
            # default-arg binding: the loop variable would otherwise be shared
            lambda loader, node, _factory=factory: _factory(*loader.construct_sequence(node)),
        )
    return HydraFastLoader


_LOADER = _build_loader()


def get_yaml_loader() -> Any:
    return _LOADER


class _Dumper(_BaseDumper):  # type: ignore[misc,valid-type]
    pass


# A string that *looks* like a bool, int or float must be quoted, or reading
# the YAML back would give a different type. These are the spellings YAML 1.1
# treats as booleans; transcribed from omegaconf's dumper so generated YAML is
# byte-comparable with it.
_YAML_BOOL_SPELLINGS = frozenset(
    ["y", "Y", "yes", "Yes", "YES", "n", "N", "no", "No", "NO", "true", "True", "TRUE", "false", "False", "FALSE", "on", "On", "ON", "off", "Off", "OFF"]
)


def _looks_numeric(text: str) -> bool:
    try:
        int(text)
        return True
    except ValueError:
        pass
    try:
        float(text)
        return True
    except ValueError:
        return False


def _str_representer(dumper: Any, data: str) -> Any:
    quote = data in _YAML_BOOL_SPELLINGS or _looks_numeric(data)
    return dumper.represent_scalar(
        "tag:yaml.org,2002:str", data, style="'" if quote else None
    )


_Dumper.add_representer(str, _str_representer)
_Dumper.add_representer(
    pathlib.PosixPath,
    lambda d, data: d.represent_scalar("tag:yaml.org,2002:str", str(data)),
)
_Dumper.add_representer(
    pathlib.WindowsPath,
    lambda d, data: d.represent_scalar("tag:yaml.org,2002:str", str(data)),
)


def _check_duplicate_keys(text: str) -> None:
    """Reject duplicate mapping keys, which libyaml silently accepts.

    Pure-Python PyYAML does not flag them either; OmegaConf adds the check in
    ``flatten_mapping``. Doing it over the event stream keeps the fast C
    constructor in play and costs a scan, not a parse.
    """
    try:
        events = list(yaml.parse(text, Loader=yaml.CSafeLoader if HAS_LIBYAML else yaml.SafeLoader))
    except yaml.YAMLError:
        return  # the real load below reports the syntax error properly

    stack: List[Optional[set]] = []
    expecting_key = []
    for event in events:
        if isinstance(event, yaml.MappingStartEvent):
            stack.append(set())
            expecting_key.append(True)
        elif isinstance(event, yaml.MappingEndEvent):
            stack.pop()
            expecting_key.pop()
        elif isinstance(event, yaml.SequenceStartEvent):
            stack.append(None)
            expecting_key.append(False)
        elif isinstance(event, yaml.SequenceEndEvent):
            stack.pop()
            expecting_key.pop()
        elif isinstance(event, (yaml.ScalarEvent, yaml.AliasEvent)):
            if stack and stack[-1] is not None and expecting_key[-1]:
                key = getattr(event, "value", None)
                if key is not None:
                    if key in stack[-1]:
                        raise yaml.constructor.ConstructorError(
                            "while constructing a mapping",
                            None,
                            f"found duplicate key {key}",
                            getattr(event, "start_mark", None),
                        )
                    stack[-1].add(key)
            if stack and stack[-1] is not None:
                expecting_key[-1] = not expecting_key[-1]
        # a nested collection occupies the value slot of its parent
        if (
            isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent))
            and len(stack) >= 2
            and stack[-2] is not None
        ):
            expecting_key[-2] = not expecting_key[-2]


_ALIAS_MARKERS = ("&", "*", "<<")


def load_yaml_str(text: str, *, check_duplicates: bool = True) -> Any:
    """Parse YAML text with OmegaConf's typing rules."""
    if check_duplicates and any(marker in text for marker in _ALIAS_MARKERS):
        # Anchors/merge keys are the only way libyaml's duplicate handling can
        # differ in a way users notice; skip the extra scan otherwise.
        _check_duplicate_keys(text)
    try:
        data = yaml.load(text, Loader=_LOADER)
    except yaml.YAMLError as exc:
        raise _wrap_yaml_error(exc) from exc
    return {} if data is None else data


def _wrap_yaml_error(exc: yaml.YAMLError) -> Exception:
    err = ValidationError(str(exc))
    return err


def _loader(text: str) -> Any:
    return load_yaml_str(text)


def load_yaml_file(path: str) -> Any:
    """Parse a YAML file, at most once per process.

    The result is shared across callers; callers that mutate must copy first.
    """
    return _cache.load_yaml(path, _loader)


def yaml_dump(data: Any, *, sort_keys: bool = False, default_flow_style: bool = False) -> str:
    return yaml.dump(  # type: ignore[no-any-return]
        data,
        Dumper=_Dumper,
        default_flow_style=default_flow_style,
        allow_unicode=True,
        sort_keys=sort_keys,
    )


def yaml_type_of(value: str) -> Any:
    """Resolve a bare scalar the way YAML would, for override values.

    ``key=1`` must give ``int`` 1, ``key=true`` must give ``bool``. Cached: an
    override sweep asks this about the same few hundred strings repeatedly.
    """
    return _scalar_cache(value)


@_cache.memoize("yaml_scalar")
def _scalar_cache(value: str) -> Any:
    try:
        return yaml.load(value, Loader=_LOADER)
    except yaml.YAMLError:
        return value


_DUMPABLE: Dict[type, Any] = {}
