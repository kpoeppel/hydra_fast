"""``OverridesParser``, matching ``hydra.core.override_parser``.

Mostly a thin front on :mod:`hydra_fast.grammar.override`. It exists because
``parse_rule(text, rule_name)`` -- parsing a *fragment* against one grammar
rule rather than a whole override -- is what hydra's own test suite drives the
grammar through, and what tooling that inspects overrides uses.
"""

from __future__ import annotations

from typing import Any, List

from ...errors import OverrideParseException
from ...grammar.antlr_errors import extraneous_input
from ...grammar.override import (
    _KEY_MODE,
    _VALUE_MODE,
    EOF,
    Override,
    _Mismatch,
    _OvParser,
    _tokenize_override,
    create_functions,
    default_functions,
)

__all__ = ["OverridesParser", "create_functions"]

# rule name -> (lexer mode, parser method)
_RULES = {
    "override": (_KEY_MODE, "parse_override"),
    "key": (_KEY_MODE, "parse_key"),
    "packageOrGroup": (_KEY_MODE, "parse_package_or_group"),
    "package": (_KEY_MODE, "parse_package"),
    "value": (_VALUE_MODE, "parse_value_only"),
    "element": (_VALUE_MODE, "parse_element"),
    "simpleChoiceSweep": (_VALUE_MODE, "parse_simple_choice_sweep"),
    "primitive": (_VALUE_MODE, "parse_primitive"),
    "dictKey": (_VALUE_MODE, "parse_dict_key"),
    "dictContainer": (_VALUE_MODE, "parse_dict"),
    "listContainer": (_VALUE_MODE, "parse_list"),
    "function": (_VALUE_MODE, "parse_function"),
}


class OverridesParser:
    """Parses override strings, or fragments of them, into typed objects."""

    def __init__(self, functions: Any = None, config_loader: Any = None) -> None:
        self.functions = functions if functions is not None else default_functions()
        self.config_loader = config_loader

    @classmethod
    def create(cls, config_loader: Any = None) -> "OverridesParser":
        return cls(functions=create_functions(), config_loader=config_loader)

    def parse_rule(self, s: str, rule_name: str) -> Any:
        """Parse ``s`` against a single grammar rule."""
        try:
            mode, method = _RULES[rule_name]
        except KeyError:
            raise OverrideParseException(s, f"Unknown rule '{rule_name}'") from None

        parser = _OvParser(_tokenize_override(s, mode), s, self.functions)
        try:
            result = getattr(parser, method)()
        except _Mismatch as exc:
            # No rule decision caught it, so report the expectation directly.
            raise parser.report(exc) from None
        # Only the `override` rule ends with EOF in the grammar, so only it
        # rejects trailing input. Every other rule stops where it stops and
        # ignores the rest -- `shuffle(float(range(10,1))))` parses as a
        # function and the stray paren is left behind.
        if rule_name == "override" and parser.peek() != EOF:
            raise OverrideParseException(
                s, extraneous_input(parser._text_at(parser.pos), "<EOF>")
            )
        if isinstance(result, Override):
            result.input_line = s
            result.validate()
        return result

    def parse_override(self, s: str) -> Override:
        override = self.parse_rule(s, "override")
        override.config_loader = self.config_loader
        return override

    def parse_overrides(self, overrides: List[str]) -> List[Override]:
        parsed = []
        for override in overrides:
            try:
                parsed.append(self.parse_override(override))
            except OverrideParseException as exc:
                raise OverrideParseException(
                    override=override,
                    message=f"{type(exc).__name__} parsing override '{override}'"
                    f"\n{exc.message}",
                ) from exc.__cause__
        return parsed
