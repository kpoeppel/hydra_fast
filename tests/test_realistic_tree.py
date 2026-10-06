"""A realistic config tree, composed and compared against hydra.

``test_real_world.py`` covers a production tree, but that tree is private, so
those twelve tests skip everywhere except one machine. This is the same kind
of exercise against a tree that ships with the repository: 15 files across 8
directories, shaped after the private one without borrowing its naming.

What it deliberately includes, because each of these is where composition gets
interesting and a synthetic flat tree would miss it:

* a group nested two levels deep (``runner/cluster/large``)
* ``# @package`` headers, named and ``_global_``
* profiles that reshape the whole config and use ``override`` on groups the
  root already selected
* absolute group references (``/runner``) from inside a group file
* relative interpolations (``${.workers}``) and ones that reach the root
  (``${outputs}``) from inside a package
* ``${oc.env:VAR,default}``, which nearly every real tree leans on
* a custom resolver
* a ``_target_`` block, so instantiate is exercised from a composed tree
* a ``???`` a per-point override has to supply, as a sweep does
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

import realistic_targets
from conftest import requires_hydra

TREE = str(Path(__file__).resolve().parent / "realistic_config")

#: (label, overrides) -- the shapes a user actually composes.
CASES: List[tuple] = [
    ("defaults", []),
    ("smoke-profile", ["+profile=smoke"]),
    ("production-profile", ["+profile=production"]),
    ("staged-with-shard", ["+profile=staged", "++shard=3"]),
    ("nested-group", ["runner=cluster/large"]),
    ("nested-group-other", ["runner=local/process"]),
    ("group-plus-value", ["storage=object", "++storage.compression=gzip"]),
    ("schedule-with-resolver", ["schedule=nightly"]),
    ("notify-on", ["notify=webhook"]),
    ("profile-then-group", ["+profile=production", "runner=local/thread"]),
    ("deep-value-override", ["++runner.workers=99"]),
    ("add-new-key", ["++extra.nested.flag=true"]),
]


def _compose(engine: str, overrides: List[str]) -> Dict[str, Any]:
    if engine == "hydra-fast":
        from hydra_fast import OmegaConf, compose, initialize_config_dir
    else:
        from hydra import compose, initialize_config_dir
        from omegaconf import OmegaConf

    realistic_targets.register(OmegaConf)
    with initialize_config_dir(version_base=None, config_dir=TREE):
        cfg = compose(config_name="pipeline", overrides=overrides)
    return OmegaConf.to_container(cfg, resolve=True)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """`oc.env` reads the real environment, so pin what the tree looks up."""
    for name in ("PIPELINE_WORKSPACE", "PIPELINE_BUCKET", "PIPELINE_WEBHOOK"):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# it composes, and to the right thing
# ---------------------------------------------------------------------------
def test_defaults_compose():
    out = _compose("hydra-fast", [])
    assert out["name"] == "pipeline"
    assert out["label"] == "pipeline-0.1"
    assert out["runner"] == {"kind": "thread", "workers": 4, "queue_depth": 4}
    # `${outputs}` reached the root from inside the storage package
    assert out["storage"]["root"] == "/var/tmp/pipeline/pipeline-0.1/data"


def test_nested_group_two_levels_deep():
    out = _compose("hydra-fast", ["runner=cluster/large"])
    assert out["runner"]["partition"] == "long"
    assert out["runner"]["queue_depth"] == 256, "relative ${.workers} followed the option"


def test_global_profile_reshapes_several_groups():
    out = _compose("hydra-fast", ["+profile=production"])
    assert out["name"] == "production"
    assert (out["runner"]["kind"], out["storage"]["kind"]) == ("cluster", "object")
    assert out["schedule"]["kind"] == "cron"
    assert out["notify"]["enabled"] is True


def test_custom_resolver_runs():
    out = _compose("hydra-fast", ["schedule=nightly"])
    assert out["schedule"]["backoff_seconds"] == 60, "${multiply:2,30}"


def test_env_interpolation_uses_the_default_when_unset():
    assert _compose("hydra-fast", [])["workspace"] == "/var/tmp/pipeline"


def test_env_interpolation_picks_up_the_variable(monkeypatch):
    monkeypatch.setenv("PIPELINE_WORKSPACE", "/scratch")
    out = _compose("hydra-fast", [])
    assert out["workspace"] == "/scratch"
    assert out["outputs"] == "/scratch/pipeline-0.1"


def test_missing_value_must_be_supplied_per_point():
    """`???` survives `to_container` as the sentinel and raises on *access*.

    Checked against hydra: `to_container(resolve=True)` yields the literal
    `"???"` rather than raising, so a sweep can inspect an unfilled config;
    the error comes when something reads the value.
    """
    from hydra_fast import OmegaConf, compose, initialize_config_dir
    from hydra_fast.errors import MissingMandatoryValue

    realistic_targets.register(OmegaConf)
    with initialize_config_dir(version_base=None, config_dir=TREE):
        unfilled = compose(config_name="pipeline", overrides=["+profile=staged"])
    assert OmegaConf.to_container(unfilled, resolve=True)["shard"] == "???"
    assert OmegaConf.is_missing(unfilled, "shard")
    with pytest.raises(MissingMandatoryValue):
        unfilled.shard

    assert _compose("hydra-fast", ["+profile=staged", "++shard=7"])["shard"] == 7


def test_selecting_a_group_twice_without_override_is_an_error():
    """What the tree would do wrong if `base` dropped its `override`."""
    from hydra_fast.errors import ConfigCompositionException

    with pytest.raises(ConfigCompositionException, match="more than once"):
        _compose("hydra-fast", ["+profile=smoke", "+schedule=once"])


# ---------------------------------------------------------------------------
# and matches hydra, which is the point
# ---------------------------------------------------------------------------
@requires_hydra
@pytest.mark.parametrize("label,overrides", CASES, ids=[c[0] for c in CASES])
def test_matches_hydra(label, overrides):
    assert _compose("hydra-fast", list(overrides)) == _compose("hydra", list(overrides))


@requires_hydra
def test_errors_match_hydra():
    """Composition failures have to agree too, not just successes."""

    def outcome(engine: str, overrides: List[str]) -> Any:
        try:
            return ("ok", _compose(engine, overrides))
        except Exception as exc:  # noqa: BLE001
            return ("raise", type(exc).__name__, str(exc).splitlines()[0])

    for overrides in (
        ["+profile=smoke", "+schedule=once"],  # group selected twice
        ["runner=nope"],  # no such option
        ["+profile=nope"],  # no such profile
        ["nosuchgroup=x"],  # no such group
    ):
        assert outcome("hydra-fast", overrides) == outcome("hydra", overrides), overrides


# ---------------------------------------------------------------------------
# instantiate, from a composed tree
# ---------------------------------------------------------------------------
def test_instantiate_a_target_from_the_composed_tree():
    from hydra_fast import OmegaConf, compose, initialize_config_dir, instantiate

    realistic_targets.register(OmegaConf)
    with initialize_config_dir(version_base=None, config_dir=TREE):
        cfg = compose(config_name="pipeline", overrides=["+profile=production"])
    sink = instantiate(cfg.sink)
    assert isinstance(sink, realistic_targets.Sink)
    # the interpolations resolved before the target saw them
    assert sink.root == "s3://pipeline-dev/production-0.1"
    assert sink.compression == "zstd"


@requires_hydra
def test_instantiate_matches_hydra():
    def built(engine: str) -> Any:
        if engine == "hydra-fast":
            from hydra_fast import OmegaConf, compose, initialize_config_dir, instantiate
        else:
            from hydra import compose, initialize_config_dir
            from hydra.utils import instantiate
            from omegaconf import OmegaConf

        realistic_targets.register(OmegaConf)
        with initialize_config_dir(version_base=None, config_dir=TREE):
            cfg = compose(config_name="pipeline", overrides=["+profile=production"])
        return instantiate(cfg.sink)

    assert built("hydra-fast") == built("hydra")


# ---------------------------------------------------------------------------
# the thing the library exists for: the same tree, many points
# ---------------------------------------------------------------------------
def test_a_sweep_over_this_tree_parses_each_file_once():
    import hydra_fast
    from hydra_fast import OmegaConf, compose, initialize_config_dir

    realistic_targets.register(OmegaConf)
    hydra_fast.clear_caches()

    points = [
        ["+profile=staged", f"++shard={index}", f"++batch={index * 8}"] for index in range(40)
    ]
    results = []
    for overrides in points:
        with initialize_config_dir(version_base=None, config_dir=TREE):
            cfg = compose(config_name="pipeline", overrides=overrides)
        results.append(OmegaConf.to_container(cfg, resolve=True))

    assert [r["shard"] for r in results] == list(range(40)), "no leakage between points"
    assert [r["batch"] for r in results] == [i * 8 for i in range(40)]

    stats = hydra_fast.cache_stats()
    files = len(list(Path(TREE).rglob("*.yaml")))
    assert stats["load_yaml_miss"] <= files, (
        f"parsed {stats['load_yaml_miss']} times for {files} files; a sweep must "
        "not re-parse"
    )
    # 40 points differing only in value overrides share one merge
    assert stats["compose_hit"] >= 39, stats


# ---------------------------------------------------------------------------
# Scale. The tree above is realistic in *shape*; a production tree is also
# large (the one this is modelled on has 236 files and ~30 resolvers), and
# size is what the caching and the defaults-list walk actually have to cope
# with. Generated rather than committed, so it costs no repository weight.
# ---------------------------------------------------------------------------
def _large_tree(root: Path, groups: int = 12, options: int = 6, depth: int = 3) -> Path:
    """A tree with `groups` groups, nested `depth` levels, plus a deep chain."""
    root.mkdir(parents=True, exist_ok=True)
    names = [f"g{index:02d}" for index in range(groups)]

    for position, group in enumerate(names):
        # every third group is nested one level deeper, as real trees are
        nested = f"{group}/sub" if position % 3 == 0 else group
        for option in range(options):
            path = root / nested / f"o{option}.yaml"
            path.parent.mkdir(parents=True, exist_ok=True)
            package = nested.replace("/", ".")
            path.write_text(
                f"# @package {package}\n"
                f"kind: o{option}\n"
                f"index: {option}\n"
                # a relative reference and one reaching the root
                f"doubled: ${{multiply:${{.index}},2}}\n"
                f"tagged: ${{join_path:${{run_id}},{group},o{option}}}\n"
            )

    selections = "".join(
        f"  - {group}/sub: o0\n" if position % 3 == 0 else f"  - {group}: o0\n"
        for position, group in enumerate(names)
    )
    # a chain of configs each pulling in the next, to exercise depth
    for level in range(depth):
        # The last link has no `defaults:` key at all -- an empty one is an
        # error, in hydra and here alike (tests/test_defaults_list.py pins it).
        nxt = f"defaults:\n  - chain{level + 1}\n" if level + 1 < depth else ""
        (root / f"chain{level}.yaml").write_text(
            f"# @package _global_\n{nxt}level{level}: {level}\n"
        )
    (root / "big.yaml").write_text(
        "defaults:\n" + selections + "  - chain0\n  - _self_\n"
        "revision: '7'\nrun_id: ${pad:${revision},5}\n"
    )
    return root


@pytest.fixture(scope="module")
def large_tree(tmp_path_factory):
    return _large_tree(tmp_path_factory.mktemp("large"))


def _compose_at(engine: str, where: str, name: str, overrides: List[str]):
    if engine == "hydra-fast":
        from hydra_fast import OmegaConf, compose, initialize_config_dir
    else:
        from hydra import compose, initialize_config_dir
        from omegaconf import OmegaConf

    realistic_targets.register(OmegaConf)
    with initialize_config_dir(version_base=None, config_dir=where):
        cfg = compose(config_name=name, overrides=overrides)
    return OmegaConf.to_container(cfg, resolve=True)


LARGE_CASES = [
    [],
    ["g01=o3"],
    ["g00/sub=o2"],
    ["g01=o3", "g02=o4", "g04=o1"],
    ["++revision=9"],
    ["++g01.index=99"],
    ["~g05"],
]


def test_large_tree_composes(large_tree):
    out = _compose_at("hydra-fast", str(large_tree), "big", [])
    assert out["level0"] == 0 and out["level2"] == 2, "the chain composed"
    assert out["g00"]["sub"]["kind"] == "o0", "nested group landed in its package"
    assert out["g01"]["doubled"] == 0
    assert out["run_id"] == "00007"


@requires_hydra
@pytest.mark.parametrize(
    "overrides", LARGE_CASES, ids=["_".join(o) or "plain" for o in LARGE_CASES]
)
def test_large_tree_matches_hydra(large_tree, overrides):
    where = str(large_tree)
    assert _compose_at("hydra-fast", where, "big", list(overrides)) == _compose_at(
        "hydra", where, "big", list(overrides)
    )


def test_large_sweep_parses_each_file_once(large_tree):
    import hydra_fast

    hydra_fast.clear_caches()
    where = str(large_tree)
    for index in range(30):
        out = _compose_at(
            "hydra-fast", where, "big", [f"++revision={index}", f"g01=o{index % 6}"]
        )
        assert out["run_id"] == str(index).zfill(5)

    files = len(list(large_tree.rglob("*.yaml")))
    misses = hydra_fast.cache_stats()["load_yaml_miss"]
    assert misses <= files, f"{misses} parses for {files} files across 30 points"
