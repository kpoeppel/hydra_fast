"""Audit of the private omegaconf surface the shim stands in for.

``docs/compatibility.md`` says ``omegaconf._utils`` and the ``Node`` layer are
*partially* reproduced -- only the names third-party code actually imports.
"Partially" is not a contract, so this measures it: for every public name on
the real modules, does the shim have one, and where a name exists on both, do
they behave the same on a representative call?

Three questions, reported separately:

1. **Coverage** -- which names exist on the real module and not on the shim.
   A gap there is only a problem if something imports it, so the output names
   them rather than failing.
2. **Node layer** -- ``cfg._get_node(k)`` and the methods code calls on the
   result, compared value by value.
3. **Behaviour** -- the ``_utils`` helpers that *are* provided, called on the
   same inputs through both.

    pip install omegaconf==2.3.0
    python bench/oracle_internals.py
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple, Union

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))


@dataclasses.dataclass
class Schema:
    n: int = 1
    s: str = "x"


#: (label, factory) -- configs the node layer is probed against.
CONFIGS = [
    ("plain", lambda api: api.create({"a": 1, "s": "t", "f": 1.5, "b": True, "n": None})),
    ("nested", lambda api: api.create({"outer": {"inner": 2}})),
    ("with-list", lambda api: api.create({"items": [1, 2, 3]})),
    ("interpolation", lambda api: api.create({"a": 1, "ref": "${a}"})),
    ("missing", lambda api: api.create({"m": "???"})),
    ("structured", lambda api: api.structured(Schema)),
]

#: Methods code written against omegaconf calls on a node.
NODE_CALLS: List[Tuple[str, Any]] = [
    ("_value", lambda node: node._value()),
    ("_is_missing", lambda node: node._is_missing()),
    ("_is_interpolation", lambda node: node._is_interpolation()),
    ("_is_none", lambda node: node._is_none()),
    ("_is_optional", lambda node: node._is_optional()),
    ("_key", lambda node: node._key()),
    ("_get_full_key", lambda node: node._get_full_key("")),
    ("type-name", lambda node: type(node).__name__),
    ("metadata.key", lambda node: node._metadata.key),
    ("metadata.optional", lambda node: node._metadata.optional),
    ("bool", lambda node: bool(node)),
    ("str", lambda node: str(node)),
    ("eq-value", lambda node: node == node._value()),
]

#: (label, callable taking the `_utils` module) for the helpers provided.
UTILS_CALLS: List[Tuple[str, Any]] = [
    ("is_structured_config(dataclass)", lambda u: u.is_structured_config(Schema)),
    ("is_structured_config(instance)", lambda u: u.is_structured_config(Schema())),
    ("is_structured_config(dict)", lambda u: u.is_structured_config({"a": 1})),
    ("is_structured_config(int)", lambda u: u.is_structured_config(5)),
    ("type_str(int)", lambda u: u.type_str(int)),
    ("type_str(str)", lambda u: u.type_str(str)),
    ("type_str(NoneType)", lambda u: u.type_str(type(None))),
    ("get_yaml_loader-callable", lambda u: callable(u.get_yaml_loader())),
    # value classification
    ("get_value_kind(1)", lambda u: u.get_value_kind(1).name),
    ("get_value_kind('???')", lambda u: u.get_value_kind("???").name),
    ("get_value_kind('${a}')", lambda u: u.get_value_kind("${a}").name),
    ("get_value_kind('x${a}y')", lambda u: u.get_value_kind("x${a}y").name),
    ("get_value_kind(None)", lambda u: u.get_value_kind(None).name),
    ("ValueKind names", lambda u: [e.name for e in u.ValueKind]),
    ("_is_missing_value('???')", lambda u: u._is_missing_value("???")),
    ("_is_missing_value('x')", lambda u: u._is_missing_value("x")),
    ("_is_interpolation('${a}')", lambda u: u._is_interpolation("${a}")),
    ("_is_interpolation(5)", lambda u: u._is_interpolation(5)),
    ("_is_none(None)", lambda u: u._is_none(None)),
    ("_is_none(0)", lambda u: u._is_none(0)),
    ("_is_special('???')", lambda u: u._is_special("???")),
    ("_is_special(1)", lambda u: u._is_special(1)),
    ("_get_value(5)", lambda u: u._get_value(5)),
    # containers and annotations
    ("is_primitive_dict({})", lambda u: u.is_primitive_dict({})),
    ("is_primitive_dict(5)", lambda u: u.is_primitive_dict(5)),
    ("is_primitive_list([])", lambda u: u.is_primitive_list([])),
    ("is_primitive_list(())", lambda u: u.is_primitive_list(())),
    ("is_primitive_container({})", lambda u: u.is_primitive_container({})),
    ("is_dict_annotation(Dict[str,int])", lambda u: u.is_dict_annotation(Dict[str, int])),
    ("is_dict_annotation(dict)", lambda u: u.is_dict_annotation(dict)),
    ("is_dict_annotation(int)", lambda u: u.is_dict_annotation(int)),
    ("is_list_annotation(List[int])", lambda u: u.is_list_annotation(List[int])),
    ("is_list_annotation(int)", lambda u: u.is_list_annotation(int)),
    ("is_tuple_annotation(Tuple[int])", lambda u: u.is_tuple_annotation(Tuple[int])),
    ("dict_kv(Dict[str,int])", lambda u: u.get_dict_key_value_types(Dict[str, int])),
    ("dict_kv(Dict)", lambda u: u.get_dict_key_value_types(Dict)),
    ("list_elem(List[int])", lambda u: u.get_list_element_type(List[int])),
    ("list_elem(List)", lambda u: u.get_list_element_type(List)),
    ("get_type_of(Schema)", lambda u: u.get_type_of(Schema).__name__),
    ("get_type_of(Schema())", lambda u: u.get_type_of(Schema()).__name__),
    ("is_attr_class(Schema)", lambda u: u.is_attr_class(Schema)),
    ("is_dataclass(Schema)", lambda u: u.is_dataclass(Schema)),
    ("is_dataclass(5)", lambda u: u.is_dataclass(5)),
    ("split_key('a.b')", lambda u: u.split_key("a.b")),
    ("split_key('a.b[0].c')", lambda u: u.split_key("a.b[0].c")),
    ("split_key('a')", lambda u: u.split_key("a")),
    # the second batch of annotation predicates
    ("is_int(5)", lambda u: u.is_int(5)),
    ("is_int('5')", lambda u: u.is_int("5")),
    ("is_int('x')", lambda u: u.is_int("x")),
    ("is_float('1.5')", lambda u: u.is_float("1.5")),
    ("is_float('x')", lambda u: u.is_float("x")),
    ("is_dict(dict)", lambda u: u.is_dict(dict)),
    ("is_dict(Dict[str,int])", lambda u: u.is_dict(Dict[str, int])),
    ("is_dict(5)", lambda u: u.is_dict(5)),
    ("is_generic_dict(Dict)", lambda u: u.is_generic_dict(Dict)),
    ("is_generic_dict(Dict[str,int])", lambda u: u.is_generic_dict(Dict[str, int])),
    ("is_generic_dict(dict)", lambda u: u.is_generic_dict(dict)),
    ("is_generic_dict(int)", lambda u: u.is_generic_dict(int)),
    ("is_generic_list(List)", lambda u: u.is_generic_list(List)),
    ("is_generic_list(List[int])", lambda u: u.is_generic_list(List[int])),
    ("is_generic_list(list)", lambda u: u.is_generic_list(list)),
    ("is_generic_list(int)", lambda u: u.is_generic_list(int)),
    ("is_union_annotation(Union[int,str])", lambda u: u.is_union_annotation(Union[int, str])),
    ("is_union_annotation(int)", lambda u: u.is_union_annotation(int)),
    ("is_container_annotation(Dict[str,int])", lambda u: u.is_container_annotation(Dict[str, int])),
    ("is_container_annotation(List[int])", lambda u: u.is_container_annotation(List[int])),
    ("is_container_annotation(int)", lambda u: u.is_container_annotation(int)),
    ("is_primitive_type_annotation(int)", lambda u: u.is_primitive_type_annotation(int)),
    ("is_primitive_type_annotation(str)", lambda u: u.is_primitive_type_annotation(str)),
    ("is_primitive_type_annotation(Dict)", lambda u: u.is_primitive_type_annotation(Dict)),
    ("_is_missing_literal('???')", lambda u: u._is_missing_literal("???")),
    ("_is_missing_literal('x')", lambda u: u._is_missing_literal("x")),
    ("yaml_is_bool('yes')", lambda u: u.yaml_is_bool("yes")),
    ("yaml_is_bool('on')", lambda u: u.yaml_is_bool("on")),
    ("yaml_is_bool('maybe')", lambda u: u.yaml_is_bool("maybe")),
]


def call(fn: Any, *args: Any) -> Tuple[str, str]:
    try:
        return ("ok", repr(fn(*args)))
    except Exception as exc:  # noqa: BLE001
        return ("raise", type(exc).__name__)


def audit_names(real: Any, shim: Any, label: str) -> List[str]:
    """Public names on the real module that the shim does not have."""
    missing = [
        name
        for name in sorted(dir(real))
        if not name.startswith("__") and not hasattr(shim, name)
    ]
    print(f"\n{label}: {len(missing)} of the real module's public names are absent")
    if missing:
        print(f"  {', '.join(missing)}")
    return missing


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()

    try:
        import omegaconf
        import omegaconf._utils as real_utils
    except ImportError:
        print("omegaconf is not installed; nothing to compare against.")
        return 1

    import hydra_fast
    import hydra_fast.compat.omegaconf_shim as shim_module

    print("=" * 74)
    print("1. name coverage")
    print("=" * 74)
    # The shim's modules are built without installing them, so this audit does
    # not disturb the process it runs in.
    shim_omegaconf, _, shim_utils, _ = shim_module._make_omegaconf_module()
    audit_names(real_utils, shim_utils, "omegaconf._utils")
    # Against the *shim's* omegaconf module, which is what an import sees --
    # not the hydra_fast package, whose own names are a different question.
    audit_names(omegaconf, shim_omegaconf, "omegaconf (top level)")

    print()
    print("=" * 74)
    print("2. node layer")
    print("=" * 74)
    diffs: List[Tuple[str, Any, Any]] = []
    checked = 0
    for label, factory in CONFIGS:
        real_cfg = factory(omegaconf.OmegaConf)
        fast_cfg = factory(hydra_fast.OmegaConf)
        for key in list(real_cfg.keys()):
            want_node = call(real_cfg._get_node, key)
            got_node = call(fast_cfg._get_node, key)
            checked += 1
            if want_node[0] != got_node[0]:
                diffs.append((f"{label}.{key} _get_node", want_node, got_node))
                continue
            if want_node[0] == "raise":
                continue
            real_node = real_cfg._get_node(key)
            fast_node = fast_cfg._get_node(key)
            for call_label, fn in NODE_CALLS:
                checked += 1
                want, got = call(fn, real_node), call(fn, fast_node)
                if want != got:
                    diffs.append((f"{label}.{key} {call_label}", want, got))
        # identity stability, which omegaconf guarantees
        first = next(iter(real_cfg.keys()))
        checked += 1
        real_stable = real_cfg._get_node(first) is real_cfg._get_node(first)
        fast_stable = fast_cfg._get_node(first) is fast_cfg._get_node(first)
        if real_stable != fast_stable:
            diffs.append((f"{label} node identity", real_stable, fast_stable))

    print(f"{checked - len(diffs)}/{checked} node-layer probes match")
    for label, want, got in diffs:
        print(f"  DIFF {label}\n    omegaconf : {want}\n    hydra-fast: {got}")

    print()
    print("=" * 74)
    print("3. _utils behaviour")
    print("=" * 74)
    util_diffs = []
    for label, fn in UTILS_CALLS:
        want, got = call(fn, real_utils), call(fn, shim_utils)
        if want != got:
            util_diffs.append((label, want, got))
    print(f"{len(UTILS_CALLS) - len(util_diffs)}/{len(UTILS_CALLS)} _utils probes match")
    for label, want, got in util_diffs:
        print(f"  DIFF {label}\n    omegaconf : {want}\n    hydra-fast: {got}")

    return 1 if (diffs or util_diffs) else 0


if __name__ == "__main__":
    raise SystemExit(main())
