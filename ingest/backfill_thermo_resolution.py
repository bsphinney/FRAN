"""backfill_thermo_resolution.py — fill raw_files.ms1_resolution / ms2_resolution for Thermo runs.

A DEDICATED pass rather than `record_raw_metadata.py --thermo`, on purpose. That tool re-reads the
whole header through ThermoRawFileParser at seconds per file -- roughly 6-9 hours over the ~6,900
Thermo raws -- and would rewrite model/serial/date values that are already correct. Resolution is
the only field that is actually new, and it comes from the scan trailers, which TRFP never emits.
Reading only that takes well under a second per file once CoreCLR is loaded, so this is ~1 h.

It drives ingest/thermo_resolution.py as ONE long-lived child process and streams paths to it, so
the .NET runtime loads once for the whole corpus instead of once per file. pythonnet is never
imported here.

WHAT IT REPAIRS. Every Thermo row FRAN has ever written carries ms2_resolution = 0: read_thermo
used to map TRFP's `mass resolution` -- a constant 0.5 placeholder, not resolving power -- through
int(round(0.5)). Measured 2026-09-24 before this existed: 0 on 6,992 rows, NULL on 18,581, not one
plausible value corpus-wide, and ms1_resolution NULL on all 25,573. So this OVERWRITES stored
values below 1000 rather than COALESCE-ing, which would preserve the 0 forever (0 is not NULL).
No real Orbitrap setting is under 1000 -- the corpus runs 15,000-120,000.

Verified 2026-09-24: Fusion Lumos 60000/15000, Exploris 480 DIA 120000/15000, both agreeing with
the resolutions the DIA-NN acquisition probe measured independently.

    python ingest/backfill_thermo_resolution.py --dry-run
    python ingest/backfill_thermo_resolution.py --apply
"""
import argparse
import functools
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/quobyte/proteomics-grp/brett/glendon/fran_ingest")
import plan_spectrum_backfill as P                                  # noqa: E402

print = functools.partial(print, flush=True)                        # noqa: A001

READER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "thermo_resolution.py")
RES_PY = os.environ.get("FRAN_THERMO_RES_PYTHON",
                        os.path.expanduser("~/trfp_probe/pyn/bin/python"))
DLL_DIR = os.environ.get("FRAN_THERMO_DLL_DIR",
                         "/quobyte/proteomics-grp/tools/ThermoRawFileParser")

SELECT = """
SELECT DISTINCT hive_path FROM raw_files
WHERE hive_path ILIKE '%%.raw' AND hive_path <> ''
  AND (ms1_resolution IS NULL OR ms2_resolution IS NULL OR ms2_resolution < 1000
       OR cycle_time_sec IS NULL)
"""

# Why cycle_time_sec is written here, kept OUT of the SQL string on purpose:
# psycopg2 reads % as parameter binding, so a "11.2%" inside a SQL comment silently changes the
# placeholder count and execute() dies with "tuple index out of range". That is exactly what
# happened when this text lived in the string, and it is the same fault as commit ba7a0b0, which
# 500'd species search from a literal % in the peptide-forms SQL. Prose lives in Python comments.
#
# cycle_time_sec sat at 34.4 percent on orbitrap rows against 98.6 on timsTOF, because read_thermo
# cannot derive it: TRFP's -m 0 output has no scan times, and the only estimate it allows
# (MS max RT x 60 / n_MS1) measured 11.2 percent mean error over 3,379 runs. The trailer reader
# opens the file anyway, so the median gap between consecutive MS1 SCANS is free -- and measured
# 0.4 to 1.4 percent against Spectronaut's own value.
#
# Repair rule, same as elsewhere: fill a gap, replace an implausible >20 s value (a stripped
# decimal separator), never overwrite a good reading.
UPDATE = """
UPDATE raw_files SET
  ms1_resolution = CASE WHEN ms1_resolution IS NULL OR ms1_resolution < 1000
                        THEN COALESCE(%s, ms1_resolution) ELSE ms1_resolution END,
  ms2_resolution = CASE WHEN ms2_resolution IS NULL OR ms2_resolution < 1000
                        THEN COALESCE(%s, ms2_resolution) ELSE ms2_resolution END,
  cycle_time_sec = CASE WHEN cycle_time_sec IS NULL OR cycle_time_sec > 20
                        THEN COALESCE(%s, cycle_time_sec) ELSE cycle_time_sec END
WHERE hive_path = %s
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="sample N files (for a timing check)")
    # Sharding, because this is I/O-bound on the share, not CPU-bound: measured 2.7 s/file on cold
    # corpus reads, so ~5 h in one process over ~6,900 raws and well under an hour split 8 ways.
    # Sharded on a STABLE SORT of the path list, so shards are disjoint and every file is covered
    # exactly once no matter which order the DB returns rows in.
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    a = ap.parse_args()

    if not os.path.exists(RES_PY):
        print(f"no pythonnet python at {RES_PY} -- set FRAN_THERMO_RES_PYTHON. Nothing done.")
        return 1

    c = P._conn(); c.autocommit = False; cur = c.cursor()
    cur.execute(SELECT + (" LIMIT %s" % a.limit if a.limit else ""))
    paths = sorted(r[0] for r in cur.fetchall())
    total = len(paths)
    if a.shards > 1:
        paths = paths[a.shard::a.shards]
    print(f"{total:,} distinct Thermo raws need a resolution; "
          f"shard {a.shard}/{a.shards} takes {len(paths):,}. apply={a.apply}")
    if not paths:
        c.close(); return 0

    # One child for the whole corpus: CoreCLR loads once, then it is a streaming pipe.
    proc = subprocess.Popen([RES_PY, READER, "--dll-dir", DLL_DIR, "--from-stdin"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True, bufsize=1)
    proc.stdin.write("\n".join(paths) + "\n")
    proc.stdin.close()

    got = mixed = none = written = 0
    for line in proc.stdout:
        line = line.strip()
        if not line.startswith("{"):
            continue
        d = json.loads(line)
        ms1, ms2 = d.get("ms1_resolution"), d.get("ms2_resolution")
        cyc, note = d.get("cycle_time_sec"), d.get("note")
        if note and "distinct values" in note:
            mixed += 1
            print(f"  MIXED {os.path.basename(d['path'])[:54]}: {note}")
        if ms1 is None and ms2 is None and cyc is None:
            none += 1
            continue
        got += 1
        if a.apply:
            cur.execute(UPDATE, (ms1, ms2, cyc, d["path"]))
            written += cur.rowcount
        if got % 500 == 0:
            if a.apply:
                c.commit()
            print(f"  [{got:,}] read ok, {written:,} rows written, {none:,} without a value")
    proc.wait()
    if a.apply:
        c.commit()
    print(f"\nDONE: {got:,} files yielded a resolution, {none:,} did not, "
          f"{mixed:,} had MIXED settings (left NULL on purpose), {written:,} raw_files rows updated.")
    if not a.apply:
        print("(dry run -- nothing written)")
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
