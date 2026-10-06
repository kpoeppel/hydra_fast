"""hydra-fast as the composition half of a compoconf pipeline.

compoconf types configs *after* composition, from a plain dict:

    cfg  = compose(...)                              # hydra-fast composes
    data = OmegaConf.to_container(cfg, resolve=True)  # -> plain dict
    root = parse_config(RootConfig, data)             # compoconf types it

So the whole contract between the two is "``to_container(resolve=True)``
returns the same plain dict hydra would have returned". These tests pin that,
including the registry-based polymorphism that makes a config group select a
*type* rather than just values -- the case where a composition bug would show
up as a wrong dataclass rather than a wrong number.

compoconf is not a dependency of hydra-fast; this skips without it.
"""

from __future__ import annotations

import dataclasses
from typing import Dict, List, Literal, Optional

import pytest

from conftest import requires_hydra

compoconf = pytest.importorskip("compoconf", reason="compoconf not installed")

from compoconf import (  # noqa: E402
    ConfigInterface,
    RegistrableConfigInterface,
    asdict,
    parse_config,
    register,
    register_interface,
)


# ---------------------------------------------------------------------------
# a config shaped like a real one: registry polymorphism, Literal, nesting
# ---------------------------------------------------------------------------
@register_interface
class Backend(RegistrableConfigInterface):
    pass


@dataclasses.dataclass
class MegatronConfig(ConfigInterface):
    tp: int = 1
    pp: int = 1
    recompute: Literal["none", "selective", "full"] = "none"


@register
class MegatronBackend(Backend):
    config_class = MegatronConfig

    def __init__(self, config):
        self.config = config


@dataclasses.dataclass
class TitanConfig(ConfigInterface):
    dp: int = 1
    compile: bool = False


@register
class TitanBackend(Backend):
    config_class = TitanConfig

    def __init__(self, config):
        self.config = config


@dataclasses.dataclass
class Optim(ConfigInterface):
    lr: float = 1e-4
    betas: List[float] = dataclasses.field(default_factory=lambda: [0.9, 0.95])


@dataclasses.dataclass
class RootConfig(ConfigInterface):
    name: str = "run"
    steps: int = 1000
    optim: Optim = dataclasses.field(default_factory=Optim)
    backend: Backend.cfgtype = dataclasses.field(default_factory=MegatronConfig)
    tags: Dict[str, str] = dataclasses.field(default_factory=dict)
    note: Optional[str] = None


TREE = {
    "config.yaml": (
        "defaults:\n"
        "  - backend: megatron\n"
        "  - _self_\n"
        "name: probe\n"
        "steps: 2000\n"
        "optim:\n"
        "  lr: 3e-4\n"
        "  betas: [0.9, 0.99]\n"
        "tags:\n"
        "  stage: ${name}-stable\n"
        "note: null\n"
    ),
    "backend/megatron.yaml": (
        "# @package backend\n"
        "class_name: MegatronBackend\n"
        "tp: 2\n"
        "pp: 4\n"
        "recompute: selective\n"
    ),
    "backend/titan.yaml": ("# @package backend\nclass_name: TitanBackend\ndp: 8\ncompile: true\n"),
}


@pytest.fixture
def config_dir(tmp_path):
    for name, body in TREE.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return str(tmp_path)


def _pipeline(engine: str, config_dir: str, overrides: List[str]):
    """Compose with ``engine``, then hand the plain dict to compoconf."""
    if engine == "hydra-fast":
        from hydra_fast import OmegaConf, compose, initialize_config_dir
    else:
        from hydra import compose, initialize_config_dir
        from omegaconf import OmegaConf

    with initialize_config_dir(version_base=None, config_dir=config_dir):
        cfg = compose(config_name="config", overrides=overrides)
    return parse_config(RootConfig, OmegaConf.to_container(cfg, resolve=True))


OVERRIDE_CASES = [
    [],
    ["backend=titan"],
    ["steps=5000"],
    ["optim.lr=0.001"],
    ["++tags.extra=hello"],
    ["backend=titan", "++backend.dp=16"],
    ["name=other"],  # the ${name} interpolation in tags.stage must follow
    ["backend.recompute=full"],
]


# ---------------------------------------------------------------------------
# the pipeline works at all
# ---------------------------------------------------------------------------
def test_pipeline_produces_a_typed_config(config_dir):
    parsed = _pipeline("hydra-fast", config_dir, [])
    assert isinstance(parsed, RootConfig)
    assert parsed.steps == 2000
    assert parsed.optim.lr == pytest.approx(3e-4)
    assert parsed.tags == {"stage": "probe-stable"}, "interpolation did not resolve"
    assert parsed.note is None


def test_config_group_selects_a_type_not_just_values(config_dir):
    """The registry case: the group picks which dataclass is built."""
    assert isinstance(_pipeline("hydra-fast", config_dir, []).backend, MegatronConfig)

    titan = _pipeline("hydra-fast", config_dir, ["backend=titan"]).backend
    assert isinstance(titan, TitanConfig)
    assert titan.dp == 8 and titan.compile is True
    # and the registry can still build the object from it
    assert isinstance(titan.instantiate(Backend), TitanBackend)


def test_literal_is_enforced_by_compoconf(config_dir):
    """Typing is compoconf's job; hydra-fast must not pre-empt or block it."""
    with pytest.raises(Exception, match="(?i)literal|invalid|allowed"):
        _pipeline("hydra-fast", config_dir, ["backend.recompute=nonsense"])


def test_coercion_is_compoconfs_to_do(config_dir):
    """A YAML string reaching an int field is coerced by `parse_config`."""
    parsed = _pipeline("hydra-fast", config_dir, ["++steps='7'"])
    assert parsed.steps == 7 and type(parsed.steps) is int


# ---------------------------------------------------------------------------
# and produces exactly what hydra would have
# ---------------------------------------------------------------------------
@requires_hydra
@pytest.mark.parametrize(
    "overrides", OVERRIDE_CASES, ids=[" ".join(o) or "none" for o in OVERRIDE_CASES]
)
def test_matches_the_same_pipeline_on_real_hydra(config_dir, overrides):
    expected = _pipeline("hydra", config_dir, list(overrides))
    got = _pipeline("hydra-fast", config_dir, list(overrides))
    assert type(got) is type(expected)
    assert type(got.backend) is type(expected.backend)
    assert asdict(got) == asdict(expected)


@requires_hydra
def test_the_handoff_dict_is_identical(config_dir):
    """The entire contract between the two halves, asserted directly.

    Values *and* types: compoconf dispatches on `isinstance`, so a `ListConfig`
    where hydra produced a `list` would change its behaviour even though the
    values compare equal.
    """
    from hydra import compose as h_compose
    from hydra import initialize_config_dir as h_init
    from omegaconf import OmegaConf as HOmegaConf

    from hydra_fast import OmegaConf as FOmegaConf
    from hydra_fast import compose as f_compose
    from hydra_fast import initialize_config_dir as f_init

    with h_init(version_base=None, config_dir=config_dir):
        expected = HOmegaConf.to_container(
            h_compose(config_name="config", overrides=["backend=titan"]), resolve=True
        )
    with f_init(version_base=None, config_dir=config_dir):
        got = FOmegaConf.to_container(
            f_compose(config_name="config", overrides=["backend=titan"]), resolve=True
        )

    def type_map(data, prefix=""):
        out = {}
        for key, value in data.items():
            out[prefix + key] = type(value).__name__
            if isinstance(value, dict):
                out.update(type_map(value, f"{prefix}{key}."))
        return out

    assert got == expected
    assert type_map(got) == type_map(expected)
