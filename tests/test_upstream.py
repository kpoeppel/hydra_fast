"""Differential tests driven by the upstream projects' own test data.

Two harnesses, both skipped unless an upstream source checkout is available:

``omegaconf grammar corpus``
    OmegaConf's ``tests/test_grammar.py`` holds ~350 ``(id, input, expected)``
    interpolation cases. Its *tests* assert against ANTLR parse trees, which
    hydra-fast has none of by design, so they cannot run directly -- but the
    corpus is data, and every input can be driven through the public API of
    both implementations and compared.

``hydra override grammar suite``
    Runs verbatim through the compat shim, since it drives the override parser
    through ``OverridesParser`` rather than through parse trees.

The checkouts are located by :mod:`bench._sources` -- an env var, or a
gitignored ``.upstream/`` directory. See the README section "Running the
differential tests" for the one-liner that populates it.

**These skip when the checkouts are absent, and that is dangerous**: they are
the strongest checks here, and a green run without them means much less. CI
sets ``HYDRA_FAST_REQUIRE_UPSTREAM=1``, which turns absence into an error.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from conftest import requires_hydra, requires_omegaconf

BENCH = Path(__file__).resolve().parent.parent / "bench"
sys.path.insert(0, str(BENCH))

import _sources  # noqa: E402
import oracle_grammar_corpus as corpus  # noqa: E402


# ---------------------------------------------------------------------------
# omegaconf's grammar corpus, through the public API
# ---------------------------------------------------------------------------
def _cases():
    config_cases, single_cases = corpus.load_corpus()
    out = []
    for case_id, text in config_cases:
        out.append(pytest.param("configValue", text, id=f"configValue-{case_id}"))
    for case_id, text in single_cases:
        if "}" in text:
            # A literal `}` closes the probe interpolation early; not
            # expressible through the resolver-argument form.
            continue
        out.append(pytest.param("singleElement", text, id=f"singleElement-{case_id}"))
    return out


CASES = _cases()
if not CASES:
    _sources.require(None, "omegaconf (grammar corpus)", "OMEGACONF_SRC")


@requires_omegaconf
@pytest.mark.skipif(not CASES, reason=_sources.missing_reason("omegaconf", "OMEGACONF_SRC"))
@pytest.mark.parametrize("rule,text", CASES)
def test_grammar_corpus_matches_omegaconf(rule, text):
    import omegaconf

    import hydra_fast

    corpus.register(omegaconf)
    corpus.register(hydra_fast)
    runner = corpus.run_config_value if rule == "configValue" else corpus.run_single_element

    expected = corpus.describe(omegaconf, runner, text)
    got = corpus.describe(hydra_fast, runner, text)
    assert expected == got


# ---------------------------------------------------------------------------
# hydra's override grammar suite, run verbatim through the shim
# ---------------------------------------------------------------------------
HYDRA_SRC = _sources.require(_sources.hydra_src(), "hydra", "HYDRA_SRC")

#: Cases allowed to fail in hydra's override suite. Empty: the suite runs
#: fully green against hydra-fast, including every assertion on ANTLR's
#: message text for malformed input (see grammar/antlr_errors.py). Kept as a
#: named set so a deliberate exemption is a visible, reviewable change rather
#: than an edit to the assertion.
ANTLR_MESSAGE_CASES: set = set()


# omegaconf suites that run through the shim and must stay fully green.
# Everything not listed either imports omegaconf-private internals (so it
# cannot be collected) or asserts against ANTLR parse trees; see
# docs/compatibility.md.
OMEGACONF_GREEN_SUITES = [
    "tests/test_struct.py",
    "tests/test_readonly.py",
    "tests/interpolation/built_in_resolvers/test_oc_select.py",
    "tests/examples/test_postponed_annotations.py",
]


OMEGACONF_SRC = _sources.require(_sources.omegaconf_src(), "omegaconf", "OMEGACONF_SRC")


def _install_shim_conftest(root: Path) -> None:
    (root / "conftest.py").write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(BENCH.parent / 'src')!r})\n"
        "import hydra_fast.compat.omegaconf_shim as shim\n"
        "shim.install()\n"
    )


@requires_omegaconf
@pytest.mark.skipif(
    OMEGACONF_SRC is None, reason=_sources.missing_reason("omegaconf", "OMEGACONF_SRC")
)
@pytest.mark.parametrize("suite", OMEGACONF_GREEN_SUITES)
def test_omegaconf_suite_is_green(suite):
    """Run an omegaconf suite verbatim against hydra-fast; require all green.

    These are the suites whose assertions are about *behaviour* rather than
    omegaconf's internals, so any failure here is a real divergence.
    """
    if not (OMEGACONF_SRC / suite).is_file():
        pytest.skip(f"{suite} not in this omegaconf checkout")
    _install_shim_conftest(OMEGACONF_SRC)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", suite, "-q", "-p", "no:cacheprovider", "--no-header"],
        check=False,
        cwd=str(OMEGACONF_SRC),
        capture_output=True,
        text=True,
    )
    if "ImportError while importing test module" in proc.stdout:
        pytest.skip(f"{suite} is not importable under this pytest")
    summary = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    assert (
        "failed" not in summary and "error" not in summary
    ), f"{suite}: {summary}\n{proc.stdout[-2500:]}"
    assert "passed" in summary, proc.stdout[-2000:]


@requires_hydra
@pytest.mark.skipif(HYDRA_SRC is None, reason=_sources.missing_reason("hydra", "HYDRA_SRC"))
def test_hydra_override_grammar_suite():
    """Run hydra's own override-parser suite against hydra-fast.

    Requires it fully green, and that 500+ cases are actually collected -- so
    a regression anywhere in the suite shows up here rather than going
    unnoticed behind a shrinking pass count.
    """
    (HYDRA_SRC / "conftest.py").write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(BENCH.parent / 'src')!r})\n"
        "import hydra_fast.compat.omegaconf_shim as shim\n"
        "shim.install()\n"
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_overrides_parser.py",
            "-q",
            "-p",
            "no:cacheprovider",
            "--no-header",
            "--continue-on-collection-errors",
        ],
        check=False,
        cwd=str(HYDRA_SRC),
        capture_output=True,
        text=True,
    )
    output = proc.stdout

    # hydra 1.3.2's test file imports `_pytest.python_api.RaisesContext`, a
    # pytest private that moved in pytest 8.4. That is an incompatibility
    # between the upstream test file and the local pytest, not something about
    # hydra-fast -- hence a skip rather than a failure, with the version
    # ceiling in the `test` extra there to avoid it.
    #
    # Under HYDRA_FAST_REQUIRE_UPSTREAM the skip is an error instead: this is
    # the strongest check in the project, and a pytest bump must not be able
    # to drop it while leaving CI green.
    if "ImportError while importing test module" in output:
        message = (
            "hydra's override suite is not importable under "
            f"pytest {pytest.__version__} (it imports a pytest private; see the "
            f"`test` extra's version ceiling):\n{output[-600:]}"
        )
        if _sources.REQUIRE:
            raise AssertionError(message)
        pytest.skip(message)

    failed = {
        line.split("::", 1)[1].split(" ")[0]
        for line in output.splitlines()
        if line.startswith(("FAILED", "ERROR")) and "::" in line
    }
    unexpected = sorted(failed - ANTLR_MESSAGE_CASES)
    assert not unexpected, (
        f"{len(unexpected)} unexpected failures in hydra's override suite:\n"
        + "\n".join(unexpected)
        + f"\n\ntail:\n{output[-1500:]}"
    )

    # Guard against the suite silently collecting nothing.
    summary = output.strip().splitlines()[-1]
    assert "passed" in summary, output[-2000:]
    passed = max(
        (int(part) for part in summary.replace(",", " ").split() if part.isdigit()),
        default=0,
    )
    assert passed >= 500, f"expected 500+ passing upstream cases, got {summary!r}"
