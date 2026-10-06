# Compatibility

Target: **hydra 1.3 / omegaconf 2.3**, the widely deployed pair. Everything
below marked "differentially tested" is compared against those packages in
`tests/`.

hydra-fast covers **config composition, parsing and interpolation**. It does
not run jobs.

## Is it 1:1?

Not for every last internal, but much closer than it was. The surface that
configs actually use -- composition, interpolation, overrides, merge, the
`OmegaConf` API, structured-config typing, error types *and* messages -- is
differentially tested against the real packages and matches.

What that rests on:

| Check | Result |
| --- | --- |
| hydra's own `test_overrides_parser.py`, run verbatim through the shim | **508 pass, 0 fail** |
| omegaconf's grammar corpus (its own ~350 interpolation cases), driven through the public API | **131/131** |
| omegaconf's `test_readonly.py` + `test_struct.py`, run verbatim | **38/38** |
| omegaconf's `test_oc_select.py`, run verbatim | **11/11** |
| structured-config typing oracle (captured from omegaconf) | **70/70** |
| syntax-error oracle: 62 malformed inputs x 12 grammar rules, vs real ANTLR | **744/744** accept/reject, **455/459** message text |
| hydra-fast's own suite | **754 pass** (742 without the private `oellm-autoexp` tree) |

All of that runs in CI via `tests/test_upstream.py`, which fails if an upstream
case regresses.

Two honest limits remain:

1. **Parse trees do not exist**, so omegaconf's `test_grammar.py` -- which
   asserts against ANTLR `ParserRuleContext` objects -- cannot run. Its corpus
   is covered through the public API instead (row 2 above). The two things a
   tree is actually *used* for are both available: see
   [docs/architecture.md](architecture.md#what-replaces-the-parse-tree).
2. **Deep private internals are partial.** `omegaconf._utils` and the node
   layer are shimmed for the names libraries actually import (see *Internals*
   below), but `omegaconf.base`, `omegaconf.basecontainer`, `omegaconf._impl`
   and the rest are not. Those are private in omegaconf and have no stable
   contract.

## Covered and differentially tested

### Config file syntax

| Feature | Notes |
| --- | --- |
| `defaults:` lists | strings, `group: option`, `group: null` |
| `_self_` | at any position; ordering respected |
| `group@package: option` | including `@_global_`, `@_here_`, `@pkg.sub` |
| `# @package` headers | including `_global_` |
| `optional group: option` | missing group is skipped |
| `override group: option` | |
| `~group` deletion | via overrides |
| option lists | `group: [a, b]` |
| interpolated group names | `impl/${platform}: fast` |
| nested config groups | groups inside group options |
| `???` (MISSING) | raises `MissingMandatoryValue` on access |
| `hydra.searchpath` | from the primary config or the command line |
| YAML typing | `2.5e-4` → float, `2024-01-02` → str, matching omegaconf's resolver set |

### Interpolation syntax

`${a.b}`, `${a[0]}`, `${a[b]}`, `${[a].b}`, relative `${.a}` / `${..a}` /
`${...a}`, nested keys `${a.${k}}`, `${${ref}}`, chained interpolations,
string concatenation `x${a}y`, escapes `\${...}` and backslash runs, quoted
values (single and double, with escaped quotes).

Resolver calls: `${ns.fn:args}`, nested resolver names `${ns.${n}:x}`, and all
argument forms — ints, floats, bools, `null`, unquoted strings, quoted strings,
lists, dicts, nested interpolations, escaped commas, empty arguments.

Built-in resolvers: `oc.env`, `oc.select`, `oc.dict.keys`, `oc.dict.values`,
`oc.create`, `oc.decode`, `oc.deprecated`.

Custom resolvers via `OmegaConf.register_new_resolver(...)`, including the
`_parent_` / `_node_` / `_root_` special parameters and `use_cache=True`
(keyed on argument spellings, as omegaconf does).

### Override syntax

`key=value`, `+key=value`, `++key=value`, `~key`, `~key=value`,
`group=option`, `group@pkg=option`, lists `key=[a,b]`, dicts `key={a:1}`,
quoted values, escapes, interpolations passed through as strings.

Sweep syntax parses to the same objects Hydra produces: `key=a,b,c`,
`choice()`, `range()`, `interval()`, `glob()`, `tag()`, `sort()`, `shuffle()`,
and the `int()`/`float()`/`str()`/`bool()`/`json_str()` casts. As in Hydra, a
sweep override raises unless the run is a multirun.

### OmegaConf API

`create`, `structured`, `load`, `save`, `merge`, `unsafe_merge`,
`to_container` (including `enum_to_str` and all three `SCMode` values),
`to_object`, `to_yaml`, `resolve`, `select`, `update`,
`masked_copy`, `from_dotlist`, `from_cli`, `missing_keys`, `get_type`,
`set_struct`, `set_readonly`, `is_readonly`, `is_config`, `is_dict`, `is_list`,
`is_missing`, `is_interpolation`, `is_none`, `register_new_resolver`,
`has_resolver`, `clear_resolver(s)`, `get_cache`, `set_cache`, `clear_cache`.

`DictConfig` / `ListConfig` mapping and sequence protocols, `open_dict`,
`read_write`, `flag_override`, `SI`, `II`, `MISSING`, and the full exception
hierarchy under its original names.

Structured configs: dataclasses (and attrs, if installed) as schemas to merge
YAML onto, with defaults, nesting and `MISSING` preserved.

### Hydra API

`compose`, `initialize`, `initialize_config_dir`, `initialize_config_module`,
`main`, `ConfigStore`, the `hydra.*` config node (with
`return_hydra_config=True`), `hydra.runtime.choices`, `hydra.overrides`.

`version_base` is a real setting, not a swallowed argument: `hydra_fast.version`
provides `setbase` / `getbase` / `base_at_least`, the `initialize*` entry points
declare it, and it gates the deprecation warnings that depend on it (currently
the `_name_`-in-package warning for `version_base` below 1.2). Composition
semantics are hydra 1.3's at every level -- the setting changes which warnings
fire, not how configs compose, so `version_base=None` resolves to 1.3.

## Deliberate deviations

Two, both about hydra 1.4-dev rather than the 1.3 target.

### Group shorthand (`db: variants/mysql`)

hydra 1.4-dev rewrites this into group `db/variants`, option `mysql`, which
moves the config's package from `db` to `db.variants`. hydra 1.3 leaves the
slash in the value and keeps the package at `db`.

**hydra-fast matches 1.3.** Marked with `NOTE:` at both sites
(`_internal/config_source.py`, `_internal/default_element.py`).

### Bracket indexing in override keys (`a.b[0]=1`)

hydra 1.3 rejects these. hydra 1.4-dev accepts them, and so does hydra-fast —
a deliberate superset, tested in `test_value_path_superset`.

### Structured-config typing

**Implemented and differentially tested** (70/70 oracle cases). A declared type
is enforced on every write and every merge, and values are *coerced*, which is
the part that matters: without it a quoted YAML value leaves a `str` in an
`int` field.

```python
merge(structured(Cfg), create({"workers": "4"}))   # -> 4, an int
setattr(cfg, "workers", "xyz")                      # -> ValidationError
cfg.tags.append(5)                                  # List[str] -> "5"
```

Covered: `int`/`float`/`str`/`bool`/`Enum`/`Optional[X]`/`List[X]`/`Dict[K, V]`,
nested dataclasses, `Any`, `???`, and interpolation strings (which bypass
validation, since their type is only knowable once resolved). A `Dict[K, V]`
field stays open to new keys even in struct mode; a dataclass field does not.

Validation lives on writes, not reads. A sweep does ~1350 reads per 7 writes
and its merge is cached, so this costs nothing measurable -- 2.72 ms/point with
it, same without.

#### Using an external typing layer instead

This layer exists for drop-in fidelity with omegaconf. It is *not* the only
way to get typed configs, and hydra-fast deliberately does not try to be one:
libraries like [compoconf](https://pypi.org/project/compoconf/),
[hydra-zen](https://pypi.org/project/hydra-zen/) and
[hydra-typing](https://pypi.org/project/hydra-typing/) all type a config
*after* composition, turning the composed result into real dataclass
instances. The seam is a single line:

```python
cfg  = compose(config_name="config", overrides=overrides)
data = OmegaConf.to_container(cfg, resolve=True)   # plain dict -- the handoff
root = parse_config(RootConfig, data)              # compoconf types it
```

hydra-fast owns the first two lines and nothing after them. Two consequences
worth knowing:

- Pick one typing layer. hydra-fast's structured-config typing and an external
  one occupy the same slot, so a config typed by compoconf should not also be
  registered as a `ConfigStore` schema -- you would be validating twice,
  against two type systems, on the hot path.
- The handoff is much cheaper here. hydra-fast already stores plain
  `dict`/`list`, so `to_container` is a copy rather than a node-graph walk:
  3.6 ms versus 46.5 ms over 60 sweep points in the measurement below.

`tests/test_compoconf.py` pins the contract -- the composed dict must match
hydra's in both values *and* types, since a typing layer dispatches on
`isinstance` and would behave differently given a `ListConfig` where hydra
produced a `list`.

### Error messages

Exception types and messages both match, including omegaconf's trailer::

    Key 'nope' not in 'Schema'
        full_key: nope
        object_type=Schema

`exc.full_key` and `exc.object_type` are set as attributes too, and list
indices render in brackets (`l[0]`).

**Malformed input** reproduces ANTLR's messages too, without ANTLR:

    token recognition error at: 'X'
    mismatched input 'X' expecting {A, B, C}
    no viable alternative at input 'X'
    extraneous input 'X' expecting Y
    missing Y at 'X'

`grammar/antlr_errors.py` builds the five shapes; the parser supplies the
expected set, ordered by each token's declaration index in the `.g4` as ANTLR
orders it, and renders bare-literal lexer rules as literals (`':'`, not
`COLON`). Picking *which* shape required modelling ANTLR's recovery -- the
single-token deletion probe, then insertion, then input mismatch -- and its
lazy lexer, since a rule that stops early never scans (and so never rejects)
the rest of the input. `bench/oracle_errors.py` measures it against real
ANTLR: 744/744 inputs accepted-or-rejected identically, 455/459 messages
character-identical.

The four that differ share one cause. Hydra parses to a tree and raises
*semantic* errors while visiting it, so a malformed fragment fails at parse
time before any function is evaluated. hydra-fast evaluates during the parse,
so where the parse would *also* have failed it reports the evaluation error
rather than the parse error:

    OverridesParser().parse_rule("choice()", "simpleChoiceSweep")
    # hydra:      mismatched input '<EOF>' expecting COMMA
    # hydra-fast: ValueError while evaluating 'choice()': empty choice is not legal

Both reject; only the phrasing differs, and only for fragment rules parsed in
isolation. Whenever the parse succeeds, behaviour is identical.

### YAML dump formatting

Byte-identical to omegaconf, including its quoting of strings that would
otherwise read back as a bool/int/float (`'n': 1`, `num: '42'`). Tested both
ways: byte equality against omegaconf, and round-trip fidelity for ambiguous
keys (`n`, `y`, `on`, `no`).

### Internals: partially reproduced

These are private in omegaconf, but libraries do reach for them, so the ones
that get imported in practice are provided:

| | status |
| --- | --- |
| `cfg._get_node(k)` | live, identity-stable `ValueNode` proxy; writes through |
| `AnyNode`, `IntegerNode`, `FloatNode`, `StringNode`, `BooleanNode`, `BytesNode`, `EnumNode` | provided; the class is picked from the declared type |
| `node._value()`, `_set_value()`, `_is_missing()`, `_is_interpolation()`, `_is_none()`, `_get_full_key()`, `_key()`, `_get_parent()`, `_get_flag()`, `_set_flag()` | provided |
| `cfg._metadata` (`object_type`, `key`, `flags`, `ref_type`, …) | provided |
| `omegaconf._utils`: `is_structured_config`, `type_str`, `get_yaml_loader`, `nullcontext` | provided |
| `hydra.core.override_parser.overrides_parser.OverridesParser`, `create_functions` | provided, including `parse_rule(text, rule)` |
| `hydra._internal.grammar.functions.Functions`, `.utils.escape_special_characters` | provided |
| `omegaconf.base`, `omegaconf.basecontainer`, `omegaconf._impl`, `omegaconf.nodes` | **not** provided |
| `cfg.__dict__` | **not** provided (`__slots__`) |

The proxies are built lazily, so code that never asks for a node pays nothing.
Anything in the "not provided" rows has no stable contract upstream; target
hydra-fast directly instead.

## Out of scope

hydra-fast composes configs. These are Hydra features for *running* things, and
are not implemented:

- job launching and the launcher plugin API (`hydra/launcher`)
- sweeper plugins and multirun execution (sweep overrides *parse*, but nothing
  executes them)
- `hydra.utils.instantiate` / `_target_` instantiation
- working-directory management, `hydra.job.chdir`, output directories
- logging configuration (`hydra/job_logging` composes, but nothing applies it)
- the `--help` / `--cfg` / `--info` command-line interface
- callbacks, `hydra.verbose`
- Hydra's plugin discovery

The `hydra/launcher=basic` and `hydra/sweeper=basic` nodes *are* registered, so
defaults lists and overrides naming them compose without error — they are just
inert.

If you need those, use Hydra for the run and hydra-fast for the sweep
expansion, which is where the time goes.

## Reporting a difference

A behavioural difference against hydra 1.3 / omegaconf 2.3 that is not listed
above is a bug. The useful report is a config tree plus overrides, in the shape
of a case in `tests/test_compose.py` — those run both implementations and
compare, so a failing one is a reproduction.
