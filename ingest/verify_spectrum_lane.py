"""verify_spectrum_lane.py — integrity check for the observed-spectrum Lance lane.

The durability guarantee ("we won't lose the data") comes from the DB registry: every Lance
dataset is recorded in delimp_spectrum_lane with a content md5. This tool walks the registry and,
for each dataset, confirms the file exists AND its content md5 still matches — so a lost or corrupt
dataset is DETECTED (and can be re-derived from the archived report on Flinders). Run it after a
backfill and on a schedule.

    python verify_spectrum_lane.py                 # check every registered dataset
    python verify_spectrum_lane.py --limit 100     # spot check
    python verify_spectrum_lane.py --missing-only  # only report problems
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import spectrum_lance as sln   # noqa: E402


def _conn():
    from refresh_leaderboards import _token
    import psycopg2
    return psycopg2.connect(host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
                            dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
                            user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
                            password=_token(), sslmode="require", connect_timeout=30)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--missing-only", action="store_true")
    a = ap.parse_args()
    conn = _conn(); cur = conn.cursor()
    cur.execute(f"""SELECT search_name, lance_path, content_md5, n_precursors, n_fragments
                    FROM delimp_spectrum_lane ORDER BY ingested_at DESC
                    {'LIMIT %d' % a.limit if a.limit else ''}""")
    rows = cur.fetchall()
    print(f"{len(rows)} registered Lance dataset(s)")
    ok = missing = corrupt = 0
    for name, path, md5, npc, nf in rows:
        if not path or not os.path.exists(path):
            missing += 1
            print(f"  [MISSING] {name}: {path}"); continue
        try:
            good = sln.verify(path, md5) if md5 else True
        except Exception as e:  # noqa: BLE001
            corrupt += 1; print(f"  [CORRUPT] {name}: {str(e)[:80]}"); continue
        if good:
            ok += 1
            if not a.missing_only:
                print(f"  [ok] {name}: {npc:,} prec / {nf:,} frag")
        else:
            corrupt += 1
            print(f"  [CHECKSUM MISMATCH] {name}: {path}")
    print(f"\nDONE: {ok} ok, {missing} missing, {corrupt} corrupt/mismatch"
          + ("  -> re-run backfill_fragments.py on the affected searches" if (missing or corrupt) else ""))
    conn.close()
    sys.exit(1 if (missing or corrupt) else 0)


if __name__ == "__main__":
    main()
