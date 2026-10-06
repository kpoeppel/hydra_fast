"""Structured-config type enforcement, compared case-by-case with omegaconf.

The case list lives in ``bench/oracle_typing.py`` so it can also be run
standalone against the real package to (re)capture the semantics.
"""

from __future__ import annotations

import dataclasses
from typing import List

import pytest

import oracle_typing as oracle  # noqa: E402  (path set up by conftest)
from conftest import requires_omegaconf
from hydra_fast import OmegaConf
from hydra_fast.errors import ConfigCompositionException, ValidationError


@requires_omegaconf
@pytest.mark.parametrize("label,op", oracle.CASES, ids=[label for label, _ in oracle.CASES])
def test_matches_omegaconf(label, op):
    import omegaconf

    import hydra_fast

    expected = oracle.describe(omegaconf, op)
    got = oracle.describe(hydra_fast, op)
    assert expected == got


# ---------------------------------------------------------------------------
# the behaviour that motivated the whole layer
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class DB:
    driver: str = "???"
    port: int = 0
    tags: List[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class AppCfg:
    db: DB = dataclasses.field(default_factory=DB)
    workers: int = 1


def test_merge_coerces_yaml_strings_to_declared_types():
    """Without this, a quoted YAML value leaves a str in an int field."""
    merged = OmegaConf.merge(
        OmegaConf.structured(AppCfg),
        OmegaConf.create({"workers": "4", "db": {"port": "5432"}}),
    )
    out = OmegaConf.to_container(merged)
    assert out["workers"] == 4 and type(out["workers"]) is int
    assert out["db"]["port"] == 5432 and type(out["db"]["port"]) is int


def test_merge_rejects_uncoercible():
    with pytest.raises(ValidationError):
        OmegaConf.merge(OmegaConf.structured(AppCfg), OmegaConf.create({"workers": "xyz"}))


def test_interpolation_bypasses_validation():
    """Composition writes `${...}` into typed fields constantly."""
    cfg = OmegaConf.structured(AppCfg)
    cfg.workers = "${db.port}"
    assert OmegaConf.to_container(cfg, resolve=False)["workers"] == "${db.port}"
    cfg.db.port = 8
    assert cfg.workers == 8


def test_missing_bypasses_validation():
    cfg = OmegaConf.structured(AppCfg)
    cfg.workers = "???"
    assert OmegaConf.is_missing(cfg, "workers")


def test_list_element_type_enforced():
    cfg = OmegaConf.structured(DB)
    cfg.tags.append(5)  # coerced to "5" for List[str]
    assert list(cfg.tags) == ["5"]


def test_dict_value_type_enforced():
    @dataclasses.dataclass
    class Holder:
        counts: dict = dataclasses.field(default_factory=dict)

    # an unparameterized dict is unconstrained
    cfg = OmegaConf.create(Holder)
    cfg.counts = {"a": "anything"}
    assert cfg.counts["a"] == "anything"


def test_schemaless_config_is_unconstrained():
    """No schema means no type map and no checking -- the common case."""
    cfg = OmegaConf.create({"n": 1})
    cfg.n = "now a string"
    assert cfg.n == "now a string"
    assert cfg._hf_root.types is None


def test_type_map_survives_deepcopy():
    import copy

    clone = copy.deepcopy(OmegaConf.structured(AppCfg))
    with pytest.raises(ValidationError):
        clone.workers = "xyz"


def test_type_map_survives_subtree_view():
    cfg = OmegaConf.structured(AppCfg)
    with pytest.raises(ValidationError):
        cfg.db.port = "xyz"


def test_type_map_rerooted_on_subtree_copy():
    import copy

    sub = copy.deepcopy(OmegaConf.structured(AppCfg).db)
    with pytest.raises(ValidationError):
        sub.port = "xyz"


# ---------------------------------------------------------------------------
# through a full composition
# ---------------------------------------------------------------------------
def _schema_tree(tmp_path):
    from hydra_fast.core.config_store import ConfigStore

    ConfigStore.instance().store(name="app_schema", node=AppCfg, provider="test")
    conf = tmp_path / "conf"
    (conf / "db").mkdir(parents=True)
    (conf / "config.yaml").write_text(
        "defaults:\n  - app_schema\n  - db: pg\n  - _self_\nworkers: '4'\n"
    )
    (conf / "db" / "pg.yaml").write_text(
        "# @package db\ndriver: postgresql\nport: '5432'\ntags: [a, b]\n"
    )
    return conf


def test_composition_coerces_through_schema(tmp_path):
    from hydra_fast import compose, initialize_config_dir

    conf = _schema_tree(tmp_path)
    with initialize_config_dir(version_base=None, config_dir=str(conf)):
        out = OmegaConf.to_container(compose(config_name="config"), resolve=True)
    assert out["workers"] == 4 and type(out["workers"]) is int
    assert out["db"]["port"] == 5432 and type(out["db"]["port"]) is int
    assert out["db"]["tags"] == ["a", "b"]


@pytest.mark.parametrize("override", ["++db.port=notanint", "++workers=xyz"])
def test_composition_rejects_bad_override(tmp_path, override):
    from hydra_fast import compose, initialize_config_dir

    conf = _schema_tree(tmp_path)
    with (
        initialize_config_dir(version_base=None, config_dir=str(conf)),
        pytest.raises(ConfigCompositionException),
    ):
        compose(config_name="config", overrides=[override])


def test_composition_type_map_survives_compose_cache(tmp_path):
    """The cached merge must carry the schema, or point 2 loses validation."""
    from hydra_fast import compose, initialize_config_dir

    conf = _schema_tree(tmp_path)
    with initialize_config_dir(version_base=None, config_dir=str(conf)):
        first = OmegaConf.to_container(compose(config_name="config"), resolve=True)
        assert type(first["workers"]) is int
        # second composition is served from the compose cache
        second = OmegaConf.to_container(
            compose(config_name="config", overrides=["++workers='7'"]), resolve=True
        )
        assert second["workers"] == 7 and type(second["workers"]) is int
