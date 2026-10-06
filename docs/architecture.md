# Architecture

Where Hydra's time goes, and what hydra-fast does instead.

## The measurement that set the design

Profiling stock `hydra 1.3.2 + omegaconf 2.3.0` composing the
`oellm-autoexp` tree once per sweep point (355 ms per point):

| share | what |
| --- | --- |
| ~48% | `create_defaults_list` → `repo.load_config` → `OmegaConf.load` → `yaml.load`. **150 YAML parses per `compose()` call**, over ~90 distinct files. |
| ~28% | `to_container(resolve=True)`, almost entirely the ANTLR visitor re-walking parse trees. |
| most of the rest | `_set_item_impl` / `_dereference_node` / `deepcopy` — the cost of OmegaConf's `Node` object graph. |

Each line became one of the three changes below.

## 1. Parse each file once per process

`hydra_fast/_cache.py` holds read-through caches keyed on file identity, and
`_internal/config_source.py` routes every load through them: raw text, parsed
YAML, and the `defaults:` list parsed into `InputDefault` objects.

A 75-point sweep over that tree: **9 parses total**, against roughly 11,000 for
stock Hydra.

Cached parse results are *shared*, so `load_config` hands back a copy of the
config body — composition merges into it. A test asserts this
(`test_load_returns_independent_copies`): a cache that hands out aliased
storage is worse than no cache.

### Validation policy

`set_validation("stat")` (default) re-`stat()`s on every lookup and invalidates
when `(mtime_ns, size, inode)` changes, so editing a config mid-process is
picked up. A `stat()` is ~1000x cheaper than a parse, so this is nearly free
for file reads.

It is *not* free for the defaults-list and compose caches, which must check
every file that could feed them. Two things keep that cheap
(`_internal/fingerprint.py`):

- `realpath` results are memoized — symlink resolution is not free and the
  answer does not change during a sweep;
- the fingerprint tuple is computed once per composition and shared by both
  caches, rather than each walking the tree.

Existence checks (`is_config` / `is_group`) are cached against the **parent
directory's mtime**. A directory's mtime changes exactly when an entry is added
or removed from it — exactly when an existence answer can change. One stat per
directory per composition replaces ~12,000 per-file stats. This was worth 20%.

`set_validation("never")` skips revalidation entirely and trusts the first read
for the life of the process. Note that after the three optimizations above it
is **no longer measurably faster**: best-of-5 over the 75-point sweep gives
2.72 ms/point with validation and 2.78 ms/point without. The safe default is
effectively free, so there is no trade to make. `"never"` is kept for config
trees large enough that the remaining per-file checks could still matter —
measure before reaching for it.

## 2. Compile interpolations to closures

`hydra_fast/grammar/interpolation.py` is a hand-written lexer and
recursive-descent parser for OmegaConf's interpolation grammar, transcribed
from `OmegaConfGrammarLexer.g4` / `OmegaConfGrammarParser.g4` and
`grammar_visitor.py` — including ANTLR's longest-match / first-rule-wins
disambiguation and the mode stack, so the accepted syntax is the same.

The important part is what it produces: not a parse tree, but a **Python
closure**, memoized on the string. Resolving `${a.b}` for the thousandth time
calls a closure that does one dict walk. There is no tree to re-walk and no
visitor dispatch.

The compiler also constant-folds: in `"prefix-${a}-suffix"` the literal
segments are unescaped and concatenated at compile time, so only the
interpolation is evaluated per resolution.

A typical sweep has a few dozen distinct interpolation strings and resolves
them thousands of times — on the benchmark above, 25 compiles for 2,015
resolutions.

### What replaces the parse tree

A closure is opaque where a tree is walkable, so the two things a tree gets
used for are provided directly.

**Parse once, resolve many.** A compiled closure takes its evaluation context
as an argument, so the same parsed interpolation resolves against any number
of configs -- exactly what `resolve_parse_tree(tree, node=...)` is for.
`hydra_fast.grammar.interpolation.parse()` is signature-compatible with
`omegaconf.grammar_parser.parse()` and returns the closure in the tree's
place; `Container.resolve_parse_tree()` evaluates it.

**Static introspection.** The compiler knows the referenced paths and resolver
names while it is building a closure, so rather than discard them it records
an `Analysis`:

```python
>>> from hydra_fast import analyze_interpolation
>>> found = analyze_interpolation("${db.host}:${db.port}/${oc.env:NAME,prod}")
>>> found.referenced_keys()
('db.host', 'db.port')
>>> found.resolvers
('oc.env',)
```

This is tested against a walk of omegaconf's own parse tree and produces the
same answer. Where a path is computed (`${a.${k}}`) it cannot be reduced to a
key, so `dynamic` is set and the key list is documented as a lower bound
rather than padded with a placeholder. It rides the compile cache, so asking
is free after the first time, and it is a function attribute -- the call path
is untouched.

`grammar/override.py` does the same for Hydra's override grammar. That matters
for staged sweeps specifically: they pass the whole sibling config down as
several hundred `++key=value` overrides per point, and Hydra builds a fresh
ANTLR lexer and parser for *every single one*.

**Error reporting.** The third thing ANTLR's machinery provides is the message
for malformed input, which Hydra surfaces verbatim — so it is observable, and
hydra's own suite asserts on it. `grammar/antlr_errors.py` builds the five
shapes ANTLR emits, and the parser supplies the expected-token set, ordered by
declaration index in the `.g4` as ANTLR orders it.

Choosing *which* shape is where the work was. It required modelling two things
that are easy to miss:

- **Recovery order.** ANTLR tries single-token deletion first ("extraneous
  input"), then insertion ("missing X at"), and only then reports an input
  mismatch. The order is observable beyond the wording: the deletion probe
  reads one token past the offending one, which runs the lexer that much
  further — so a *lexer* error just beyond surfaces instead of the parser
  error. That is why `['a\', 'b']` reports the unterminated quote.
- **Lazy lexing.** ANTLR pulls tokens on demand, so a rule that stops early
  never scans the rest of the input and therefore never rejects it:
  `parse_rule("key=!", "key")` *succeeds* upstream, because `!` is never
  looked at. An eager tokenizer rejects input the parser was never going to
  read. `_TokenStream` lexes on demand for this reason, not for speed.

Both were found by differential testing against real ANTLR — the same exercise
also turned up two genuine grammar bugs (`package`'s empty alternative was not
gated on its FOLLOW set; an all-whitespace `dictKey` was accepted).
`bench/oracle_errors.py` measures the result: 744/744 inputs accepted or
rejected identically, 455/459 messages character-identical.

## 3. Plain dicts instead of a node graph

`hydra_fast/container.py`. Storage is a plain `dict`/`list` tree; interpolation
strings sit in it as ordinary `str` and resolve on access. `DictConfig` is a
two-slot view — a root reference and a path tuple — so:

- creating a child view is nearly free (no allocation of wrapped nodes);
- merging is a dict walk (`merge.py`);
- `deepcopy` copies data, not an object graph — and goes through `_copy.py`,
  which skips `copy.deepcopy`'s memo table and per-object dispatch for the
  dict/list/scalar shape that configs actually are.

Flags (`struct`, `readonly`) live in a dict on the root keyed by path, with
inheritance by walking up the path, which reproduces OmegaConf's flag
inheritance without a parent pointer per node.

Two consequences worth knowing:

- A view is **live**: `cfg.a` sees later writes through `cfg`. Same as
  OmegaConf.
- A node interpolation that resolves to a container yields a **view**, not the
  raw dict, because resolvers receive `${some.node}` as a `DictConfig` and call
  OmegaConf APIs on it. Found by differential testing against the real tree.

## 4. Memoize composition on what can change it

Two caches in `_internal/config_loader.py` and `_internal/defaults_list.py`:

**The defaults list.** Building it means loading every config in the tree to
read its own `defaults:` and `@package` header. Only overrides that *select a
config group* can change the outcome; the `++key=value` overrides a sweep
varies per point cannot. So the cache key holds only the group-selecting
subset — which is what lets 500 sweep points share one tree walk.

Deciding which overrides are group-selecting is itself done by Hydra's
`Overrides` class, so that is built first and the key derived from it. A cache
miss re-runs the real thing including the validation that reports unused
overrides, so a hit can only ever happen for an override set that already
validated.

**The merged config.** Given a defaults list and a search path, merging is a
pure function. Sweep points differing only in value overrides share one merge.
The result is copied before return, because the caller mutates it (struct flag,
overrides, hydra bookkeeping). `test_value_overrides_do_not_leak_between_points`
pins that down.

## What was *not* changed

The Defaults List algorithm itself is ported from Hydra essentially verbatim
(`_internal/defaults_list.py`, `_internal/default_element.py`). It *is* the
specification for how configs compose — `_self_` ordering, package resolution,
`override`/`optional` keywords, deletions, interpolated group names — and
re-deriving it would have produced something subtly different.

It is also not where the time went. Once file loading is cached and the
containers are plain, walking the tree is cheap. Keeping the original structure
means it stays diffable against upstream; deviations carry a `NOTE:` comment.

## Where the remaining time goes

At 3.5 ms per point on the real tree, the profile is roughly:

- ~55% `to_container(resolve=True)` — and about half of *that* is the config's
  own custom resolvers doing real work (building Megatron CLI strings);
- ~25% defaults-list construction, mostly `Overrides.__init__` classifying
  overrides against the repository;
- the rest spread across merging, copying and override parsing.

The dominant remaining cost is genuine work rather than overhead, which is why
the last few optimizations returned 20-25% rather than multiples.
