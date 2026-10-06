"""Differential check: hydra_fast's grammar vs omegaconf's ANTLR grammar.

Resolves the same interpolation strings through both and reports mismatches.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from omegaconf import DictConfig, ListConfig, OmegaConf  # noqa: E402

# (config, key) pairs: build the config in omegaconf, read `key`, compare with
# hydra_fast's compiled closure evaluated against the same data.
CASES = [
    # plain node references
    ({"a": 1, "b": "${a}"}, "b"),
    ({"a": {"b": {"c": 7}}, "x": "${a.b.c}"}, "x"),
    ({"a": [10, 20, 30], "x": "${a[1]}"}, "x"),
    ({"a": {"b": 1}, "x": "${a[b]}"}, "x"),
    ({"a": {"b": 1}, "x": "${[a].b}"}, "x"),
    # relative references
    ({"a": {"b": 1, "c": "${.b}"}}, "a.c"),
    ({"a": {"b": {"c": "${..d}"}, "d": 5}}, "a.b.c"),
    ({"top": 1, "a": {"b": {"c": "${...top}"}}}, "a.b.c"),
    # string concatenation and typing
    ({"a": 1, "b": "x${a}y"}, "b"),
    ({"a": 1, "b": "${a}${a}"}, "b"),
    ({"a": 1.5, "b": "${a}"}, "b"),
    ({"a": True, "b": "${a}"}, "b"),
    ({"a": None, "b": "${a}"}, "b"),
    ({"a": [1, 2], "b": "${a}"}, "b"),
    ({"a": {"k": 1}, "b": "${a}"}, "b"),
    # escapes
    ({"b": r"\${a}"}, "b"),
    ({"a": 1, "b": "\\\\${a}"}, "b"),
    ({"b": "a\\b"}, "b"),
    ({"b": "100%"}, "b"),
    ({"b": "a$b"}, "b"),
    ({"b": "$"}, "b"),
    ({"b": "}"}, "b"),
    ({"b": "{}"}, "b"),
    # nested interpolation in the key
    ({"a": {"x": 5}, "k": "x", "b": "${a.${k}}"}, "b"),
    ({"ref": "a", "a": 3, "b": "${${ref}}"}, "b"),
    # resolvers
    ({"b": "${oc.env:HF_TEST_VAR,fallback}"}, "b"),
    ({"b": "${oc.env:HF_TEST_VAR,'a,b'}"}, "b"),
    ({"a": 2, "b": "${oc.select:a,9}"}, "b"),
    ({"b": "${oc.select:nope,9}"}, "b"),
    ({"a": {"x": 1, "y": 2}, "b": "${oc.dict.keys:a}"}, "b"),
    ({"a": {"x": 1, "y": 2}, "b": "${oc.dict.values:a}"}, "b"),
    ({"b": "${oc.decode:'[1,2,3]'}"}, "b"),
    # resolver args: lists, dicts, quotes, nesting
    ({"b": "${hf.echo:[1,2,[3,4]]}"}, "b"),
    ({"b": "${hf.echo:{a:1,b:two}}"}, "b"),
    ({"b": "${hf.echo:'hello world'}"}, "b"),
    ({"b": '${hf.echo:"hello world"}'}, "b"),
    ({"a": 4, "b": "${hf.echo:'v=${a}'}"}, "b"),
    ({"b": "${hf.echo:}"}, "b"),
    ({"b": "${hf.echo:null}"}, "b"),
    ({"b": "${hf.echo:true,False}"}, "b"),
    ({"b": "${hf.echo:1_000}"}, "b"),
    ({"b": "${hf.echo:2.5e-4}"}, "b"),
    ({"b": "${hf.echo:-1}"}, "b"),
    ({"b": "${hf.echo:inf,nan}"}, "b"),
    ({"b": "${hf.echo:a/b-c.d}"}, "b"),
    ({"b": "${hf.echo:x\\,y}"}, "b"),
    ({"b": "${hf.echo:'a${hf.echo:b}c'}"}, "b"),
    ({"a": 1, "b": "pre${hf.echo:${a}}post"}, "b"),
    # nested resolver as a resolver name
    ({"n": "echo", "b": "${hf.${n}:7}"}, "b"),
    # whitespace tolerance
    ({"a": 1, "b": "${ a }"}, "b"),
    ({"b": "${hf.echo: 1 , 2 }"}, "b"),
]


def register(oc: object) -> None:
    OmegaConf.register_new_resolver(
        "hf.echo", lambda *a: list(a) if len(a) != 1 else a[0], replace=True
    )


def main() -> int:
    register(OmegaConf)
    failures = []
    for index, (data, key) in enumerate(CASES):
        cfg = OmegaConf.create(data)
        try:
            expected = OmegaConf.select(cfg, key)
            if isinstance(expected, (DictConfig, ListConfig)):
                expected = OmegaConf.to_container(expected, resolve=True)
            expected_err = None
        except Exception as exc:  # noqa: BLE001
            expected, expected_err = None, f"{type(exc).__name__}"

        # hydra_fast side
        try:
            from hydra_fast import OmegaConf as FastOmegaConf
            from hydra_fast.container import Container as FastContainer

            FastOmegaConf.register_new_resolver(
                "hf.echo", lambda *a: list(a) if len(a) != 1 else a[0], replace=True
            )
            fcfg = FastOmegaConf.create(data)
            got = FastOmegaConf.select(fcfg, key)
            if isinstance(got, FastContainer):
                got = FastOmegaConf.to_container(got, resolve=True)
            got_err = None
        except Exception as exc:  # noqa: BLE001
            got, got_err = None, f"{type(exc).__name__}"

        if expected_err or got_err:
            ok = (expected_err is not None) == (got_err is not None)
            detail = f"omegaconf={expected_err or expected!r} fast={got_err or got!r}"
        else:
            # repr comparison so NaN compares equal to itself
            ok = expected == got or repr(expected) == repr(got)
            detail = f"omegaconf={expected!r} fast={got!r}"
        if not ok:
            failures.append((index, data, key, detail))

    for index, data, key, detail in failures:
        src = data.get(key.split(".")[0]) if "." in key else data.get(key)
        print(f"FAIL[{index}] key={key!r} src={src!r}\n      {detail}")
    print(f"\n{len(CASES) - len(failures)}/{len(CASES)} grammar cases match")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
