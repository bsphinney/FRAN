"""build_run_flags.py - give every raw RUN a stable train/val/test split and a shareability class.

Design doc section 4.6 (docs/superpowers/specs/2026-11-12-training-data-retention-design.md).

SPLIT BY RUN, AND ASSIGN IT ONCE. The same raw run appears in many searches and lane datasets (SN
re-exports, the DIA-NN corpus re-search, superset searches): 30% of runs sit in 2+ datasets. A
split by search or dataset therefore puts one run's spectra on both sides. The key is
run_key = lower(raw_basename), extension already stripped, so every copy and every search of a run
lands on the same side. Two different runs that happen to share a name also share a side, which is
conservative (it can only reduce leakage).

split = md5(run_key) as an integer mod 100: 0-4 test, 5-9 val, 10-99 train ("v1"). Deterministic,
so a rebuilt table gives the same answer. Never rewritten: the upsert below leaves split alone on
conflict. A model evaluated on "test" must still be evaluated on the same runs next year.

SHAREABLE decides whether model WEIGHTS trained on the run may be published. Precedence is
deliberately conservative -- the first rule that matches wins:
  client    any search of the run carries customer provenance (client / PI / service customer /
            CoreOmics submission), or a raw copy lives under a service directory
  public    a search of the run is a public dataset (pride_accession, sharing_status 'public',
            or a PXD accession in its name or path)
  internal  the core's own QC / teaching runs (hela_qcs, shortcourse, training_DDA_data)
  unknown   no evidence either way -- treat as client
shareable IS refreshed on every run (evidence accumulates as provenance is filled).

    python build_run_flags.py            # dry run: distribution, nothing written
    python build_run_flags.py --apply
"""
import argparse
import sys

SPLIT_RULE = "v1: int(md5(lower(raw_basename))[0:8],16) % 100 -> 0-4 test, 5-9 val, else train"

EVIDENCE_SQL = r"""
WITH runs AS (
    SELECT lower(r.raw_basename) AS run_key,
           bool_or(coalesce(r.raw_path, '') ~* '/(service|SERVICE)/'
                   OR coalesce(r.hive_path, '') ~* '/(service|SERVICE)/')               AS svc_path,
           bool_or(coalesce(r.raw_path, '') || ' ' || coalesce(r.hive_path, '')
                   ~* '(hela_qcs|shortcourse|training_DDA_data)')                       AS own_path,
           bool_or(coalesce(r.raw_path, '') || ' ' || coalesce(r.hive_path, '')
                   ~ 'PXD[0-9]{6}')                                                       AS pxd_path
      FROM raw_files r
     WHERE nullif(btrim(r.raw_basename), '') IS NOT NULL
     GROUP BY 1),
srch AS (
    SELECT lower(r.raw_basename) AS run_key,
           bool_or(nullif(btrim(p.client), '') IS NOT NULL
                   OR nullif(btrim(p.pi), '') IS NOT NULL
                   OR nullif(btrim(p.service_customer), '') IS NOT NULL
                   OR nullif(btrim(p.coreomics_submission_id), '') IS NOT NULL)          AS cust,
           bool_or(nullif(btrim(s.pride_accession), '') IS NOT NULL
                   OR s.sharing_status = 'public'
                   OR s.search_name ~ 'PXD[0-9]{6}' OR s.output_dir ~ 'PXD[0-9]{6}')     AS pub
      FROM raw_files r
      JOIN search_raw_files sr ON sr.raw_path = r.raw_path
      JOIN delimp_searches s ON s.id = sr.search_id
      LEFT JOIN delimp_search_provenance p ON p.search_id = s.id
     WHERE nullif(btrim(r.raw_basename), '') IS NOT NULL
     GROUP BY 1)
SELECT runs.run_key, runs.svc_path, runs.own_path, runs.pxd_path,
       coalesce(srch.cust, false), coalesce(srch.pub, false)
  FROM runs LEFT JOIN srch USING (run_key)
"""


def split_of(run_key):
    import hashlib
    v = int(hashlib.md5(run_key.encode("utf-8")).hexdigest()[:8], 16) % 100
    return "test" if v < 5 else "val" if v < 10 else "train"


def shareable_of(svc_path, own_path, pxd_path, cust, pub):
    if cust:
        return "client", "a search of this run carries customer provenance"
    if svc_path:
        return "client", "a raw copy lives under a service directory"
    if pub or pxd_path:
        return "public", "public dataset (PRIDE/PXD accession or sharing_status public)"
    if own_path:
        return "internal", "core QC / teaching run (hela_qcs, shortcourse, training_DDA_data)"
    return "unknown", "no provenance either way; treat as client"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    from coreomics_import import _conn
    conn = _conn()
    with conn.cursor() as c:
        c.execute("SET statement_timeout = '20min'")
        c.execute(EVIDENCE_SQL)
        rows = c.fetchall()
    out = []
    dist = {}
    for run_key, svc, own, pxd, cust, pub in rows:
        sp = split_of(run_key)
        sh, why = shareable_of(svc, own, pxd, cust, pub)
        out.append((run_key, sp, SPLIT_RULE, sh, why))
        dist[(sp, sh)] = dist.get((sp, sh), 0) + 1
    print(f"{len(out):,} runs")
    for (sp, sh), n in sorted(dist.items()):
        print(f"  {sp:5s} {sh:8s} {n:7,}")
    if not a.apply:
        print("dry run: nothing written (--apply to write)")
        return 0
    from psycopg2.extras import execute_values
    with conn.cursor() as c:
        c.execute("SET lock_timeout = '5s'")
        execute_values(c, """
            INSERT INTO delimp_run_flags (run_key, split, split_rule, shareable, shareable_reason)
            VALUES %s
            ON CONFLICT (run_key) DO UPDATE
               SET shareable = EXCLUDED.shareable,
                   shareable_reason = EXCLUDED.shareable_reason,
                   updated_at = now()
             WHERE (delimp_run_flags.shareable, delimp_run_flags.shareable_reason)
                   IS DISTINCT FROM (EXCLUDED.shareable, EXCLUDED.shareable_reason)""",
                       out, page_size=2000)
    conn.commit()
    with conn.cursor() as c:
        c.execute("SELECT split, shareable, count(*) FROM delimp_run_flags GROUP BY 1, 2 ORDER BY 1, 2")
        print("in table:", c.fetchall())
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
