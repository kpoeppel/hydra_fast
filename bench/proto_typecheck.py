"""Prototype: what would OmegaConf-style type enforcement cost?

hydra-fast drops OmegaConf's runtime type validation on structured configs
because storage is plain data -- there is no per-value node to hold a declared
type. The question this answers: is that a *necessary* consequence of the
design, or just unimplemented?

The mechanism prototyped here keeps a `path -> declared type` map on the config
root, populated when a dataclass is flattened, and validates on write. Reads
are untouched, which is the point: a sweep does ~1350 reads per 7 writes.

Run it to get the cost on the real benchmark:

    python bench/proto_typecheck.py
"""

from __future__ import annotations

import contextlib
import dataclasses
import enum
import os
import sys
import time
import typing
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))


# ---------------------------------------------------------------------------
# the validator
# ---------------------------------------------------------------------------
_NoneType = type(None)


def declared_types(cls, prefix=(), out=None):
    """Flatten a dataclass into {path tuple: annotation}."""
    if out is None:
        out = {}
    hints = typing.get_type_hints(cls)
    for field in dataclasses.fields(cls):
        annotation = hints.get(field.name, typing.Any)
        path = prefix + (field.name,)
        out[path] = annotation
        inner = _unwrap_optional(annotation)
        if dataclasses.is_dataclass(inner):
            declared_types(inner, path, out)
    return out


def _unwrap_optional(annotation):
    origin = typing.get_origin(annotation)
    if origin is typing.Union or str(origin) == "types.UnionType":
        args = [a for a in typing.get_args(annotation) if a is not _NoneType]
        if len(args) == 1:
            return args[0]
    return annotation


def validate(value, annotation):
    """Coerce/validate like OmegaConf's typed nodes. Raises ValueError."""
    if annotation is typing.Any or annotation is None:
        return value

    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)

    # Optional[X] / X | None
    if origin is typing.Union or str(origin) == "types.UnionType":
        if value is None and _NoneType in args:
            return None
        inner = [a for a in args if a is not _NoneType]
        if len(inner) == 1:
            return validate(value, inner[0])
        for candidate in inner:  # best-effort for real unions
            try:
                return validate(value, candidate)
            except ValueError:
                continue
        raise ValueError(f"'{value}' does not match {annotation}")

    if origin in (list, typing.List):
        if not isinstance(value, list):
            raise ValueError(f"expected a list, got {type(value).__name__}")
        if args:
            return [validate(item, args[0]) for item in value]
        return value

    if origin in (dict, typing.Dict):
        if not isinstance(value, dict):
            raise ValueError(f"expected a dict, got {type(value).__name__}")
        if len(args) == 2:
            return {validate(k, args[0]): validate(v, args[1]) for k, v in value.items()}
        return value

    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        if isinstance(value, annotation):
            return value
        if isinstance(value, str):
            try:
                return annotation[value]
            except KeyError:
                raise ValueError(
                    f"'{value}' is not a member of {annotation.__name__}"
                ) from None
        raise ValueError(f"cannot convert {value!r} to {annotation.__name__}")

    if dataclasses.is_dataclass(annotation):
        if not isinstance(value, dict):
            raise ValueError(f"expected a mapping for {annotation.__name__}")
        return value

    if annotation is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.lower()
            if lowered in ("true", "yes", "on", "1"):
                return True
            if lowered in ("false", "no", "off", "0"):
                return False
        if isinstance(value, int):
            return bool(value)
        raise ValueError(f"Value '{value}' could not be converted to bool")

    if annotation is int:
        # bool is an int subclass but omegaconf rejects it for an int field
        if isinstance(value, bool):
            raise ValueError(f"Value '{value}' could not be converted to Integer")
        if isinstance(value, int):
            return value
        if isinstance(value, (str, float)):
            try:
                coerced = int(value)
            except (TypeError, ValueError):
                raise ValueError(
                    f"Value '{value}' could not be converted to Integer"
                ) from None
            if isinstance(value, float) and coerced != value:
                raise ValueError(f"Value '{value}' could not be converted to Integer")
            return coerced
        raise ValueError(f"Value '{value}' could not be converted to Integer")

    if annotation is float:
        if isinstance(value, bool):
            raise ValueError(f"Value '{value}' could not be converted to Float")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                raise ValueError(f"Value '{value}' could not be converted to Float") from None
        raise ValueError(f"Value '{value}' could not be converted to Float")

    if annotation is str:
        if isinstance(value, (str, int, float, bool)):
            return str(value)
        raise ValueError(f"Value '{value}' could not be converted to str")

    return value


# ---------------------------------------------------------------------------
# wire it into hydra_fast by monkeypatch, for measurement only
# ---------------------------------------------------------------------------
def install(validate_merges: bool = False):
    """Patch validation into the write paths. Returns a stats dict."""
    import hydra_fast._internal.config_loader as CL
    from hydra_fast import _structured as HS
    from hydra_fast import container as HC
    from hydra_fast import merge as HM

    stats = {"checked": 0, "rejected": 0}

    # 1. record declared types when a dataclass is flattened
    orig_flatten = HS.structured_to_plain
    type_maps = {}

    def flatten(value):
        plain = orig_flatten(value)
        cls = value if isinstance(value, type) else type(value)
        if dataclasses.is_dataclass(cls):
            with contextlib.suppress(Exception):  # unresolvable hints: no checking
                type_maps[id(plain)] = declared_types(cls)
        return plain

    HS.structured_to_plain = flatten

    # 2. the root carries the type map, so merges and copies keep it
    def types_for(root):
        return type_maps.get(id(root.data))

    # 3. validate on write
    orig_set = HC.DictConfig._hf_set

    def checked_set(self, key, value, attribute):
        table = types_for(self._hf_root)
        if table is not None:
            annotation = table.get(self._hf_path + (key,))
            if annotation is not None:
                stats["checked"] += 1
                try:
                    value = validate(value, annotation)
                except ValueError:
                    stats["rejected"] += 1
                    raise
        return orig_set(self, key, value, attribute)

    HC.DictConfig._hf_set = checked_set

    # Overrides reach the config through OmegaConf.update, not __setattr__,
    # so that path needs the same check for the measurement to be honest.
    from hydra_fast import omegaconf_api as OA

    orig_update = OA.OmegaConf.update

    def checked_update(cfg, key, value=None, *, merge=True, force_add=False):
        table = types_for(cfg._hf_root)
        if table is not None:
            from hydra_fast.grammar.interpolation import split_key

            annotation = table.get(tuple(split_key(key)))
            if annotation is not None:
                stats["checked"] += 1
                try:
                    value = validate(value, annotation)
                except ValueError:
                    stats["rejected"] += 1
                    raise
        return orig_update(cfg, key, value, merge=merge, force_add=force_add)

    OA.OmegaConf.update = checked_update
    CL.OmegaConf.update = checked_update

    if validate_merges:
        # worst case: check every value that flows through a merge
        orig_merge = HM.merge_into

        def checked_merge(dest, src, _prefix=()):
            stats["checked"] += len(src)
            return orig_merge(dest, src)

        HM.merge_into = CL.merge_into = checked_merge

    return stats


# ---------------------------------------------------------------------------
# schema for the mechanism check
#
# These must live at module level: `from __future__ import annotations` turns
# every annotation into a string, and typing.get_type_hints() resolves those
# against module globals -- a dataclass defined inside a function would have
# unresolvable hints and silently get no type map.
# ---------------------------------------------------------------------------
class Color(enum.Enum):
    RED = 1
    GREEN = 2


@dataclasses.dataclass
class Inner:
    x: int = 1


@dataclasses.dataclass
class Schema:
    n: int = 1
    f: float = 1.0
    s: str = "x"
    b: bool = True
    c: Color = Color.RED
    opt: typing.Optional[int] = None
    lst: typing.List[int] = dataclasses.field(default_factory=lambda: [1])
    dct: typing.Dict[str, int] = dataclasses.field(default_factory=dict)
    inner: Inner = dataclasses.field(default_factory=Inner)


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------
def main() -> int:
    defaults = {
        "OUTPUT_DIR": "./output",
        "PROJECT_DIR": ".",
        "HF_HOME": "/tmp/hf",
        "SLURM_ACCOUNT": "bench",
        "SLURM_PARTITION": "bench",
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)

    import bench_compose as B

    B.register_resolvers("fast")
    import hydra_fast

    config_dir = Path(B.AUTOEXP) / "config"
    if not config_dir.is_dir():
        print(f"config tree not found at {config_dir}", file=sys.stderr)
        return 2
    points = B.sweep_overrides(50)

    def measure(label, repeats=5):
        hydra_fast.clear_caches()
        B.run_fast(points[:1], config_dir, "autoexp")
        best = None
        for _ in range(repeats):
            hydra_fast.clear_caches()
            start = time.perf_counter()
            B.run_fast(points, config_dir, "autoexp")
            elapsed = time.perf_counter() - start
            best = elapsed if best is None else min(best, elapsed)
        print(f"  {label:34s} {best * 1e3:7.1f} ms   {best / len(points) * 1e3:5.2f} ms/point")
        return best

    print("50-point sweep, real config tree, best of 5:")
    baseline = measure("no type checking (shipped)")

    stats = install(validate_merges=False)
    with_checks = measure("type checking on writes")

    overhead = (with_checks / baseline - 1) * 100
    print(f"\n  writes validated: {stats['checked']}")
    print(f"  overhead: {overhead:+.1f}%")

    # Does the mechanism actually reject what omegaconf rejects?
    print("\nmechanism check (does it catch what omegaconf catches?):")
    from hydra_fast import OmegaConf

    cases = [
        ("n", "not an int", "reject"),
        ("n", "42", "accept->42"),
        ("n", 7, "accept"),
        ("n", True, "reject"),
        ("f", "1.5", "accept->1.5"),
        ("f", "nope", "reject"),
        ("b", "false", "accept->False"),
        ("c", "GREEN", "accept->Color.GREEN"),
        ("c", "PURPLE", "reject"),
        ("opt", None, "accept"),
        ("opt", "3", "accept->3"),
        ("lst", [1, 2], "accept"),
        ("lst", ["a"], "reject"),
        ("dct", {"k": 1}, "accept"),
        ("dct", {"k": "no"}, "reject"),
    ]
    passed = 0
    for key, value, expectation in cases:
        cfg = OmegaConf.structured(Schema)
        try:
            setattr(cfg, key, value)
            got = f"accept->{getattr(cfg, key)!r}"
            rejected = False
        except Exception as exc:  # noqa: BLE001
            got = f"reject ({type(exc).__name__})"
            rejected = True
        want_reject = expectation == "reject"
        ok = rejected == want_reject
        passed += ok
        flag = "ok " if ok else "BAD"
        print(f"  {flag} {key}={value!r:14s} expected {expectation:22s} got {got}")
    print(f"\n  {passed}/{len(cases)} behave as omegaconf would")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
