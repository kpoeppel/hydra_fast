"""Cache behaviour: correctness under concurrency, and bounded growth."""

from __future__ import annotations

import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor

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
            out = _compose(conf, [f"++run={index}", "db=postgres" if index % 2 else "db=mysql"])
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
