"""Exception hierarchy mirroring omegaconf.errors and hydra.errors.

Names and base classes match the originals so ``except`` clauses written
against hydra/omegaconf keep working.
"""

from __future__ import annotations

from typing import Any, Optional


class OmegaConfBaseException(Exception):
    """Base class for all OmegaConf-compatible exceptions."""

    full_key: Optional[str]
    value: Any
    key: Any
    parent_node: Any
    child_node: Any
    object_type: Any
    object_type_str: Optional[str]
    ref_type: Any
    ref_type_str: Optional[str]
    msg: Optional[str]

    _initialized = False

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.message = _args[0] if _args else None
        self.full_key = None
        self.value = None
        self.key = None
        self.parent_node = None
        self.child_node = None
        self.object_type = None
        self.object_type_str = None
        self.ref_type = None
        self.ref_type_str = None
        self.msg = self.message
        super().__init__(*_args)


def decorate(
    exc: "OmegaConfBaseException",
    full_key: Optional[str] = None,
    object_type: Optional[str] = None,
) -> "OmegaConfBaseException":
    """Append omegaconf's standard error trailer and set its attributes.

    Every config error omegaconf raises carries where it happened::

        Key 'nope' not in 'Schema'
            full_key: nope
            object_type=Schema

    Code catching these reads ``exc.full_key`` / ``exc.object_type``, so both
    the rendered message and the attributes are set here.
    """
    lines = [str(exc.args[0]) if exc.args else ""]
    if full_key is not None:
        lines.append(f"    full_key: {full_key}")
    if object_type is not None:
        lines.append(f"    object_type={object_type}")
    exc.args = ("\n".join(lines),)
    exc.full_key = full_key
    exc.object_type_str = object_type
    exc.object_type = object_type
    exc.msg = exc.args[0]
    return exc


class MissingMandatoryValue(OmegaConfBaseException):
    """Thrown when a value marked ``???`` is accessed."""


class KeyValidationError(OmegaConfBaseException, ValueError):
    """Thrown when a key of invalid type is used."""


class ValidationError(OmegaConfBaseException, ValueError):
    """Thrown when a value fails validation."""


class UnsupportedValueType(OmegaConfBaseException, ValueError):
    """Thrown when an unsupported value type is assigned to a config."""


class ReadonlyConfigError(OmegaConfBaseException):
    """Thrown when a read-only config is modified."""


class ConfigTypeError(OmegaConfBaseException, TypeError):
    """Thrown when a config is used in a way its type does not support."""


class ConfigValueError(OmegaConfBaseException, ValueError):
    """Thrown on an invalid value."""


class ConfigAttributeError(OmegaConfBaseException, AttributeError):
    """Thrown on attribute access to a key that does not exist."""


class ConfigKeyError(OmegaConfBaseException, KeyError):
    """Thrown on item access to a key that does not exist."""

    def __str__(self) -> str:
        # KeyError.__str__ reprs the message; keep omegaconf's plain text.
        return str(self.args[0]) if self.args else ""


class ConfigIndexError(OmegaConfBaseException, IndexError):
    """Thrown on out-of-range list index."""


class InterpolationResolutionError(OmegaConfBaseException, ValueError):
    """Base class for failures while resolving an interpolation."""


class InterpolationKeyError(InterpolationResolutionError):
    """Thrown when an interpolation references a key that does not exist."""


class InterpolationToMissingValueError(InterpolationResolutionError):
    """Thrown when an interpolation resolves to a ``???`` value."""


class InterpolationValidationError(InterpolationResolutionError, ValidationError):
    """Thrown when a resolved interpolation fails validation."""


class UnsupportedInterpolationType(InterpolationResolutionError):
    """Thrown when an interpolation names a resolver that is not registered."""


class GrammarParseError(OmegaConfBaseException):
    """Thrown when an interpolation or override string cannot be parsed."""


# ---------------------------------------------------------------------------
# hydra.errors
# ---------------------------------------------------------------------------
class HydraException(Exception):
    pass


class CompactHydraException(HydraException):
    pass


class OverrideParseException(CompactHydraException):
    def __init__(self, override: str, message: str) -> None:
        super().__init__(message)
        self.override = override
        self.message = message


class InstantiationException(CompactHydraException):
    pass


class ConfigCompositionException(CompactHydraException):
    pass


class SearchPathException(CompactHydraException):
    pass


class MissingConfigException(IOError, ConfigCompositionException):
    def __init__(
        self,
        message: str,
        missing_cfg_file: Optional[str] = None,
        options: Optional[Any] = None,
    ) -> None:
        super().__init__(message)
        self.missing_cfg_file = missing_cfg_file
        self.options = options


class HydraDeprecationError(HydraException):
    pass
