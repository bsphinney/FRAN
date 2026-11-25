"""sne_one.py — fully process ONE Spectronaut .sne into FRAN, on a Windows box with Spectronaut.

  1. manageSNE export with the FRAN report schema (FRAN.rs), as TSV, to the local C: drive
  2. corpus_ingest --bulk-copy, keyed on the .sne path (--output-dir), so a re-run replaces
  3. COPY THE REPORT TO FLINDERS (<share>\\Data\\FRAN_reports\\<sne name>\\<export folder>\\)
  4. sne_xic_ingest, when an _XIC-DBs folder sits beside the .sne

  py sne_one.py --sne "<path.sne>" --name "<search_name>" [--xic-dir "<dir>"] [--batch-size N]

WHY STEP 3 EXISTS (added 2026-11-25). The observed-spectrum lane is built on Hive, from the report:
fill_spectrum_lanes.py looks for it under FRAN_reports/<name>/. This script used to leave the report
on C:\\fran_sne_one and nowhere else, so Hive never saw it. Measured 2026-11-25: of the 166 searches
ingested from Windows in August and September 2026, 100 had no spectrum lane; of the 184 ingested on
Hive from FRAN_reports, 3 had none. A search without its report on Flinders can only get a lane
after another Spectronaut export.

A search that is ALREADY ingested but has no report on Flinders is healed by re-running this script
on it: it exports and copies the report, and does not ingest again.

Files are copied as <name>.part and renamed when complete, so neither Hive's lane job nor its
auto-ingest scan (both match *.tsv / *.parquet) can pick up a half-written report.
"""
import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import time

# FRAN dir = same Flinders share, different mount letter per node: R: on win-1, K: on win-2.
# Prefer R: so win-1 behaviour is unchanged; fall back to K: (win-2). (path-robust, win-2 2026-08-21)
FRAN = next((p for p in (r"R:\Data\FRAN_SNE_export", r"K:\Data\FRAN_SNE_export") if os.path.isdir(p)),
            r"R:\Data\FRAN_SNE_export")
sys.path.insert(0, FRAN)

DLL = r"C:\Program Files (x86)\Biognosys\Spectronaut\bin\Spectronaut.dll"
RS = os.path.join(FRAN, "FRAN.rs")
OUTBASE = r"C:\fran_sne_one"
# The archive Hive reads: /nfs/lssc0/flinders/proteomics/Data/FRAN_reports
REPORTS = os.path.join(os.path.dirname(FRAN), "FRAN_reports")


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def db():
    import psycopg2
    from refresh_leaderboards import _token
    return psycopg2.connect(host="pgfarm.library.ucdavis.edu", port=5432,
        dbname="uc-davis-genome-center-proteomics-core/delimp", user="genome-proteomics-service-account",
        password=_token(), sslmode="require", connect_timeout=30, options="-c statement_timeout=1800000")


# ----------------------------------------------------------------------------- report archive
def is_report(fn):
    """The same test Hive's fill_spectrum_lanes.is_report applies."""
    f = fn.lower()
    return f.endswith((".parquet", ".tsv")) and "report_fran" in f


def sne_stem(sne):
    """The folder name Hive looks for: the .sne file name without its extension. For a directory
    literally named ".sne" (Spectronaut's store inside an experiment folder) it is the parent's name."""
    p = str(sne).replace("\\", "/").rstrip("/")
    stem = re.sub(r"\.sne$", "", p.rsplit("/", 1)[-1], flags=re.I)
    return stem or p.rsplit("/", 2)[-2]


def flinders_report_dir(sne, reports_root=None):
    return os.path.join(reports_root or REPORTS, sne_stem(sne))


def flinders_has_report(sne, reports_root=None):
    """True when a non-empty FRAN report is already archived for this .sne: directly in
    FRAN_reports/<stem>/ or in one of its immediate sub-folders (where Hive's lane job looks)."""
    top = flinders_report_dir(sne, reports_root)
    if not os.path.isdir(top):
        return False
    dirs = [top] + [os.path.join(top, e) for e in os.listdir(top) if os.path.isdir(os.path.join(top, e))]
    for d in dirs:
        try:
            for fn in os.listdir(d):
                p = os.path.join(d, fn)
                if is_report(fn) and os.path.isfile(p) and os.path.getsize(p) > 0:
                    return True
        except OSError:
            pass
    return False


def copy_report_to_flinders(report, sne, reports_root=None):
    """Copy the export folder that holds `report` to FRAN_reports/<stem>/<export folder>/.

    Each file goes over as <name>.part and is renamed once its size matches, the report itself LAST,
    so a reader never sees a partial report and a report on Flinders means the copy finished.
    Returns the destination report path. Raises OSError when a copy cannot be verified.
    """
    src_dir = os.path.dirname(os.path.abspath(report))
    dest_dir = os.path.join(flinders_report_dir(sne, reports_root), os.path.basename(src_dir))
    os.makedirs(dest_dir, exist_ok=True)
    names = [f for f in sorted(os.listdir(src_dir)) if os.path.isfile(os.path.join(src_dir, f))]
    rep_name = os.path.basename(report)
    names = [f for f in names if f != rep_name] + [rep_name]      # the report last
    for fn in names:
        src, dst = os.path.join(src_dir, fn), os.path.join(dest_dir, fn)
        want = os.path.getsize(src)
        if os.path.isfile(dst) and os.path.getsize(dst) == want:
            continue
        part = dst + ".part"
        shutil.copyfile(src, part)
        got = os.path.getsize(part)
        if got != want:
            raise OSError(f"copy of {fn} is {got} bytes, source is {want}")
        os.replace(part, dst)
    return os.path.join(dest_dir, rep_name)


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sne", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--xic-dir", default=None)
    ap.add_argument("--batch-size", type=int, default=0)
    ap.add_argument("--no-flinders-copy", action="store_true",
                    help="do not archive the report on Flinders (the search then gets no spectrum lane)")
    a = ap.parse_args()
    sne, name = a.sne, a.name
    out = os.path.join(OUTBASE, re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:70]); os.makedirs(out, exist_ok=True)
    xd = a.xic_dir
    if not xd:
        cand = [d for d in glob.glob(os.path.join(os.path.dirname(sne), "*_XIC-DBs")) if glob.glob(os.path.join(d, "*.xic.db"))]
        xd = cand[0] if cand else None
    ndb = len(glob.glob(os.path.join(xd, "*.xic.db"))) if xd else 0
    log(f"=== {name} === sne={sne} xic-dir={xd} ({ndb} dbs)")
    archive = not a.no_flinders_copy
    if archive and not os.path.isdir(REPORTS):
        log(f"NO report archive at {REPORTS} -> abort (is the Flinders share mapped? "
            f"--no-flinders-copy skips the archive, and the spectrum lane with it)"); sys.exit(5)

    # resume: skip fully-done items (report ingested AND archived AND (no xic dir OR xic already present))
    con = db(); c = con.cursor()
    c.execute("SELECT id FROM delimp_searches WHERE output_dir=%s AND n_precursors_total>0 LIMIT 1", (sne,))
    done = c.fetchone()
    ingest = True
    if done:
        xic_present = False
        if xd:
            c.execute("SELECT 1 FROM delimp_precursor_xic WHERE search_id=%s LIMIT 1", (done[0],))
            xic_present = c.fetchone() is not None
        con.close()
        archived = (not archive) or flinders_has_report(sne)
        if archived and ((not xd) or xic_present):
            log(f"SKIP (already ingested{' + XIC' if xic_present else ''}, report on Flinders): {name}"); return
        if (not xd) or xic_present:
            # Ingested, but its report never reached Flinders: export and archive only.
            ingest = False; xd = None
            log("already ingested, but NO report on Flinders -> export + archive only (no re-ingest)")
        else:
            log("report already ingested; XIC missing -> will redo export+XIC")
    else:
        con.close()

    # 1) export FRAN report (v21) as TSV. NOT --writeParquet: manageSNE's parquet writer HANGS at
    # "Generating Experiment Report" on some .sne (leaves a ~27KB stub, never returns). TSV is
    # reliable and spectronaut_to_corpus/sne_xic_ingest both read TSV. Timeout guards any other hang.
    log("step 1: manageSNE export (Spectronaut, TSV) ...")
    t0 = time.time()
    try:
        rc = subprocess.run(["dotnet", DLL, "manageSNE", "-sne", sne, "-o", out, "-rs", RS],
                            stdout=open(os.path.join(out, "manageSNE.log"), "w"), stderr=subprocess.STDOUT,
                            timeout=2400).returncode
    except subprocess.TimeoutExpired:
        log("manageSNE TIMEOUT (>40min) -> abort this .sne (hung export)"); sys.exit(1)
    reps = [p for p in glob.glob(os.path.join(out, "**", "*Report_FRAN*.tsv"), recursive=True) if os.path.getsize(p) > 0]
    log(f"step 1 rc={rc} in {int(time.time()-t0)}s; FRAN report(s)={len(reps)}")
    if not reps:
        log("NO FRAN report -> abort (see manageSNE.log)"); sys.exit(1)
    rep = max(reps, key=os.path.getsize)

    # 2) ingest report (raw-metadata auto-populates via corpus_ingest go-forward wiring)
    if ingest:
        log("step 2: corpus_ingest --bulk-copy ...")
        t0 = time.time()
        rc2 = subprocess.run([sys.executable, os.path.join(FRAN, "corpus_ingest.py"), rep,
                              "--engine", "spectronaut", "--name", name, "--output-dir", sne, "--bulk-copy"],
                             stdout=open(os.path.join(out, "ingest.log"), "w"), stderr=subprocess.STDOUT).returncode
        log(f"step 2 rc={rc2} in {int(time.time()-t0)}s")
        if rc2 != 0:
            log("corpus_ingest FAILED -> abort (see ingest.log); report NOT archived"); sys.exit(2)

    # 3) archive the report on Flinders, AFTER a good ingest: a report that reached Flinders without
    # its search would be picked up by Hive's auto-ingest under a second output_dir.
    archive_failed = False
    if archive:
        t0 = time.time()
        try:
            dst = copy_report_to_flinders(rep, sne)
            log(f"step 3: report archived on Flinders in {int(time.time()-t0)}s -> {dst}")
        except OSError as e:
            archive_failed = True
            log(f"step 3 FAILED: report NOT archived on Flinders ({e}). The search is ingested but "
                f"will get no spectrum lane until this script is re-run on it.")

    # 4) XIC (if dbs present)
    if not xd:
        log(f"DONE ({'report only, no _XIC-DBs' if ingest else 'archive only'}): {name}")
        sys.exit(4 if archive_failed else 0)
    con = db(); c = con.cursor()
    c.execute("SELECT id, n_precursors_total FROM delimp_searches WHERE output_dir=%s ORDER BY ingested_at DESC LIMIT 1", (sne,))
    row = c.fetchone(); con.close()
    if not row:
        log("no search row after ingest -> abort XIC"); sys.exit(3)
    sid, nprec = row
    log(f"search_id={sid} n_precursors={nprec}")
    xargs = [sys.executable, os.path.join(FRAN, "sne_xic_ingest.py"), "--report", rep, "--xic-dir", xd, "--search-id", sid, "--pg"]
    if a.batch_size:
        xargs += ["--batch-size", str(a.batch_size)]
    log(f"step 4: sne_xic_ingest ({ndb} dbs, n_prec={nprec}) ...")
    t0 = time.time()
    rc3 = subprocess.run(xargs, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for t in (rc3.stdout.strip().splitlines()[-3:] if rc3.stdout else []): log("  xic> " + t)
    log(f"step 4 rc={rc3.returncode} in {int(time.time()-t0)}s")
    log(f"DONE (report+XIC ok): {name}" if (rc3.returncode == 0 and "upserted" in (rc3.stdout or "")) else f"DONE (report ok, XIC issue): {name}")
    sys.exit(4 if archive_failed else 0)


if __name__ == "__main__":
    main()
