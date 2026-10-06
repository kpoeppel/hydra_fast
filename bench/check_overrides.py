"""Differential check: hydra_fast's override parser vs Hydra's ANTLR parser."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hydra.core.override_parser.overrides_parser import OverridesParser  # noqa: E402

from hydra_fast.grammar.override import parse_override  # noqa: E402

LINES = [
    # plain
    "key=value",
    "key=1",
    "key=1.5",
    "key=true",
    "key=False",
    "key=null",
    "key=",
    "key=2.5e-4",
    "key=1_000_000",
    "key=inf",
    "key=-10",
    "key=a-b_c",
    "key=/a/b/c",
    "key=100%",
    "key=a.b.c",
    # dotted / nested keys
    "a.b.c=1",
    "db.driver=mysql",
    # prefixes
    "+key=value",
    "++key=value",
    "~key",
    "~key=value",
    "+key.sub=1",
    "++a.b.c=1",
    # groups & packages
    "db=mysql",
    "db@pkg=mysql",
    "hydra/launcher=basic",
    "db@_global_=mysql",
    "db@=mysql",
    "group=null",
    # quoting
    "key='hello world'",
    'key="hello world"',
    "key='a,b'",
    "key='${a.b}'",
    r"key='it\'s'",
    'key="back\\\\slash"',
    "key=''",
    # containers
    "key=[]",
    "key=[1,2,3]",
    "key=[a,b,[1,2]]",
    "key={}",
    "key={a:1,b:2}",
    "key={a:{b:[1,2]}}",
    "key=[1, 2, 3]",
    # interpolations
    "key=${a.b}",
    "key=${oc.env:USER,me}",
    "key=pre${a}post",
    # sweeps
    "key=a,b,c",
    "key=1,2,3",
    "key=choice(a,b)",
    "key=choice(1,2,3)",
    "key=range(1,5)",
    "key=range(0,10,2)",
    "key=range(1.0,2.0,0.5)",
    "key=interval(0,1)",
    "group=glob(*)",
    "group=glob(*,exclude=foo)",
    "key=tag(log,choice(a,b))",
    "key=sort(choice(3,1,2))",
    "key=sort(choice(1,2,3),reverse=true)",
    "key=int(1.5)",
    "key=float(1)",
    "key=str(1)",
    "key=bool(true)",
    # escapes
    r"key=a\,b",
    r"key=a\ b",
    r"key=\[notalist\]",
]


def describe_hydra(line: str) -> object:
    parser = OverridesParser.create()
    ov = parser.parse_override(line)
    return (
        ov.type.name,
        ov.key_or_group,
        ov.package,
        ov.value_type.name if ov.value_type else None,
        repr(ov.value()),
    )


def describe_fast(line: str) -> object:
    ov = parse_override(line)
    return (
        ov.type.name,
        ov.key_or_group,
        ov.package,
        ov.value_type.name if ov.value_type else None,
        repr(ov.value()),
    )


# hydra 1.3 rejects bracket indexing in override keys; hydra 1.4-dev accepts it
# and so does hydra_fast. Checked separately so the superset stays deliberate.
SUPERSET = ["a.b[0]=1", "a[0].b=1"]


def check_superset() -> None:
    for line in SUPERSET:
        ov = parse_override(line)
        assert ov.key_or_group == line.split("=")[0], ov
        assert ov.is_value_path, f"{line} should parse as a value path"
    print(f"{len(SUPERSET)} documented-superset cases parse (hydra 1.3 rejects these)")


def main() -> int:
    check_superset()
    failures = []
    for line in LINES:
        try:
            expected: object = describe_hydra(line)
        except Exception as exc:  # noqa: BLE001
            expected = f"ERROR:{type(exc).__name__}"
        try:
            got: object = describe_fast(line)
        except Exception as exc:  # noqa: BLE001
            got = f"ERROR:{type(exc).__name__}"

        if expected != got:
            failures.append((line, expected, got))

    for line, expected, got in failures:
        print(f"FAIL {line!r}\n  hydra: {expected}\n  fast : {got}")
    print(f"\n{len(LINES) - len(failures)}/{len(LINES)} override cases match")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
