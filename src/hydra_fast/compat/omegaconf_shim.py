"""Make ``import omegaconf`` (and ``import hydra``) resolve to hydra-fast.

Libraries register their resolvers and build their configs against the
``omegaconf`` module at import time. To use hydra-fast underneath such a
library without editing it, install this shim *before* importing the library:

    import hydra_fast.compat.omegaconf_shim as shim
    shim.install()
    import someone_elses_library   # its OmegaConf is hydra_fast's

:func:`install` is a deliberate, explicit act -- importing ``hydra_fast`` never
does this on its own. Call :func:`uninstall` to put the real modules back.

Only the public surface is shimmed. Code reaching into ``omegaconf._utils`` or
``omegaconf.basecontainer`` will not find what it expects; such code needs to
target hydra-fast directly.

The synthetic packages keep the *real* package's ``__path__`` where it is
installed, so a submodule nobody shimmed (``hydra._internal.core_plugins``,
say) still imports from upstream. Shimmed names land in ``sys.modules`` and
win regardless, since import consults that before any search path.
"""

from __future__ import annotations

import enum  # noqa: F401  (referenced by DictKeyType)
import importlib.machinery
import sys
import types
from typing import Any, Dict, List, Optional, Sequence

__all__ = ["install", "installed", "uninstall"]

_SHIMMED = (
    "omegaconf",
    "omegaconf.errors",
    "omegaconf._utils",
    "omegaconf.grammar_parser",
    "hydra",
    "hydra.errors",
    "hydra.version",
    "hydra.core",
    "hydra.core.singleton",
    "hydra.core.config_store",
    "hydra.core.override_parser",
    "hydra.core.override_parser.types",
    "hydra.core.override_parser.overrides_parser",
    "hydra._internal",
    "hydra._internal.grammar",
    "hydra._internal.grammar.functions",
    "hydra._internal.grammar.utils",
)

_saved: Dict[str, Any] = {}
_installed = False


def _real_path(dotted: str) -> List[str]:
    """``__path__`` of the real installed package, or ``[]`` if absent.

    Uses ``PathFinder`` rather than ``import``, because by the time this runs
    the name may already be shimmed in ``sys.modules`` -- and importing the
    real package is not wanted in any case, only locating it.
    """
    search: Optional[Sequence[str]] = None
    for part in dotted.split("."):
        try:
            spec = importlib.machinery.PathFinder.find_spec(part, search)
        except (ImportError, AttributeError, ValueError):
            return []
        if spec is None or not spec.submodule_search_locations:
            return []
        search = list(spec.submodule_search_locations)
    return list(search or [])


def installed() -> bool:
    return _installed


def _make_omegaconf_module() -> types.ModuleType:
    import hydra_fast
    from hydra_fast import container, errors, omegaconf_api

    module = types.ModuleType("omegaconf")
    module.__doc__ = "hydra-fast shim standing in for omegaconf."
    module.OmegaConf = omegaconf_api.OmegaConf
    module.DictConfig = container.DictConfig
    module.ListConfig = container.ListConfig
    module.Container = container.Container
    module.Node = container.Node
    module.MISSING = container.MISSING
    module.SCMode = omegaconf_api.SCMode

    # Node classes, for code doing isinstance(cfg._get_node(k), IntegerNode).
    from hydra_fast import nodes as _nodes

    for _name in (
        "ValueNode",
        "AnyNode",
        "IntegerNode",
        "FloatNode",
        "StringNode",
        "BooleanNode",
        "BytesNode",
        "EnumNode",
    ):
        setattr(module, _name, getattr(_nodes, _name))
    module.SI = omegaconf_api.SI
    module.II = omegaconf_api.II
    module.open_dict = omegaconf_api.open_dict
    module.read_write = omegaconf_api.read_write
    module.flag_override = omegaconf_api.flag_override
    module.__version__ = f"hydra-fast-{hydra_fast.__version__}"

    # Type aliases and a few internals that libraries import by name.
    import typing

    module.DictKeyType = typing.Union[str, bytes, int, "enum.Enum", float, bool]
    module.ListMergeMode = None  # omegaconf 2.4 only; present so getattr works

    utils_module = types.ModuleType("omegaconf._utils")
    from hydra_fast import _structured as _hf_structured
    from hydra_fast import _yaml as _hf_yaml
    from hydra_fast.grammar import functions as _hf_funcs

    utils_module.is_structured_config = _hf_structured.is_structured
    utils_module.type_str = _hf_funcs.type_str
    utils_module.get_yaml_loader = _hf_yaml.get_yaml_loader
    utils_module.nullcontext = __import__("contextlib").nullcontext
    utils_module.__doc__ = (
        "Partial stand-in for omegaconf._utils. Only the handful of helpers "
        "that third-party code imports are provided; this module is private "
        "in omegaconf and has no stable contract."
    )
    module._utils = utils_module

    grammar_parser_module = types.ModuleType("omegaconf.grammar_parser")
    from hydra_fast.grammar import interpolation as _hf_interp

    grammar_parser_module.parse = _hf_interp.parse
    grammar_parser_module.GrammarParseError = errors.GrammarParseError
    grammar_parser_module.__doc__ = (
        "Stand-in for omegaconf.grammar_parser. `parse()` returns hydra-fast's "
        "compiled closure rather than an ANTLR parse tree; it round-trips "
        "through Container.resolve_parse_tree() the same way. Walking the "
        "structure is done with hydra_fast.analyze_interpolation() instead."
    )
    module.grammar_parser = grammar_parser_module

    for name in (
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
        "ConfigIndexError",
        "InterpolationResolutionError",
        "InterpolationKeyError",
        "InterpolationToMissingValueError",
        "InterpolationValidationError",
        "UnsupportedInterpolationType",
        "GrammarParseError",
    ):
        setattr(module, name, getattr(errors, name))

    errors_module = types.ModuleType("omegaconf.errors")
    for name in dir(errors):
        if not name.startswith("_"):
            setattr(errors_module, name, getattr(errors, name))
    module.errors = errors_module

    return module, errors_module, utils_module, grammar_parser_module


def _make_hydra_modules() -> Dict[str, types.ModuleType]:
    import hydra_fast
    from hydra_fast import api, errors
    from hydra_fast.core import config_store
    from hydra_fast.grammar import override as override_types

    hydra_module = types.ModuleType("hydra")
    hydra_module.__doc__ = "hydra-fast shim standing in for hydra."
    # A real __path__ keeps unshimmed submodules importable from upstream;
    # the shimmed ones are in sys.modules and are found before any search.
    hydra_module.__path__ = _real_path("hydra")
    hydra_module.compose = api.compose
    hydra_module.initialize = api.initialize
    hydra_module.initialize_config_dir = api.initialize_config_dir
    hydra_module.initialize_config_module = api.initialize_config_module
    hydra_module.main = api.main
    hydra_module.__version__ = f"hydra-fast-{hydra_fast.__version__}"
    hydra_module.MissingConfigException = errors.MissingConfigException

    hydra_errors = types.ModuleType("hydra.errors")
    for name in (
        "HydraException",
        "CompactHydraException",
        "OverrideParseException",
        "InstantiationException",
        "ConfigCompositionException",
        "SearchPathException",
        "MissingConfigException",
        "HydraDeprecationError",
    ):
        setattr(hydra_errors, name, getattr(errors, name))
    hydra_module.errors = hydra_errors

    hydra_core = types.ModuleType("hydra.core")
    hydra_core.__path__ = _real_path("hydra.core")

    core_store = types.ModuleType("hydra.core.config_store")
    core_store.ConfigStore = config_store.ConfigStore
    core_store.ConfigStoreWithProvider = config_store.ConfigStoreWithProvider
    core_store.ConfigNode = config_store.ConfigNode
    hydra_core.config_store = core_store

    override_parser = types.ModuleType("hydra.core.override_parser")
    override_parser.__path__ = _real_path("hydra.core.override_parser")

    from hydra_fast.core.override_parser import overrides_parser as _op

    parser_module = types.ModuleType("hydra.core.override_parser.overrides_parser")
    parser_module.OverridesParser = _op.OverridesParser
    parser_module.create_functions = _op.create_functions
    override_parser.overrides_parser = parser_module

    # hydra._internal.grammar.{functions,utils}: the override grammar's
    # calling convention and escaping helpers.
    from hydra_fast.grammar import functions as _gf
    from hydra_fast.grammar import utils as _gu

    internal = types.ModuleType("hydra._internal")
    internal.__path__ = _real_path("hydra._internal")
    internal_grammar = types.ModuleType("hydra._internal.grammar")
    internal_grammar.__path__ = _real_path("hydra._internal.grammar")
    grammar_functions = types.ModuleType("hydra._internal.grammar.functions")
    grammar_functions.Functions = _gf.Functions
    grammar_functions.FunctionCall = _gf.FunctionCall
    grammar_utils = types.ModuleType("hydra._internal.grammar.utils")
    grammar_utils.escape_special_characters = _gu.escape_special_characters
    grammar_utils.is_type_matching = _gf.is_type_matching
    internal_grammar.functions = grammar_functions
    internal_grammar.utils = grammar_utils
    internal.grammar = internal_grammar
    hydra_module._internal = internal

    from hydra_fast import version as _hf_version

    version_module = types.ModuleType("hydra.version")
    version_module.__version__ = hydra_module.__version__
    version_module.getbase = _hf_version.getbase
    version_module.setbase = _hf_version.setbase
    version_module.base_at_least = _hf_version.base_at_least
    version_module.Version = _hf_version.Version
    version_module.VersionBase = _hf_version.VersionBase
    version_module._UNSPECIFIED_ = _hf_version._UNSPECIFIED_
    version_module.__compat_version__ = _hf_version.Version(_hf_version.__compat_version__)
    hydra_module.version = version_module

    # hydra.core.singleton: test fixtures save and restore singleton state, so
    # it has to be *this* Singleton -- the real one holds a different registry
    # and would restore nothing hydra-fast owns.
    from hydra_fast.core import singleton as _hf_singleton

    singleton_module = types.ModuleType("hydra.core.singleton")
    singleton_module.Singleton = _hf_singleton.Singleton
    hydra_core.singleton = singleton_module

    parser_types = types.ModuleType("hydra.core.override_parser.types")
    for name in (
        "Override",
        "OverrideType",
        "ValueType",
        "Key",
        "Glob",
        "Quote",
        "QuotedString",
        "Sweep",
        "ChoiceSweep",
        "RangeSweep",
        "IntervalSweep",
        "FloatRange",
        "ListExtensionOverrideValue",
        "Transformer",
    ):
        setattr(parser_types, name, getattr(override_types, name))
    override_parser.types = parser_types
    hydra_core.override_parser = override_parser

    return {
        "hydra": hydra_module,
        "hydra.errors": hydra_errors,
        "hydra.version": version_module,
        "hydra.core": hydra_core,
        "hydra.core.singleton": singleton_module,
        "hydra.core.config_store": core_store,
        "hydra.core.override_parser": override_parser,
        "hydra.core.override_parser.types": parser_types,
        "hydra.core.override_parser.overrides_parser": parser_module,
        "hydra._internal": internal,
        "hydra._internal.grammar": internal_grammar,
        "hydra._internal.grammar.functions": grammar_functions,
        "hydra._internal.grammar.utils": grammar_utils,
    }


def install(*, omegaconf: bool = True, hydra: bool = True) -> None:
    """Replace ``omegaconf`` and/or ``hydra`` in ``sys.modules``.

    Idempotent. Modules already imported are saved so :func:`uninstall` can
    restore them.
    """
    global _installed
    if _installed:
        return

    replacements: Dict[str, types.ModuleType] = {}
    if omegaconf:
        oc_module, oc_errors, oc_utils, oc_grammar = _make_omegaconf_module()
        replacements["omegaconf"] = oc_module
        replacements["omegaconf.errors"] = oc_errors
        replacements["omegaconf._utils"] = oc_utils
        replacements["omegaconf.grammar_parser"] = oc_grammar
    if hydra:
        replacements.update(_make_hydra_modules())

    for name, module in replacements.items():
        if name in sys.modules:
            _saved[name] = sys.modules[name]
        sys.modules[name] = module
    _installed = True


def uninstall() -> None:
    """Undo :func:`install`."""
    global _installed
    if not _installed:
        return
    for name in _SHIMMED:
        if name in _saved:
            sys.modules[name] = _saved.pop(name)
        else:
            sys.modules.pop(name, None)
    _saved.clear()
    _installed = False
