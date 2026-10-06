"""Differential check of *syntax error messages* against real ANTLR.

Hydra surfaces ANTLR's parser errors verbatim, so the message a user sees for
a malformed override is part of the observable behaviour -- and hydra's own
suite asserts on it. ``hydra_fast/grammar/antlr_errors.py`` reproduces the
five message shapes without ANTLR; this harness is what that was built
against.

For each input it runs both parsers over the same grammar rule and reports:

* **accept/reject** agreement -- does hydra-fast accept exactly what ANTLR
  accepts? A disagreement here is a real grammar bug, not a wording one.
* **message** agreement, for the inputs both reject.

The corpus is generated rather than listed: a set of malformed fragments
crossed with every rule the parser exposes, which is how the empty-``package``
grammar bug surfaced (it only showed up on the ``key`` rule).

    pip install hydra-core==1.3.2
    python bench/oracle_errors.py              # summary
    python bench/oracle_errors.py --show-diffs # every message that differs
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

#: Rules both implementations expose, as ``parse_rule`` names.
RULES = [
    "override",
    "key",
    "packageOrGroup",
    "package",
    "value",
    "element",
    "simpleChoiceSweep",
    "primitive",
    "dictKey",
    "dictContainer",
    "listContainer",
    "function",
]

#: Fragments chosen to hit each error shape: an unmatchable character (lexer),
#: a missing closer (insertion recovery), trailing junk (deletion recovery),
#: and a wrong token where a specific one is required (input mismatch).
FRAGMENTS = [
    # lexer: nothing can start here
    "a b",
    "key=a b",
    "!",
    "key=!",
    "a!b",
    "#",
    "key=#",
    # unterminated regions
    "key=[1,2,3]'",
    r"['a\', 'b']",
    r"{a: 'a\', b: 'b'}",
    "key='unterminated",
    'key="unterminated',
    "key=${unterminated",
    # missing closers
    "[1,2",
    "{a:1",
    "key=[1,2",
    "key={a:1",
    "choice(a,b",
    "range(1,2",
    "interval(0,1",
    "key=choice(a,b",
    # trailing junk
    "[1,2]]",
    "{a:1}}",
    "key=[1,2]]",
    "key=value extra",
    "key=1,2,3,",
    "key=,",
    # wrong token where one is required
    "key=",
    "=value",
    "key==value",
    "key=[,1]",
    "key={:1}",
    "key={a:}",
    "key={a 1}",
    "key=[1 2]",
    "key=(1,2)",
    "key@=value",
    "key@@pkg=value",
    "~~key",
    "+++key=value",
    # quoted dict keys, which the grammar does not allow
    "{'a': 1}",
    'key={"a": 1}',
    # dollars and dots in group positions
    "group$=option",
    "$key=value",
    "key.$a=value",
    # empty everything
    "",
    " ",
    "=",
    "@",
    ":",
    ",",
    # near-misses on sweeps and functions
    "choice()",
    "range()",
    "interval(1)",
    "sort(1,",
    "tag(",
    "int(",
    "glob(",
    "shuffle(float(range(10,1))))",
    # structurally deep
    "key=[[1,[2,]]]",
    "key={a:{b:{c:}}}",
    "key=[{a:1},{b:}]",
]


def _parse(parser: Any, text: str, rule: str) -> Tuple[bool, Optional[str]]:
    """``(accepted, message)`` -- the message only matters when rejected."""
    try:
        parser.parse_rule(text, rule)
        return True, None
    except Exception as exc:  # both raise their own OverrideParseException
        return False, str(exc)


def _message_core(message: str) -> str:
    """The ANTLR-generated part, dropping each side's own framing.

    Both wrap the ANTLR message in their own prefix/suffix; only the ANTLR
    sentence is being compared here.
    """
    for line in message.splitlines():
        line = line.strip()
        for shape in (
            "token recognition error at:",
            "mismatched input ",
            "no viable alternative at input ",
            "extraneous input ",
            "missing ",
        ):
            if line.startswith(shape):
                return line
    return message.strip().splitlines()[-1] if message.strip() else ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--show-diffs", action="store_true", help="print every differing message")
    ap.add_argument(
        "--show-accepts", action="store_true", help="print accept/reject disagreements"
    )
    args = ap.parse_args()

    try:
        from hydra.core.override_parser.overrides_parser import (
            OverridesParser as RealParser,
        )
        from hydra.core.override_parser.overrides_parser import (
            create_functions as real_functions,
        )
    except ImportError:
        print("hydra-core is not installed; nothing to compare against.")
        return 1

    from hydra_fast.core.override_parser.overrides_parser import OverridesParser as FastParser
    from hydra_fast.core.override_parser.overrides_parser import (
        create_functions as fast_functions,
    )

    real = RealParser(real_functions())
    fast = FastParser(fast_functions())

    total = 0
    accept_agree = 0
    both_reject = 0
    message_agree = 0
    accept_diffs: List[Tuple[str, str, bool, bool]] = []
    message_diffs: List[Tuple[str, str, str, str]] = []

    for rule in RULES:
        for text in FRAGMENTS:
            total += 1
            real_ok, real_msg = _parse(real, text, rule)
            fast_ok, fast_msg = _parse(fast, text, rule)
            if real_ok != fast_ok:
                accept_diffs.append((rule, text, real_ok, fast_ok))
                continue
            accept_agree += 1
            if real_ok:
                continue
            both_reject += 1
            real_core = _message_core(real_msg or "")
            fast_core = _message_core(fast_msg or "")
            if real_core == fast_core:
                message_agree += 1
            else:
                message_diffs.append((rule, text, real_core, fast_core))

    print(f"corpus: {len(FRAGMENTS)} fragments x {len(RULES)} rules = {total} cases")
    print(f"accept/reject agreement: {accept_agree}/{total}")
    print(f"message agreement:       {message_agree}/{both_reject} (of cases both reject)")

    if accept_diffs and (args.show_accepts or not args.show_diffs):
        print(f"\n{len(accept_diffs)} accept/reject disagreements:")
        for rule, text, real_ok, fast_ok in accept_diffs[:40]:
            verb = "accepts" if fast_ok else "rejects"
            other = "accepts" if real_ok else "rejects"
            print(f"  {rule:18s} {text!r:32s} hydra {other}, hydra-fast {verb}")

    if args.show_diffs:
        print(f"\n{len(message_diffs)} differing messages:")
        for rule, text, real_core, fast_core in message_diffs:
            print(f"  {rule}: {text!r}")
            print(f"    hydra     : {real_core}")
            print(f"    hydra-fast: {fast_core}")

    return 0 if not accept_diffs else 2


if __name__ == "__main__":
    raise SystemExit(main())
