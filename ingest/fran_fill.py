"""fran_fill.py - run FRAN's metadata fillers, in dependency order, on whatever is still missing.

WHY THIS EXISTS. corpus_ingest.py writes a search's core rows. A dozen separate scripts fill the
rest -- where the raw file is, what instrument made it, its resolution, the organism taxon, the
per-search PTM rollup, the multi-engine confirmation count -- and NOTHING CALLED THEM. They were
run by hand during the spring backfills, so the corpus looked complete until the DIA-NN drip began
ingesting hundreds of searches a month. Measured 2026-11-13 by ingest month, Jun -> Nov:
raw_files.hive_path 0.99 -> 0, instrument_model / acquisition_date 0.99 -> 0.03-0.10,
cycle_time_sec 0.98 -> 0, PTM rollup ~100% -> 0 of ~640 searches.

None of the fillers takes a list of search ids; each one selects "rows still missing X" and is
idempotent (COALESCE, or only-NULL updates). So a scheduled pass over everything missing IS the
post-ingest hook, without editing the refuse-gated corpus_ingest.py.

STEPS (order is load-bearing -- see each script's docstring):
  1 resolve_raw_hive_paths        hive_path for rows that have none (filesystem walk)
  2 record_raw_metadata --bruker  instrument / acquisition facts from each .d   (needs 1)
  3 record_raw_metadata --thermo  the same from each .raw                       (needs 1)
  4 backfill_thermo_resolution    ms1/ms2 resolution + cycle time for .raw      (needs 1)
  5 backfill_raw_metadata_from_siblings  copy from a same-basename row -- MUST be after 2-4, or it
                                  fills the NULL with a cousin's value and blocks the real read
  6 backfill_organism_taxon, 7 backfill_organism_taxid   sample_metadata.organism_taxon_id
  8 engines_confirming            refresh_for() on the raw basenames of recently ingested searches
                                  (its docstring says corpus_ingest calls it; nothing does)
  9 refresh_search_ptm            per-search PTM rollup for searches that have none
 --weekly adds:
 10 predict_tissue, 11 predict_blood_fraction   whole-corpus rescoring (scores depend on the
                                  corpus-wide universe, so per-batch runs would be wasted)

NOT HERE, deliberately: the peptide consensus (no builder exists in the repo -- the table was
filled ad hoc), lane linking (the DIA-NN drip ingests --no-fragments, so there is no lane to link),
backfill_run_overview / backfill_run_design (Spectronaut-only and DDL on --apply), one-off repairs
and index builds.

A step that fails is recorded and the next one still runs: a broken Thermo reader must not stop
the PTM rollup. The status JSON (one file per run -- one writer per file on Quobyte) is what
fran_health_scan.py reads to tell whether the fillers are actually running.

    python fran_fill.py [--weekly] [--dry-run] [--only STEP,...] [--skip STEP,...] [--status-dir DIR]
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS_DIR = "/quobyte/proteomics-grp/brett/scratch/fran_health/fill"
RECENT_DAYS = 10      # engines_confirming: basenames of searches ingested in this window

# (name, argv after the script path, timeout seconds, weekly_only). Script = name before any ":".
# Where raws live on Hive. resolve_raw_hive_paths' own default is only the first and a to-hive
# drop box, which is part of why hive_path stopped filling: the drip's runs live under SERVICE/Data.
RAW_ROOTS = ["/nfs/lssc0/flinders/proteomics/Data", "/quobyte/proteomics-grp/to-hive",
             "/quobyte/proteomics-grp/SERVICE", "/quobyte/proteomics-grp/Data",
             "/quobyte/proteomics-grp/MRS", "/quobyte/proteomics-grp/hela_qcs",
             "/quobyte/proteomics-grp/training_DDA_data",
             "/quobyte/proteomics-grp/2026_shortcourse_data"]

STEPS = [
    ("resolve_raw_hive_paths", [x for r in RAW_ROOTS for x in ("--root", r)], 6 * 3600, False),
    ("record_raw_metadata:bruker", ["--bruker", "--only-missing", "--apply"], 10 * 3600, False),
    ("record_raw_metadata:thermo", ["--thermo", "--only-missing", "--apply"], 10 * 3600, False),
    ("backfill_thermo_resolution", ["--apply"], 6 * 3600, False),
    ("backfill_raw_metadata_from_siblings", ["--apply"], 3600, False),
    ("backfill_organism_taxon", ["--apply"], 1800, False),
    ("backfill_organism_taxid", ["--apply"], 1800, False),
    ("engines_confirming", None, 3600, False),            # in-process, see engines_confirming()
    ("refresh_search_ptm", ["--batch", "50"], 4 * 3600, False),
    ("predict_tissue", ["--apply"], 2 * 3600, True),
    ("predict_blood_fraction", ["--apply"], 2 * 3600, True),
]
# The dry-run form of each step: the flag that turns writing off. Scripts that write by default
# take --dry-run; scripts that are dry by default simply lose --apply.
DRY_FLAG = {"resolve_raw_hive_paths": ["--dry-run"]}
NO_DRY_RUN = {"refresh_search_ptm"}   # writes unconditionally; skipped under --dry-run


def run_script(name, args, timeout, dry):
    script = name.split(":")[0]
    if dry:
        if script in NO_DRY_RUN:
            return {"rc": None, "skipped": "no dry-run mode"}
        args = [x for x in args if x != "--apply"] + DRY_FLAG.get(script, [])
    cmd = [sys.executable, "-u", os.path.join(HERE, script + ".py")] + args
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=HERE)
        rc, out, err = r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        rc, out, err = "timeout", (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""), "timed out after %ds" % timeout
    sys.stdout.write(out[-6000:])
    sys.stderr.write(err[-3000:])
    return {"rc": rc, "seconds": round(time.time() - t0), "tail": (out or "")[-600:],
            "err_tail": (err or "")[-600:]}


def engines_confirming(dry):
    """refresh_for() over the raw basenames of recently ingested searches."""
    sys.path.insert(0, HERE)
    import backfill_engines_confirming as B
    from coreomics_import import _conn
    t0 = time.time()
    conn = _conn()
    try:
        with conn.cursor() as c:
            c.execute("""SELECT DISTINCT r.raw_basename
                           FROM delimp_searches s
                           JOIN search_raw_files sr ON sr.search_id = s.id
                           JOIN raw_files r ON r.raw_path = sr.raw_path
                          WHERE s.ingested_at > now() - %s::interval
                            AND r.raw_basename IS NOT NULL""", ("%d days" % RECENT_DAYS,))
            names = [x[0] for x in c.fetchall()]
        if dry:
            return {"rc": 0, "seconds": round(time.time() - t0),
                    "tail": "dry run: would refresh %d basenames" % len(names)}
        res = B.refresh_for(conn, names)
        conn.commit()
        return {"rc": 0, "seconds": round(time.time() - t0),
                "tail": "refreshed %d basenames: %s" % (len(names), str(res)[:300])}
    except Exception as e:  # noqa: BLE001 - recorded, never stops the remaining steps
        conn.rollback()
        return {"rc": 1, "seconds": round(time.time() - t0), "err_tail": repr(e)[:600]}
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weekly", action="store_true", help="also run the whole-corpus rescoring")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default="", help="comma-separated step names")
    ap.add_argument("--skip", default="", help="comma-separated step names to leave out")
    ap.add_argument("--status-dir", default=STATUS_DIR)
    a = ap.parse_args()
    only = {x.strip() for x in a.only.split(",") if x.strip()}
    skip = {x.strip() for x in a.skip.split(",") if x.strip()}

    run = {"started": time.strftime("%FT%T"), "host": os.uname().nodename,
           "job": os.environ.get("SLURM_JOB_ID"), "weekly": a.weekly, "dry_run": a.dry_run,
           "steps": {}}
    for name, args, timeout, weekly_only in STEPS:
        if only and name not in only and name.split(":")[0] not in only:
            continue
        if weekly_only and not a.weekly:
            continue
        if name in skip or name.split(":")[0] in skip:
            run["steps"][name] = {"rc": None, "skipped": "--skip"}
            continue
        print(f"\n===== {time.strftime('%F %T')} step {name} =====", flush=True)
        res = engines_confirming(a.dry_run) if args is None else run_script(name, args, timeout, a.dry_run)
        run["steps"][name] = res
        print(f"===== {name}: rc={res.get('rc')} {res.get('seconds', '-')}s "
              f"{res.get('skipped', '')}", flush=True)
    run["finished"] = time.strftime("%FT%T")
    run["n_failed"] = sum(1 for s in run["steps"].values() if s.get("rc") not in (0, None))

    try:
        os.makedirs(a.status_dir, exist_ok=True)
        tag = run["job"] or time.strftime("%Y%m%d_%H%M%S")
        with open(os.path.join(a.status_dir, f"fill_{tag}.json"), "w") as fh:
            json.dump(run, fh, indent=1)
    except OSError as e:
        print(f"could not write status: {e}")
    print(f"\nfran_fill: {len(run['steps'])} step(s), {run['n_failed']} failed")
    return 1 if run["n_failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
