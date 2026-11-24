"""Audit 2026-11: re-ingest must not clobber good raw_files / sample-metadata values, and
backfill_fragments must exit non-zero when a report failed.

Runs under pytest or as a plain script with the project venv.
"""
import importlib.util
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_INGEST = os.path.join(_HERE, "..", "ingest")
sys.path.insert(0, _INGEST)


def _src():
    return open(os.path.join(_INGEST, "corpus_ingest.py"), encoding="utf-8").read()


def _set_clause(src, table):
    """The `DO UPDATE SET ...` text of the upsert into `table`, as {column: expression}."""
    m = re.search(r"INSERT INTO " + table + r"\b.*?DO UPDATE\s+SET(.*?)\"\"\"", src, re.S)
    assert m, f"no upsert into {table}"
    body = re.sub(r"--[^\n]*", "", m.group(1))                  # drop SQL comments
    out = {}
    for col, expr in re.findall(r"(\w+)\s*=\s*(COALESCE\(.*?\)|EXCLUDED\.\w+)\s*(?:,|$)", " ".join(body.split())):
        out[col] = expr
    return out


def test_raw_files_upsert_never_overwrites_with_null():
    sets = _set_clause(_src(), "raw_files")
    assert sets, "could not parse the raw_files upsert"
    bare = [c for c, e in sets.items() if not e.startswith("COALESCE")]
    assert not bare, f"raw_files columns overwritten without COALESCE: {bare}"
    for c in ("acquisition_method", "samples_per_day", "gradient_minutes", "instrument_model"):
        assert c in sets


def test_sniffed_acquisition_beats_stored_beats_guess():
    sets = _set_clause(_src(), "raw_files")
    # sniffed value first, the stored value second, the engine/platform guess (EXCLUDED) last
    assert re.match(r"COALESCE\(%s, raw_files\.acquisition_method, EXCLUDED\.acquisition_method\)",
                    sets["acquisition_method"])


def test_sample_metadata_upsert_keeps_organism_when_new_value_is_null():
    sets = _set_clause(_src(), "delimp_sample_metadata")
    assert sets.get("organism_name", "").startswith("COALESCE(EXCLUDED.organism_name")
    assert sets.get("organism_taxon_id", "").startswith("COALESCE(EXCLUDED.organism_taxon_id")


def _backfill(monkey_process_one):
    spec = importlib.util.spec_from_file_location("backfill_fragments", os.path.join(_INGEST, "backfill_fragments.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.process_one = monkey_process_one
    return m


def _run(m, argv):
    old = sys.argv
    sys.argv = ["backfill_fragments.py"] + argv
    try:
        m.main()
        return 0
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1
    finally:
        sys.argv = old


def test_backfill_exits_nonzero_when_a_report_failed():
    def boom(rp, out_dir, dry, resume):
        raise RuntimeError("parse failed")
    assert _run(_backfill(boom), ["a_report.parquet", "--dry-run"]) == 1


def test_backfill_exits_zero_when_all_reports_succeed():
    ok = lambda rp, out_dir, dry, resume: ("n", None, 5, 50, None, 1)  # noqa: E731
    assert _run(_backfill(ok), ["a_report.parquet", "--dry-run"]) == 0


if __name__ == "__main__":
    fails = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f(); print("PASS", n)
            except Exception as e:  # noqa: BLE001
                fails += 1; print("FAIL", n, type(e).__name__, e)
    sys.exit(1 if fails else 0)
