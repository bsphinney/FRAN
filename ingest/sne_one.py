"""sne_one.py — fully process ONE Spectronaut .sne into FRAN, on a Windows box with Spectronaut.

  1. manageSNE export with the FRAN report schema (FRAN.rs), as TSV, to the local C: drive
  2. corpus_ingest --bulk-copy, keyed on the .sne path (--output-dir), so a re-run replaces
  3. COPY THE REPORT TO FLINDERS (<share>\\Data\\FRAN_reports\\<sne name>\\<export folder>\\)
     and, once the copy is verified, delete the export from C: (logs are kept)
     and the XIC dbs (an _XIC-DBs folder beside the .sne) to .../<export folder>/xic_dbs/, from
     which Hive builds the chromatogram lane
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
import hashlib
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
    """To the console, and to node_reports/<HOST>/sne_one.log on the share so the run can be
    followed from Hive (there is no shell into this computer)."""
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)
    try:
        import node_report
        node_report.report("sne_one", m, FRAN if os.path.isdir(FRAN) else None)
    except Exception:  # noqa: BLE001 -- a status line must never stop an ingest
        pass


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
    parts = [x for x in str(sne).replace("\\", "/").rstrip("/").split("/") if x]
    stem = re.sub(r"\.sne$", "", parts[-1], flags=re.I) if parts else ""
    if not stem and len(parts) > 1:
        stem = re.sub(r"^[A-Za-z]:$", "", parts[-2])
    if not stem:
        raise ValueError(f"cannot name the experiment from {sne!r}")
    return stem


def export_dir(name, sne, base=None):
    """A local export folder for ONE .sne: the readable name plus a hash of the .sne path. The name
    alone, cut to 70 characters, put two experiments with a long common prefix in one folder, and
    'the largest report in the folder' could then be the other experiment's."""
    tag = hashlib.md5(str(sne).lower().encode("utf-8")).hexdigest()[:8]
    return os.path.join(base or OUTBASE, re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:60] + "_" + tag)


def pick_report(out, since):
    """The FRAN report THIS run exported: non-empty, written at or after `since` (a re-run leaves the
    older exports in the folder). The largest when one export wrote several."""
    reps = [p for p in glob.glob(os.path.join(out, "**", "*Report_FRAN*.tsv"), recursive=True)
            if os.path.getsize(p) > 0 and os.path.getmtime(p) >= since - 5]
    return max(reps, key=os.path.getsize) if reps else None


NO_XIC_MARKER = "NO_XIC_IN_SNE.txt"
XIC_SUBDIR = "xic_dbs"
XIC_DONE = "COMPLETE.txt"


def is_xic_db(fn):
    """Spectronaut's "All XIC" export: one SQLite file per raw file. Never a macOS ._ stub."""
    f = fn.lower()
    return (f.endswith(".xic.db") or f.endswith(".sqlite")) and not fn.startswith("._")


def flinders_report_path(sne, reports_root=None):
    """The newest archived FRAN report for this .sne on Flinders, or None."""
    top = flinders_report_dir(sne, reports_root)
    found = []
    try:
        dirs = [top] + [os.path.join(top, e) for e in os.listdir(top) if os.path.isdir(os.path.join(top, e))]
    except OSError:
        return None
    for d in dirs:
        try:
            found += [os.path.join(d, fn) for fn in os.listdir(d)
                      if is_report(fn) and os.path.isfile(os.path.join(d, fn)) and os.path.getsize(os.path.join(d, fn)) > 0]
        except OSError:
            pass
    return max(found, key=lambda p: (os.path.basename(os.path.dirname(p)), p)) if found else None


def flinders_has_xic(sne, reports_root=None):
    """True when a COMPLETE set of XIC dbs is archived beside any archived report of this .sne."""
    top = flinders_report_dir(sne, reports_root)
    try:
        return (os.path.isfile(os.path.join(top, XIC_SUBDIR, XIC_DONE))      # report directly in <stem>/
                or any(os.path.isfile(os.path.join(top, e, XIC_SUBDIR, XIC_DONE)) for e in os.listdir(top)))
    except OSError:
        return False


def copy_xic_dbs_to_flinders(xic_dir, dest_report):
    """Copy the *.xic.db files to <the archived report's folder>/xic_dbs/, where Hive builds the
    chromatogram lane from them. Same .part-then-rename rule as the report; COMPLETE.txt is written
    LAST and holds the file count, so Hive never builds a lane from half a set. Returns (n, GB)."""
    dest = os.path.join(os.path.dirname(dest_report), XIC_SUBDIR)
    os.makedirs(dest, exist_ok=True)
    names = sorted(f for f in os.listdir(xic_dir) if is_xic_db(f) and os.path.isfile(os.path.join(xic_dir, f)))
    if not names:
        raise OSError(f"no *.xic.db in {xic_dir}")
    total = 0
    for fn in names:
        src, dst = os.path.join(xic_dir, fn), os.path.join(dest, fn)
        want = os.path.getsize(src); total += want
        if os.path.isfile(dst) and os.path.getsize(dst) == want:
            continue
        part = dst + ".part"
        shutil.copyfile(src, part)
        if os.path.getsize(part) != want:
            raise OSError(f"copy of {fn} is {os.path.getsize(part)} bytes, source is {want}")
        os.replace(part, dst)
    with open(os.path.join(dest, XIC_DONE + ".part"), "w") as fh:
        fh.write(f"{len(names)} files\n{total} bytes\nfrom {xic_dir}\n")
    os.replace(os.path.join(dest, XIC_DONE + ".part"), os.path.join(dest, XIC_DONE))
    return len(names), total / 1e9


def sne_gb(sne):
    """Size of the experiment in GB: the .sne file, or everything under a .sne directory. 0 if unreadable."""
    try:
        if os.path.isfile(sne):
            return os.path.getsize(sne) / 1e9
        return sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(sne) for f in fs) / 1e9
    except OSError:
        return 0.0


def export_timeout_min(gb, override=0):
    """How long Spectronaut may take to write the report before the export is called hung.
    It was a flat 40 minutes, and the largest experiments need hours (six needed about 8 h in August
    2026). 40 minutes, plus 30 minutes per GB of .sne, never more than 12 hours."""
    if override and override > 0:
        return float(override)
    return min(12 * 60.0, 40.0 + 30.0 * max(gb, 0.0))


EXPORT_OK = "EXPORT_OK.txt"


def leftover_xics(out):
    """A COMPLETE XIC export still on C: from an earlier run whose copy to Flinders failed (the
    newest xics_* folder holding EXPORT_OK.txt and at least one db), or None."""
    try:
        dirs = sorted((os.path.join(out, e) for e in os.listdir(out) if e.startswith("xics_")), reverse=True)
    except OSError:
        return None
    for d in dirs:
        try:
            if os.path.isfile(os.path.join(d, EXPORT_OK)) and any(is_xic_db(f) for f in os.listdir(d)):
                return d
        except OSError:
            pass
    return None


def kill_tree(proc):
    """Stop the export AND anything it started. On Windows Popen.kill() ends only the one process;
    a worker left running keeps writing to C: and keeps its files locked against the clean-up."""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=60)
        except Exception:  # noqa: BLE001
            pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass


def find_same_path(sne, rows):
    """The search FRAN holds for exactly this .sne path, compared the way find_missed_sne compares
    (case, slashes and drive letters normalised). rows: (id, output_dir, search_name). id or None."""
    from find_uningested import norm_path
    me = norm_path(sne)
    return next((sid for sid, od, _ in rows if od and norm_path(od) == me), None)


def watch_export(proc, limit_s, out, stop_free_gb, poll_s=30, free=None, clock=time.time, sleep=time.sleep):
    """Wait for the Spectronaut export. Returns None when it ended by itself, "timeout" when it ran
    past limit_s, "disk" when the export drive fell under stop_free_gb (XIC dbs run to GBs per raw
    file and nobody is watching). In both of those cases the process is killed first."""
    free = free or free_gb
    t0 = clock()
    while True:
        if proc.poll() is not None:
            return None
        why = "timeout" if clock() - t0 > limit_s else ("disk" if free(out) < stop_free_gb else None)
        if why:
            kill_tree(proc)
            try:
                proc.wait(timeout=60)
            except Exception:  # noqa: BLE001
                pass
            return why
        sleep(poll_s)


def free_gb(path):
    """Free space, in GB, on the drive that holds `path` (the nearest existing parent)."""
    p = os.path.abspath(path)
    while not os.path.isdir(p) and os.path.dirname(p) != p:
        p = os.path.dirname(p)
    return shutil.disk_usage(p).free / 1e9


def remove_local_export(out, base=None):
    """Delete the export folders inside ONE per-.sne folder, keeping its log files. Only ever a
    direct child of the export base (C:\\fran_sne_one), never anything else. Returns GB freed.
    Called only after the report is verified on Flinders: without this every report stayed on C:
    and an unattended batch filled the drive."""
    base = os.path.abspath(base or OUTBASE)
    out = os.path.abspath(out)
    if os.path.dirname(out) != base or not os.path.isdir(out):
        return 0.0
    freed = 0
    for e in os.listdir(out):
        d = os.path.join(out, e)
        if os.path.isdir(d) and not os.path.islink(d):
            freed += sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(d) for f in fs)
            shutil.rmtree(d, ignore_errors=True)
    return freed / 1e9


def find_known_elsewhere(sne, rows):
    """A search FRAN already holds for this experiment under ANOTHER path, or None.
    rows: (id, output_dir, search_name) of the Spectronaut searches. The same .sne sits on several
    drives (B: project folders, S:\\sne_storage, the share); FRAN records the one it was ingested
    from. Same rule as find_missed_sne's SAME_NAME: the .sne name against each search's output_dir
    name and search_name, export timestamps ignored."""
    from find_uningested import name_keys, norm_path
    want = name_keys(sne_stem(sne))
    me = norm_path(sne)
    for sid, od, nm in rows:
        if od and norm_path(od) == me:
            continue
        keys = set(name_keys(nm)) if nm else set()
        if od:
            parts = norm_path(od).rsplit("/", 2)
            keys |= name_keys(parts[-1])
            if parts[-1] == ".sne" and len(parts) > 1:
                keys |= name_keys(parts[-2])
        if want & keys:
            return sid, od, nm
    return None


def flinders_report_dir(sne, reports_root=None):
    return os.path.join(reports_root or REPORTS, sne_stem(sne))


def flinders_has_report(sne, reports_root=None):
    """True when a non-empty FRAN report is already archived for this .sne: directly in
    FRAN_reports/<stem>/ or in one of its immediate sub-folders (where Hive's lane job looks)."""
    top = flinders_report_dir(sne, reports_root)
    if not os.path.isdir(top):
        return False
    try:
        dirs = [top] + [os.path.join(top, e) for e in os.listdir(top) if os.path.isdir(os.path.join(top, e))]
    except OSError:
        return False
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
    ap.add_argument("--min-free-gb", type=float, default=60.0,
                    help="refuse to start with less free space than this on the export drive (default 60)")
    ap.add_argument("--keep-local", action="store_true",
                    help="keep the export on C: after it is archived on Flinders (default: delete it)")
    ap.add_argument("--export-timeout-min", type=float, default=0,
                    help="minutes Spectronaut may take to export (default: 40 + 30 per GB of .sne, at most 720)")
    ap.add_argument("--no-export-xics", action="store_true",
                    help="do not ask Spectronaut for the XICs. By default, when no _XIC-DBs folder sits "
                         "beside the .sne, the export also writes them (--setXICExportDirectory); that "
                         "only yields files for an .sne saved WITH ion traces")
    ap.add_argument("--export-xics", action="store_true", help=argparse.SUPPRESS)   # old name: now the default
    ap.add_argument("--stop-free-gb", type=float, default=15.0,
                    help="kill an export when the export drive has less than this left (default 15)")
    ap.add_argument("--min-share-free-tb", type=float, default=20.0,
                    help="do not export or copy XIC dbs when the share has less than this free (default 20)")
    ap.add_argument("--no-xic-archive", action="store_true",
                    help="do not copy the XIC dbs to Flinders (the search then gets no chromatogram lane)")
    ap.add_argument("--backfill-only", action="store_true",
                    help="never ingest: only archive a missing report / XIC dbs for an experiment FRAN "
                         "already holds. Exits 8, exporting nothing, when FRAN does not hold it")
    ap.add_argument("--force-ingest", action="store_true",
                    help="ingest even though FRAN holds a search with this experiment's name from another path")
    a = ap.parse_args()
    sne, name = a.sne, a.name
    out = export_dir(name, sne)
    xd = a.xic_dir
    if not xd:
        cand = [d for d in glob.glob(os.path.join(os.path.dirname(sne), "*_XIC-DBs")) if glob.glob(os.path.join(d, "*.xic.db"))]
        xd = cand[0] if cand else None
    ndb = len(glob.glob(os.path.join(xd, "*.xic.db"))) if xd else 0
    log(f"=== {name} === sne={sne} xic-dir={xd} ({ndb} dbs)")
    archive = not a.no_flinders_copy
    xsrc = xd                       # where the XIC dbs are; xd is cleared below when the PG step is skipped
    if a.backfill_only:
        xd = None                   # backfill writes nothing to the database, the XIC table included
    xic_archive = archive and not a.no_xic_archive

    def archive_xics(dest_report):
        """Copy the XIC dbs beside the archived report. True when done or not needed, False on failure."""
        if not (xic_archive and share_ok and xsrc) or flinders_has_xic(sne):
            return True
        if not dest_report:
            log("XIC dbs NOT archived: no archived report found on Flinders to put them beside")
            return False
        t = time.time()
        try:
            n, gb = copy_xic_dbs_to_flinders(xsrc, dest_report)
            log(f"XIC dbs archived on Flinders: {n} files, {gb:.1f} GB in {int(time.time()-t)}s")
            return True
        except OSError as e:
            log(f"XIC dbs NOT archived on Flinders ({e}): no chromatogram lane until this is re-run")
            return False
    if name != sne_stem(sne):
        log(f"note: --name differs from the .sne name ({sne_stem(sne)!r}); the report is archived "
            f"under the .sne name, which is what Hive matches on")
    if archive and not os.path.isdir(REPORTS):
        log(f"NO report archive at {REPORTS} -> abort (is the Flinders share mapped? "
            f"--no-flinders-copy skips the archive, and the spectrum lane with it)"); sys.exit(5)

    no_xic_marker = os.path.join(flinders_report_dir(sne), NO_XIC_MARKER)
    share_ok = True
    if xic_archive:
        try:
            share_ok = free_gb(REPORTS) / 1000.0 >= a.min_share_free_tb
        except OSError:
            share_ok = False
        if not share_ok:
            log(f"under {a.min_share_free_tb:g} TB free on the share: XIC dbs are NOT exported or copied this run")

    def xic_needed(cur, sid):
        """Does this experiment still need its XIC dbs on Flinders? Not when they are there, when an
        earlier export showed the .sne holds none, or when the search already has an XIC lane."""
        if not (xic_archive and share_ok) or flinders_has_xic(sne) or os.path.isfile(no_xic_marker):
            return False
        if sid:
            cur.execute("SELECT 1 FROM delimp_xic_lane WHERE search_id::text=%s LIMIT 1", (str(sid),))
            if cur.fetchone():
                return False
        return True

    # resume: skip fully-done items (report ingested AND archived AND XICs dealt with)
    con = db(); c = con.cursor()
    c.execute("SELECT id, output_dir, search_name FROM delimp_searches "
              "WHERE search_engine='spectronaut' AND n_precursors_total>0")
    sn_rows = c.fetchall()
    same = find_same_path(sne, sn_rows)
    done = (same,) if same else None
    ingest = True
    need_x = True
    if done:
        xic_present = False
        if xd:
            c.execute("SELECT 1 FROM delimp_precursor_xic WHERE search_id=%s LIMIT 1", (done[0],))
            xic_present = c.fetchone() is not None
        need_x = xic_needed(c, done[0])
        con.close()
        archived = (not archive) or flinders_has_report(sne)
        export_for_xic = need_x and not xsrc and not a.no_export_xics
        ingest = False        # the search row is there: never delete and re-insert it for an export
        if archived and ((not xd) or xic_present) and not export_for_xic:
            # Nothing to export. The XIC dbs may still be missing on Flinders: a plain copy.
            ok = archive_xics(flinders_report_path(sne)) if need_x else True
            log(f"SKIP (already ingested{' + XIC' if xic_present else ''}, report on Flinders): {name}")
            sys.exit(0 if ok else 4)
        if (not xd) or xic_present:
            xd = None
            log("already ingested -> export only (no re-ingest): "
                + ("no report on Flinders" if not archived else "XICs to export for the chromatogram lane"))
        else:
            log("report already ingested; XIC missing -> export + XIC only (no re-ingest)")
    else:
        other = None if a.force_ingest else find_known_elsewhere(sne, sn_rows)
        if a.backfill_only and not other:
            con.close()
            log(f"backfill only: FRAN holds no Spectronaut search for this experiment -> nothing done: {name}")
            sys.exit(8)
        need_x = xic_needed(c, other[0] if other else None)
        con.close()
        if other:
            # The same experiment, ingested from another drive or folder. Ingesting it again from
            # here would store it twice. Its report is archived by the .sne NAME, which is the same.
            export_for_xic = need_x and not xsrc and not a.no_export_xics
            if ((not archive) or flinders_has_report(sne)) and not export_for_xic:
                ok = archive_xics(flinders_report_path(sne)) if need_x else True
                log(f"SKIP: FRAN already holds this experiment as search {other[0]} from {other[1]} "
                    f"(--force-ingest if it really is a different search)")
                sys.exit(0 if ok else 4)
            ingest = False; xd = None
            log(f"FRAN already holds this experiment as search {other[0]} from {other[1]} -> export + "
                f"archive only, no ingest (--force-ingest to ingest it anyway)")

    # 1) export FRAN report (v21) as TSV. NOT --writeParquet: manageSNE's parquet writer HANGS at
    # "Generating Experiment Report" on some .sne (leaves a ~27KB stub, never returns). TSV is
    # reliable and spectronaut_to_corpus/sne_xic_ingest both read TSV. Timeout guards any other hang.
    limit_min = export_timeout_min(sne_gb(sne), a.export_timeout_min)
    # XICs: when no _XIC-DBs folder sits beside the .sne, ask Spectronaut for them in the same export.
    # --setXICExportDirectory is a GLOBAL option and must come BEFORE the command (manual, Table 13).
    # An earlier run exported the XICs but could not copy them to Flinders: copy them now, instead
    # of asking Spectronaut for hours to write them again.
    left = leftover_xics(out) if (need_x and not xsrc and archive and not ingest) else None
    dest = flinders_report_path(sne) if left else None
    if left and dest:
        xsrc = left
        ok = archive_xics(dest)
        if ok and not a.keep_local:
            remove_local_export(out)
        log(f"DONE (XIC dbs from an earlier export {'archived' if ok else 'still NOT archived'}): {name}")
        sys.exit(0 if ok else 4)
    os.makedirs(out, exist_ok=True)
    # Anything already in this experiment's export folder is left over from a failed run and is
    # about to be exported again: remove it first, or failed experiments pile up on C:.
    stale = remove_local_export(out)
    if stale > 0.01:
        log(f"removed {stale:.1f} GB left on C: by an earlier failed run of this experiment")
    fg = free_gb(out)
    if fg < a.min_free_gb:
        log(f"only {fg:.0f} GB free on the export drive ({OUTBASE}), need {a.min_free_gb:g} -> abort "
            f"before exporting (free space, or lower --min-free-gb)"); sys.exit(6)

    def note_no_xic(why_text):
        """So a re-run does not export this experiment's XICs again for the same result."""
        try:
            os.makedirs(os.path.dirname(no_xic_marker), exist_ok=True)
            with open(no_xic_marker, "w") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M')} {why_text}: {sne}\n(delete this file to try again)\n")
        except OSError:
            pass

    def export(with_xics):
        xdir = os.path.join(out, "xics_" + time.strftime("%Y%m%d_%H%M%S")) if with_xics else None
        if xdir:
            os.makedirs(xdir, exist_ok=True)
        log(f"step 1: manageSNE export (Spectronaut, TSV{' + XICs' if xdir else ''}); .sne is "
            f"{sne_gb(sne):.1f} GB, limit {limit_min:.0f} min ...")
        pre = ["--setXICExportDirectory", xdir] if xdir else []
        with open(os.path.join(out, "manageSNE.log"), "w") as lf:
            proc = subprocess.Popen(["dotnet", DLL, *pre, "manageSNE", "-sne", sne, "-o", out, "-rs", RS],
                                    stdout=lf, stderr=subprocess.STDOUT)
            why = watch_export(proc, limit_min * 60, out, a.stop_free_gb)
        return xdir, why, proc.returncode

    t0 = time.time()
    xexp, why, rc = export(need_x and not xsrc and not a.no_export_xics)
    if why == "disk" and xexp:
        # The XICs of this experiment do not fit on C:. That is about this experiment, not the
        # computer: clear it, remember it, and export the report alone.
        time.sleep(5)
        remove_local_export(out)
        log(f"export STOPPED: under {a.stop_free_gb:g} GB left on C: while writing XICs. Partial export "
            f"removed ({free_gb(out):.0f} GB free now). Exporting again WITHOUT XICs")
        if archive:
            note_no_xic("the XIC dbs of this experiment do not fit on the export drive")
        need_x = False
        t0 = time.time()
        xexp, why, rc = export(False)
    if why:
        time.sleep(5)
        remove_local_export(out)
        if why == "timeout":
            log(f"manageSNE TIMEOUT (>{limit_min:.0f} min) -> abort this .sne (hung, or needs a longer "
                f"--export-timeout-min). Partial export removed"); sys.exit(7)
        log(f"export STOPPED: under {a.stop_free_gb:g} GB left on the export drive, with no XICs being "
            f"written. Partial export removed ({free_gb(out):.0f} GB free now)"); sys.exit(6)
    rep = pick_report(out, t0)
    if xexp:
        got = [f for f in os.listdir(xexp) if is_xic_db(f)]
        log(f"XIC export from the .sne: {len(got)} db(s) in {xexp}"
            + ("" if got else " (none: was the .sne saved WITH ion traces?)"))
        if got:
            xsrc = xexp
            with open(os.path.join(xexp, EXPORT_OK), "w") as fh:     # the export ran to its end
                fh.write(f"{len(got)} files\n")
        elif rep and archive:
            note_no_xic("Spectronaut wrote no XIC dbs for this experiment")
    log(f"step 1 rc={rc} in {int(time.time()-t0)}s; FRAN report={'yes' if rep else 'NONE'}")
    if not rep:
        remove_local_export(out)
        log("NO FRAN report from this export -> abort (see manageSNE.log)"); sys.exit(1)

    # 2) ingest report (raw-metadata auto-populates via corpus_ingest go-forward wiring)
    if ingest:
        log("step 2: corpus_ingest --bulk-copy ...")
        t0 = time.time()
        with open(os.path.join(out, "ingest.log"), "w") as lf:
            rc2 = subprocess.run([sys.executable, os.path.join(FRAN, "corpus_ingest.py"), rep,
                                  "--engine", "spectronaut", "--name", name, "--output-dir", sne, "--bulk-copy"],
                                 stdout=lf, stderr=subprocess.STDOUT).returncode
        log(f"step 2 rc={rc2} in {int(time.time()-t0)}s")
        if rc2 != 0:
            remove_local_export(out)
            log("corpus_ingest FAILED -> abort (see ingest.log); report NOT archived, export removed"); sys.exit(2)

    # 3) archive the report on Flinders, AFTER a good ingest: a report that reached Flinders without
    # its search would be picked up by Hive's auto-ingest under a second output_dir.
    archive_failed = False
    archived_ok = False

    def cleanup():
        if archived_ok and not a.keep_local:
            log(f"local export removed ({remove_local_export(out):.1f} GB freed on C:; logs kept in {out})")

    # An experiment FRAN already holds, exported again only for its XICs: its report is on Flinders
    # already. A second copy would give Hive's lane job two reports to choose between.
    existing = flinders_report_path(sne) if (archive and not ingest) else None
    if archive:
        t0 = time.time()
        try:
            dst = existing or copy_report_to_flinders(rep, sne)
            archived_ok = True
            log(f"step 3: report {'already' if existing else 'archived'} on Flinders "
                f"({int(time.time()-t0)}s) -> {dst}")
            if not archive_xics(dst):
                archive_failed = True
                archived_ok = False          # keep the local export: it may hold the only XIC dbs
        except OSError as e:
            archive_failed = True
            log(f"step 3 FAILED: report NOT archived on Flinders ({e}). The search is ingested but "
                f"will get no spectrum lane until this script is re-run on it.")

    # 4) XIC (if dbs present)
    if not xd:
        log(f"DONE ({'report only, no _XIC-DBs' if ingest else 'archive only'}): {name}")
        cleanup()
        sys.exit(4 if archive_failed else 0)
    con = db(); c = con.cursor()
    if done:
        c.execute("SELECT id, n_precursors_total FROM delimp_searches WHERE id=%s", (done[0],))
    else:
        c.execute("SELECT id, n_precursors_total FROM delimp_searches WHERE output_dir=%s ORDER BY ingested_at DESC LIMIT 1", (sne,))
    row = c.fetchone(); con.close()
    if not row:
        cleanup()
        # corpus_ingest's duplicate guard exits 0 without writing when another output_dir already
        # holds the same raw files and precursor count: there is no row under this path.
        log("no search row under this .sne after ingest (see ingest.log: duplicate guard?) -> abort XIC"); sys.exit(3)
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
    xic_ok = rc3.returncode == 0 and "upserted" in (rc3.stdout or "")
    log(f"DONE (report+XIC ok): {name}" if xic_ok else f"DONE (report ok, XIC issue): {name}")
    cleanup()
    sys.exit(4 if archive_failed else (0 if xic_ok else 3))


if __name__ == "__main__":
    main()
