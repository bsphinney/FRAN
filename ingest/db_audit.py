"""db_audit.py — hunt for data-integrity issues in the FRAN corpus DB. READ-ONLY.

Prompted by finding raw_files.platform systematically mislabeled (timstof/diaPASEF rows that are
actually Thermo .raw). Checks the same class of problem elsewhere: metadata that disagrees with
ground truth, NULL-that-should-not-be, broken cross-table links, and out-of-range values. Uses
pg_stats (no scan) for the big precursor table so it stays cheap.

    python db_audit.py
"""
from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plan_spectrum_backfill as P

FIND = []


def add(sev, tbl, msg):
    FIND.append((sev, tbl, msg))


def q1(cur, sql, params=None):
    if params:
        cur.execute(sql, params)
    else:
        cur.execute(sql)  # no params -> psycopg2 leaves literal % (e.g. ilike '%.raw') alone
    r = cur.fetchone()
    return r[0] if r else None


def main():
    c = P._conn(); c.autocommit = True
    cur = c.cursor()
    cur.execute("SET statement_timeout='120000'")

    # ---- raw_files: format vs recorded metadata (the known bug class) ----
    n = q1(cur, "select count(*) from raw_files where platform='timstof' and raw_path ilike '%.raw'")
    if n: add("HIGH", "raw_files", f"{n:,} rows platform=timstof but path is a Thermo .raw")
    n = q1(cur, "select count(*) from raw_files where platform='orbitrap' and raw_path ilike '%.d'")
    if n: add("HIGH", "raw_files", f"{n:,} rows platform=orbitrap but path is a Bruker .d")
    n = q1(cur, "select count(*) from raw_files where acquisition_method ilike '%pasef%' and raw_path ilike '%.raw'")
    if n: add("HIGH", "raw_files", f"{n:,} rows acquisition_method=PASEF (Bruker-only) on a Thermo .raw")
    n = q1(cur, "select count(*) from raw_files where acquisition_method ilike '%pasef%' and platform<>'timstof'")
    if n: add("MED", "raw_files", f"{n:,} rows have a PASEF method but platform<>timstof")
    print("platform vs true file format:")
    cur.execute("""select case when raw_path ilike '%.raw' then 'thermo(.raw)' when raw_path ilike '%.d'
                   then 'bruker(.d)' else 'other' end fmt, platform, count(*) from raw_files
                   group by 1,2 order by 3 desc""")
    for fmt, plat, cnt in cur.fetchall():
        print(f"   {fmt:14} platform={plat}  {cnt:,}")

    # ---- raw_files: columns that are entirely / mostly NULL ----
    tot = q1(cur, "select count(*) from raw_files")
    for col in ("instrument_model", "instrument_serial", "md5", "hive_path", "acquisition_date",
                "gradient_minutes", "nce", "ms2_resolution"):
        nn = q1(cur, f"select count(*) from raw_files where {col} is null or ({col})::text=''")
        if nn == tot:
            add("MED", "raw_files", f"{col} is NULL for ALL {tot:,} rows (never populated)")
        elif nn > tot * 0.6:
            add("LOW", "raw_files", f"{col} NULL for {nn:,}/{tot:,} rows")

    # ---- searches ----
    cur.execute("select sharing_status, count(*) from delimp_searches group by 1 order by 2 desc")
    ss = cur.fetchall()
    if len(ss) == 1:
        add("MED", "delimp_searches", f"sharing_status is '{ss[0][0]}' for ALL {ss[0][1]:,} searches (no per-search sharing set)")
    n = q1(cur, "select count(*) from delimp_searches where n_precursors_total is null or n_precursors_total=0")
    if n: add("MED", "delimp_searches", f"{n:,} searches have NULL/0 n_precursors_total")
    n = q1(cur, "select count(*) from delimp_searches where search_engine is null or search_engine=''")
    if n: add("LOW", "delimp_searches", f"{n:,} searches have no search_engine")

    # ---- spectrum-lane linkage ----
    n = q1(cur, "select count(*) from delimp_spectrum_lane where search_id is null")
    if n: add("LOW", "delimp_spectrum_lane", f"{n:,} datasets still have NULL search_id (unlinked)")
    n = q1(cur, """select count(*) from delimp_spectrum_lane l where l.search_id is not null
                   and not exists (select 1 from delimp_searches s where s.id=l.search_id)""")
    if n: add("HIGH", "delimp_spectrum_lane", f"{n:,} datasets point to a search_id that doesn't exist in delimp_searches")

    # ---- precursor_xic linkage (search_id is TEXT, not the uuid FK) ----
    n = q1(cur, """select count(*) from (select distinct search_id from delimp_precursor_xic) t
                   where search_id !~ '^[0-9a-fA-F]{8}-'""")
    tt = q1(cur, "select count(distinct search_id) from delimp_precursor_xic")
    if n: add("MED", "delimp_precursor_xic", f"{n:,}/{tt:,} distinct search_id values are name-slugs, not uuids — won't join to delimp_searches")

    # ---- value ranges from pg_stats (no scan) ----
    print("\nprecursor value ranges (from planner stats, approximate):")
    cur.execute("""select attname, histogram_bounds::text from pg_stats
                   where tablename='delimp_precursors' and attname in
                   ('q_value','charge','precursor_mz','rt','irt','pep','best_q_value')""")
    for name, hb in cur.fetchall():
        if not hb:
            continue
        vals = hb.strip("{}").split(",")
        lo, hi = vals[0], vals[-1]
        print(f"   {name:14} ~[{lo} .. {hi}]")
        if "NaN" in (lo, hi) or "nan" in hb.lower():
            add("HIGH", "delimp_precursors", f"{name} contains NaN values (invalid for a score/measurement)")
        try:
            lof, hif = float(lo), float(hi)
            if name in ("q_value", "pep", "best_q_value") and (lof < 0 or hif > 1):
                add("MED", "delimp_precursors", f"{name} outside [0,1]: seen ~[{lo}..{hi}]")
            if name == "charge" and (lof < 1 or hif > 12):
                add("MED", "delimp_precursors", f"charge out of plausible range: ~[{lo}..{hi}]")
            if name in ("precursor_mz", "rt") and lof < 0:
                add("MED", "delimp_precursors", f"{name} has negative values: ~[{lo}..{hi}]")
            if name == "irt" and (lof < -500 or hif > 1000):
                add("LOW", "delimp_precursors", f"irt has extreme outliers: ~[{lo}..{hi}] (known stray -2900/3e12)")
        except ValueError:
            pass

    # ---- raw_files rows per physical raw (raw_basename repeats = same raw across searches) ----
    n = q1(cur, """select count(*) from (select raw_basename from raw_files
                   where raw_basename is not null group by 1 having count(*)>1) t""")
    dup_rows = q1(cur, "select count(*) from raw_files")
    dist_bn = q1(cur, "select count(distinct raw_basename) from raw_files where raw_basename is not null")
    if dist_bn and dup_rows and dist_bn < dup_rows:
        add("LOW", "raw_files", f"{dup_rows:,} rows but only {dist_bn:,} distinct raw_basenames "
            f"(a raw stored once per search that used it — expected, but no unique raw table)")

    print("\n================ FINDINGS (by severity) ================")
    order = {"HIGH": 0, "MED": 1, "LOW": 2}
    for sev, tbl, msg in sorted(FIND, key=lambda f: order[f[0]]):
        print(f"  [{sev:4}] {tbl}: {msg}")
    print(f"\n{len(FIND)} findings.")
    c.close()


if __name__ == "__main__":
    main()
