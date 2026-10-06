"""Differential audit of flag-violation errors across the container API.

Every mutating operation on DictConfig/ListConfig, under ``readonly`` and
``struct``, compared against omegaconf: exception type and message text.

omegaconf names the *operation* in these messages -- "Cannot pop from
read-only node", "Cannot sort a read-only ListConfig" -- and hydra-fast's
``_hf_check_writable`` has a generic fallback that most call sites were
taking. This harness found fourteen operations that differed, two of them
behavioural rather than cosmetic; ``tests/test_errors.py`` pins the results.

    pip install omegaconf==2.3.0
    python bench/oracle_flag_errors.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "src"))

import omegaconf  # noqa: E402

import hydra_fast  # noqa: E402

DICT_OPS = [
    ("__setitem__ existing", lambda c: c.__setitem__("a", 2)),
    ("__setitem__ new", lambda c: c.__setitem__("zz", 2)),
    ("__setattr__ existing", lambda c: setattr(c, "a", 2)),
    ("__setattr__ new", lambda c: setattr(c, "zz", 2)),
    ("__delitem__", lambda c: c.__delitem__("a")),
    ("__delattr__", lambda c: delattr(c, "a")),
    ("pop", lambda c: c.pop("a")),
    ("popitem", lambda c: c.popitem()),
    ("setdefault existing", lambda c: c.setdefault("a", 9)),
    ("setdefault new", lambda c: c.setdefault("zz", 9)),
    ("update", lambda c: c.update({"a": 3})),
    ("clear", lambda c: c.clear()),
]

LIST_OPS = [
    ("__setitem__", lambda c: c.__setitem__(0, 9)),
    ("__delitem__", lambda c: c.__delitem__(0)),
    ("append", lambda c: c.append(9)),
    ("extend", lambda c: c.extend([9])),
    ("insert", lambda c: c.insert(0, 9)),
    ("pop", lambda c: c.pop()),
    ("remove", lambda c: c.remove(1)),
    ("clear", lambda c: c.clear()),
    ("sort", lambda c: c.sort()),
    ("reverse", lambda c: c.reverse()),
    ("__iadd__", lambda c: c.__iadd__([9])),
]


def outcome(api, factory, flag, operation):
    cfg = api.create(factory())
    if flag == "readonly":
        api.set_readonly(cfg, True)
    elif flag == "struct":
        api.set_struct(cfg, True)
    try:
        operation(cfg)
        return ("ok", "")
    except Exception as exc:  # noqa: BLE001
        first = str(exc).splitlines()[0] if str(exc) else ""
        return (type(exc).__name__, first)


#: (container, flag, operation) triples where hydra-fast deliberately differs.
#: Reported, but not counted as a failure. Mirrors DELIBERATE_DIVERGENCES in
#: tests/test_errors.py.
DELIBERATE = {
    # omegaconf's `__delattr__` skips the struct check its `__delitem__`
    # applies, so `delattr(cfg, "a")` bypasses struct mode. Enforced here.
    ("DictConfig", "struct", "__delattr__"),
}


def run(title, ops, factory, flags):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")
    diffs = 0
    for flag in flags:
        for label, operation in ops:
            want = outcome(omegaconf.OmegaConf, factory, flag, operation)
            got = outcome(hydra_fast.OmegaConf, factory, flag, operation)
            if want == got:
                continue
            intended = (title, flag, label) in DELIBERATE
            if not intended:
                diffs += 1
            print(f"\n{'INTENDED' if intended else 'DIFF'} [{flag}] {label}")
            print(f"  omegaconf : {want[0]}: {want[1]}")
            print(f"  hydra-fast: {got[0]}: {got[1]}")
    print(f"\n{title}: {diffs} unintended differences")
    return diffs


def main() -> int:
    total = 0
    total += run("DictConfig", DICT_OPS, lambda: {"a": 1, "b": 2}, ["readonly", "struct"])
    total += run("ListConfig", LIST_OPS, lambda: [1, 2], ["readonly", "struct"])
    print(f"\n{'=' * 78}\nTOTAL UNINTENDED DIFFERENCES: {total}")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
