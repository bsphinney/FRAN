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
import ast, os, re, sys, tempfile

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


# The full DIA-NN 2.x report column set, as measured across all 80 pilot reports on 2026-09-23.
# Every column FRAN maps must appear here, so that a mapping typo ("Ms1.Apex.MZ.Delta") shows up
# as an EMPTY key rather than passing a test built from the same typo.
DIANN_ROW = {
    "Run": "run_a", "Stripped.Sequence": "PEPTIDEK", "Modified.Sequence": "PEPTIDEK",
    "Precursor.Charge": 2, "Precursor.Mz": 500.25, "RT": 12.5, "IM": 1.05,
    "iRT": 30.0, "iIM": 1.04,
    "Q.Value": 0.001, "Global.Q.Value": 0.002, "PG.Q.Value": 0.003,
    "Precursor.Quantity": 12345.0, "Precursor.Normalised": 12000.0,
    "Protein.Group": "P12345", "Genes": "GENEA",
    "PEP": 0.03, "Empirical.Quality": 0.8, "Precursor.Id": "PEPTIDEK2", "FWHM": 0.027,
    # added 2026-09-23 — per-precursor
    "RT.Start": 12.1, "RT.Stop": 12.9, "Predicted.RT": 12.4, "Predicted.iRT": 29.6,
    "Predicted.IM": 1.06, "Predicted.iIM": 1.05,
    "Ms1.Area": 9000.0, "Ms1.Normalised": 8800.0, "Ms1.Apex.Area": 7000.0,
    "Ms1.Apex.Mz.Delta": 0.001, "Ms1.Total.Signal.Before": 1e9, "Ms1.Total.Signal.After": 9e8,
    "Ms1.Profile.Corr": 0.91, "Quantity.Quality": 0.88, "Evidence": 2.5, "Mass.Evidence": 0.7,
    "Channel.Evidence": 0.97, "Averagine": 0.95, "Normalisation.Factor": 0.98,
    "Normalisation.Noise": 0.01, "Best.Fr.Mz": 559.34, "Best.Fr.Mz.Delta": 0.002,
    "Peptidoform.Q.Value": 0.004, "Global.Peptidoform.Q.Value": 0.005, "Proteotypic": 1,
    "PTM.Site.Confidence": 1.0,
    # added 2026-09-23 — protein/gene level, consumed into delimp_proteins
    "PG.MaxLFQ": 5e5, "PG.MaxLFQ.Quality": 0.9, "PG.PEP": 0.0001,
    "Global.PG.Q.Value": 0.0002, "Protein.Q.Value": 0.0003,
    "Genes.MaxLFQ": 4e5, "Genes.MaxLFQ.Unique": 3e5, "Genes.MaxLFQ.Quality": 0.89,
    "Genes.MaxLFQ.Unique.Quality": 0.87, "GG.Q.Value": 0.0004,
    # present in every report and deliberately NOT mapped — see the migration's exclusions
    "Channel": "", "Decoy": 0, "Translated.Q.Value": 0.0, "Channel.Q.Value": 0.0,
    "PG.TopN": 0.0, "Genes.TopN": 0.0, "Run.Index": 0, "Precursor.Lib.Index": 12,
    "Protein.Ids": "P12345", "Protein.Names": "PROT_HUMAN",
    "Site.Occupancy.Probabilities": "PEPTIDEK2", "Protein.Sites": "",
    "Lib.Q.Value": 0.001, "Lib.Peptidoform.Q.Value": 0.002, "Lib.PG.Q.Value": 0.003,
    "Lib.PTM.Site.Confidence": 1.0,
}


def _diann_rows_for(row):
    import pandas as pd
    return list(CI._diann_rows(pd.DataFrame([row])))


def _diann_keys():
    rows = _diann_rows_for(DIANN_ROW)
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

_cols = [c.strip() for c in CI._PREC_COLS.split(",")]
check("_PREC_COLS names are unique", len(_cols) == len(set(_cols)),
      f"duplicates: {sorted({c for c in _cols if _cols.count(c) > 1})}")

_no_pg = [c for c in _cols if c != "protein_group"]
check("the no-protein_group column list is exactly one shorter",
      len(_no_pg) == len(_cols) - 1, f"{len(_no_pg)} vs {len(_cols)}")


print("== the DIA-NN report columns added 2026-09-23 ==")

# ORDER, not just membership. _diann_block() emits its 25 values positionally against the run of
# names spliced into _PREC_COLS; if someone inserts a column in one place and not the other, every
# column after the splice point receives the wrong value and NOTHING raises -- the types are all
# `real`, so PostgreSQL accepts rt_stop's number into predicted_rt without a murmur. Reading both
# lists side by side is the only way to catch it, so read them out of the source and compare.
_src = open(os.path.join(REPO, "ingest", "corpus_ingest.py")).read()
_tree = ast.parse(_src)
_block_fn = next((n for n in ast.walk(_tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_diann_block"), None)
check("_diann_block() exists", _block_fn is not None)

_block_names = []
if _block_fn:
    _ret = max((n.value for n in ast.walk(_block_fn)
                if isinstance(n, ast.Return) and isinstance(n.value, ast.Tuple)),
               key=lambda t: len(t.elts))
    for elt in _ret.elts:                       # _flt(x.get("name")) -> "name"
        inner = elt.args[0] if isinstance(elt, ast.Call) and elt.args else None
        if isinstance(inner, ast.Call) and inner.args and isinstance(inner.args[0], ast.Constant):
            _block_names.append(inner.args[0].value)
        else:
            _block_names.append(f"<unparsed {ast.dump(elt)[:40]}>")

check("_diann_block() emits exactly _DIANN_PREC_COLS, in order",
      _block_names == list(CI._DIANN_PREC_COLS),
      f"block={_block_names}\n          cols={list(CI._DIANN_PREC_COLS)}")

check("every _DIANN_PREC_COLS name is in _PREC_COLS",
      CI._DIANN_PREC_COL_SET <= CI._PREC_COL_SET,
      f"missing: {sorted(CI._DIANN_PREC_COL_SET - CI._PREC_COL_SET)}")

# The protein/gene-level values must NOT be precursor columns — that is the whole point of
# measuring their grain — but they must still reach delimp_proteins rather than vanish.
check("protein-level keys are not delimp_precursors columns",
      not (CI._PROTEIN_LEVEL_KEYS & CI._PREC_COL_SET),
      f"leaked: {sorted(CI._PROTEIN_LEVEL_KEYS & CI._PREC_COL_SET)}")
check("protein-level keys are declared drops for the precursor INSERT",
      CI._PROTEIN_LEVEL_KEYS <= CI._PREC_DROPPED_OK,
      f"undeclared: {sorted(CI._PROTEIN_LEVEL_KEYS - CI._PREC_DROPPED_OK)}")
check("the delimp_proteins INSERT appends _PROTEIN_LEVEL_COLS",
      "_prot_extra" in _src and "INSERT INTO delimp_proteins" in _src)

# The migration and the code must name the same columns. They live in different files and nothing
# but this check stops one from gaining a column the other never hears about.
_MIGDIR = os.path.join(REPO, "ingest", "migrations")
# TWO migration files now carry the DIA-NN precursor columns: the 2026-09-23 set of 35, and the
# 2026-09-24 set of 6 that reverses part of its exclusion list (the Lib.* family, Protein.Ids and
# Protein.Sites -- see that file's header for the measurements). They are concatenated rather than
# the newer one replacing the older, because the code's _DIANN_PREC_COLS is the union of both and
# the equality check below must see the same union.
_sql = "\n".join(open(os.path.join(_MIGDIR, f)).read() for f in (
    "2026-09-23_diann_report_columns.sql",
    "2026-09-24_diann_lib_and_protein_columns.sql"))


def _sql_columns(text):
    """-> {table: {column: type}}. Keyed BY TABLE: an earlier version sorted into two buckets with
    `delimp_precursors ? a : b`, which quietly filed a third table's columns under `delimp_proteins`
    and made both equality checks below lie."""
    out = {}
    for ln in text.splitlines():
        m = re.match(r"\s*ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+)\s+([\w ]+?)\s*;", ln)
        if m:
            out.setdefault(m.group(1), {})[m.group(2)] = m.group(3).strip().lower()
    return out


_bytable = _sql_columns(_sql)
check("the DIA-NN migration touches exactly delimp_precursors and delimp_proteins",
      set(_bytable) == {"delimp_precursors", "delimp_proteins"}, f"tables: {sorted(_bytable)}")
_sql_prec = set(_bytable.get("delimp_precursors", {}))
_sql_prot = set(_bytable.get("delimp_proteins", {}))
check("migration adds exactly the _DIANN_PREC_COLS precursor columns",
      _sql_prec == CI._DIANN_PREC_COL_SET,
      f"sql-only={sorted(_sql_prec - CI._DIANN_PREC_COL_SET)} "
      f"code-only={sorted(CI._DIANN_PREC_COL_SET - _sql_prec)}")
check("migration adds exactly the _PROTEIN_LEVEL_COLS protein columns",
      _sql_prot == CI._PROTEIN_LEVEL_KEYS,
      f"sql-only={sorted(_sql_prot - CI._PROTEIN_LEVEL_KEYS)} "
      f"code-only={sorted(CI._PROTEIN_LEVEL_KEYS - _sql_prot)}")

# DIA-NN's floats are all float32, so `real` is bit-exact and `double precision` would only double
# the bytes. Pin it: widening a column later is a table rewrite on a 261 GB table.
#
# `text` is allowed ONLY for the two columns that are genuinely strings in report.parquet
# (measured: pyarrow type `string`). Named explicitly rather than allowing text everywhere --
# the point of this check is that a float column must never be created as `double precision`,
# and a blanket "or text" would let that through for anything someone typed wrongly.
_TEXT_OK = {"delimp_precursors.protein_ids", "delimp_precursors.protein_sites"}
_wrong = {f"{t}.{c}": ty for t, cols in _bytable.items() for c, ty in cols.items()
          if ty not in ("real", "boolean") and not (ty == "text" and f"{t}.{c}" in _TEXT_OK)}
check("every DIA-NN migration column is `real` (or the one boolean)", not _wrong, f"{_wrong}")

# Every migration under migrations/ must be runnable by the runner, whatever it is for. The
# runner refuses anything that is not a bare additive ALTER, and refuses DEFAULT outright.
sys.path.insert(0, os.path.join(REPO, "ingest"))
import migrate_diann_columns as MIG                                               # noqa: E402
for _f in sorted(os.listdir(_MIGDIR)):
    if not _f.endswith(".sql"):
        continue
    _txt = open(os.path.join(_MIGDIR, _f)).read()
    if "ADD COLUMN" not in _txt.upper():
        continue                                 # CREATE TABLE migrations are not this runner's job
    try:
        _plan = [MIG.parse(s) for s in MIG.statements(_txt)]
        check(f"{_f} parses as additive, DEFAULT-free ALTERs", bool(_plan))
    except Exception as e:                        # noqa: BLE001
        check(f"{_f} parses as additive, DEFAULT-free ALTERs", False, str(e)[:160])

check("the xic-lane link-key migration exists and is its own file (not folded into the DIA-NN set)",
      os.path.exists(os.path.join(_MIGDIR, "2026-09-23_xic_lane_source_output_dir.sql")))


print("== the deployment import closure (the 2026-09-23 ModuleNotFoundError) ==")

# A sync that copied the 15 DIFFERING files and missed the 5 ABSENT ones killed a 53-search run:
# raw_metadata.py imported tdf_safe, which had never been copied to that target. The closure walk
# has to see imports nested inside functions, because those are the ones that fail hours in
# rather than at start-up.
import audit_deploy_sync as ADS                                                   # noqa: E402
_ing = os.path.join(REPO, "ingest")
_pyfiles = {f for f in os.listdir(_ing) if f.endswith(".py")}
_closure = ADS.closure(_ing, _pyfiles)
check("corpus_ingest.py is an entry point of the closure", "corpus_ingest.py" in _closure)
for _need in ("raw_metadata.py", "tdf_safe.py", "versions.py", "provenance.py"):
    check(f"closure includes `{_need}`", _need in _closure,
          f"closure: {sorted(_closure)}")

# The specific shape that got through: a lazy, function-scoped import.
_rm = ADS.local_imports(os.path.join(_ing, "raw_metadata.py"), _pyfiles)
check("raw_metadata.py's import of tdf_safe is seen", "tdf_safe.py" in _rm, f"saw {sorted(_rm)}")
_ci = ADS.local_imports(os.path.join(_ing, "corpus_ingest.py"), _pyfiles)
check("corpus_ingest.py's function-scoped imports are seen (versions, provenance)",
      {"versions.py", "provenance.py"} <= _ci, f"saw {sorted(_ci)}")
check("no migration statement carries a DEFAULT (it would rewrite 261 GB)",
      "default" not in _sql.lower().split("-- ")[0] or
      not any("default" in l.lower() for l in _sql.splitlines()
              if l.strip().upper().startswith("ALTER TABLE")))

# The mapping must actually PRODUCE these values from a real report row, not merely name them.
_r = _diann_rows_for(DIANN_ROW)[0]
_unset = sorted(c for c in CI._DIANN_PREC_COLS if _r.get(c) is None)
check("every DIA-NN precursor column is populated from a full report row", not _unset,
      f"still None: {_unset}")
_unset_p = sorted(c for c in CI._PROTEIN_LEVEL_COLS if _r.get(c) is None)
check("every protein-level key is populated from a full report row", not _unset_p,
      f"still None: {_unset_p}")

# PTM.Site.Confidence fills the EXISTING site_localization_probability column, but only where
# there is a modification to localize: DIA-NN writes a literal 1.0 on every unmodified precursor.
check("site_localization_probability is NULL on an unmodified precursor",
      _r.get("site_localization_probability") is None,
      f"got {_r.get('site_localization_probability')!r} for an unmodified peptide")
_mod = dict(DIANN_ROW, **{"Modified.Sequence": "PEPTIDEK(UniMod:21)",
                          "PTM.Site.Confidence": 0.62})
_rm = _diann_rows_for(_mod)[0]
check("site_localization_probability carries PTM.Site.Confidence on a modified precursor",
      _rm.get("n_mods") and abs((_rm.get("site_localization_probability") or 0) - 0.62) < 1e-6,
      f"n_mods={_rm.get('n_mods')} value={_rm.get('site_localization_probability')!r}")

# The excluded columns must stay excluded: a later "completeness" edit that adds Decoy or Channel
# back is re-adding columns measured to hold ONE value across 45,058,279 rows.
#
# This list SHRANK on 2026-09-24. Six columns moved from here to the inclusion check below, by a
# deliberate decision recorded in 2026-09-24_diann_lib_and_protein_columns.sql: the four Lib.*
# columns (the earlier migration itself called that exclusion "a judgement call rather than a
# measurement" and named the decoy/ML lane as the condition for reversing it), plus Protein.Ids
# (constant within Protein.Group in only 86.8% of groups, so genuinely per-precursor) and
# Protein.Sites (the only record of WHERE on the protein a modification sits).
#
# The ten below stay out, and the reasons are NOT alike -- do not collapse them into one rule.
# Six are structurally constant; run_index is derivable from the raw_path FK (4-12 distinct values
# per search); precursor_lib_index joins to a library FRAN does not store;
# site_occupancy_probabilities is ~8.5 GB of which 94% echoes Precursor.Id verbatim, its numeric
# content already kept as site_localization_probability; protein_names is a FASTA lookup.
for _col in ("channel", "decoy", "translated_q_value", "channel_q_value", "pg_topn",
             "genes_topn", "run_index", "precursor_lib_index", "protein_names",
             "site_occupancy_probabilities"):
    check(f"`{_col}` stays out of _PREC_COLS (excluded with evidence — see the migration)",
          _col not in CI._PREC_COL_SET)

# ...and the six that were deliberately ADDED must actually be carried. Without this, reverting
# the 2026-09-24 decision would silently pass every check in this file.
for _col in ("lib_q_value", "lib_peptidoform_q_value", "lib_pg_q_value",
             "lib_ptm_site_confidence", "protein_ids", "protein_sites"):
    check(f"`{_col}` IS carried (added with evidence — 2026-09-24 migration)",
          _col in CI._PREC_COL_SET)

# A report that predates these columns (FragPipe bundles DIA-NN 1.8.2b8) must still ingest.
_old = {k: v for k, v in DIANN_ROW.items()
        if k in ("Run", "Stripped.Sequence", "Modified.Sequence", "Precursor.Charge",
                 "Precursor.Mz", "RT", "Q.Value", "Precursor.Quantity", "Protein.Group", "Genes")}
try:
    _ro = _diann_rows_for(_old)[0]
    check("an old report with none of the new columns still yields a record", bool(_ro))
    check("its new keys are all None, not missing",
          all(_ro.get(c, "MISSING") is None for c in CI._DIANN_PREC_COLS),
          f"non-None/missing: {[c for c in CI._DIANN_PREC_COLS if _ro.get(c, 'MISSING') is not None]}")
except Exception as e:  # noqa: BLE001
    check("an old report with none of the new columns still yields a record", False, repr(e))

# DIA-NN 1.x names the predicted RT `RT.Predicted` and has no `iRT`, so `irt` and `predicted_rt`
# both resolve to the SAME report column. Selecting a duplicated column and renaming it silently
# collapses the frame, so the adapter de-duplicates and fans the value back out.
_alias = dict(_old); _alias["RT.Predicted"] = 31.5
try:
    _ra = _diann_rows_for(_alias)[0]
    check("two mapping keys resolving to one report column both receive the value",
          _ra.get("irt") == 31.5 and _ra.get("predicted_rt") == 31.5,
          f"irt={_ra.get('irt')!r} predicted_rt={_ra.get('predicted_rt')!r}")
except Exception as e:  # noqa: BLE001
    check("two mapping keys resolving to one report column both receive the value", False, repr(e))


print("== INSERT tuple arity must match the column list ==")

# Parsed from the source, not executed: both tuples are built inside list comprehensions in
# ingest(), which needs a live DB, so AST-counting is the only way to check arity without one.
# Highest-consequence invariant in the file — an off-by-one corrupts every column after the
# insertion point, and psycopg2 only raises once a re-ingest is already running against prod.
#
# Both tuples splice in the 25 DIA-NN values as `*_diann_block(x)`, so a raw len() of the tuple
# elements counts that whole run as one. Expand a Starred call by looking up the arity of the
# function it calls — otherwise this check reads 26 where the INSERT sends 50 and the arity
# invariant, the most consequential one here, silently stops meaning anything.
_fn_arity = {n.name: max((len(r.value.elts) for r in ast.walk(n)
                          if isinstance(r, ast.Return) and isinstance(r.value, ast.Tuple)),
                         default=None)
             for n in ast.walk(_tree) if isinstance(n, ast.FunctionDef)}


def _arity(tup):
    total = 0
    for elt in tup.elts:
        if isinstance(elt, ast.Starred):
            fn = (elt.value.func.id if isinstance(elt.value, ast.Call)
                  and isinstance(elt.value.func, ast.Name) else None)
            n = _fn_arity.get(fn)
            if n is None:
                return None                     # unresolvable: fail loudly rather than guess
            total += n
        else:
            total += 1
    return total


_tuples = sorted(a for a in (_arity(n.elt) for n in ast.walk(_tree)
                             if isinstance(n, ast.ListComp) and isinstance(n.elt, ast.Tuple)
                             and len(n.elt.elts) > 15) if a is not None)
check("exactly two precursor-row tuples exist (write_pg and non-write_pg)", len(_tuples) == 2,
      f"found arities {_tuples}")
if len(_tuples) == 2:
    check("non-write_pg tuple matches the protein_group-less column list",
          _tuples[0] == len(_cols) - 1, f"{_tuples[0]} values vs {len(_cols) - 1} columns")
    check("the spliced DIA-NN block is the width the column list reserves for it",
          _fn_arity.get("_diann_block") == len(CI._DIANN_PREC_COLS),
          f"_diann_block returns {_fn_arity.get('_diann_block')} values, "
          f"_DIANN_PREC_COLS names {len(CI._DIANN_PREC_COLS)}")
    check("write_pg tuple matches the full column list",
          _tuples[1] == len(_cols), f"{_tuples[1]} values vs {len(_cols)} columns")


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
