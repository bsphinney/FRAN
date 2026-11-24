"""The audit repair scripts must be dry runs unless --apply is given, and a dry run must be unable
to write (read-only session)."""
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REP = os.path.join(_HERE, "..", "ingest", "repairs")
sys.path.insert(0, _REP)
SCRIPTS = ("recompute_is_contaminant.py", "null_nan_protein_group.py", "reconcile_raw_files.py")


def test_apply_is_off_by_default():
    import _common
    a = _common.base_parser("x").parse_args([])
    assert a.apply is False and a.dry_run is True
    assert _common.base_parser("x").parse_args(["--apply"]).apply is True


def test_dry_run_connection_is_read_only():
    src = open(os.path.join(_REP, "_common.py"), encoding="utf-8").read()
    assert "set_session(readonly=True, autocommit=True)" in src


def test_every_write_is_behind_apply():
    for name in SCRIPTS:
        src = open(os.path.join(_REP, name), encoding="utf-8").read()
        body = re.sub(r'""".*?"""', "", src, flags=re.S)
        lines = body.splitlines()
        for i, l in enumerate(lines):
            if re.search(r"\b(UPDATE|DELETE FROM)\b", l) and "execute" in l or re.search(r'^\s*"(UPDATE|DELETE FROM)', l):
                # walk back to the nearest `if a.apply` at a lower indent
                ind = len(l) - len(l.lstrip())
                # a write inside the helper `_update`, which main() calls only under `if a.apply`
                if any(lines[j].startswith("def _update") for j in range(i, -1, -1)) and \
                        re.search(r"^\s+if a\.apply:\s*$.*?_update\(", "\n".join(lines), re.S | re.M):
                    continue
                ok = any(re.match(r"\s*if a\.apply", lines[j]) and (len(lines[j]) - len(lines[j].lstrip())) < ind
                         for j in range(i, -1, -1))
                assert ok, f"{name}: write not under `if a.apply`: {l.strip()[:70]}"


def test_pick_is_exact_when_sample_covers_everything():
    import _common
    ids = list(range(10))
    assert _common.pick(ids, 0) == (ids, False)
    assert _common.pick(ids, 20) == (ids, False)
    sub, est = _common.pick(ids, 4)
    assert est is True and len(sub) == 4


if __name__ == "__main__":
    fails = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f(); print("PASS", n)
            except Exception as e:  # noqa: BLE001
                fails += 1; print("FAIL", n, type(e).__name__, e)
    sys.exit(1 if fails else 0)
