"""hydra-fast's own version, and the ``version_base`` compatibility level.

Hydra gates a few deprecations on the version a user declares through
``version_base=``. hydra-fast implements 1.3 semantics throughout, so the
level changes no composition behaviour -- but the deprecation warnings it
controls are observable, which is why this is a real setting and not a no-op.

Versions compare as ``(major, minor)``, which is all Hydra ever compares them
on. That keeps ``packaging`` out of the dependency list for what amounts to a
two-integer tuple.
"""

from __future__ import annotations

from typing import Any, Optional

from .core.singleton import Singleton

#: Source of truth for hydra-fast's version.
__version__ = "0.1.0"

__all__ = ["__version__", "base_at_least", "getbase", "setbase"]

#: Sentinel for "the caller did not say". Hydra exposes this by name.
_UNSPECIFIED_: Any = object()

#: The level assumed when nobody has declared one, matching Hydra.
__compat_version__ = "1.1"

#: The Hydra semantics hydra-fast implements. ``version_base=None`` means
#: "whatever is current", so it resolves to this -- *not* to hydra-fast's own
#: package version, which numbers a different thing entirely.
__hydra_version__ = "1.3"


class Version:
    """A ``major.minor`` version that orders and prints like Hydra's."""

    __slots__ = ("_parts",)

    def __init__(self, text: str) -> None:
        major, _, rest = str(text).partition(".")
        minor, _, _ = rest.partition(".")
        self._parts = (int(major), int(minor or 0))

    def __eq__(self, other: Any) -> Any:
        if isinstance(other, Version):
            return self._parts == other._parts
        return NotImplemented

    def __lt__(self, other: "Version") -> bool:
        return self._parts < other._parts

    def __le__(self, other: "Version") -> bool:
        return self._parts <= other._parts

    def __gt__(self, other: "Version") -> bool:
        return self._parts > other._parts

    def __ge__(self, other: "Version") -> bool:
        return self._parts >= other._parts

    def __hash__(self) -> int:
        return hash(self._parts)

    @property
    def major(self) -> int:
        return self._parts[0]

    @property
    def minor(self) -> int:
        return self._parts[1]

    def __str__(self) -> str:
        return f"{self._parts[0]}.{self._parts[1]}"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Version({str(self)!r})"


class VersionBase(metaclass=Singleton):
    """Holds the level, as a singleton so test fixtures can save/restore it."""

    def __init__(self) -> None:
        self.version_base: Optional[Version] = _UNSPECIFIED_

    def setbase(self, version: Version) -> None:
        assert isinstance(version, Version), f"Unexpected Version type : {type(version)}"
        self.version_base = version

    def getbase(self) -> Optional[Version]:
        return self.version_base

    @staticmethod
    def instance(*args: Any, **kwargs: Any) -> "VersionBase":
        return Singleton.instance(VersionBase, *args, **kwargs)

    @staticmethod
    def set_instance(instance: "VersionBase") -> None:
        assert isinstance(instance, VersionBase)
        Singleton._instances[VersionBase] = instance  # type: ignore[assignment]


def base_at_least(ver: str) -> bool:
    """Is the declared level at least ``ver``? Defaults to the compat level."""
    current = VersionBase.instance().getbase()
    if current is _UNSPECIFIED_:
        current = Version(__compat_version__)
        VersionBase.instance().setbase(current)
    assert isinstance(current, Version)
    return current >= Version(ver)


def getbase() -> Optional[Version]:
    current = VersionBase.instance().getbase()
    return None if current is _UNSPECIFIED_ else current


def setbase(ver: Any) -> None:
    """Declare the level. ``None`` means "current", as in Hydra."""
    if ver is _UNSPECIFIED_:
        # Hydra warns here. hydra-fast's own entry points always pass a value,
        # so reaching this means third-party code left it out; assume the
        # compat level silently rather than warn about an argument the caller
        # may not know exists.
        resolved = Version(__compat_version__)
    elif ver is None:
        resolved = Version(__hydra_version__)
    else:
        resolved = Version(ver)
        if resolved < Version(__compat_version__):
            # Imported here: errors pulls in the container machinery, and this
            # module is imported from hydra_fast/__init__ before that exists.
            from .errors import HydraException

            raise HydraException(f'version_base must be >= "{__compat_version__}"')
    VersionBase.instance().setbase(resolved)
