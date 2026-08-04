"""build_protein_peptide_counts.py — precompute COUNT(DISTINCT stripped_seq) per protein_group.

WHY THIS CANNOT STAY IN THE REQUEST PATH. The gene page needs a TRUE distinct peptide count per
protein group (SUM(n_unique_peptides) is a sum of per-run counts and overstated by up to 56x). The
live query uses idx_prec_protein_group correctly, but its cost is dominated by heap I/O whose
locality varies wildly by gene -- and NOT by row count:

    ALB   45 groups  1,742,102 precursor rows ->  3.5s   @work_mem=256MB
    ACTB  27 groups    362,144 precursor rows -> 13.4s
    KRT1  14 groups    801,967 precursor rows -> 27.4s   <- half ALB's rows, 8x the time

Raising work_mem (app/db.py) took ALB from 31.6s to 3.5s by keeping the per-group sort in memory,
and that alone fixed ALB. It cannot fix KRT1, because there the time is random reads over a 138 GB
heap, not sorting. Any per-request bound therefore either times out on some genes -- rendering "—"
for every protein group, which is what the page did -- or holds a pooled connection for 27s.

So compute it ONCE, offline, and let the page read a 522k-row table.

DESIGN
  - Work list is delimp_mv_protein_agg (521,871 protein groups, already materialized) rather than a
    DISTINCT over 416M precursor rows, which times out on its own.
  - CHUNKED and RESUMABLE: each batch is one bounded query committed on its own, so the job can be
    killed and restarted, and it never holds a long transaction or a huge sort against the shared
    PG Farm cluster. Restart skips what is already computed.
  - Writes a real TABLE, not a materialized view: REFRESH MATERIALIZED VIEW would redo all 416M rows
    every time, while this can be topped up for just the protein groups a new ingest touched
    (--only-missing, the default).

    sbatch ingest/build_protein_peptide_counts.sbatch          # the full build
    python ingest/build_protein_peptide_counts.py --batch 400 --limit 2000   # a taste
"""
import argparse
import functools
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
print = functools.partial(print, flush=True)   # noqa: A001

DDL = """
CREATE TABLE IF NOT EXISTS delimp_protein_peptide_count (
    protein_group     text PRIMARY KEY,
    n_peptides        integer NOT NULL,
    n_precursor_rows  bigint,
    computed_at       timestamptz NOT NULL DEFAULT now()
)
"""


def _conn(timeout_ms=600000):
    import psycopg2
    from refresh_leaderboards import _token
    return psycopg2.connect(
        host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
        dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30,
        options=f"-c statement_timeout={timeout_ms}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=250,
                    help="protein groups per query; keep modest so one slow gene cannot stall a "
                         "huge batch")
    ap.add_argument("--limit", type=int, default=0, help="stop after N groups (0 = all)")
    ap.add_argument("--work-mem", default="256MB")
    ap.add_argument("--all", action="store_true",
                    help="recompute groups already present (default: only missing ones)")
    a = ap.parse_args()

    conn = _conn(); conn.autocommit = False
    cur = conn.cursor()
    cur.execute("SET LOCAL lock_timeout = '10s'")
    cur.execute(DDL)
    conn.commit()
    print("table delimp_protein_peptide_count ready")

    where = "" if a.all else """
        WHERE NOT EXISTS (SELECT 1 FROM delimp_protein_peptide_count c
                          WHERE c.protein_group = a.protein_group)"""
    cur.execute(f"SELECT protein_group FROM delimp_mv_protein_agg a {where} ORDER BY protein_group")
    todo = [r[0] for r in cur.fetchall()]
    if a.limit:
        todo = todo[:a.limit]
    cur.execute("SELECT count(*) FROM delimp_protein_peptide_count")
    have = cur.fetchone()[0]
    print(f"{len(todo):,} protein groups to compute ({have:,} already stored)")
    if not todo:
        print("nothing to do"); conn.close(); return

    t0 = time.time()
    done = 0
    slow = []
    for i in range(0, len(todo), a.batch):
        chunk = todo[i:i + a.batch]
        tb = time.time()
        try:
            cur.execute(f"SET work_mem = '{a.work_mem}'")
            cur.execute("""
                INSERT INTO delimp_protein_peptide_count
                       (protein_group, n_peptides, n_precursor_rows)
                SELECT protein_group, COUNT(DISTINCT stripped_seq), COUNT(*)
                FROM delimp_precursors
                WHERE protein_group = ANY(%s)
                GROUP BY protein_group
                ON CONFLICT (protein_group) DO UPDATE
                  SET n_peptides = EXCLUDED.n_peptides,
                      n_precursor_rows = EXCLUDED.n_precursor_rows,
                      computed_at = now()""", (chunk,))
            conn.commit()
        except Exception as e:                                    # noqa: BLE001
            # one pathological batch must not lose the whole run; it stays missing and a later
            # pass retries it, so this is a skip rather than a failure.
            conn.rollback()
            print(f"  batch {i//a.batch}: SKIPPED after {time.time()-tb:.0f}s "
                  f"{type(e).__name__}: {str(e)[:90]}")
            continue
        dt = time.time() - tb
        done += len(chunk)
        if dt > 60:
            slow.append((i, dt))
        rate = done / max(time.time() - t0, 1)
        eta = (len(todo) - done) / rate / 60 if rate else 0
        print(f"  {done:>7,}/{len(todo):,}  batch {dt:>5.1f}s  {rate:>5.1f} groups/s  ETA {eta:>5.1f} min")

    cur.execute("SELECT count(*), sum(n_peptides), max(n_peptides) FROM delimp_protein_peptide_count")
    n, s, mx = cur.fetchone()
    print(f"\nstored {n:,} protein groups; peptides summed {s:,}, max {mx:,}")
    print(f"{len(slow)} batches took over 60s")
    print(f"elapsed {(time.time()-t0)/60:.1f} min")

    import versions as V
    V.record_run(cur, "protein_peptide_counts", "1.0.0", notes=f"{n} groups")
    conn.commit(); conn.close()
    print("DONE")


if __name__ == "__main__":
    main()
