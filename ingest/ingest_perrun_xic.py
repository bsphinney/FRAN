"""ingest_perrun_xic.py — load genuine PER-RUN XICs from an xic_lance dataset.

Answers Request 4 of FRAN_XIC_SCHEMA_FIX_BRIEF.md: the engine side needs trace-level validation of
their extractor against a stored reference for the SAME acquisition, and could only compare against
cross-run averages. The data already existed as Lance
(glendon/xic_lance/Dog_yeast_entrapment_SN21.xic.lance, 18,287 precursors covering
11May2026_DIA_60spd_VER_185_S2-B5_1_21766, the engine benchmark file) -- it was simply never in the
corpus as per-run rows.

TWO THINGS THAT MAKE THESE ROWS DIFFERENT FROM EVERY EXISTING ROW, both made explicit rather than
left to be discovered:

* `trace_rt_basis = 'absolute'`. The consensus lane's trace rt values are RELATIVE minutes from the
  apex, on a [-0.5, +0.5] grid, because runs are apex-aligned before averaging. These rows carry the
  acquisition's real absolute retention times straight from Spectronaut's XIC-DB. Mixing the two
  conventions silently is precisely the class of error that produced a 388-second offset, so the
  basis is a column, not a convention to remember.
* `precursor_id` is RUN-SCOPED: '<modified_seq><charge>@<run>'. The table's PK is precursor_id, and
  the existing convention '<modified_seq><charge>' is only unique because there is at most one
  consensus row per precursor. A per-run row for a precursor that also has a consensus row would
  collide and the ON CONFLICT would DESTROY the consensus row. No existing id contains '@' (verified,
  0 of 264,275), so the suffix is unambiguous.

    python ingest/ingest_perrun_xic.py <dataset.xic.lance>            # dry run
    python ingest/ingest_perrun_xic.py <dataset.xic.lance> --apply
"""
import argparse
import functools
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
print = functools.partial(print, flush=True)   # noqa: A001

DEFAULT_DS = "/quobyte/proteomics-grp/brett/glendon/xic_lance/Dog_yeast_entrapment_SN21.xic.lance"


def _conn():
    import psycopg2
    from refresh_leaderboards import _token
    return psycopg2.connect(
        host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
        dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30,
        options="-c statement_timeout=600000")


def build_rows(ds_path, limit=0):
    """Lance -> rows shaped like delimp_precursor_xic, keeping absolute RTs."""
    import lance
    ds = lance.dataset(ds_path)
    cols = ["search_id", "search_name", "run", "raw_path", "stripped_seq", "modified_seq", "charge",
            "precursor_mz", "rt", "q_value", "is_decoy", "n_ms1", "n_ms2",
            "trace_label", "trace_ms_level", "trace_rt", "trace_intensity"]
    have = [c for c in cols if c in ds.schema.names]
    t = ds.scanner(columns=have, limit=limit or None).to_table().to_pylist()
    rows = []
    for r in t:
        if r.get("is_decoy"):
            continue                      # decoys must never leak into a public lane
        run = r.get("run")
        mseq = r.get("modified_seq") or r.get("stripped_seq")
        ch = int(r.get("charge") or 0)
        if not run or not mseq or not ch:
            continue
        labels = r.get("trace_label") or []
        lvls = r.get("trace_ms_level") or []
        rts = r.get("trace_rt") or []
        ints = r.get("trace_intensity") or []

        ms1_trace, frags = [], []
        for i, lab in enumerate(labels):
            rt_i = list(rts[i]) if i < len(rts) and rts[i] is not None else []
            in_i = list(ints[i]) if i < len(ints) and ints[i] is not None else []
            if not rt_i or not in_i:
                continue
            pts = [{"rt": round(float(a), 5), "i": float(b)} for a, b in zip(rt_i, in_i)]
            apex = max((p["i"] for p in pts), default=0.0)
            lvl = lvls[i] if i < len(lvls) else None
            if lvl == 1 or str(lab).lower().startswith("ms1"):
                ms1_trace = pts
                continue
            frags.append({"label": str(lab), "apex": apex, "trace": pts})
        if not frags and not ms1_trace:
            continue
        # rel_intensity against the strongest fragment, matching the consensus lane's convention
        top = max((f["apex"] for f in frags), default=0.0) or 1.0
        for f in frags:
            f["rel_intensity"] = round(f["apex"] / top, 6)
        ms1_apex = max((p["i"] for p in ms1_trace), default=0.0)
        # rt_apex from THIS run's own MS1 trace -- a real absolute RT for a named acquisition,
        # unlike the consensus lane where it is whichever run happened to be most intense.
        rt_apex = (max(ms1_trace, key=lambda p: p["i"])["rt"] if ms1_trace
                   else (float(r["rt"]) if r.get("rt") is not None else None))
        rows.append((
            f"{mseq}{ch}@{run}", r.get("stripped_seq"), ch,
            float(r["precursor_mz"]) if r.get("precursor_mz") is not None else None,
            f"per-run: {run}", str(r.get("search_id") or r.get("search_name") or "")[:200],
            "spectronaut", None, rt_apex, ms1_apex,
            json.dumps(ms1_trace), json.dumps(frags), len(frags),
            False, 1, mseq, run, "absolute"))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset", nargs="?", default=DEFAULT_DS)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    if not os.path.isdir(a.dataset):
        sys.exit(f"no such Lance dataset: {a.dataset}")
    rows = build_rows(a.dataset, a.limit)
    print(f"{len(rows):,} per-run precursor rows built from {os.path.basename(a.dataset)}")
    if not rows:
        sys.exit("nothing to ingest")
    runs = {r[16] for r in rows}
    print(f"runs covered: {len(runs)} -> {sorted(runs)[:3]}")
    nfrag = sum(r[12] for r in rows)
    print(f"fragments: {nfrag:,}  (mean {nfrag/len(rows):.1f} per precursor)")
    ex = rows[0]
    tr = json.loads(ex[11])[0]["trace"] if json.loads(ex[11]) else []
    if tr:
        print(f"absolute-RT check: first trace spans {tr[0]['rt']:.3f}..{tr[-1]['rt']:.3f} min "
              f"(consensus rows span -0.5..+0.5 by contrast)")

    conn = _conn(); conn.autocommit = False
    cur = conn.cursor()
    cur.execute("SET LOCAL lock_timeout = '10s'")
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_name='delimp_precursor_xic'""")
    have = {r[0] for r in cur.fetchall()}
    need = {"is_consensus", "n_runs_averaged", "modified_seq", "run"}
    if not need.issubset(have):
        sys.exit(f"run ingest/fix_xic_schema.py --apply first; missing {sorted(need - have)}")

    if not a.apply:
        print("\nDRY RUN — re-run with --apply. Inserts NEW run-scoped precursor_ids "
              "('<modified_seq><charge>@<run>'); no existing row is touched.")
        conn.rollback(); conn.close(); return

    if "trace_rt_basis" not in have:
        cur.execute("ALTER TABLE delimp_precursor_xic ADD COLUMN IF NOT EXISTS trace_rt_basis text")
        conn.commit()
        cur.execute("""UPDATE delimp_precursor_xic SET trace_rt_basis='relative_to_apex'
                       WHERE trace_rt_basis IS NULL""")
        print(f"marked {cur.rowcount:,} existing rows trace_rt_basis='relative_to_apex'")
        cur.execute("""COMMENT ON COLUMN delimp_precursor_xic.trace_rt_basis IS
          'What the rt values inside ms1/fragments[].trace MEAN. ''relative_to_apex'': minutes from '
          'the apex on a [-0.5,+0.5] grid (every consensus row -- runs are apex-aligned before '
          'averaging). ''absolute'': the acquisition''s real retention time (per-run rows ingested '
          'from an xic_lance dataset). NEVER compare the two without converting; mixing them is what '
          'produced a 388-second offset for one consumer.'""")
        conn.commit()

    import psycopg2.extras
    psycopg2.extras.execute_values(cur, """
        INSERT INTO delimp_precursor_xic
          (precursor_id, stripped_seq, charge, precursor_mz, raw_path, search_id, engine,
           engine_version, rt_apex, ms1_apex, ms1, fragments, n_fragments_total,
           is_consensus, n_runs_averaged, modified_seq, run, trace_rt_basis)
        VALUES %s
        ON CONFLICT (precursor_id) DO UPDATE SET
          rt_apex=EXCLUDED.rt_apex, ms1_apex=EXCLUDED.ms1_apex, ms1=EXCLUDED.ms1,
          fragments=EXCLUDED.fragments, n_fragments_total=EXCLUDED.n_fragments_total,
          trace_rt_basis=EXCLUDED.trace_rt_basis""", rows, page_size=200)
    print(f"upserted {len(rows):,} per-run rows")
    conn.commit()

    cur.execute("""SELECT count(*), count(DISTINCT run) FROM delimp_precursor_xic
                   WHERE trace_rt_basis='absolute'""")
    n, nr = cur.fetchone()
    print(f"verify: {n:,} absolute-RT per-run rows across {nr} runs")
    cur.execute("""SELECT count(*) FROM delimp_precursor_xic
                   WHERE trace_rt_basis='absolute' AND is_consensus""")
    bad = cur.fetchone()[0]
    print(f"check: per-run rows wrongly flagged consensus: {bad:,}")
    cur.execute("SELECT count(*) FROM delimp_precursor_xic WHERE trace_rt_basis IS NULL")
    print(f"check: rows with no declared rt basis: {cur.fetchone()[0]:,}")

    import versions as V
    V.record_run(cur, "perrun_xic_ingest", "1.0.0", notes=f"{len(rows)} rows, runs={sorted(runs)}")
    conn.commit(); conn.close()
    print("DONE")


if __name__ == "__main__":
    main()
