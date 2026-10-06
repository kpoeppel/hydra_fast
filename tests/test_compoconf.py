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
import datetime
import decimal
import enum
import pathlib
import uuid
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
    "backend/titan.yaml": (
        "# @package backend\nclass_name: TitanBackend\ndp: 8\ncompile: true\n"
    ),
}


@pytest.fixture
def config_dir(tmp_path):
    for name, body in TREE.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return str(tmp_path)


def _composed_dict(engine: str, config_dir: str, overrides: List[str]):
    """The handoff itself: compose with ``engine``, resolve to a plain dict."""
    if engine == "hydra-fast":
        from hydra_fast import OmegaConf, compose, initialize_config_dir
    else:
        from hydra import compose, initialize_config_dir
        from omegaconf import OmegaConf

    with initialize_config_dir(version_base=None, config_dir=config_dir):
        cfg = compose(config_name="config", overrides=overrides)
    return OmegaConf.to_container(cfg, resolve=True)


def _pipeline_with(engine: str, config_dir: str, overrides: List[str], config_class):
    """Compose with ``engine``, then hand the plain dict to compoconf."""
    return parse_config(config_class, _composed_dict(engine, config_dir, overrides))


def _pipeline(engine: str, config_dir: str, overrides: List[str]):
    return _pipeline_with(engine, config_dir, overrides, RootConfig)


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


# ---------------------------------------------------------------------------
# compoconf 0.3.1 added Enum/Path/datetime/Decimal/UUID fields. Those arrive
# across the boundary as the strings YAML produces, so the composed dict has
# to carry them unchanged for the far side to parse them.
# ---------------------------------------------------------------------------
_HAS_STDLIB_SCALARS = hasattr(compoconf, "parse_file")  # landed together in 0.3.x

requires_compoconf_scalars = pytest.mark.skipif(
    not _HAS_STDLIB_SCALARS, reason="needs compoconf 0.3.1+ (stdlib scalar support)"
)


class Mode(enum.Enum):
    FAST = "fast"
    SLOW = "slow"


@dataclasses.dataclass
class Scalars(ConfigInterface):
    mode: Mode = Mode.SLOW
    where: pathlib.Path = pathlib.Path(".")
    when: datetime.datetime = datetime.datetime(2020, 1, 1)
    amount: decimal.Decimal = decimal.Decimal("0")
    ident: Optional[uuid.UUID] = None


SCALARS_YAML = (
    "mode: fast\n"
    "where: /tmp/out\n"
    "when: '2024-03-01T12:30:00'\n"
    "amount: '1.25'\n"
    "ident: '12345678-1234-5678-1234-567812345678'\n"
)


@pytest.fixture
def scalars_dir(tmp_path):
    (tmp_path / "config.yaml").write_text(SCALARS_YAML)
    return str(tmp_path)


@requires_compoconf_scalars
def test_stdlib_scalars_survive_the_boundary(scalars_dir):
    parsed = _pipeline_with("hydra-fast", scalars_dir, [], Scalars)
    assert parsed.mode is Mode.FAST
    assert parsed.where == pathlib.Path("/tmp/out")
    assert parsed.when == datetime.datetime(2024, 3, 1, 12, 30)
    assert parsed.amount == decimal.Decimal("1.25")
    assert parsed.ident == uuid.UUID("12345678-1234-5678-1234-567812345678")


@requires_compoconf_scalars
@requires_hydra
def test_stdlib_scalars_match_real_hydra(scalars_dir):
    expected = _pipeline_with("hydra", scalars_dir, [], Scalars)
    got = _pipeline_with("hydra-fast", scalars_dir, [], Scalars)
    assert asdict(got) == asdict(expected)


@requires_compoconf_scalars
def test_strict_types_rejects_what_hydra_quotes(tmp_path):
    """Not a hydra-fast quirk -- real Hydra produces the same string.

    Quoting is how Hydra's grammar forces a string, so a strict pass and a
    Hydra override layer pull in opposite directions. Pinned because the
    answer is counter-intuitive and documented in docs/compatibility.md.
    """
    (tmp_path / "config.yaml").write_text("steps: '10'\n")

    @dataclasses.dataclass
    class Counted(ConfigInterface):
        steps: int = 1

    data = _composed_dict("hydra-fast", str(tmp_path), [])
    assert data["steps"] == "10", "a quoted scalar must stay a string"
    assert parse_config(Counted, dict(data)).steps == 10  # coerced
    with pytest.raises(Exception, match="(?i)expected int"):
        parse_config(Counted, dict(data), strict_types=True)


# ---------------------------------------------------------------------------
# Typing only ever sees *resolved* values, and resolution preserves the type
# of a whole-value interpolation. So an interpolation into a typed int field
# stays an int, and only a literal quoted scalar arrives as a string -- which
# is the single case `strict_types=True` turns into an error.
# ---------------------------------------------------------------------------
RESOLUTION_CASES = [
    ("plain", "steps: 10\n", 10, int),
    ("interpolation", "steps: ${base}\n", 10, int),
    ("interpolation-quoted", "steps: '${base}'\n", 10, int),
    ("interpolation-concat", "steps: v${base}\n", "v10", str),
    ("literal-quoted", "steps: '10'\n", "10", str),
]


@pytest.mark.parametrize(
    "label,body,expected,expected_type",
    RESOLUTION_CASES,
    ids=[c[0] for c in RESOLUTION_CASES],
)
def test_resolution_preserves_type_for_whole_value_interpolations(
    tmp_path, label, body, expected, expected_type
):
    (tmp_path / "config.yaml").write_text("base: 10\n" + body)
    data = _composed_dict("hydra-fast", str(tmp_path), [])
    assert data["steps"] == expected
    assert type(data["steps"]) is expected_type


@requires_hydra
@pytest.mark.parametrize(
    "label,body,expected,expected_type",
    RESOLUTION_CASES,
    ids=[c[0] for c in RESOLUTION_CASES],
)
def test_resolution_types_match_real_hydra(tmp_path, label, body, expected, expected_type):
    (tmp_path / "config.yaml").write_text("base: 10\n" + body)
    want = _composed_dict("hydra", str(tmp_path), [])["steps"]
    got = _composed_dict("hydra-fast", str(tmp_path), [])["steps"]
    assert (got, type(got)) == (want, type(want))
