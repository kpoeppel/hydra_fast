"""Static introspection of interpolations, without resolving them.

A compiled closure is opaque, so the structure is recorded while the compiler
builds one. This is the capability an ANTLR parse tree would otherwise be
walked for -- dependency analysis, linting, "which keys does this config
read?" -- and the tests compare against exactly that walk.
"""

from __future__ import annotations

import pytest

from conftest import requires_omegaconf
from hydra_fast import OmegaConf
from hydra_fast import analyze_interpolation as analyze


# ---------------------------------------------------------------------------
# the capability itself
# ---------------------------------------------------------------------------
def test_static_node_references():
    found = analyze("${db.host}:${db.port}/${oc.env:DB_NAME,prod}")
    assert found.referenced_keys() == ("db.host", "db.port")
    assert found.resolvers == ("oc.env",)
    assert not found.dynamic


def test_plain_string_has_nothing():
    found = analyze("just text")
    assert not found.has_interpolation
    assert found.referenced_keys() == ()
    assert found.resolvers == ()


def test_non_string_is_tolerated():
    assert analyze(None).referenced_keys() == ()  # type: ignore[arg-type]
    assert analyze(5).referenced_keys() == ()  # type: ignore[arg-type]


def test_relative_references_are_not_absolute_keys():
    """`${..a}` has no absolute key until you know where it sits."""
    found = analyze("${..a}")
    assert found.referenced_keys() == ()
    assert len(found.node_refs) == 1
    assert found.node_refs[0].relative_dots == 2
    assert found.node_refs[0].parts == ("a",)


def test_bracket_path_is_flattened():
    found = analyze("${a[b].c}")
    assert found.node_refs[0].parts == ("a", "b", "c")


def test_computed_path_is_reported_as_a_lower_bound():
    """`${a.${k}}` cannot be reduced to a key, so say so rather than guess."""
    found = analyze("${a.${k}}")
    assert found.dynamic
    # the inner reference IS statically known
    assert "k" in found.referenced_keys()
    # the outer one is flagged, not silently turned into a fake key
    outer = [ref for ref in found.node_refs if ref.dynamic]
    assert len(outer) == 1
    assert outer[0].parts == ("a",)


def test_computed_resolver_name_is_dynamic():
    found = analyze("${ns.${which}:x}")
    assert found.dynamic
    assert found.resolvers == ()


def test_resolver_arguments_are_traversed():
    found = analyze("${oc.select:${fallback.key},default}")
    assert found.resolvers == ("oc.select",)
    assert found.referenced_keys() == ("fallback.key",)


def test_nested_resolvers():
    found = analyze("${oc.env:${a.b},${oc.select:c,d}}")
    assert set(found.resolvers) == {"oc.env", "oc.select"}
    assert found.referenced_keys() == ("a.b",)


def test_escaped_interpolation_is_not_a_reference():
    assert analyze(r"\${not.a.ref}").referenced_keys() == ()


def test_analysis_is_cached_with_the_compile():
    first = analyze("${a.b}")
    second = analyze("${a.b}")
    assert first is second, "analysis should ride the compile cache"


# ---------------------------------------------------------------------------
# a realistic use: which keys does a config depend on?
# ---------------------------------------------------------------------------
def _dependencies(cfg) -> dict:
    """Map each key holding an interpolation to the keys it reads."""
    out = {}

    def walk(data, prefix=()):
        items = data.items() if isinstance(data, dict) else enumerate(data)
        for key, value in items:
            path = prefix + (key,)
            if isinstance(value, (dict, list)):
                walk(value, path)
            elif isinstance(value, str):
                found = analyze(value)
                if found.has_interpolation:
                    out[".".join(str(p) for p in path)] = found.referenced_keys()

    walk(cfg._hf_container())
    return out


def test_dependency_graph_of_a_config():
    cfg = OmegaConf.create(
        {
            "db": {"host": "h", "port": 5432},
            "url": "${db.host}:${db.port}",
            "label": "run-${db.host}",
            "nested": {"deep": "${url}"},
            "plain": "nothing here",
            "envy": "${oc.env:HOME,/tmp}",
        }
    )
    assert _dependencies(cfg) == {
        "url": ("db.host", "db.port"),
        "label": ("db.host",),
        "nested.deep": ("url",),
        "envy": (),
    }


# ---------------------------------------------------------------------------
# compared with walking omegaconf's parse tree, which is the alternative
# ---------------------------------------------------------------------------
TREE_CASES = [
    "${db.host}:${db.port}/${oc.env:DB_NAME,prod}",
    "${a.b}",
    "${a[b].c}",
    "prefix-${x}-suffix",
    "${oc.select:${fallback.key},default}",
    "${oc.env:X}${oc.select:a.b,d}",
    "${a}${b}${c}",
    "no interpolation at all",
]


def _omegaconf_tree_refs(text):
    """The same information, extracted by walking omegaconf's parse tree."""
    from omegaconf import grammar_parser
    from omegaconf.grammar.gen.OmegaConfGrammarParser import OmegaConfGrammarParser as P

    tree = grammar_parser.parse(text)
    refs, resolvers = [], []

    def collect(node):
        if isinstance(node, P.InterpolationNodeContext):
            refs.append(node.getText()[2:-1])
        elif isinstance(node, P.InterpolationResolverContext):
            resolvers.append(node.getChild(1).getText())
        for index in range(getattr(node, "getChildCount", lambda: 0)()):
            collect(node.getChild(index))

    collect(tree)
    return tuple(refs), tuple(resolvers)


@requires_omegaconf
@pytest.mark.parametrize("text", TREE_CASES)
def test_matches_a_walk_of_omegaconfs_parse_tree(text):
    expected_refs, expected_resolvers = _omegaconf_tree_refs(text)
    found = analyze(text)
    assert found.referenced_keys() == expected_refs
    assert found.resolvers == expected_resolvers


# ---------------------------------------------------------------------------
# parse once, resolve many -- the other thing a parse tree is used for
# ---------------------------------------------------------------------------
def test_parse_then_resolve_against_several_configs():
    """The compiled closure plays the role omegaconf's parse tree plays."""
    from hydra_fast.grammar.interpolation import parse

    tree = parse("${a.b}")
    for data, expected in (({"a": {"b": 1}}, 1), ({"a": {"b": "two"}}, "two")):
        cfg = OmegaConf.create(data)
        assert cfg.resolve_parse_tree(tree, node=cfg._get_node("a")) == expected


def test_parse_single_element_rule():
    from hydra_fast.grammar.interpolation import parse

    tree = parse("[1,2,3]", parser_rule="singleElement", lexer_mode="VALUE_MODE")
    cfg = OmegaConf.create({})
    assert cfg.resolve_parse_tree(tree) == [1, 2, 3]


def test_parse_rejects_unknown_rule():
    from hydra_fast.errors import GrammarParseError
    from hydra_fast.grammar.interpolation import parse

    with pytest.raises(GrammarParseError):
        parse("x", parser_rule="noSuchRule")


def test_resolve_parse_tree_uses_the_node_for_relative_refs():
    from hydra_fast.grammar.interpolation import parse

    tree = parse("${.sibling}")
    cfg = OmegaConf.create({"outer": {"sibling": 7, "here": 0}})
    node = cfg.outer._get_node("here")
    assert cfg.resolve_parse_tree(tree, node=node) == 7
