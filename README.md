# hydra-fast

[![CI](https://github.com/kpoeppel/hydra_fast/actions/workflows/ci.yml/badge.svg)](https://github.com/kpoeppel/hydra_fast/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue)](https://pypi.org/project/hydra-fast/)
[![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/kpoeppel/hydra_fast/blob/main/LICENSE)

Hydra-compatible config composition, built for the case Hydra is slowest at:
composing the same config tree hundreds of times to build a parameter sweep.

```python
from hydra_fast import compose, initialize_config_dir

with initialize_config_dir(config_dir="/path/to/conf", version_base=None):
    cfg = compose(config_name="config", overrides=["db=postgres", "db.port=5433"])
```

Same config file syntax, same `${...}` interpolation grammar, same override
grammar, same `OmegaConf`/`compose` API. Roughly 100x faster on a sweep.

It reimplements the composition and interpolation core — including
`instantiate`/`_target_` — but not job launching or sweeper plugins. Within
that scope it is validated against the
real packages — including running **hydra's own override-grammar suite** and
**omegaconf's own grammar corpus** against it.
[docs/compatibility.md](https://github.com/kpoeppel/hydra_fast/blob/main/docs/compatibility.md) is explicit about the boundary.

## The numbers

Composing one config tree once per sweep point, then fully resolving it —
which is what building a sweep does. `hydra 1.3.2 + omegaconf 2.3.0`,
CPython 3.12, Linux, best-of-N wall clock.

**A 75-point sweep over a real production config tree** ([OpenEuroLLM
`oellm-autoexp`](https://github.com/OpenEuroLLM/oellm-autoexp): 239 YAML
files, deep group nesting, ~30 custom resolvers):

| | total | per point |
| --- | --- | --- |
| hydra + omegaconf | **23.0 s** | 306 ms |
| hydra-fast | **0.19 s** | 2.5 ms |

Seconds to milliseconds — **~120x**.

Per point on the same tree, by configuration:

| | per point | vs stock |
| --- | --- | --- |
| hydra + omegaconf | 306 ms | 1x |
| hydra + omegaconf, with a hand-written cache layer monkeypatched in [^1] | 71 ms | 4x |
| **hydra-fast** | **2.5 ms** | **~120x** |

The default `validation="stat"` — which re-checks the filesystem on every cache
lookup, so a config edited mid-process is picked up — costs nothing measurable
here: best-of-5 over this sweep gives 2.72 ms/point with validation and
2.78 ms/point without. The validation work is amortized to a handful of
`stat()` calls per composition (see
[docs/architecture.md](https://github.com/kpoeppel/hydra_fast/blob/main/docs/architecture.md)), so there is no safety-for-speed
trade to make. Keep the default.

[^1]: `oellm-autoexp` ships seven monkeypatched cache layers over Hydra
    (`hydra_staged_sweep/config/cache.py`). hydra-fast is ~25x faster than
    that, because caching alone cannot get past Hydra's object model — see
    [docs/architecture.md](https://github.com/kpoeppel/hydra_fast/blob/main/docs/architecture.md).

Across repeated runs on a shared machine the speedup landed between **60x and
130x**, with the low end being runs where the box was busy. Treat ~100x as the
expected figure and measure on your own tree; the ratio depends on how much of
your config is interpolation (where hydra-fast wins most) versus custom
resolvers doing real work (where it cannot help).

Reproduce without any private config tree:

```console
$ python bench/bench_synthetic.py --points 100 --repeat 3
tree: 25 yaml files, 400 keys in base, 6 groups x 4 options; 100 sweep points
equivalence on first 3 points: OK
  hydra+omegaconf  total=  46762.6 ms   per_point= 467.63 ms
  hydra-fast       total=    368.6 ms   per_point=   3.69 ms

speedup: 126.9x
yaml parses: 13 (vs 161 reads requested)
```

The benchmark verifies both implementations produce identical output before
reporting a time.

## Why it is faster

Three things, in rough order of impact. [docs/architecture.md](https://github.com/kpoeppel/hydra_fast/blob/main/docs/architecture.md)
has the detail.

**Each file is read and parsed once per process.** Stock Hydra re-reads and
re-parses every YAML file in the tree on *every* `compose()` call — about 150
parses per call on the tree above, so ~11,000 parses for a 75-point sweep.
hydra-fast parses each file once: 9 parses for the whole sweep. This was the
single biggest cost, about half of stock Hydra's time.

**Interpolations compile to closures.** OmegaConf re-walks an ANTLR parse tree
every time it resolves `${a.b}`. hydra-fast has a hand-written parser that
compiles each distinct string *once* into a Python closure, cached on the
string. A sweep typically has a few dozen distinct interpolation strings and
resolves them thousands of times. (Having no parse tree costs nothing
functionally: parse-once-resolve-many works the same way, and
`analyze_interpolation()` recovers static introspection — see
[docs/architecture.md](https://github.com/kpoeppel/hydra_fast/blob/main/docs/architecture.md#what-replaces-the-parse-tree).)

**Configs are plain dicts.** OmegaConf wraps every value in a `Node` object
with its own metadata and parent pointer. A 600-key config is 600+ objects to
allocate, link, and later deep-copy. In hydra-fast the storage *is* a
`dict`/`list` tree and `DictConfig` is a two-slot view over it, so merging is a
dict walk and copying is a data copy.

On top of that, the defaults list and the merged config are memoized on exactly
what can change them — notably *not* on the `++key=value` overrides a sweep
varies per point, which cannot affect either.

## Correctness

Speed is only interesting if the answer is the same. Every result is checked
against the real Hydra and OmegaConf:

```console
$ pytest
853 passed, 3 skipped
```

856 tests. What skips depends on what is installed: three need
compoconf 0.3.1+, twelve need the private `oellm-autoexp` tree, and the
upstream-suite tests need the source checkouts fetched (see *Running the
differential tests*). Set `HYDRA_FAST_REQUIRE_UPSTREAM=1` to turn the last
group's skips into errors, as CI does.

- **Upstream suites run against hydra-fast** (`tests/test_upstream.py`):
  hydra's own `test_overrides_parser.py` — **508 cases, fully green**;
  omegaconf's own grammar corpus driven through the public API — **131/131**;
  omegaconf's `test_readonly.py`, `test_struct.py`, `test_oc_select.py` run
  verbatim — **all green**.
- **70 structured-config typing cases** against an oracle captured from
  omegaconf — coercion, rejection, `Optional`, `Enum`, `List[X]`, `Dict[K,V]`,
  nested dataclasses, and through a full composition.
- **~60 interpolation cases** resolved through both OmegaConf and hydra-fast and
  compared — node references, relative references (`${..a}`), nested keys
  (`${a.${k}}`), escapes, quoting, every argument form, all built-in resolvers.
- **Exception types and messages** compared case by case, including omegaconf's
  `full_key`/`object_type` trailer — and, for malformed input, ANTLR's own
  phrasing reproduced without ANTLR: **744/744** inputs accepted or rejected
  identically and **455/459** messages character-identical across 62 malformed
  inputs x 12 grammar rules (`bench/oracle_errors.py`).
- **66 override cases** parsed by both Hydra's ANTLR parser and hydra-fast, with
  the full parsed shape compared — type, key, package, value type, value.
- **33 composition cases** over 12 config trees covering `_self_` ordering,
  `@package` headers, `override`/`optional` keywords, deletions, option lists,
  interpolated group names, nested groups.
- **12 composition cases** over the real `oellm-autoexp` tree with all its
  custom resolvers registered.
- Cache-correctness tests: that a file edited mid-process is picked up, that
  group overrides are not served a cached merge, and that one sweep point's
  overrides cannot leak into another's.
- **Packaging and handoff tests**: that every shipped config file is tracked by
  git (not just present on disk), and that the plain dict handed to an external
  typing layer such as [compoconf](https://pypi.org/project/compoconf/) matches
  hydra's in both values and types.

What this still does not prove: omegaconf's `test_grammar.py` asserts against
ANTLR parse trees, which hydra-fast has none of by design, and several
omegaconf suites import private internals that are only partially shimmed. Both
boundaries are itemised in
[docs/compatibility.md](https://github.com/kpoeppel/hydra_fast/blob/main/docs/compatibility.md) — treat it as the contract.

### Contributing

```console
$ pip install -e ".[test,attrs,compoconf]" pre-commit
$ pre-commit install
```

`pre-commit run --all-files` runs what CI runs: ruff (lint and format), mypy,
bandit, yamllint, and the usual whitespace/YAML checks. ruff replaces the
black/flake8/isort/pylint stack — the configuration lives in `pyproject.toml`
at 95 columns.

Two files are exempt from both the linter and the formatter:
`_internal/defaults_list.py` and `_internal/default_element.py` are ported from
hydra essentially verbatim so they stay diffable against upstream, and
reformatting them would defeat that.

### Running the differential tests

```console
$ pip install -e ".[test,attrs,compoconf]"
$ python .github/scripts/fetch_upstream.py   # upstream suites + grammar corpus
$ pytest
```

The second step matters. Upstream's own test suites and grammar corpus ship in
their sdists, not their wheels, so they are downloaded into a gitignored
`.upstream/`. Without them the strongest checks here **skip**, and a green run
means much less. Set `HYDRA_FAST_REQUIRE_UPSTREAM=1` to turn those skips into
errors — CI always does.

`bench/_sources.py` is the one place that decides where those trees live
(`HYDRA_SRC`, `OMEGACONF_SRC`, `OELLM_AUTOEXP`, else `.upstream/`) and which
versions are expected.

Two skips are expected and harmless: `tests/test_real_world.py` needs the
`oellm-autoexp` config tree, which is not public.

## Install

```console
pip install hydra-fast
```

Only dependency is PyYAML. Install it built against libyaml (the usual binary
wheels are) and hydra-fast uses the C scanner automatically.

## Using it

### As itself

```python
import hydra_fast
from hydra_fast import OmegaConf, compose, initialize_config_dir

with initialize_config_dir(config_dir="/abs/path/to/conf", version_base=None):
    cfg = compose(config_name="config", overrides=["group=option", "++key=value"])
print(OmegaConf.to_yaml(cfg, resolve=True))
```

`initialize`, `initialize_config_dir`, `initialize_config_module`, `compose`,
`main`, and the `OmegaConf` static API all match their Hydra/OmegaConf
counterparts.

### Under a library written against omegaconf

If a library you depend on does `from omegaconf import OmegaConf` to register
its resolvers, install the shim **before importing it**:

```python
import hydra_fast.compat.omegaconf_shim as shim
shim.install()

import that_library   # its OmegaConf is now hydra-fast's
```

`install()` is always explicit — importing `hydra_fast` never redirects
anything on its own. Only the public surface is shimmed; code reaching into
`omegaconf._utils` or `omegaconf.basecontainer` needs to target hydra-fast
directly.

### Cache control

```python
import hydra_fast

hydra_fast.set_validation("never")  # trust the first read; fastest
hydra_fast.clear_caches()           # drop everything
hydra_fast.cache_stats()            # hit/miss counters per layer
hydra_fast.get_validation()         # "stat" or "never"
```

`"stat"` is the default: every cache lookup re-`stat()`s the files it depends
on, so editing a config on disk mid-process invalidates exactly the entries
that depend on it. No explicit reload call is needed.

That covers more than edits. A file *added* to a group, a file *removed* from
one, a whole new group directory, and a changed `defaults:` list are all
picked up, because existence is cached against the parent directory's mtime --
which changes exactly when an entry is added or removed. `tests/test_cache.py`
pins each case.

`"never"` skips all of that and trusts the first read for the life of the
process, so a config edited afterwards is *not* seen; `clear_caches()` is the
only way to pick it up. `get_validation()` reports the current mode, which
matters if something else set it.

On the benchmarks above `"never"` is **not** measurably faster — the
validation is already amortized down to a few `stat()` calls per composition.
It exists for workloads with much larger config trees, where the per-file
checks could still show up. Measure before switching; the default is safe.

## Compatibility

Target is **hydra 1.3 / omegaconf 2.3** — the widely deployed pair.
[docs/compatibility.md](https://github.com/kpoeppel/hydra_fast/blob/main/docs/compatibility.md) lists what is covered, the few
deliberate deviations, and what is out of scope (job launching, sweeper
plugins, `instantiate`).

The short version: config **composition, parsing and syntax** are covered and
differentially tested. hydra-fast composes configs; it does not run jobs.

## Repository layout

```
src/hydra_fast/     the package
tests/              differential and unit tests
bench/              benchmarks and standalone differential checks
docs/               architecture.md, compatibility.md
ATTRIBUTION/        upstream licenses and what came from where
hydra/, omegaconf/  upstream source checkouts, for reference only
```

The `hydra/` and `omegaconf/` directories at the root are upstream checkouts
kept for reading and diffing. They are **not** part of the distribution, and
they are not what the tests compare against — the tests use the *installed*
`hydra-core` / `omegaconf`. Note that with the repo root on `sys.path` those
directories shadow the installed packages as empty namespace packages;
`tests/conftest.py` drops the root from `sys.path` for exactly that reason.

## Licensing

MIT. hydra-fast is a derivative work of Hydra (MIT) and OmegaConf (BSD
3-Clause); the Defaults List algorithm in particular is ported from Hydra
rather than re-derived, because that algorithm *is* the composition spec. See
[ATTRIBUTION/](https://github.com/kpoeppel/hydra_fast/tree/main/ATTRIBUTION) for exactly what came from where.
