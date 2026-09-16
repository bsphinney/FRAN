"""Catch the failure where an adapter parses a field and the INSERT silently throws it away.

THE BUG THIS EXISTS FOR (audit 2026-09-16). `ingest/spectronaut_to_corpus.py` has parsed
`EG.PEP` into every precursor record since its first day. `_PREC_COLS` in
`ingest/corpus_ingest.py` — the only insert path into delimp_precursors — had no `pep` column in
its list. An adapter builds a dict and the INSERT names its columns explicitly, so a key with no
matching column is **not an error**: no exception, no changed row count, nothing in any log. The
value was discarded on every row of every Spectronaut ingest, and nobody noticed for months.
Three DIA-NN fields were lost the same way when the consolidated path narrowed.

Reading the code cannot catch this — both halves look correct in isolation. Only comparing the
keys an adapter PRODUCES against the columns the INSERT CONSUMES catches it, which is what this
does. No database required: it runs the real adapters over synthetic reports and inspects the
dicts they yield.

The companion check is `ingest/audit_column_coverage.py`, which measures what is actually
populated in PG Farm and is the thing to run when you want the corpus-wide picture.
"""
import os, sys, tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "ingest"))

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


import corpus_ingest as CI                                                        # noqa: E402
from spectronaut_to_corpus import iter_records                                    # noqa: E402

ACCOUNTED = CI._PREC_COL_SET | CI._PREC_DROPPED_OK


# --------------------------------------------------------------------------------------------
# A synthetic Spectronaut report using the real FRAN (Normal) column names.
# --------------------------------------------------------------------------------------------
SN_COLS = ["R.FileName", "PEP.StrippedSequence", "EG.ModifiedSequence", "FG.Charge",
           "EG.Qvalue", "EG.GlobalPrecursorQvalue", "FG.PrecMz", "EG.ApexRT", "EG.IonMobility",
           "FG.Quantity", "EG.PEP", "EG.PTMLocalizationProbabilities", "EG.PTMAssayProbability",
           "EG.HasLocalizationInformation"]
SN_ROW = ["run_a", "PEPTIDEK", "_PEPTIDEK_", "2",
          "0.001", "0.002", "500.25", "12.5", "1.05",
          "12345.0", "0.03", "_PEPTIDEK_", "0.99",
          "False"]


def _spectronaut_keys():
    fd, path = tempfile.mkstemp(suffix=".tsv"); os.close(fd)
    try:
        with open(path, "w") as fh:
            fh.write("\t".join(SN_COLS) + "\n")
            fh.write("\t".join(SN_ROW) + "\n")
        recs = list(iter_records(path))
        return set(recs[0]) if recs else set()
    finally:
        os.unlink(path)


def _diann_keys():
    import pandas as pd
    df = pd.DataFrame([{
        "Run": "run_a", "Stripped.Sequence": "PEPTIDEK", "Modified.Sequence": "PEPTIDEK",
        "Precursor.Charge": 2, "Precursor.Mz": 500.25, "RT": 12.5, "IM": 1.05,
        "Q.Value": 0.001, "Global.Q.Value": 0.002, "PG.Q.Value": 0.003,
        "Precursor.Quantity": 12345.0, "Precursor.Normalised": 12000.0,
        "Protein.Group": "P12345", "Genes": "GENEA",
        "PEP": 0.03, "Empirical.Quality": 0.8, "Precursor.Id": "PEPTIDEK2", "FWHM": 0.027,
    }])
    rows = list(CI._diann_rows(df))
    return set(rows[0]) if rows else set()


print("== every record key an adapter emits must reach a column or be a declared drop ==")

sn = _spectronaut_keys()
check("spectronaut adapter yields records", bool(sn))
sn_orphans = sn - ACCOUNTED
check("spectronaut: no silently-discarded keys", not sn_orphans,
      f"orphaned: {sorted(sn_orphans)} — add the column to _PREC_COLS or declare it in _PREC_DROPPED_OK")

dn = _diann_keys()
check("diann adapter yields records", bool(dn))
dn_orphans = dn - ACCOUNTED
check("diann: no silently-discarded keys", not dn_orphans,
      f"orphaned: {sorted(dn_orphans)} — add the column to _PREC_COLS or declare it in _PREC_DROPPED_OK")


print("== the regressions the audit found must stay fixed ==")

# These four were parsed-then-dropped (pep) or written-then-lost (the DIA-NN three). Pinning them
# by name: a future narrowing of _PREC_COLS re-introduces the exact 2026-09-16 bug, and a generic
# orphan check would NOT catch that — removing a column from _PREC_COLS while the adapter still
# emits the key shows up here, loudly, by name.
for col in ("pep", "empirical_quality", "precursor_id_diann", "peak_fwhm",
            "site_localization_probability", "intensity"):
    check(f"_PREC_COLS still carries `{col}`", col in CI._PREC_COL_SET)

# The tuple built for the INSERT must have exactly as many values as the column list names.
# A mismatch is a hard ingest failure, but only at runtime against a live DB; count it here.
check("_PREC_COLS has no duplicate column", len(CI._PREC_COLS.split(",")) == len(CI._PREC_COL_SET))

# protein_group must stay non-final so the `.replace("protein_group,", "")` that builds the
# no-protein-group variant keeps working; if it moved last, the trailing comma would not match
# and the column list would silently keep a column the tuple does not supply.
_cols = [c.strip() for c in CI._PREC_COLS.split(",")]
check("protein_group is not the last column (the .replace() depends on its trailing comma)",
      _cols[-1] != "protein_group", f"order tail: {_cols[-3:]}")


print("== the guard itself must fire (proving this test can fail) ==")

CI._warned_unmapped.clear()
import io, contextlib                                                             # noqa: E402
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    CI._warn_unmapped_record_keys({"stripped_seq": "X", "a_field_no_column_receives": 1})
fired = "a_field_no_column_receives" in buf.getvalue()
check("guard warns on an unmapped key", fired, f"captured: {buf.getvalue()!r}")

CI._warned_unmapped.clear()
buf2 = io.StringIO()
with contextlib.redirect_stdout(buf2):
    CI._warn_unmapped_record_keys({"stripped_seq": "X", "run": "r", "charge": 2})
check("guard stays silent on a clean record", buf2.getvalue() == "", f"captured: {buf2.getvalue()!r}")

CI._warned_unmapped.clear()


print()
if FAILS:
    print(f"FAILED ({len(FAILS)}): {', '.join(FAILS)}")
    sys.exit(1)
print("all checks passed")
