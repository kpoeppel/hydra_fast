"""Differential check over OmegaConf's *own* grammar corpus.

OmegaConf's ``tests/test_grammar.py`` holds ~350 ``(id, input, expected)``
cases covering every corner of the interpolation grammar. Those tests
themselves assert against ANTLR *parse trees*, which hydra-fast has none of by
design -- so they cannot run against it. The corpus, though, is just data.

This harness imports the corpus from an omegaconf source checkout and drives
each input through the **public API** of both implementations, comparing
results. That gets the breadth of upstream's grammar coverage without needing
a parse tree.

    pip download --no-deps --no-binary :all: omegaconf==2.3.0 -d /tmp/oc
    tar xzf /tmp/oc/omegaconf-2.3.0.tar.gz -C /tmp/oc
    OMEGACONF_SRC=/tmp/oc/omegaconf-2.3.0 python bench/oracle_grammar_corpus.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

import _sources  # noqa: E402


def _corpus_dir() -> Path | None:
    found = _sources.omegaconf_src()
    if found is None or not (found / "tests" / "test_grammar.py").is_file():
        return None
    return found


def load_corpus() -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """``(config_value_cases, single_element_cases)`` as (id, input) pairs.

    Read with ``ast`` rather than imported: importing the module would pull in
    omegaconf internals and ANTLR, and all that is wanted here is the literal
    data.
    """
    import ast

    root = _corpus_dir()
    if root is None:
        return [], []
    source = (root / "tests" / "test_grammar.py").read_text()
    tree = ast.parse(source)

    wanted = {
        "PARAMS_SINGLE_ELEMENT_NO_INTERPOLATION": "single",
        "PARAMS_SINGLE_ELEMENT_WITH_INTERPOLATION": "single",
        "PARAMS_CONFIG_VALUE": "config",
    }
    config_cases: List[Tuple[str, str]] = []
    single_cases: List[Tuple[str, str]] = []

    for node in tree.body:
        targets = getattr(node, "targets", None) or (
            [node.target] if hasattr(node, "target") else []
        )
        for target in targets:
            name = getattr(target, "id", None)
            if name not in wanted or not isinstance(node.value, ast.List):
                continue
            bucket = config_cases if wanted[name] == "config" else single_cases
            for entry in node.value.elts:
                if not isinstance(entry, ast.Tuple) or len(entry.elts) < 2:
                    continue
                try:
                    case_id = ast.literal_eval(entry.elts[0])
                    text = ast.literal_eval(entry.elts[1])
                except ValueError:
                    continue  # an expected value that is an expression, not a literal
                if isinstance(case_id, str) and isinstance(text, str):
                    bucket.append((case_id, text))
    return config_cases, single_cases


# ---------------------------------------------------------------------------
# drivers
# ---------------------------------------------------------------------------
# The corpus assumes this config and these resolvers exist; transcribed from
# omegaconf's BASE_TEST_CFG / fixtures.
BASE = {
    "str": "hi",
    "int": 123,
    "float": 1.2,
    "dict": {"bar": 10, "a": {"b": {"c": 1}}},
    "list": [1, 2],
    "list_nested": [[1, 2], [3, 4]],
    "null": None,
    "bool": True,
    "missing": "???",
    "a": {"b": {"c": 7}},
    "b": "bar",
    "x": 1,
    "y": 2,
    "n": 3,
    "k": "b",
    "ref": "a",
    "FOO": "foo",
}


def _echo(*args: Any) -> Any:
    return list(args) if len(args) != 1 else args[0]


def register(module: Any) -> None:
    for name in ("test", "echo", "first", "ns1.ns2.test", "identity"):
        module.OmegaConf.register_new_resolver(name, _echo, replace=True)


def run_config_value(module: Any, text: str) -> Any:
    """A config value, as it would appear on the right of a YAML key."""
    data = dict(BASE)
    data["__probe__"] = text
    cfg = module.OmegaConf.create(data)
    value = module.OmegaConf.select(cfg, "__probe__")
    if type(value).__name__ in ("DictConfig", "ListConfig"):
        return module.OmegaConf.to_container(value, resolve=True)
    return value


def run_single_element(module: Any, text: str) -> Any:
    """A single element, reached through a resolver argument.

    ``${echo:<text>}`` parses ``<text>`` with the same VALUE_MODE rules the
    ``singleElement`` corpus targets, so the corpus can be driven without a
    parse tree.
    """
    data = dict(BASE)
    data["__probe__"] = "${echo:" + text + "}"
    cfg = module.OmegaConf.create(data)
    value = module.OmegaConf.select(cfg, "__probe__")
    if type(value).__name__ in ("DictConfig", "ListConfig"):
        return module.OmegaConf.to_container(value, resolve=True)
    return value


def describe(module: Any, runner: Any, text: str) -> str:
    try:
        return repr(runner(module, text))
    except Exception as exc:  # noqa: BLE001
        return f"RAISE {type(exc).__name__}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    config_cases, single_cases = load_corpus()
    if not config_cases and not single_cases:
        print(
            "omegaconf source checkout not found; set OMEGACONF_SRC to an "
            "unpacked omegaconf sdist",
            file=sys.stderr,
        )
        return 2

    try:
        import omegaconf
    except ImportError:
        print("reference omegaconf not installed", file=sys.stderr)
        return 2
    import hydra_fast

    register(omegaconf)
    register(hydra_fast)

    total = 0
    skipped = 0
    mismatches = []
    for label, runner, cases in (
        ("configValue", run_config_value, config_cases),
        ("singleElement", run_single_element, single_cases),
    ):
        for case_id, text in cases:
            # Only a literal `}` is genuinely inexpressible: it would close
            # the probe interpolation early. Quotes and commas are fine --
            # both implementations see the same resolver-argument text.
            if runner is run_single_element and "}" in text:
                skipped += 1
                continue
            total += 1
            expected = describe(omegaconf, runner, text)
            got = describe(hydra_fast, runner, text)
            if expected != got and repr(expected) != repr(got):
                mismatches.append((label, case_id, text, expected, got))
            elif args.verbose:
                print(f"ok   {label}/{case_id}: {text!r} -> {expected}")

    for label, case_id, text, expected, got in mismatches:
        print(f"DIFF {label}/{case_id}  input={text!r}")
        print(f"       omegaconf : {expected[:110]}")
        print(f"       hydra_fast: {got[:110]}")

    print(
        f"\n{total - len(mismatches)}/{total} corpus cases match "
        f"({skipped} not expressible through the public API)"
    )
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
