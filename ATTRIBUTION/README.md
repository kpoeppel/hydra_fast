# Attribution

hydra-fast is a derivative work of two projects. This directory holds their
license texts; the table below says exactly what came from where.

| Upstream | License | What hydra-fast takes from it |
| --- | --- | --- |
| [Hydra](https://github.com/facebookresearch/hydra) (`hydra-core`) | MIT (`LICENSE-hydra`) | The Defaults List algorithm, ported near-verbatim into `src/hydra_fast/_internal/defaults_list.py` and `src/hydra_fast/_internal/default_element.py`. Hydra's own config schema and its group YAMLs, copied into `src/hydra_fast/conf/`. The structure of the config search path, repository, sources and loader. The override grammar, transcribed from `OverrideLexer.g4` / `OverrideParser.g4` into a hand-written parser. |
| [OmegaConf](https://github.com/omry/omegaconf) | BSD 3-Clause (`LICENSE-omegaconf`) | The container API surface (`OmegaConf`, `DictConfig`, `ListConfig`), reimplemented over plain dicts. The interpolation grammar, transcribed from `OmegaConfGrammarLexer.g4` / `OmegaConfGrammarParser.g4` and `grammar_visitor.py` into a hand-written parser. Merge semantics, the YAML typing rules, and the built-in `oc.*` resolvers. |

## Why port rather than rewrite

The Defaults List algorithm *is* the specification for how Hydra composes
configs. Its behaviour around `_self_` ordering, package headers, `override`
and `optional` keywords, deletions and interpolated group names is intricate
and load-bearing; re-deriving it would have produced something subtly
different. So it is ported with its original structure intact, which also
keeps it diffable against upstream when Hydra changes.

The speedup does not come from changing that algorithm. It comes from what
sits underneath it — see `docs/architecture.md`.

## Files kept close to upstream

These are intentionally *not* reformatted, so `diff` against the upstream file
stays readable:

- `src/hydra_fast/_internal/defaults_list.py`
- `src/hydra_fast/_internal/default_element.py`
- `src/hydra_fast/conf/hydra/**/*.yaml`

Deviations from upstream are marked with a `NOTE:` comment at the site.
