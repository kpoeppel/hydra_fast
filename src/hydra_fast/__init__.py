"""hydra-fast: Hydra-compatible config composition, built for speed.

A drop-in reimplementation of the config composition and interpolation core of
`hydra-core` + `omegaconf`, aimed at the case those two are slowest at:
composing the same config tree hundreds of times for a parameter sweep.

Three things account for the speedup:

* **Plain storage.** Configs are dicts and lists, not trees of ``Node``
  objects, so merging and copying are dict operations.
* **Compiled interpolations.** The ``${...}`` grammar is a hand-written parser
  that compiles each string to a closure once, instead of re-walking an ANTLR
  parse tree on every resolution.
* **Process-wide caching.** A YAML file is read and parsed once per process,
  and defaults lists and composed configs are memoized on what can actually
  change them.

Usage mirrors hydra::

    from hydra_fast import compose, initialize_config_dir

    with initialize_config_dir(config_dir="/path/to/conf", version_base=None):
        cfg = compose(config_name="config", overrides=["db=mysql", "x=1"])
"""

from ._cache import clear_all as clear_caches
from ._cache import set_validation
from ._cache import stats as cache_stats
from .container import MISSING, Container, DictConfig, ListConfig, Node
from .errors import (
    ConfigAttributeError,
    ConfigCompositionException,
    ConfigKeyError,
    ConfigTypeError,
    ConfigValueError,
    GrammarParseError,
    HydraException,
    InterpolationKeyError,
    InterpolationResolutionError,
    InterpolationToMissingValueError,
    KeyValidationError,
    MissingConfigException,
    MissingMandatoryValue,
    OmegaConfBaseException,
    OverrideParseException,
    ReadonlyConfigError,
    UnsupportedInterpolationType,
    UnsupportedValueType,
    ValidationError,
)
from .grammar.interpolation import Analysis, NodeRef
from .grammar.interpolation import analyze as analyze_interpolation
from .nodes import (
    AnyNode,
    BooleanNode,
    BytesNode,
    EnumNode,
    FloatNode,
    IntegerNode,
    StringNode,
    ValueNode,
)
from .omegaconf_api import (
    II,
    SI,
    OmegaConf,
    SCMode,
    flag_override,
    open_dict,
    read_write,
)
from .version import __version__

__all__ = [
    "__version__",
    # omegaconf surface
    "DictConfig",
    "ListConfig",
    "Container",
    "Node",
    "ValueNode",
    "AnyNode",
    "IntegerNode",
    "FloatNode",
    "StringNode",
    "BooleanNode",
    "BytesNode",
    "EnumNode",
    "OmegaConf",
    "SCMode",
    "MISSING",
    "SI",
    "II",
    "open_dict",
    "read_write",
    "flag_override",
    # errors
    "OmegaConfBaseException",
    "MissingMandatoryValue",
    "ValidationError",
    "KeyValidationError",
    "UnsupportedValueType",
    "ReadonlyConfigError",
    "ConfigTypeError",
    "ConfigValueError",
    "ConfigAttributeError",
    "ConfigKeyError",
    "InterpolationResolutionError",
    "InterpolationKeyError",
    "InterpolationToMissingValueError",
    "UnsupportedInterpolationType",
    "GrammarParseError",
    "HydraException",
    "ConfigCompositionException",
    "OverrideParseException",
    "MissingConfigException",
    # caches
    "clear_caches",
    "cache_stats",
    "set_validation",
    # interpolation introspection
    "analyze_interpolation",
    "Analysis",
    "NodeRef",
]


def __getattr__(name: str):
    """Expose the hydra-side API lazily, so importing stays cheap."""
    if name in (
        "compose",
        "initialize",
        "initialize_config_dir",
        "initialize_config_module",
        "main",
    ):
        from . import api

        return getattr(api, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
