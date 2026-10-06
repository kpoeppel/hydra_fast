"""Cache behaviour: correctness under concurrency, and bounded growth."""

from __future__ import annotations

import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import hydra_fast
from hydra_fast import _cache


def _tree(root):
    conf = root / "conf"
    (conf / "db").mkdir(parents=True)
    (conf / "config.yaml").write_text(
        textwrap.dedent("""
        defaults:
          - db: mysql
          - _self_
        app: demo
        msg: "driver=${db.driver}"
        """).lstrip()
    )
    (conf / "db" / "mysql.yaml").write_text("driver: mysql\nport: 3306\n")
    (conf / "db" / "postgres.yaml").write_text("driver: postgresql\nport: 5432\n")
    return conf


def _compose(conf, overrides):
    from hydra_fast import OmegaConf, compose, initialize_config_dir

    with initialize_config_dir(version_base=None, config_dir=str(conf)):
        return OmegaConf.to_container(
            compose(config_name="config", overrides=overrides), resolve=True
        )


# ---------------------------------------------------------------------------
# bounded growth
# ---------------------------------------------------------------------------
def test_bounded_cache_evicts():
    cache = _cache.new_cache("test_evict", maxsize=16)
    for index in range(100):
        cache[index] = index
    assert len(cache) <= 16, "cache grew past its bound"
    # the most recent insert survives
    assert cache[99] == 99


def test_bounded_cache_overwrite_does_not_evict():
    cache = _cache.new_cache("test_overwrite", maxsize=4)
    for _ in range(50):
        cache["same"] = 1
    assert len(cache) == 1


def test_memoize_is_bounded():
    calls = []

    @_cache.memoize("test_memo", maxsize=8)
    def square(value):
        calls.append(value)
        return value * value

    for index in range(64):
        square(index)
    assert len(_cache._REGISTRY["test_memo"]) <= 8


def test_set_max_entries():
    cache = _cache.new_cache("test_resize", maxsize=1000)
    _cache.set_max_entries(4, "test_resize")
    for index in range(50):
        cache[index] = index
    assert len(cache) <= 4


def test_every_cache_is_bounded():
    """A new cache that forgets its bound would reintroduce the leak."""
    for name, cache in _cache._REGISTRY.items():
        assert isinstance(cache, _cache._BoundedCache), name
        assert cache.maxsize >= 1, name


def test_composition_caches_survive_eviction_pressure(tmp_path):
    """Evicting a composition cache must cost speed, never correctness."""
    conf = _tree(tmp_path)
    # The composition caches register when the loader module is first
    # imported, which composing once guarantees.
    _compose(conf, [])
    _cache.set_max_entries(1, "compose")
    _cache.set_max_entries(1, "defaults")
    try:
        for index in range(10):
            out = _compose(
                conf, [f"++run={index}", "db=postgres" if index % 2 else "db=mysql"]
            )
            expected = "postgresql" if index % 2 else "mysql"
            assert out["db"]["driver"] == expected
            assert out["run"] == index
    finally:
        _cache.set_max_entries(512, "compose")
        _cache.set_max_entries(512, "defaults")


# ---------------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------------
def test_concurrent_composition_is_correct(tmp_path):
    """Many threads composing at once must each get their own right answer."""
    conf = _tree(tmp_path)
    _compose(conf, [])  # warm, so threads race on hits rather than misses

    def work(index):
        group = "postgres" if index % 2 else "mysql"
        out = _compose(conf, [f"db={group}", f"++run={index}"])
        return index, group, out

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, range(80)))

    for index, group, out in results:
        expected = "postgresql" if group == "postgres" else "mysql"
        assert out["db"]["driver"] == expected, f"point {index} got {out['db']}"
        assert out["run"] == index


def test_concurrent_clear_does_not_break_readers(tmp_path):
    """clear_all() racing live compositions may cost hits, never correctness."""
    conf = _tree(tmp_path)
    _compose(conf, [])
    stop = threading.Event()
    errors = []

    def clearer():
        while not stop.is_set():
            hydra_fast.clear_caches()

    def composer(index):
        try:
            out = _compose(conf, [f"++run={index}"])
            assert out["run"] == index
            assert out["db"]["driver"] == "mysql"
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=clearer, daemon=True)
    thread.start()
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(composer, range(60)))
    finally:
        stop.set()
        thread.join(timeout=5)

    assert not errors, f"{len(errors)} failures, first: {errors[0]!r}"


def test_concurrent_file_reads_agree(tmp_path):
    """Racing readers of one file must all see the same content."""
    path = tmp_path / "f.yaml"
    path.write_text("a: 1\nb: two\n")
    hydra_fast.clear_caches()

    from hydra_fast._yaml import load_yaml_file

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: load_yaml_file(str(path)), range(100)))
    assert all(result == {"a": 1, "b": "two"} for result in results)


def test_concurrent_interpolation_resolution():
    """The grammar memo is shared; concurrent compiles must not cross wires."""
    from hydra_fast import OmegaConf

    hydra_fast.clear_caches()

    def work(index):
        cfg = OmegaConf.create({"n": index, "msg": "value is ${n}", "ref": "${n}"})
        out = OmegaConf.to_container(cfg, resolve=True)
        assert out["msg"] == f"value is {index}"
        assert out["ref"] == index
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(work, range(200)))


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------
def test_stats_report_sizes_and_evictions():
    cache = _cache.new_cache("test_stats", maxsize=4)
    for index in range(20):
        cache[index] = index
    stats = _cache.stats()
    assert stats["test_stats_size"] <= 4
    assert stats.get("test_stats_evicted", 0) >= 1


def test_clear_all_empties_everything(tmp_path):
    conf = _tree(tmp_path)
    _compose(conf, [])
    assert _cache.stats()["load_yaml_size"] > 0
    hydra_fast.clear_caches()
    assert _cache.stats()["load_yaml_size"] == 0


# ---------------------------------------------------------------------------
# What the default `stat` validation actually notices.
#
# Editing a file is the easy case and is covered in test_compose.py. Adding
# and removing files is the interesting one: existence is cached against the
# *parent directory's* mtime (one stat per directory per composition instead
# of ~12,000 per-file stats), which is only correct because a directory's
# mtime changes exactly when an entry is added or removed from it. These pin
# that reasoning.
# ---------------------------------------------------------------------------
_DETECT_TREE = {
    "config.yaml": "defaults:\n  - db: pg\n  - _self_\nname: base\n",
    "db/pg.yaml": "# @package db\nhost: pg-host\n",
    "db/mysql.yaml": "# @package db\nhost: my-host\n",
}


def _detect_tree(root):
    for name, body in _DETECT_TREE.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return root


def _detect_compose(root, overrides=()):
    """The composed container, or the exception name if it failed."""
    from hydra_fast import OmegaConf, compose, initialize_config_dir

    with initialize_config_dir(version_base=None, config_dir=str(root)):
        try:
            cfg = compose(config_name="config", overrides=list(overrides))
        except Exception as exc:  # noqa: BLE001 -- "it stopped existing" is a result
            return type(exc).__name__
    return OmegaConf.to_container(cfg, resolve=True)


def test_adding_an_option_to_a_group_is_noticed(tmp_path):
    root = _detect_tree(tmp_path / "add")
    assert _detect_compose(root, ["db=extra"]) == "MissingConfigException"

    (root / "db" / "extra.yaml").write_text("# @package db\nhost: extra-host\n")
    assert _detect_compose(root, ["db=extra"])["db"]["host"] == "extra-host"


def test_removing_an_option_from_a_group_is_noticed(tmp_path):
    root = _detect_tree(tmp_path / "remove")
    assert _detect_compose(root, ["db=mysql"])["db"]["host"] == "my-host"

    (root / "db" / "mysql.yaml").unlink()
    assert _detect_compose(root, ["db=mysql"]) == "MissingConfigException"


def test_adding_a_whole_group_directory_is_noticed(tmp_path):
    """A new *directory* changes what an override means, not just its value.

    `+extra=one` with no `extra/` group adds a plain key holding the string
    "one" -- that is hydra's behaviour too, checked against it. Once the
    directory exists the same override selects a group instead, so this pins
    that a directory appearing mid-process is noticed, which is the case the
    directory-mtime caching could plausibly miss.
    """
    root = _detect_tree(tmp_path / "newgroup")
    assert _detect_compose(root, ["+extra=one"])["extra"] == "one"

    (root / "extra").mkdir()
    (root / "extra" / "one.yaml").write_text("# @package extra\nv: 1\n")
    assert _detect_compose(root, ["+extra=one"])["extra"] == {"v": 1}


def test_changing_a_defaults_list_is_noticed(tmp_path):
    root = _detect_tree(tmp_path / "defaults")
    assert _detect_compose(root)["db"]["host"] == "pg-host"

    (root / "config.yaml").write_text("defaults:\n  - db: mysql\n  - _self_\nname: base\n")
    assert _detect_compose(root)["db"]["host"] == "my-host"


def test_validation_never_goes_stale_and_clear_caches_recovers(tmp_path):
    """The documented trade: `never` trusts the first read for the process.

    `clear_caches()` is the escape hatch, which is the only way to pick up a
    change in that mode.
    """
    import hydra_fast

    root = _detect_tree(tmp_path / "never")
    before = _cache.get_validation()
    try:
        _cache.clear_all()
        hydra_fast.set_validation("never")
        assert _detect_compose(root)["db"]["host"] == "pg-host"

        (root / "db" / "pg.yaml").write_text("# @package db\nhost: EDITED\n")
        assert _detect_compose(root)["db"]["host"] == "pg-host", "`never` must not re-read"

        hydra_fast.clear_caches()
        assert _detect_compose(root)["db"]["host"] == "EDITED"
    finally:
        hydra_fast.set_validation(before)
        _cache.clear_all()


def test_validation_mode_round_trips_and_rejects_typos():
    """A typo must not silently disable change detection."""
    import hydra_fast

    before = hydra_fast.get_validation()
    try:
        hydra_fast.set_validation("never")
        assert hydra_fast.get_validation() == "never"
        hydra_fast.set_validation("stat")
        assert hydra_fast.get_validation() == "stat"
        with pytest.raises(ValueError, match="unknown validation mode"):
            hydra_fast.set_validation("statt")
        assert hydra_fast.get_validation() == "stat"
    finally:
        hydra_fast.set_validation(before)
