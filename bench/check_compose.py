"""Differential check: hydra_fast composition vs hydra composition.

Composes the same (config tree, config name, overrides) through both and
compares the fully resolved containers. Runs each side in a subprocess-free
way by importing both -- they share no global state.

    python bench/check_compose.py                   # synthetic trees
    python bench/check_compose.py --autoexp         # the real oellm-autoexp tree
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import textwrap
import traceback
from pathlib import Path
from typing import Any, List, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

import _sources  # noqa: E402

AUTOEXP = _sources.autoexp_src()


# ---------------------------------------------------------------------------
# synthetic config trees exercising composition features
# ---------------------------------------------------------------------------
def write_tree(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"))
    return root


TREES: dict = {}

TREES["basic"] = (
    {
        "conf/config.yaml": """
        defaults:
          - db: mysql
          - _self_
        app: demo
        msg: "driver=${db.driver} port=${db.port}"
        nested:
          a: 1
          b: ${nested.a}
        """,
        "conf/db/mysql.yaml": "driver: mysql\nport: 3306\nuser: root\n",
        "conf/db/postgres.yaml": "driver: postgresql\nport: 5432\nuser: postgres\n",
    },
    "config",
    [
        [],
        ["db=postgres"],
        ["db.port=9999"],
        ["++newkey=1"],
        ["+added=2"],
        ["app=changed"],
        ["~db.user"],
        ["db=postgres", "db.user=admin", "++extra.deep.key=hello"],
        ["nested.a=42"],
        ["db=null"],
    ],
)

TREES["packages"] = (
    {
        "conf/config.yaml": """
        defaults:
          - server/db: mysql
          - server/db@alt: postgres
          - _self_
        top: 1
        """,
        "conf/server/db/mysql.yaml": "driver: mysql\nport: 3306\n",
        "conf/server/db/postgres.yaml": "driver: postgresql\nport: 5432\n",
    },
    "config",
    [[], ["server/db=postgres"], ["server/db@alt=mysql"], ["server.db.port=1"]],
)

TREES["package_header"] = (
    {
        "conf/config.yaml": """
        defaults:
          - group: opt
          - _self_
        root_key: 1
        """,
        "conf/group/opt.yaml": """
        # @package _global_
        promoted:
          x: 1
        """,
        "conf/group/other.yaml": """
        # @package custom.pkg
        y: 2
        """,
    },
    "config",
    [[], ["group=other"]],
)

TREES["self_order"] = (
    {
        "conf/config.yaml": """
        defaults:
          - _self_
          - db: mysql
        shared: from_root
        """,
        "conf/db/mysql.yaml": "shared: from_db\ndriver: mysql\n",
    },
    "config",
    [[]],
)

TREES["optional_and_override"] = (
    {
        "conf/config.yaml": """
        defaults:
          - base
          - optional missing_group: nope
          - override inner/deep: b
          - _self_
        top: 1
        """,
        "conf/base.yaml": """
        defaults:
          - inner/deep: a
        base_key: yes_
        """,
        "conf/inner/deep/a.yaml": "which: a\n",
        "conf/inner/deep/b.yaml": "which: b\n",
    },
    "config",
    [[], ["inner/deep=a"]],
)

TREES["defaults_interpolation"] = (
    {
        "conf/config.yaml": """
        defaults:
          - platform: linux
          - impl/${platform}: fast
          - _self_
        top: 1
        """,
        "conf/platform/linux.yaml": "name: linux\n",
        "conf/platform/mac.yaml": "name: mac\n",
        "conf/impl/linux/fast.yaml": "impl: linux-fast\n",
        "conf/impl/mac/fast.yaml": "impl: mac-fast\n",
    },
    "config",
    [[], ["platform=mac"]],
)

TREES["list_options"] = (
    {
        "conf/config.yaml": """
        defaults:
          - group:
            - a
            - b
          - _self_
        top: 1
        """,
        "conf/group/a.yaml": "a_key: 1\nshared: a\n",
        "conf/group/b.yaml": "b_key: 2\nshared: b\n",
    },
    "config",
    [[]],
)

TREES["missing_and_resolvers"] = (
    {
        "conf/config.yaml": """
        defaults:
          - _self_
        required: ???
        from_env: ${oc.env:HF_CHECK_VAR,fallback}
        selected: ${oc.select:nothing.here,default_val}
        keys: ${oc.dict.keys:sub}
        values: ${oc.dict.values:sub}
        sub:
          one: 1
          two: ${sub.one}
        decoded: ${oc.decode:'[1,2,3]'}
        """,
    },
    "config",
    [["++required=given"], ["++required=given", "++sub.one=7"]],
)

TREES["shorthand_group"] = (
    {
        "conf/config.yaml": """
        defaults:
          - db: variants/mysql
          - _self_
        top: 1
        """,
        "conf/db/variants/mysql.yaml": "driver: mysql\n",
        "conf/db/variants/postgres.yaml": "driver: postgresql\n",
    },
    "config",
    [[], ["db/variants=postgres"]],
)

TREES["deletion"] = (
    {
        "conf/config.yaml": """
        defaults:
          - db: mysql
          - extra: thing
          - _self_
        top: 1
        """,
        "conf/db/mysql.yaml": "driver: mysql\n",
        "conf/extra/thing.yaml": "e: 1\n",
    },
    "config",
    [[], ["~extra"], ["~db"]],
)

TREES["interp_types"] = (
    {
        "conf/config.yaml": """
        defaults:
          - _self_
        i: 10
        f: 2.5
        b: true
        n: null
        s: text
        li: [1, 2, 3]
        di: {k: v}
        ref_i: ${i}
        ref_li: ${li}
        ref_di: ${di}
        concat: "i=${i} f=${f} b=${b} n=${n}"
        idx: ${li[1]}
        deep: ${di.k}
        esc: "\\${not_an_interp}"
        pct: "100%"
        """,
    },
    "config",
    [[], ["i=99"], ["li=[9,8]"], ["di={k:other}"]],
)


# ---------------------------------------------------------------------------
# runners
# ---------------------------------------------------------------------------
def run_hydra(config_dir: Path, config_name: str, overrides: List[str]) -> Any:
    from hydra import compose, initialize_config_dir
    from omegaconf import OmegaConf

    with initialize_config_dir(version_base=None, config_dir=str(config_dir)):
        cfg = compose(config_name=config_name, overrides=overrides)
        return OmegaConf.to_container(cfg, resolve=True)


def run_fast(config_dir: Path, config_name: str, overrides: List[str]) -> Any:
    from hydra_fast import OmegaConf, compose, initialize_config_dir

    with initialize_config_dir(version_base=None, config_dir=str(config_dir)):
        cfg = compose(config_name=config_name, overrides=overrides)
        return OmegaConf.to_container(cfg, resolve=True)


def normalize(value: Any) -> Any:
    """Make results comparable: NaN-safe, tuple/list-agnostic."""
    if isinstance(value, dict):
        return {
            key: normalize(item)
            for key, item in sorted(value.items(), key=lambda kv: str(kv[0]))
        }
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    if isinstance(value, float) and value != value:
        return "nan"
    return value


def compare(
    label: str, config_dir: Path, config_name: str, overrides: List[str]
) -> Tuple[bool, str]:
    try:
        expected = normalize(run_hydra(config_dir, config_name, overrides))
        expected_err = None
    except Exception as exc:  # noqa: BLE001
        expected, expected_err = None, type(exc).__name__
    try:
        got = normalize(run_fast(config_dir, config_name, overrides))
        got_err = None
    except Exception as exc:  # noqa: BLE001
        got, got_err = None, type(exc).__name__
        got_tb = traceback.format_exc()

    if expected_err or got_err:
        ok = (expected_err is not None) and (got_err is not None)
        detail = f"hydra raised {expected_err}, fast raised {got_err}"
        if not ok and got_err:
            detail += "\n" + textwrap.indent(got_tb, "      ")
        return ok, detail

    if expected == got:
        return True, ""
    return False, _diff(expected, got)


def _diff(expected: Any, got: Any, path: str = "") -> str:
    lines: List[str] = []
    if isinstance(expected, dict) and isinstance(got, dict):
        for key in sorted(set(expected) | set(got), key=str):
            sub = f"{path}.{key}" if path else str(key)
            if key not in expected:
                lines.append(f"      +{sub} = {got[key]!r} (only in fast)")
            elif key not in got:
                lines.append(f"      -{sub} = {expected[key]!r} (only in hydra)")
            elif expected[key] != got[key]:
                lines.extend(_diff(expected[key], got[key], sub).splitlines())
        return "\n".join(lines)
    if isinstance(expected, list) and isinstance(got, list) and len(expected) == len(got):
        for index, (exp, act) in enumerate(zip(expected, got, strict=False)):
            if exp != act:
                lines.extend(_diff(exp, act, f"{path}[{index}]").splitlines())
        return "\n".join(lines)
    return f"      {path or '<root>'}: hydra={expected!r} fast={got!r}"


# ---------------------------------------------------------------------------
# autoexp cases
# ---------------------------------------------------------------------------
AUTOEXP_OVERRIDES = [
    ["slurm=juwels", "container=juwels"],
    ["slurm=juwels", "container=juwels", "++index=3"],
    ["slurm=leonardo", "container=leonardo"],
    ["slurm=lumi", "container=lumi"],
    ["slurm=base", "container=none"],
    ["slurm=juwels", "container=juwels", "job=auto_restart"],
    ["slurm=juwels", "container=juwels", "++backend.megatron.lr=0.001"],
    ["slurm=juwels", "container=juwels", "++backend.megatron.global_batch_size=256"],
    ["slurm=juwels", "container=juwels", "backend=megatron_torchdist"],
    ["slurm=juwels", "container=juwels", "postprocess=eval_opensci"],
    ["slurm=snellius", "container=snellius"],
    ["slurm=marenostrum", "container=marenostrum"],
]


def register_resolvers(which: str) -> None:
    """Register oellm-autoexp's custom resolvers against the chosen backend.

    The autoexp modules bind ``from omegaconf import OmegaConf`` at import
    time, so the shim has to be installed before they are imported -- which is
    why the two backends cannot share a process and the autoexp comparison
    runs each side in a subprocess.
    """
    sys.path.insert(0, str(AUTOEXP))
    os.environ["HYDRA_STAGED_SWEEP_CACHE"] = "0"
    if which == "fast":
        import hydra_fast.compat.omegaconf_shim as shim

        shim.install()
    from oellm_autoexp.argparse_schema.resolver import register_argparse_resolver
    from oellm_autoexp.backends.megatron.cli_metadata import (
        MEGATRON_ACTION_SPECS,
        MEGATRON_ARG_METADATA,
    )
    from oellm_autoexp.hydra_staged_sweep.config.resolvers import register_default_resolvers

    register_default_resolvers()
    register_argparse_resolver(
        "argsmegatron",
        arg_metadata=dict(MEGATRON_ARG_METADATA),
        action_specs=dict(MEGATRON_ACTION_SPECS),
        skip_defaults=True,
    )
    register_argparse_resolver("cliargs")


def worker(impl: str, config_dir: str, config_name: str, overrides: List[str]) -> int:
    """Compose once and print the resolved container as JSON on stdout."""
    import json

    register_resolvers(impl)
    runner = run_fast if impl == "fast" else run_hydra
    try:
        result = normalize(runner(Path(config_dir), config_name, overrides))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"__error__": type(exc).__name__, "msg": str(exc)[:400]}))
        return 0
    print(json.dumps({"__ok__": result}, default=repr, sort_keys=True))
    return 0


def run_in_subprocess(
    impl: str, config_dir: Path, config_name: str, overrides: List[str]
) -> Any:
    import json
    import subprocess

    proc = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            impl,
            "--config-dir",
            str(config_dir),
            "--config-name",
            config_name,
            "--overrides",
            *overrides,
        ],
        capture_output=True,
        text=True,
        cwd=str(Path.home()),
    )
    line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    if not line:
        return {"__error__": "NoOutput", "msg": proc.stderr[-400:]}
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {"__error__": "BadOutput", "msg": line[:400]}


def compare_subprocess(
    config_dir: Path, config_name: str, overrides: List[str]
) -> Tuple[bool, str]:
    expected = run_in_subprocess("hydra", config_dir, config_name, overrides)
    got = run_in_subprocess("fast", config_dir, config_name, overrides)

    if "__error__" in expected or "__error__" in got:
        ok = ("__error__" in expected) and ("__error__" in got)
        return ok, (
            f"hydra={expected.get('__error__', 'ok')}: "
            f"{expected.get('msg', '')[:200]}\n"
            f"      fast={got.get('__error__', 'ok')}: {got.get('msg', '')[:200]}"
        )
    if expected["__ok__"] == got["__ok__"]:
        return True, ""
    return False, _diff(expected["__ok__"], got["__ok__"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--autoexp", action="store_true", help="also run the real config tree")
    ap.add_argument("--only", default=None, help="run a single synthetic tree by name")
    ap.add_argument("--worker", default=None, choices=["hydra", "fast"])
    ap.add_argument("--config-dir", default=None)
    ap.add_argument("--config-name", default=None)
    ap.add_argument("--overrides", nargs="*", default=[])
    args = ap.parse_args()

    if args.worker:
        return worker(args.worker, args.config_dir, args.config_name, args.overrides)

    os.environ.setdefault("OUTPUT_DIR", "./output")
    os.environ.setdefault("PROJECT_DIR", ".")
    os.environ.setdefault("HF_HOME", "/tmp/hf")
    os.environ.setdefault("SLURM_ACCOUNT", "bench")
    os.environ.setdefault("SLURM_PARTITION", "bench")

    base = Path(tempfile.mkdtemp(prefix="hydra-fast-check-compose-"))
    base.mkdir(parents=True, exist_ok=True)

    passed = 0
    failed: List[Tuple[str, str]] = []

    names = [args.only] if args.only else list(TREES)
    for name in names:
        files, config_name, override_sets = TREES[name]
        root = write_tree(base / name, files)
        for overrides in override_sets:
            label = f"{name} {overrides}"
            ok, detail = compare(label, root / "conf", config_name, overrides)
            if ok:
                passed += 1
            else:
                failed.append((label, detail))

    if args.autoexp:
        if not (AUTOEXP / "config").is_dir():
            print(f"skipping --autoexp: {AUTOEXP} not found", file=sys.stderr)
        else:
            for overrides in AUTOEXP_OVERRIDES:
                label = f"autoexp {overrides}"
                ok, detail = compare_subprocess(AUTOEXP / "config", "autoexp", overrides)
                if ok:
                    passed += 1
                else:
                    failed.append((label, detail))

    for label, detail in failed:
        print(f"FAIL {label}")
        if detail:
            print(detail)
    total = passed + len(failed)
    print(f"\n{passed}/{total} composition cases match hydra")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
