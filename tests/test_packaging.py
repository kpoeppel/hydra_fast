"""The distribution ships what it needs to, and git tracks what it ships.

hydra-fast carries its own copy of Hydra's builtin ``hydra/*`` config group
(``conf/hydra/env/default.yaml`` and friends). Those are data files, so
nothing imports them and no other test notices if they go missing -- but
``compose(return_hydra_config=True)`` stops working entirely.

They went missing once: ``.gitignore`` held an unanchored ``hydra/`` for the
reference checkout at the repository root, which also matched
``src/hydra_fast/conf/hydra/``. The files were on disk, so local runs and
locally built wheels were fine; anything built from a fresh clone was not.
Hence the git-level assertion below.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONF = ROOT / "src" / "hydra_fast" / "conf"

#: Config groups Hydra's own defaults list names, so a missing one is a hard
#: failure rather than a quietly absent key.
REQUIRED_GROUPS = [
    "env",
    "help",
    "hydra_help",
    "hydra_logging",
    "job_logging",
    "output",
]

#: What each group's `default.yaml` must actually contribute to the composed
#: `hydra` node. Keyed by group so a failure names the file to look at. These
#: are effects rather than key names because `output/default.yaml` carries a
#: `# @package hydra` header and merges in at the top level.
GROUP_EFFECTS = {
    "env": lambda node: "env" in node,
    # `app_name` is `${hydra.job.name}`, which is MISSING until a job runs,
    # so check the key rather than resolving it.
    "help": lambda node: "app_name" in node.help,
    "hydra_help": lambda node: "hydra_help" in node,
    "hydra_logging": lambda node: node.hydra_logging.version == 1,
    "job_logging": lambda node: node.job_logging.version == 1,
    "output": lambda node: node.output_subdir == ".hydra",
}


def test_builtin_hydra_configs_are_present_on_disk():
    for group in REQUIRED_GROUPS:
        assert (CONF / "hydra" / group / "default.yaml").is_file(), (
            f"conf/hydra/{group}/default.yaml is missing"
        )


@pytest.mark.parametrize("group", REQUIRED_GROUPS)
def test_builtin_hydra_configs_compose(group):
    """The end the data files exist for."""
    from hydra_fast import compose, initialize_config_dir

    with initialize_config_dir(version_base=None, config_dir=str(CONF)):
        cfg = compose(config_name=None, overrides=[], return_hydra_config=True)
    assert GROUP_EFFECTS[group](cfg.hydra), (
        f"conf/hydra/{group}/default.yaml did not reach the composed hydra node"
    )


@pytest.mark.skipif(not (ROOT / ".git").exists(), reason="not a git checkout")
def test_git_tracks_every_shipped_config():
    """On disk is not enough -- a fresh clone must get them too."""
    tracked = subprocess.run(
        ["git", "ls-files", "src/hydra_fast/conf"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    on_disk = sorted(
        str(path.relative_to(ROOT)) for path in CONF.rglob("*.yaml")
    )
    untracked = sorted(set(on_disk) - set(tracked))
    assert not untracked, (
        "these config files ship in the package but git does not track them, "
        f"so a fresh clone would not have them: {untracked}"
    )


@pytest.mark.skipif(not (ROOT / ".git").exists(), reason="not a git checkout")
def test_git_ignores_the_reference_checkouts_only_at_the_root():
    """`/hydra/` must stay anchored, or it eats the builtin config tree."""
    probe = subprocess.run(
        ["git", "check-ignore", "src/hydra_fast/conf/hydra/env/default.yaml"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert probe.returncode != 0, (
        "src/hydra_fast/conf/hydra/ is gitignored; the reference-checkout "
        "patterns need a leading slash to stay anchored to the repo root"
    )
