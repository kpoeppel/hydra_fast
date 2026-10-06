"""Capture OmegaConf's exact structured-config typing semantics.

Used as the oracle for hydra-fast's type layer: run against omegaconf to
record what it does, then against hydra-fast to compare. Every case is an
operation on a typed schema, reported as the resulting value+type or the
exception type.

    python bench/oracle_typing.py            # compare both
    python bench/oracle_typing.py --dump     # print omegaconf's answers
"""

from __future__ import annotations

import argparse
import dataclasses
import enum
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


class Color(enum.Enum):
    RED = 1
    GREEN = 2


@dataclasses.dataclass
class Inner:
    x: int = 1
    y: str = "why"


@dataclasses.dataclass
class Schema:
    n: int = 1
    f: float = 1.0
    s: str = "x"
    b: bool = True
    c: Color = Color.RED
    opt: Optional[int] = None
    anything: Any = None
    lst: List[int] = dataclasses.field(default_factory=lambda: [1, 2])
    slst: List[str] = dataclasses.field(default_factory=list)
    dct: Dict[str, int] = dataclasses.field(default_factory=dict)
    inner: Inner = dataclasses.field(default_factory=Inner)
    req: str = "???"


# Each case: (label, operation). The operation gets (module, cfg) and returns
# something comparable; raising is a valid outcome and is recorded as such.
def _assign(key: str, value: Any):
    def op(mod, cfg):
        setattr(cfg, key, value)
        return getattr(cfg, key)

    return op


def _setitem(key: str, value: Any):
    def op(mod, cfg):
        cfg[key] = value
        return cfg[key]

    return op


def _merge(payload: dict):
    def op(mod, cfg):
        merged = mod.OmegaConf.merge(cfg, mod.OmegaConf.create(payload))
        return mod.OmegaConf.to_container(merged)

    return op


def _update(key: str, value: Any):
    def op(mod, cfg):
        mod.OmegaConf.update(cfg, key, value)
        return mod.OmegaConf.select(cfg, key)

    return op


def _dotlist(entry: str):
    def op(mod, cfg):
        cfg.merge_with_dotlist([entry])
        return mod.OmegaConf.to_container(cfg)

    return op


CASES: List[Tuple[str, Any]] = [
    # -- int ------------------------------------------------------------
    ("assign int<-int", _assign("n", 7)),
    ("assign int<-str digits", _assign("n", "42")),
    ("assign int<-str junk", _assign("n", "nope")),
    ("assign int<-float exact", _assign("n", 3.0)),
    ("assign int<-float frac", _assign("n", 3.5)),
    ("assign int<-bool", _assign("n", True)),
    ("assign int<-None", _assign("n", None)),
    ("assign int<-list", _assign("n", [1])),
    # -- float ----------------------------------------------------------
    ("assign float<-int", _assign("f", 2)),
    ("assign float<-str", _assign("f", "1.5")),
    ("assign float<-str junk", _assign("f", "x")),
    ("assign float<-bool", _assign("f", True)),
    ("assign float<-None", _assign("f", None)),
    # -- str ------------------------------------------------------------
    ("assign str<-int", _assign("s", 5)),
    ("assign str<-bool", _assign("s", True)),
    ("assign str<-None", _assign("s", None)),
    ("assign str<-list", _assign("s", [1])),
    # -- bool -----------------------------------------------------------
    ("assign bool<-str true", _assign("b", "true")),
    ("assign bool<-str yes", _assign("b", "yes")),
    ("assign bool<-str junk", _assign("b", "maybe")),
    ("assign bool<-int 1", _assign("b", 1)),
    ("assign bool<-int 2", _assign("b", 2)),
    ("assign bool<-None", _assign("b", None)),
    # -- enum -----------------------------------------------------------
    ("assign enum<-member", _assign("c", Color.GREEN)),
    ("assign enum<-name", _assign("c", "GREEN")),
    ("assign enum<-qualified", _assign("c", "Color.GREEN")),
    ("assign enum<-value", _assign("c", 2)),
    ("assign enum<-bad name", _assign("c", "PURPLE")),
    ("assign enum<-None", _assign("c", None)),
    # -- Optional / Any -------------------------------------------------
    ("assign opt<-None", _assign("opt", None)),
    ("assign opt<-str digits", _assign("opt", "3")),
    ("assign opt<-junk", _assign("opt", "x")),
    ("assign any<-anything", _assign("anything", {"k": [1, 2]})),
    ("assign any<-None", _assign("anything", None)),
    # -- containers -----------------------------------------------------
    ("assign List[int]<-ints", _assign("lst", [3, 4])),
    ("assign List[int]<-str digits", _assign("lst", ["3", "4"])),
    ("assign List[int]<-junk", _assign("lst", ["a"])),
    ("assign List[int]<-scalar", _assign("lst", 5)),
    ("assign Dict[str,int]<-ok", _assign("dct", {"a": 1})),
    ("assign Dict[str,int]<-str vals", _assign("dct", {"a": "1"})),
    ("assign Dict[str,int]<-junk", _assign("dct", {"a": "x"})),
    ("assign Dict[str,int]<-list", _assign("dct", [1])),
    # -- nested dataclass -----------------------------------------------
    ("assign nested<-dict", _assign("inner", {"x": 9})),
    ("assign nested field", _update("inner.x", "9")),
    ("assign nested field junk", _update("inner.x", "junk")),
    ("assign nested<-scalar", _assign("inner", 5)),
    # -- missing --------------------------------------------------------
    ("assign req<-value", _assign("req", "given")),
    ("assign req<-???", _assign("req", "???")),
    # -- setitem mirrors setattr ----------------------------------------
    ("setitem int<-str", _setitem("n", "42")),
    ("setitem int<-junk", _setitem("n", "nope")),
    # -- merge (the common pattern) -------------------------------------
    ("merge str digits into int", _merge({"n": "42"})),
    ("merge junk into int", _merge({"n": "nope"})),
    ("merge str into float", _merge({"f": "1.5"})),
    ("merge str into bool", _merge({"b": "true"})),
    ("merge name into enum", _merge({"c": "GREEN"})),
    ("merge str list into List[int]", _merge({"lst": ["3", "4"]})),
    ("merge junk list into List[int]", _merge({"lst": ["a"]})),
    ("merge str vals into Dict[str,int]", _merge({"dct": {"a": "1"}})),
    ("merge nested", _merge({"inner": {"x": "9"}})),
    ("merge nested junk", _merge({"inner": {"x": "junk"}})),
    ("merge unknown key", _merge({"unknown": 1})),
    ("merge None into int", _merge({"n": None})),
    ("merge None into opt", _merge({"opt": None})),
    ("merge ??? into int", _merge({"n": "???"})),
    # -- dotlist (overrides path) ---------------------------------------
    ("dotlist int<-digits", _dotlist("n=42")),
    ("dotlist int<-junk", _dotlist("n=nope")),
    ("dotlist nested", _dotlist("inner.x=9")),
    # -- update ---------------------------------------------------------
    ("update int<-str digits", _update("n", "42")),
    ("update int<-junk", _update("n", "nope")),
    ("update enum<-name", _update("c", "GREEN")),
]


def describe(mod, op) -> str:
    cfg = mod.OmegaConf.structured(Schema)
    try:
        result = op(mod, cfg)
    except Exception as exc:  # noqa: BLE001
        return f"RAISE {type(exc).__name__}"
    if hasattr(result, "_hf_container") or type(result).__name__ in (
        "DictConfig",
        "ListConfig",
    ):
        result = mod.OmegaConf.to_container(result)
    if isinstance(result, dict):
        # only report the keys the case touched, with their types
        return "{" + ", ".join(
            f"{k}={v!r}:{type(v).__name__}" for k, v in sorted(result.items())
        ) + "}"
    return f"{result!r}:{type(result).__name__}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", action="store_true", help="print omegaconf's answers only")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    try:
        import omegaconf as OC
    except ImportError:
        print("reference omegaconf not installed", file=sys.stderr)
        return 2

    if args.dump:
        for label, op in CASES:
            print(f"{label:38s} {describe(OC, op)}")
        return 0

    import hydra_fast as HF

    mismatches = []
    for label, op in CASES:
        expected = describe(OC, op)
        got = describe(HF, op)
        if expected != got:
            mismatches.append((label, expected, got))
        elif args.verbose:
            print(f"ok   {label:38s} {expected}")

    for label, expected, got in mismatches:
        print(f"DIFF {label}")
        print(f"       omegaconf : {expected[:130]}")
        print(f"       hydra_fast: {got[:130]}")

    print(f"\n{len(CASES) - len(mismatches)}/{len(CASES)} typing cases match omegaconf")
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
