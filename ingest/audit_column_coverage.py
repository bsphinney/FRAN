#!/usr/bin/env python3
"""Measure which delimp_precursors columns are actually populated, per engine, and diff it
against a committed baseline so a silent regression shows up as a number that moved.

WHY THIS EXISTS. On 2026-09-16 an audit found that `delimp_precursors.pep` had been parsed from
every Spectronaut report and written on none of them, that four DIA-NN fields had quietly stopped
being carried when the ingest path was consolidated, and that `intensity_log2` was read by two
user-facing surfaces while having no writer at all. None of it raised an error. The corpus simply
had columns that were empty, and nothing compared "what we write" against "what is there".

TWO MEASUREMENTS, AND THE DIFFERENCE MATTERS:

  * `pg_stats.null_frac` is a corpus-wide view, free (a catalog read, no table scan), and can be
    STALE -- delimp_precursors was last analyzed 2026-08-28 when this was written. Good for
    "which columns are empty across all history", useless for "did last night's ingest break".
  * The per-engine scoped sample reads the NEWEST search for each engine and is the regression
    signal. A writer that broke yesterday shows up here today while the corpus-wide fraction
    barely twitches, because one new search is a rounding error against 416M rows.

Do not replace the second with the first. The whole failure mode this guards against is a change
that only affects new data.

USAGE
    python3 ingest/audit_column_coverage.py                      # human-readable report
    python3 ingest/audit_column_coverage.py --json PATH          # write/refresh the baseline
    python3 ingest/audit_column_coverage.py --check PATH         # diff vs baseline, exit 1 on regression

Imports only coreomics_import, per the Hive deployment constraint that binds every script in
this directory (no app.*; fran_ingest/ is a flat scp'd directory with no app/ above it).
"""
from __future__ import annotations
import argparse, json, os, sys, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from coreomics_import import _conn as _base_conn                                  # noqa: E402

TABLE = "delimp_precursors"
# delimp_proteins is measured too, from 2026-09-23. The DIA-NN widening put ten columns there
# (PG.MaxLFQ and the protein/gene FDR family) because they are constant within (run, protein
# group) and so are not per-precursor facts. A writer regression in those ten would be invisible
# to a precursors-only audit -- which is the exact shape of the bug this file exists to catch.
# TABLE stays the primary one so the existing report, baseline key and --check keep their meaning.
TABLES = (TABLE, "delimp_proteins")
SAMPLE_ROWS = 50_000          # per search; bounds the scan on a multi-million-row search
REGRESSION_DROP = 0.20        # a column losing >20 points of coverage vs baseline is a regression

# Columns whose emptiness is a property of the INSTRUMENT or the SEARCH CONFIGURATION, not of the
# writer. The gate compares the baseline's search against whatever is NEWEST for that engine now,
# so these move legitimately the moment a different kind of experiment is ingested -- the first
# Orbitrap DIA-NN search would report `diann.im: 100% -> 0%` and exit 1. A gate that fires on
# correct behaviour teaches people to ignore it, which is worse than no gate.
#
# They are still MEASURED and still appear in the report; they just cannot fail the check.
#   im / iim  -- ion mobility: timsTOF has it, Orbitrap does not
#   mods      -- the jsonb; DIA-NN populates it, the Spectronaut adapter sets None on purpose
#   normalized_intensity -- NULL for Spectronaut by adapter design (refuses per-fragment areas)
#   irt       -- depends on whether the library carries iRT
#   site_localization_probability -- only when the search enabled PTM localization
#
# The 2026-09-23 DIA-NN widening adds a second reason a column can legitimately empty out: the
# ENGINE VERSION that produced the report. DIA-NN grew these columns over time, and FRAN's corpus
# spans 1.7.10 to 2.7.0, so "is this column populated?" depends on which release ran the search.
# Measured over every reachable DIA-NN report in the corpus:
#     2.6.1 / 2.7.0 (8.5M rows)   all 36 mapped columns present
#     2.3.0 / 2.5.1 (23.5M rows)  35/36 -- Averagine arrived in 2.6
#     1.9           (0.2M rows)   30/36
#     1.8.2         (0.3M rows)   26/36
# The gate compares the baseline's search against whatever is NEWEST for that engine, so ingesting
# one 2.3.0 search after a 2.7.0 one would report `diann.averagine: 100% -> 0%` and exit 1 on
# entirely correct behaviour. Listed here for the same reason `im` is: a gate that cries wolf
# gets ignored, and then it catches nothing.
_VERSION_DEPENDENT = frozenset({
    "averagine",                                          # DIA-NN 2.6+
    "best_fr_mz", "best_fr_mz_delta", "channel_evidence",  # absent in 1.8.2 and 1.9
    "ms1_apex_mz_delta",                                  # absent in 1.8.2 and 1.9
    "ms1_apex_area", "peptidoform_q_value", "global_peptidoform_q_value",  # absent in 1.8.2
})

CONFIG_DEPENDENT = frozenset({
    "im", "iim", "mods", "normalized_intensity", "irt", "site_localization_probability",
}) | _VERSION_DEPENDENT


def _conn(timeout_ms: int = 300_000):
    con = _base_conn()
    with con.cursor() as cur:
        cur.execute(f"SET statement_timeout = {int(timeout_ms)}")
    con.commit()
    return con


def columns(cur, table: str = TABLE) -> list[str]:
    cur.execute("""select column_name from information_schema.columns
                    where table_name=%s order by ordinal_position""", (table,))
    return [r[0] for r in cur.fetchall()]


def newest_per_engine(cur) -> list[tuple]:
    """The most recently ingested search for each engine — the regression surface."""
    cur.execute("""
        select distinct on (search_engine) search_engine, id, search_name, ingested_at::date
          from delimp_searches
         where search_engine is not null and ingested_at is not null
         order by search_engine, ingested_at desc""")
    return cur.fetchall()


def scoped_coverage(cur, search_id: str, cols: list[str], table: str = TABLE) -> dict:
    """Non-null fraction of every column within one search, bounded to SAMPLE_ROWS."""
    sel = ", ".join(f'count("{c}")' for c in cols)
    cur.execute(f"""select count(*), {sel} from (
                      select * from {table} where search_id = %s limit {SAMPLE_ROWS}) t""",
                (search_id,))
    row = cur.fetchone()
    total = row[0] or 0
    return {c: (row[i + 1] / total if total else None) for i, c in enumerate(cols)}, total


def corpus_null_frac(cur, table: str = TABLE) -> dict:
    """pg_stats view. Free, possibly stale — reported alongside its own staleness."""
    cur.execute("""select attname, null_frac from pg_stats
                    where schemaname='public' and tablename=%s""", (table,))
    return {r[0]: 1.0 - float(r[1]) for r in cur.fetchall()}


def build(cur) -> dict:
    cols = columns(cur)
    cur.execute("""select greatest(coalesce(last_analyze,'epoch'),
                                   coalesce(last_autoanalyze,'epoch'))::date
                     from pg_stat_user_tables where relname=%s""", (TABLE,))
    r = cur.fetchone()
    out = {
        "generated": datetime.date.today().isoformat(),
        "table": TABLE,
        "pg_stats_last_analyze": str(r[0]) if r and r[0] else None,
        "corpus_non_null_frac": corpus_null_frac(cur),
        "per_engine": {},
    }
    # Secondary tables get their own block, keyed by table name. A NEW top-level key rather than a
    # reshape of `per_engine`: check() reads `per_engine` and an older baseline that predates this
    # simply has no `per_table` to compare, which degrades to "not tracked yet" instead of a crash.
    out["per_table"] = {}
    for tbl in TABLES[1:]:
        tcols = columns(cur, tbl)
        blk = {}
        for engine, sid, name, ing in newest_per_engine(cur):
            frac, n = scoped_coverage(cur, sid, tcols, tbl)
            blk[engine] = {"search_id": str(sid), "search_name": name, "ingested": str(ing),
                           "rows_sampled": n, "non_null_frac": frac}
        out["per_table"][tbl] = blk
    for engine, sid, name, ing in newest_per_engine(cur):
        frac, n = scoped_coverage(cur, sid, cols)
        out["per_engine"][engine] = {
            "search_id": str(sid), "search_name": name, "ingested": str(ing),
            "rows_sampled": n, "non_null_frac": frac,
        }
    return out


def render(rep: dict) -> str:
    L = [f"# delimp_precursors column coverage — {rep['generated']}", "",
         f"pg_stats last ANALYZE: {rep['pg_stats_last_analyze']} "
         f"(corpus-wide numbers are only as fresh as this)", ""]
    for engine, blk in sorted(rep["per_engine"].items()):
        L += [f"## {engine} — newest search `{blk['search_name']}` "
              f"(ingested {blk['ingested']}, {blk['rows_sampled']:,} rows sampled)", ""]
        empty = sorted(c for c, f in blk["non_null_frac"].items() if f is not None and f == 0.0)
        part = sorted((c, f) for c, f in blk["non_null_frac"].items() if f and 0 < f < 1.0)
        full = sorted(c for c, f in blk["non_null_frac"].items() if f == 1.0)
        L += [f"- **fully populated ({len(full)})**: {', '.join(full) or '—'}",
              f"- **partial ({len(part)})**: " +
              (", ".join(f"{c} {f:.1%}" for c, f in part) or "—"),
              f"- **EMPTY ({len(empty)})**: {', '.join(empty) or '—'}", ""]
    for tbl, tblk in sorted(rep.get("per_table", {}).items()):
        L += [f"# {tbl} column coverage", ""]
        for engine, blk in sorted(tblk.items()):
            empty = sorted(c for c, f in blk["non_null_frac"].items() if f is not None and f == 0.0)
            part = sorted((c, f) for c, f in blk["non_null_frac"].items() if f and 0 < f < 1.0)
            full = sorted(c for c, f in blk["non_null_frac"].items() if f == 1.0)
            L += [f"## {tbl} / {engine} — `{blk['search_name']}` "
                  f"({blk['rows_sampled']:,} rows sampled)", "",
                  f"- **fully populated ({len(full)})**: {', '.join(full) or '—'}",
                  f"- **partial ({len(part)})**: " +
                  (", ".join(f"{c} {f:.1%}" for c, f in part) or "—"),
                  f"- **EMPTY ({len(empty)})**: {', '.join(empty) or '—'}", ""]
    return "\n".join(L)


def check(rep: dict, baseline: dict) -> int:
    """Fail when a WRITER-CONTROLLED column that was populated for an engine no longer is.

    Movement in a CONFIG_DEPENDENT column is reported but never fails: see that constant for why
    a gate that fires on a legitimate instrument change is worse than no gate at all.
    """
    bad, informational = [], []

    def compare(now_blocks: dict, base_blocks: dict, label: str) -> None:
        """One table's per-engine blocks against the baseline's. Appends to bad/informational."""
        for engine, blk in now_blocks.items():
            base = base_blocks.get(engine)
            if not base:
                print(f"  note: {label}{engine!r} absent from baseline — not a regression, "
                      f"refresh the baseline to start tracking it")
                continue
            same_search = base.get("search_id") == blk.get("search_id")
            for col, now in blk["non_null_frac"].items():
                was = base["non_null_frac"].get(col)
                if was is None or now is None or was - now <= REGRESSION_DROP:
                    continue
                line = (f"{label}{engine}.{col}: {was:.1%} -> {now:.1%} "
                        f"(baseline search {base['search_name']}, now {blk['search_name']})")
                # A drop within the SAME search is always the writer's doing — no instrument or
                # config changed underneath it — so it fails even for a config-dependent column.
                if col in CONFIG_DEPENDENT and not same_search:
                    informational.append(line)
                else:
                    bad.append(line)

    compare(rep["per_engine"], baseline.get("per_engine", {}), "")
    for tbl, tblk in rep.get("per_table", {}).items():
        compare(tblk, baseline.get("per_table", {}).get(tbl, {}), f"{tbl}/")
    if informational:
        print("instrument/config-dependent movement (NOT failing — see CONFIG_DEPENDENT):")
        for b in sorted(informational):
            print("  " + b)
    if bad:
        print("COVERAGE REGRESSION -- a column that used to be written is no longer being written:")
        for b in sorted(bad):
            print("  " + b)
        return 1
    print("no coverage regression against baseline")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="write the measured report here (use to refresh the baseline)")
    ap.add_argument("--check", help="diff against this baseline; exit 1 on regression")
    a = ap.parse_args()

    con = _conn(); cur = con.cursor()
    try:
        rep = build(cur)
    finally:
        con.rollback(); con.close()

    if a.json:
        with open(a.json, "w") as fh:
            json.dump(rep, fh, indent=1, sort_keys=True)
        print(f"wrote {a.json}")
    if a.check:
        with open(a.check) as fh:
            return check(rep, json.load(fh))
    if not a.json:
        print(render(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
