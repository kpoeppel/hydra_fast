# Changelog

Releases are published to PyPI by `.github/workflows/release.yml` when a
GitHub Release is published. To cut one: bump `version` in `pyproject.toml`,
add the section below, tag `vX.Y.Z`, push the tag, then publish the Release.
The workflow re-runs the full test suite against the tagged commit and refuses
to upload if the tag and the packaged version disagree.

## 0.1.0

First release.

A reimplementation of the config composition and interpolation core of
`hydra-core` 1.3 + `omegaconf` 2.3, built for sweeps: composing one config
tree hundreds of times.

- Process-wide caching: each YAML file is read and parsed once per process,
  rather than once per `compose()` call.
- Hand-written interpolation and override parsers that compile each distinct
  string to a Python closure, replacing the ANTLR parse-tree walk.
- Plain `dict`/`list` storage with `DictConfig` as a view, replacing
  OmegaConf's per-value `Node` object graph.
- Defaults-list and merged-config memoization keyed on the overrides that can
  actually change them.

Measured ~120x on a 75-point sweep over a real production config tree (23.0 s
→ 0.19 s, best-of-N); 60-130x across repeated runs on a shared machine.
Differentially tested against the real hydra and omegaconf: 1025 tests and
ten differential oracles, none of which needs anything outside the repository.

Compatibility work beyond the original surface:

- Structured-config **type enforcement and coercion**, so a quoted YAML value
  lands in a declared `int` field as an `int`. Validated on writes and merges,
  which costs nothing measurable (a sweep does ~1350 reads per 7 writes, and
  its merge is cached).
- `oc.dict.values` returns live references rather than resolved values.
- Exception **messages** match, including omegaconf's `full_key`/`object_type`
  trailer, and list indices render in brackets.
- YAML dumps are byte-identical to omegaconf's.
- `ValueNode` proxies for `cfg._get_node()` -- live, identity-stable, typed --
  plus a `_metadata` shim, built lazily so unused code paths cost nothing.
- Caches are thread-safe and bounded.
- Interpolation syntax is validated at `create()` time, as omegaconf does.
- `OmegaConf.is_struct`, `SCMode`, struct enforcement in merge and dotlists,
  readonly enforcement on in-place merge and list mutation.
- **ANTLR's syntax-error messages, reproduced without ANTLR.** All five shapes
  (`token recognition error`, `mismatched input`, `no viable alternative`,
  `extraneous input`, `missing X at`), with expected-token sets ordered as
  ANTLR orders them. Choosing the right shape meant modelling ANTLR's recovery
  (deletion probe, then insertion, then input mismatch) and its lazy lexer --
  a rule that stops early never scans the rest of the input, so it never
  rejects it. Measured at 744/744 accept/reject and 459/459 message text
  against real ANTLR over 62 malformed inputs x 12 grammar rules
  (`bench/oracle_errors.py`).
- Two grammar bugs this surfaced and fixed: `package`'s empty alternative was
  accepted unconditionally rather than gated on its FOLLOW set (so `key@=value`
  was rejected and `:` accepted), and an all-whitespace `dictKey` was accepted.
- `version_base` is a real setting (`hydra_fast.version`, with `setbase` /
  `getbase` / `base_at_least`) and gates the `_name_`-in-package deprecation
  warning, rather than being accepted and ignored.
- The shim's synthetic packages keep the real package's `__path__`, so an
  unshimmed submodule (`hydra._internal.core_plugins`) still imports from
  upstream instead of failing.

- **`instantiate` / `_target_`**, with all five protocol keys (`_target_`,
  `_args_`, `_recursive_`, `_convert_`, `_partial_`) plus `call`, `get_class`,
  `get_method`, `get_object` and `hydra.utils` through the shim. It costs
  nothing unless used: a tree carrying a `_target_` in every node composes
  within noise of one carrying none, because `_target_` is an ordinary string
  key until something asks for an object (`bench/bench_instantiate.py`).
- **The Defaults List algorithm audited against hydra** over 44 cases --
  `_self_` placement, `override` at depth, deletions, `optional`, package
  rebinding, interpolated group names. It found two message-level divergences
  that lost information: an in-file `override` that matched nothing suppressed
  hydra's actionable suggestion, and the missing-`_self_` warning had been
  dropped in the port.
- **The private omegaconf surface enumerated rather than "partial"**: the node
  layer matches on 174/174 probes and `omegaconf._utils` on 75/75. Fixing it
  found three real gaps -- containers lacked `_value()`/`_is_optional()`,
  `_get_node` was not identity-stable, and `ValueNode.__bool__` broke the
  `if cfg._get_node(k):` existence idiom for falsy values.
- `delattr` respects struct mode, which omegaconf's does not -- the one place
  the container API is deliberately stricter.
- Change detection covers more than edits: a file added to a group, removed
  from one, a new group directory, and a changed `defaults:` list are all
  picked up, because existence is cached against the parent directory's mtime.
- The handoff to an external typing layer (compoconf, hydra-zen, hydra-typing)
  is pinned: the composed dict must match hydra's in values *and* types.

Validated by running the upstream suites against it (`tests/test_upstream.py`):
hydra's own override-grammar suite (**508 cases, fully green**), omegaconf's
grammar corpus (131/131 through the public API), and omegaconf's
readonly/struct/oc_select suites verbatim. The package type-checks clean under
mypy and scores 10.00/10 under pylint, both enforced by pre-commit and CI.
See `docs/compatibility.md` for what remains out of scope.
