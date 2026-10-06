"""Composition: config trees composed through hydra-fast and through hydra."""

from __future__ import annotations

import textwrap
from typing import Any, Dict, List

import pytest

import hydra_fast
from conftest import requires_hydra


def write_tree(root, files: Dict[str, str]):
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"))
    return root


# name -> (files, config_name, [override sets])
TREES: Dict[str, Any] = {
    "basic": (
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
    ),
    "packages": (
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
    ),
    "package_header": (
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
    ),
    "self_order": (
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
    ),
    "optional_and_override": (
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
    ),
    "defaults_interpolation": (
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
    ),
    "list_options": (
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
    ),
    "missing_and_resolvers": (
        {
            "conf/config.yaml": """
            defaults:
              - _self_
            required: ???
            from_env: ${oc.env:HF_CHECK_UNSET,fallback}
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
    ),
    "shorthand_group": (
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
    ),
    "deletion": (
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
    ),
    "interp_types": (
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
    ),
    "nested_groups": (
        {
            "conf/config.yaml": """
            defaults:
              - outer: one
              - _self_
            root: r
            """,
            "conf/outer/one.yaml": """
            defaults:
              - inner: x
            o: one
            """,
            "conf/outer/two.yaml": """
            defaults:
              - inner: y
            o: two
            """,
            "conf/outer/inner/x.yaml": "i: x\n",
            "conf/outer/inner/y.yaml": "i: y\n",
        },
        "config",
        [[], ["outer=two"], ["outer/inner=y"]],
    ),
}


def _normalize(value):
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, float) and value != value:
        return "nan"
    return value


def _run_hydra(config_dir, config_name, overrides):
    from hydra import compose, initialize_config_dir
    from omegaconf import OmegaConf

    with initialize_config_dir(version_base=None, config_dir=str(config_dir)):
        cfg = compose(config_name=config_name, overrides=overrides)
        return _normalize(OmegaConf.to_container(cfg, resolve=True))


def _run_fast(config_dir, config_name, overrides):
    from hydra_fast import OmegaConf, compose, initialize_config_dir

    with initialize_config_dir(version_base=None, config_dir=str(config_dir)):
        cfg = compose(config_name=config_name, overrides=overrides)
        return _normalize(OmegaConf.to_container(cfg, resolve=True))


def _params():
    for name, (files, config_name, override_sets) in TREES.items():
        for overrides in override_sets:
            yield pytest.param(
                name,
                files,
                config_name,
                overrides,
                id=f"{name}-{'_'.join(overrides) or 'none'}",
            )


@requires_hydra
@pytest.mark.parametrize("name,files,config_name,overrides", list(_params()))
def test_matches_hydra(tmp_path, name, files, config_name, overrides: List[str]):
    root = write_tree(tmp_path / name, files)
    config_dir = root / "conf"

    try:
        expected = _run_hydra(config_dir, config_name, overrides)
        expected_error = None
    except Exception as exc:  # noqa: BLE001
        expected, expected_error = None, type(exc).__name__

    try:
        got = _run_fast(config_dir, config_name, overrides)
        got_error = None
    except Exception as exc:  # noqa: BLE001
        got, got_error = None, type(exc).__name__

    if expected_error or got_error:
        assert (
            expected_error and got_error
        ), f"hydra={expected_error or 'ok'} fast={got_error or 'ok'}"
        return
    assert expected == got


# ---------------------------------------------------------------------------
# cache behaviour
# ---------------------------------------------------------------------------
def test_file_is_parsed_once_across_compositions(tmp_path):
    """The headline promise: N compositions, one parse per file."""
    files, config_name, _ = TREES["basic"]
    config_dir = write_tree(tmp_path / "basic", files) / "conf"

    hydra_fast.clear_caches()
    for index in range(25):
        _run_fast(config_dir, config_name, [f"++run={index}"])

    stats = hydra_fast.cache_stats()
    # config.yaml and db/mysql.yaml -- each parsed exactly once
    assert stats["load_yaml_miss"] == 2, stats
    assert stats["load_yaml_hit"] >= 25, stats
    # the defaults list and the merge are each computed once
    assert stats["defaults_miss"] == 1, stats
    assert stats["compose_miss"] == 1, stats
    assert stats["defaults_hit"] == 24, stats
    assert stats["compose_hit"] == 24, stats


def test_edit_on_disk_invalidates(tmp_path):
    """Default stat validation must notice a config changing mid-process."""
    files, config_name, _ = TREES["basic"]
    config_dir = write_tree(tmp_path / "edit", files) / "conf"

    first = _run_fast(config_dir, config_name, [])
    assert first["db"]["port"] == 3306

    target = config_dir / "db" / "mysql.yaml"
    # bump mtime far enough that a coarse clock still sees a change
    target.write_text("driver: mysql\nport: 7777\nuser: root\n")
    import os

    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    second = _run_fast(config_dir, config_name, [])
    assert second["db"]["port"] == 7777


def test_validation_never_does_not_reread(tmp_path):
    files, config_name, _ = TREES["basic"]
    config_dir = write_tree(tmp_path / "never", files) / "conf"

    hydra_fast.set_validation("never")
    try:
        first = _run_fast(config_dir, config_name, [])
        assert first["db"]["port"] == 3306
        (config_dir / "db" / "mysql.yaml").write_text("driver: mysql\nport: 7777\n")
        second = _run_fast(config_dir, config_name, [])
        # deliberately stale: that is what "never" buys
        assert second["db"]["port"] == 3306
    finally:
        hydra_fast.set_validation("stat")
        hydra_fast.clear_caches()


def test_group_override_bypasses_compose_cache(tmp_path):
    """A group-selecting override must not be served a cached merge."""
    files, config_name, _ = TREES["basic"]
    config_dir = write_tree(tmp_path / "groups", files) / "conf"

    mysql = _run_fast(config_dir, config_name, ["db=mysql"])
    postgres = _run_fast(config_dir, config_name, ["db=postgres"])
    assert mysql["db"]["driver"] == "mysql"
    assert postgres["db"]["driver"] == "postgresql"


def test_value_overrides_do_not_leak_between_points(tmp_path):
    """Cached merges are copied, so one point cannot see another's overrides."""
    files, config_name, _ = TREES["basic"]
    config_dir = write_tree(tmp_path / "leak", files) / "conf"

    first = _run_fast(config_dir, config_name, ["++only_in_first=1"])
    second = _run_fast(config_dir, config_name, [])
    assert "only_in_first" in first
    assert "only_in_first" not in second


def test_config_store_change_invalidates_cache(tmp_path):
    """Structured configs are not file-backed, so stat() cannot catch a change.

    A generation counter on ConfigStore.store() covers that gap; without it a
    config re-registered mid-process would be served from the cached merge.
    """
    from dataclasses import dataclass

    from hydra_fast import OmegaConf, compose, initialize_config_dir
    from hydra_fast.core.config_store import ConfigStore

    conf = tmp_path / "store" / "conf"
    conf.mkdir(parents=True)
    # No undeclared keys: a schema-backed config rejects those (see
    # test_schema_rejects_undeclared_key), which is not what this test is about.
    (conf / "config.yaml").write_text("defaults:\n  - schema\n  - _self_\n")

    store = ConfigStore.instance()

    @dataclass
    class First:
        val: str = "first"

    store.store(name="schema", node=First(), provider="test")

    with initialize_config_dir(version_base=None, config_dir=str(conf)):
        before = OmegaConf.to_container(compose(config_name="config"), resolve=True)
        assert before["val"] == "first"

        @dataclass
        class Second:
            val: str = "second"
            extra: int = 7

        store.store(name="schema", node=Second(), provider="test")

        after = OmegaConf.to_container(compose(config_name="config"), resolve=True)
        assert after["val"] == "second"
        assert after["extra"] == 7


@requires_hydra
def test_schema_rejects_undeclared_key(tmp_path):
    """A ConfigStore schema makes the config struct-like, as in hydra."""
    from dataclasses import dataclass

    from hydra_fast import compose, initialize_config_dir
    from hydra_fast.core.config_store import ConfigStore
    from hydra_fast.errors import ConfigCompositionException

    conf = tmp_path / "strict" / "conf"
    conf.mkdir(parents=True)
    (conf / "config.yaml").write_text("defaults:\n  - strict_schema\n  - _self_\ntop: 1\n")

    @dataclass
    class Strict:
        val: str = "v"

    ConfigStore.instance().store(name="strict_schema", node=Strict(), provider="test")

    with (
        initialize_config_dir(version_base=None, config_dir=str(conf)),
        pytest.raises(ConfigCompositionException, match="top"),
    ):
        compose(config_name="config")


def test_return_hydra_config(tmp_path):
    files, config_name, _ = TREES["basic"]
    config_dir = write_tree(tmp_path / "hydranode", files) / "conf"

    from hydra_fast import compose, initialize_config_dir

    with initialize_config_dir(version_base=None, config_dir=str(config_dir)):
        without = compose(config_name=config_name, overrides=[])
        assert "hydra" not in without

        with_node = compose(config_name=config_name, overrides=[], return_hydra_config=True)
        assert "hydra" in with_node
        assert with_node.hydra.job.config_name == config_name
        assert with_node.hydra.runtime.choices["db"] == "mysql"


def test_missing_config_raises(tmp_path):
    files, _, _ = TREES["basic"]
    config_dir = write_tree(tmp_path / "missing", files) / "conf"

    from hydra_fast import compose, initialize_config_dir
    from hydra_fast.errors import MissingConfigException

    with (
        initialize_config_dir(version_base=None, config_dir=str(config_dir)),
        pytest.raises(MissingConfigException),
    ):
        compose(config_name="does_not_exist", overrides=[])


def test_sweep_override_requires_multirun(tmp_path):
    files, config_name, _ = TREES["basic"]
    config_dir = write_tree(tmp_path / "sweep", files) / "conf"

    from hydra_fast import compose, initialize_config_dir
    from hydra_fast.errors import ConfigCompositionException

    with (
        initialize_config_dir(version_base=None, config_dir=str(config_dir)),
        pytest.raises(ConfigCompositionException),
    ):
        compose(config_name=config_name, overrides=["db.port=1,2,3"])
