"""migrate_qc_training_tables.py - create the FRAN-QC and training-split tables (2026-11-13).

Approved by Brett 2026-11-13 ("lets do it all"): the health scan's results go in the database so
the FRAN-QC page (/qc) can show them, and the training corpus gets the per-run facts the design doc
(docs/superpowers/specs/2026-11-12-training-data-retention-design.md, sections 2 and 4.6) lists as
missing: the isolation-window scheme, a stable train/val/test split by RAW RUN, and whether a
run's data may go into published model weights.

All four are NEW tables, so nothing existing is altered or locked. CREATE ... IF NOT EXISTS makes
this idempotent. lock_timeout stays short anyway: on 2026-07-29 an ungranted AccessExclusiveLock
queued behind a long read and stalled the live site.

    python migrate_qc_training_tables.py            # show the DDL
    python migrate_qc_training_tables.py --apply
"""
import argparse
import sys

DDL = [
    # ---- FRAN-QC -----------------------------------------------------------------------------
    """CREATE TABLE IF NOT EXISTS delimp_qc_scan (
        id          bigserial PRIMARY KEY,
        scanned_at  timestamptz NOT NULL DEFAULT now(),
        kind        text NOT NULL CHECK (kind IN ('daily', 'weekly', 'manual')),
        n_red       integer NOT NULL,
        n_amber     integer NOT NULL,
        findings    jsonb NOT NULL,
        metrics     jsonb NOT NULL DEFAULT '{}'::jsonb,
        job_id      text,
        host        text)""",
    "CREATE INDEX IF NOT EXISTS delimp_qc_scan_at ON delimp_qc_scan (scanned_at DESC)",
    """COMMENT ON TABLE delimp_qc_scan IS
       'One row per run of ingest/fran_health_scan.py: RED/AMBER findings and the measured metrics '
       '(fill rates by ingest window, per-search coverage, backlog, DB size, drip and SLURM state). '
       'Read by the staff-only FRAN-QC page (/qc).'""",
    """CREATE TABLE IF NOT EXISTS delimp_qc_event (
        id          bigserial PRIMARY KEY,
        at          timestamptz NOT NULL,
        source      text NOT NULL,
        kind        text NOT NULL,
        summary     text NOT NULL,
        detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
        dedupe_key  text UNIQUE)""",
    "CREATE INDEX IF NOT EXISTS delimp_qc_event_at ON delimp_qc_event (at DESC)",
    """COMMENT ON TABLE delimp_qc_event IS
       'What the self-healing layer DID: held jobs released / steered off bad nodes (slurm_heal), '
       'fill runs and failed steps (fran_fill), stranded or parked search sets (the DIA-NN drip). '
       'dedupe_key makes re-importing the same log line a no-op.'""",
    # ---- training: isolation-window scheme per physical raw file -----------------------------
    """CREATE TABLE IF NOT EXISTS delimp_run_windows (
        hive_path           text PRIMARY KEY,
        raw_basename        text NOT NULL,
        kind                text NOT NULL,
        instrument          text,
        dia_mz_lo           double precision,
        dia_mz_hi           double precision,
        dia_n_windows       integer,
        dia_width_med       double precision,
        windows             jsonb,
        collision_energies  jsonb,
        scheme_md5          text,
        note                text,
        source              text NOT NULL,
        probed_at           timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS delimp_run_windows_basename ON delimp_run_windows (raw_basename)",
    "CREATE INDEX IF NOT EXISTS delimp_run_windows_scheme ON delimp_run_windows (scheme_md5)",
    """COMMENT ON TABLE delimp_run_windows IS
       'The isolation-window scheme each raw file ACTUALLY acquired (probe_acquisition.py), not '
       'raw_files.mass_range_*. windows: Bruker = [{group, scan_begin, scan_end, mz_lo, mz_hi, ce}] '
       '(the TIMS scan range is the mobility slice); Thermo = [[mz_lo, mz_hi, n_scans_seen]]. '
       'scheme_md5 groups runs acquired with the same method. NULL windows + note = not DIA or '
       'unreadable, a real answer. Needed by DIA de novo / rescoring models (Cascadia, DIANovo).'""",
    # ---- training: split + shareability per RAW RUN ------------------------------------------
    """CREATE TABLE IF NOT EXISTS delimp_run_flags (
        run_key           text PRIMARY KEY,
        split             text NOT NULL CHECK (split IN ('train', 'val', 'test')),
        held_out          boolean GENERATED ALWAYS AS (split <> 'train') STORED,
        split_rule        text NOT NULL,
        shareable         text NOT NULL CHECK (shareable IN ('public', 'internal', 'client', 'unknown')),
        shareable_reason  text,
        set_at            timestamptz NOT NULL DEFAULT now(),
        updated_at        timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS delimp_run_flags_split ON delimp_run_flags (split)",
    """COMMENT ON TABLE delimp_run_flags IS
       'Per raw RUN (run_key = lower(raw_basename), extension stripped): a STABLE train/val/test '
       'split and whether models trained on the run may be published. Split by run, never by '
       'search or dataset: 30% of runs sit in 2+ lane datasets, so a dataset split leaks. split '
       'is assigned once and never rewritten (see build_run_flags.py). shareable: public = a '
       'public (PXD) dataset; internal = the core''s own QC / training runs; client = customer '
       'data; unknown = no evidence either way (treat as client).'""",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if not a.apply:
        print(";\n\n".join(DDL) + ";")
        return 0
    from coreomics_import import _conn
    conn = _conn()
    conn.autocommit = False
    with conn.cursor() as c:
        c.execute("SET lock_timeout = '5s'")
        c.execute("SET statement_timeout = '60s'")
        for stmt in DDL:
            c.execute(stmt)
            print("ok:", " ".join(stmt.split())[:90])
    conn.commit()
    with conn.cursor() as c:
        c.execute("""SELECT relname FROM pg_class WHERE relname IN
                     ('delimp_qc_scan','delimp_qc_event','delimp_run_windows','delimp_run_flags')
                     ORDER BY 1""")
        print("present:", [r[0] for r in c.fetchall()])
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
