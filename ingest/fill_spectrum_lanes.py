"""fill_spectrum_lanes.py -- daily: give every Spectronaut search a spectrum lane.

WHY THIS EXISTS. The observed-spectrum Lance lane (one <search>.lance per Spectronaut search,
registered in delimp_spectrum_lane) was only ever written by hand-launched backfills: 1,542 lanes in
July 2026, 162 on 2026-11-13, none since. auto_ingest.py deliberately does not pass --lance-dir, the
nightly fran_fill.py has no spectrum step, and no cron line wrote it, so every new search went
without a lane and nothing healed the old gaps. This is the DIA-NN XIC job's pattern
(fill_diann_xic_lanes.py) applied to the spectrum lane: plan from the DB, write only what is
missing, record a plan JSON the health scan reads.

    fill_spectrum_lanes.py [--apply] [--max N] [--workers N]

Dry run by default (plans, writes only a dryrun_plan_*.json). --apply writes. One process plans AND
writes (a single sbatch task), so there is exactly one writer per dataset and per state file.

WHAT IT DECIDES, per Spectronaut search with NO row in delimp_spectrum_lane
  write         a FRAN report is on disk and no usable dataset exists -> parse it with
                backfill_fragments' own _extract/_assemble_table, write the Lance dataset, verify,
                register against THIS search_id.
  register      a dataset for this search is already on disk but unregistered (a killed run) and its
                row count matches the corpus -> verify and register it; never rewrite it.
  link          a registered dataset with search_id NULL has the matching name AND row count ->
                set its search_id, after its run set is checked (see IDENTITY AND SAFETY).
  needs_reexport  no report on disk and no dataset: the search can only get a lane after a Windows
                re-export from its .sne. Recorded every night, never attempted, never retried.
  dup_of_laned  no report, but another search's lane has the same name and the same precursor count
                (the same experiment ingested twice). Its content exists; the registry has one
                search_id per dataset. Reported separately, not written.
  parked        3 failed attempts. Left alone until the state file entry is cleared by hand.

NAME COLLISIONS. backfill_fragments names a dataset <timestamp-stripped report name>.lance, so two
different searches with the same name share a path, and its mode="overwrite" would replace the first
search's data. Measured 2026-11-24: 247 of the 592 un-laned searches map by name to a dataset
registered to ANOTHER search (166 with a different precursor count: genuinely different data). A new
lane therefore takes the plain name only when nobody holds it; otherwise <name>__<search_id8>.lance
(the XIC lane's convention). An existing dataset is never overwritten for another search.

VERIFICATION (lanes fail silently; "the write returned" proves nothing). After a write the dataset
is reopened and its row count compared with the corpus precursor count (delimp_searches
.n_precursors_total, else count(*) of delimp_precursors for that search_id). Exact is "exact"; a
difference of at most max(10, 0.1 percent) is "near" and passes, because 78 of the 97 existing lanes
that are not exact differ by under 1 percent and a handful of rows is dedup noise; anything larger
is a FAILURE and the dataset is NOT registered. --strict makes "near" a failure too.

IDENTITY AND SAFETY (added after review, 2026-11-24). The governing rule: this job must be unable to
replace a dataset or a registry row it did not itself create for that same search.
  * A count is not identity: two same-name searches can have close counts. The dataset's distinct
    run names (normalised: basename, extension stripped, lower case) must EQUAL the search's runs in
    the corpus (search_raw_files, filtered by search_id). Checked in the child after parsing and
    BEFORE anything is written; also for `register` and `link` (read from the dataset).
    Verified read-only on HIVE: 40/40 sampled exact-count lanes and 60/60 near-count lanes have run
    sets equal under this normalisation.
  * A write goes to <path>.incoming.<jobid> with mode="create", is count-checked, reopened, and only
    then renamed to <path>. Nothing is ever written onto an existing path (any existing file or
    directory at the path, openable or not, is foreign and refused). A failure removes the incoming
    directory (it is this run's own); a registration failure after the rename moves the dataset to
    <path>.failed.<jobid>. The planner only ever matches <name>.lance, never .incoming/.failed.
  * Registration uses this module's own conditional upsert (UPSERT_SQL): it can only insert, or
    update a row that is unlinked or already this search's; zero rows is a failure. It never calls
    spectrum_lance.register (which COALESCEs and overwrites counts and md5 of any row at the path).
  * "near" count matches are accepted for plain-path writes only; a suffixed path needs an exact count.
    Every near lane is listed in the result JSON.
  * link_spectrum_lane.py is NOT run here (it links on a unique name with no count or run check).
    When two un-laned searches match one unlinked row it is left alone and listed as ambiguous_link.

RETRIES. state.json counts failed attempts per search. A write that raises, mismatches or is killed
counts; a job pre-empted and requeued under the SAME job id does not. After 3 the search is parked.
If a search has several exports on disk, attempt k uses candidate k (newest first), so a stale
newest export is not retried forever. Exit status is 1 if any write failed.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

D = os.environ.get("FRAN_SPECTRUM_FILL_DIR", "/quobyte/proteomics-grp/brett/scratch/fran_health/spectrum_lane")
LANCE = os.environ.get("FRAN_SPECTRUM_LANCE_DIR", "/quobyte/proteomics-grp/brett/glendon/spectra_lance")
REPORT_ROOTS = ["/nfs/lssc0/flinders/proteomics/Data/FRAN_reports",
                "/nfs/lssc0/flinders/proteomics/Data/FRAN_SNE_export"]
MAX_ATTEMPTS = 3
# Peak memory of one parse, estimated from the report's size on disk. Parquet decompresses ~10x into
# pandas; a TSV parsed with usecols holds roughly 1.5x. The sbatch has 128 GB: keep the sum of the
# reports in flight under MEM_BUDGET_GB, and refuse (reason too_big) one that cannot fit alone.
# Measured 2026-11-24: 196 parquet reports are all under 4 GB, 49 TSVs run to 170 GB.
MEM_BUDGET_GB = float(os.environ.get("FRAN_SPECTRUM_MEM_GB", "100"))
TOL_ABS, TOL_REL = 10, 0.001


# --------------------------------------------------------------------------- pure helpers
def _safe(n):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", n or "search")


def _strip_ts(n):
    return re.sub(r"^\d{8}_\d{6}_", "", n or "")


def _base(p):
    return os.path.basename(str(p or "").replace("\\", "/").rstrip("/"))


def report_name(p):
    """Same rule as backfill_fragments.report_name (kept here so planning needs no pandas import)."""
    b = re.sub(r"_Report(_FRAN \(Normal\))?\.(parquet|tsv)$", "", _base(p), flags=re.I)
    return b or _base(os.path.dirname(str(p).replace("\\", "/")))


def is_report(fn):
    f = fn.lower()
    return f.endswith((".parquet", ".tsv")) and "report_fran" in f


def candidate_names(search_name, real_name, output_dir, report_path):
    """Names a search's report directory could carry, with and without the export timestamp."""
    out = []
    for s in (search_name, real_name, re.sub(r"\.sne$", "", _base(output_dir), flags=re.I),
              report_name(report_path) if report_path else None):
        if s:
            for k in (s, _strip_ts(s)):
                if k and k not in out:
                    out.append(k)
    return out


def find_reports(names, output_dir, listdir=os.listdir, isdir=os.path.isdir, isfile=os.path.isfile,
                 getsize=os.path.getsize, roots=None):
    """All archived FRAN reports for a search, NEWEST FIRST, without walking a tree: the search's own
    output_dir when it is a directory, else <root>/<name>/ and its immediate <ts>_<name>/ children.
    A handful of listdir calls per search, never a scan of the whole reports root."""
    roots = REPORT_ROOTS if roots is None else roots
    dirs = []
    if output_dir and not str(output_dir).lower().endswith(".sne") and "\\" not in str(output_dir) \
            and isdir(output_dir):
        dirs.append(output_dir)
    for root in roots:
        for nm in names:
            top = os.path.join(root, nm)
            if isdir(top):
                dirs.append(top)
                try:
                    dirs += [os.path.join(top, e) for e in sorted(listdir(top))
                             if isdir(os.path.join(top, e))]
                except OSError:
                    pass
    found = []
    for d in dirs:
        try:
            ents = listdir(d)
        except OSError:
            continue
        for fn in ents:
            p = os.path.join(d, fn)
            if is_report(fn) and p not in found:
                try:
                    if isfile(p) and getsize(p) > 0:
                        found.append(p)
                except OSError:
                    pass
    return sorted(found, key=lambda p: (os.path.basename(os.path.dirname(p)), p), reverse=True)


RUN_EXT = re.compile(r"\.(d|raw|mzml|wiff2?|tdf)$", re.I)


def norm_run(x):
    """Run name as the lane's `run` column and search_raw_files.raw_path both spell it, made comparable:
    last path component (either slash), extension dropped, lower case."""
    b = str(x).replace("\\", "/").rstrip("/").split("/")[-1]
    return RUN_EXT.sub("", b).lower()


def run_set(values):
    return {norm_run(v) for v in values if v is not None}


def is_suffixed(path):
    return bool(re.search(r"__[0-9a-f]{8}\.lance$", str(path)))


def est_gb(path, nbytes):
    # UNMEASURED factors. They are guesses (parquet decompresses roughly 10x into pandas; a TSV parsed
    # with usecols roughly 1.5x). The child records its real peak RSS per report (peak_rss_gb in the
    # result JSON) so these can be corrected after the first real run.
    return nbytes / 1e9 * (10.0 if str(path).lower().endswith(".parquet") else 1.5) + 1.0


def dir_bytes(path):
    n = 0
    for dp, _dn, fns in os.walk(path):
        for f in fns:
            try:
                n += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return n


def counts_match(lane_n, corpus_n, strict=False):
    """-> 'exact' | 'near' | None. See the module docstring for why 'near' exists."""
    if lane_n is None or not corpus_n:
        return None
    if int(lane_n) == int(corpus_n):
        return "exact"
    if strict:
        return None
    return "near" if abs(int(lane_n) - int(corpus_n)) <= max(TOL_ABS, TOL_REL * int(corpus_n)) else None


def decide(s, reports, registry, on_disk_rows, taken, state, strict=False, exists=os.path.lexists, claims=None):
    """One un-laned search -> a job dict (action write|register|link) or a skip dict.

    s            dict: sid, name, corpus_n, ingested_at, names
    reports      find_reports() result for it (newest first), may be empty
    registry     {lance_path: (row_id, search_id, n_precursors)} for every delimp_spectrum_lane row
    on_disk_rows {lance_path: row count} for datasets that exist on disk and open (only paths asked about)
    taken        set of paths already chosen in this plan (mutated; EVERY chosen path is added)
    state        {sid: {"attempts": n, "parked": bool, ...}}
    exists       path-exists test. ANY entry at a path (file, dir, unopenable dir) makes it occupied.
    claims       {unlinked-row path: [sids of un-laned searches that match it by name and count]}
    """
    claims = claims or {}
    sid, corpus_n = s["sid"], s["corpus_n"]
    st = state.get(sid, {})
    if st.get("parked"):
        return None, dict(sid=sid, name=s["name"], reason="parked", report=bool(reports),
                          detail=st.get("last_error", ""), ingested_at=s["ingested_at"])

    def occupied(p):
        return p in taken or p in registry or bool(exists(p))

    ambiguous = None
    # 1. A dataset by this search's name that is registered with NULL search_id, or already on disk
    #    unregistered, and whose row count is this search's: adopt it rather than write another.
    for nm in s["names"]:
        p = os.path.join(LANCE, _safe(_strip_ts(nm)) + ".lance")
        if p in taken:
            continue
        reg = registry.get(p)
        if reg is not None and reg[1] is None and counts_match(reg[2], corpus_n, strict):
            if len(claims.get(p, ())) > 1:
                ambiguous = p        # two un-laned searches fit this row: never pick one by date
                continue
            taken.add(p)
            return dict(sid=sid, name=s["name"], action="link", row_id=reg[0], lance_path=p,
                        corpus_n=corpus_n, ingested_at=s["ingested_at"]), None
        if reg is None and p in on_disk_rows and counts_match(on_disk_rows[p], corpus_n, strict):
            taken.add(p)
            return dict(sid=sid, name=s["name"], action="register", lance_path=p, corpus_n=corpus_n,
                        report=reports[0] if reports else None, ingested_at=s["ingested_at"]), None
    # 2. Needs a write. No report -> the search cannot get a lane without a Windows re-export.
    if not reports:
        if ambiguous:
            return None, dict(sid=sid, name=s["name"], reason="ambiguous_link", report=False,
                              detail=f"{ambiguous} fits {len(claims[ambiguous])} un-laned searches", ingested_at=s["ingested_at"])
        for nm in s["names"]:
            p = os.path.join(LANCE, _safe(_strip_ts(nm)) + ".lance")
            reg = registry.get(p)
            if reg is not None and reg[1] is not None and counts_match(reg[2], corpus_n, strict) == "exact":
                return None, dict(sid=sid, name=s["name"], reason="dup_of_laned", report=False,
                                  detail=f"{p} (search {reg[1]})", ingested_at=s["ingested_at"])
        return None, dict(sid=sid, name=s["name"], reason="needs_reexport", report=False,
                          detail=s.get("output_dir") or "", ingested_at=s["ingested_at"])
    rep = reports[st.get("attempts", 0) % len(reports)]
    name = report_name(rep)
    plain = os.path.join(LANCE, _safe(_strip_ts(name)) + ".lance")
    suffixed = os.path.join(LANCE, f"{_safe(_strip_ts(name))}__{sid[:8]}.lance")
    prev = st.get("lance_path")     # the path a failed attempt used; its dataset was removed, so it is free again
    path = next((c for c in ([prev] if prev else []) + [plain, suffixed] if not occupied(c)), None)
    if path is None:
        return None, dict(sid=sid, name=s["name"], reason="path_occupied", report=True,
                          detail=f"{plain} and {suffixed} are both taken", ingested_at=s["ingested_at"])
    taken.add(path)
    return dict(sid=sid, name=s["name"], action="write", report=rep, lance_path=path,
                corpus_n=corpus_n, n_candidates=len(reports), ingested_at=s["ingested_at"]), None


# --------------------------------------------------------------------------- state
class StateError(Exception):
    pass


def load_state():
    """No file is a fresh start. A file that exists but cannot be parsed is NOT: loading it as {} would
    forget every attempt count and every parked search, so stop and say so."""
    f = os.path.join(D, "state.json")
    if not os.path.exists(f):
        return {}
    try:
        with open(f) as fh:
            st = json.load(fh)
        if not isinstance(st, dict):
            raise ValueError("not a JSON object")
        return st
    except (OSError, ValueError) as e:
        raise StateError(f"{f} exists but cannot be read ({e!r}); refusing to start from an empty state. "
                         f"Fix or move it by hand.")


def save_state(state):
    """Atomic: write a temp file, rename over state.json. One writer (the parent process)."""
    os.makedirs(D, exist_ok=True)
    tmp = os.path.join(D, f"state.json.tmp.{os.uname().nodename}.{os.getpid()}.{os.environ.get('SLURM_JOB_ID', 'nojob')}")
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1)
    os.replace(tmp, os.path.join(D, "state.json"))


def settle_in_flight(state, jid, active=None):
    """A search left in_flight by a DIFFERENT job died mid-write (OOM, time limit, node loss): count
    it as an attempt. The same job id means a requeue after pre-emption, which is not the data's fault.
    Only searches whose child had actually STARTED are in_flight (see mark_in_flight)."""
    for sid, st in state.items():
        f = st.get("in_flight")
        if f and f != jid and active is not None and sid not in active:
            st.pop("in_flight", None)          # the new plan no longer includes this search: nothing to charge
        elif f and f != jid:
            st["attempts"] = st.get("attempts", 0) + 1
            st["last_error"] = f"job {f} died while writing this lane"
            st["parked"] = st["attempts"] >= MAX_ATTEMPTS
            st.pop("in_flight", None)


def record_failure(state, sid, err, path=None):
    st = state.setdefault(sid, {})
    if path:
        st["lance_path"] = path
    st["attempts"] = st.get("attempts", 0) + 1
    st["last_error"] = str(err)[:400]
    st["last_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    st["parked"] = st["attempts"] >= MAX_ATTEMPTS
    st.pop("in_flight", None)


_state_lock = threading.Lock()


def _squeue_state(jobid):
    """'RUNNING' etc., '' when squeue does not know the job, None when squeue itself failed."""
    try:
        r = subprocess.run(["squeue", "-h", "-j", str(jobid), "-o", "%T"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return "" if "Invalid job id" in (r.stderr or "") else None
    return r.stdout.strip()


def acquire_lock(jid, job_state=_squeue_state):
    """One --apply at a time: the directory D/apply.lock holding this job id. A lock whose job squeue no
    longer knows is stale and is taken over; one whose job is alive, or that cannot be checked, is not.
    --dependency=singleton in the sbatch stays the first line of defence."""
    os.makedirs(D, exist_ok=True)
    lock = os.path.join(D, "apply.lock")
    for _ in range(2):
        try:
            os.mkdir(lock)
            with open(os.path.join(lock, "job"), "w") as fh:
                fh.write(str(jid))
            return lock
        except FileExistsError:
            try:
                holder = open(os.path.join(lock, "job")).read().strip()
            except OSError:
                holder = ""
            if holder == str(jid):
                return lock                                    # our own, after a requeue
            st = job_state(holder) if holder.isdigit() else None
            if st == "":
                shutil.rmtree(lock, ignore_errors=True)        # holder is gone: stale
                continue
            raise StateError(f"another --apply run holds {lock} (job {holder or '?'}, state {st or 'unknown'}); not starting")
    raise StateError(f"could not take {lock}")


def release_lock(lock, jid):
    try:
        if open(os.path.join(lock, "job")).read().strip() == str(jid):
            shutil.rmtree(lock, ignore_errors=True)
    except OSError:
        pass


def append_jsonl(path, obj):
    """Parent-only, one line per finished search, flushed and fsynced: a timeout loses nothing."""
    with open(path, "a") as fh:
        fh.write(json.dumps(obj) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


class ConnLost(Exception):
    pass


def is_conn_error(e):
    try:
        import psycopg2
        return isinstance(e, (psycopg2.OperationalError, psycopg2.InterfaceError))
    except ImportError:
        return False


class Db:
    """The one PG connection, kept honest over a many-hour run: reconnect once on a lost connection."""
    def __init__(self, connect):
        self.connect, self.conn = connect, connect()

    def reconnect(self):
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
        self.conn = self.connect()

    def run(self, fn):
        for attempt in (1, 2):
            try:
                if getattr(self.conn, "closed", 0):
                    self.reconnect()
                return fn(self.conn)
            except Exception as e:  # noqa: BLE001
                if not is_conn_error(e):
                    raise
                if attempt == 2:
                    raise ConnLost(repr(e)[:200])
                try:
                    self.reconnect()
                except Exception as e2:  # noqa: BLE001
                    raise ConnLost(repr(e2)[:200])


def mark_in_flight(state, sid, jid):
    """Immediately before ONE child starts: persist that this search is being written by this job."""
    with _state_lock:
        state.setdefault(sid, {})["in_flight"] = jid
        save_state(state)


# --------------------------------------------------------------------------- plan
def _connect():
    from coreomics_import import _conn
    return _conn()


def gather(conn):
    """Read-only: every un-laned Spectronaut search and the registry."""
    cur = conn.cursor()
    cur.execute("""SELECT s.id, s.search_name, s.output_dir, s.n_precursors_total, s.ingested_at,
                          p.real_search_name, p.report_path, p.output_dir
                     FROM delimp_searches s
                     LEFT JOIN delimp_search_provenance p ON p.search_id = s.id
                    WHERE s.search_engine = 'spectronaut'
                      AND NOT EXISTS (SELECT 1 FROM delimp_spectrum_lane l WHERE l.search_id = s.id)
                    ORDER BY s.ingested_at DESC""")
    rows = cur.fetchall()
    cur.execute("SELECT id, lance_path, search_id, n_precursors FROM delimp_spectrum_lane")
    registry = {r[1]: (r[0], str(r[2]) if r[2] else None, r[3]) for r in cur.fetchall()}
    searches = []
    for sid, name, od, n, ing, real, rpath, pod in rows:
        searches.append(dict(sid=str(sid), name=name, output_dir=od, corpus_n=n, ingested_at=ing.isoformat() if ing else None,
                             names=candidate_names(name, real, od or pod, rpath)))
    return searches, registry


def fill_corpus_counts(conn, searches):
    """n_precursors_total is NULL for a few searches; count them (a search_id filter only, never a scan)."""
    cur = conn.cursor()
    for s in searches:
        if not s["corpus_n"]:
            cur.execute("SELECT count(*) FROM delimp_precursors WHERE search_id = %s", (s["sid"],))
            s["corpus_n"] = cur.fetchone()[0] or None


def corpus_runs(conn, sid):
    """The search's runs, normalised: search_raw_files for THIS search_id (a small table; never a scan)."""
    cur = conn.cursor()
    cur.execute("SELECT raw_path FROM search_raw_files WHERE search_id = %s", (sid,))
    return sorted(run_set(r[0] for r in cur.fetchall()))


def dataset_rows(path):
    try:
        import lance
        return lance.dataset(path).count_rows()
    except Exception:  # noqa: BLE001 - an unopenable dir is "not a dataset"
        return None


def report_run_set(report):
    """Distinct normalised runs with q <= 0.01 in a PARQUET report (the same rows the lane keeps), reading
    only two columns. None for a TSV (a full scan of up to 60 GB; the child checks it before writing)."""
    if not str(report).lower().endswith(".parquet"):
        return None
    import pyarrow.parquet as pq
    import backfill_fragments as B
    names = pq.read_schema(report).names
    cols = B._resolve(names, {"run": B.PREC_MAP["run"], "q": B.PREC_MAP["q_value"]})
    if "run" not in cols:
        return set()
    t = pq.read_table(report, columns=list(dict.fromkeys(cols.values()))).to_pandas()
    if "q" in cols:
        import pandas as pd
        t = t[pd.to_numeric(t[cols["q"]], errors="coerce") <= 0.01]
    return run_set(t[cols["run"]].dropna().unique())


def plan(conn, a, jid):
    searches, registry = gather(conn)
    fill_corpus_counts(conn, searches)
    state = load_state()
    if a.apply:
        settle_in_flight(state, jid, {s["sid"] for s in searches})
    # A dataset a dead run verified and renamed but could not register: register it now, after the same
    # identity check as any other register job. Only while the search is still un-laned.
    pending, taken = [], set()
    active = {s["sid"]: s for s in searches}
    for sid, st in list(state.items()):
        pr = st.get("pending_register")
        if not pr:
            continue
        if sid not in active or not os.path.isdir(pr["lance_path"]) or pr["lance_path"] in registry:
            st.pop("pending_register", None)
            continue
        taken.add(pr["lance_path"])
        pending.append(dict(sid=sid, name=active[sid]["name"], action="register", lance_path=pr["lance_path"],
                            corpus_n=active[sid]["corpus_n"], ingested_at=active[sid]["ingested_at"],
                            corpus_runs=corpus_runs(conn, sid), pending=True,
                            est_gb=round(dir_bytes(pr["lance_path"]) / 1e9 * 10.0 + 1.0, 1)))
    searches = [s for s in searches if s["sid"] not in {j["sid"] for j in pending}]
    # which un-laned searches fit each unlinked registry row (name + count): >1 means ambiguous
    claims = {}
    for s in searches:
        for nm in s["names"]:
            p = os.path.join(LANCE, _safe(_strip_ts(nm)) + ".lance")
            reg = registry.get(p)
            if reg is not None and reg[1] is None and counts_match(reg[2], s["corpus_n"], a.strict):
                claims.setdefault(p, [])
                if s["sid"] not in claims[p]:
                    claims[p].append(s["sid"])
    jobs, skipped = list(pending), []
    n_preflight_refused = 0
    for s in searches:
        reports = find_reports(s["names"], s["output_dir"])
        cruns = None
        if a.preflight and reports:
            cruns = set(corpus_runs(conn, s["sid"]))
            keep = []
            for r in reports:
                try:
                    rs = report_run_set(r)
                except Exception:  # noqa: BLE001 - unreadable report: the child will say why
                    rs = None
                if rs is None or (cruns and rs == cruns):
                    keep.append(r)
            if not keep:
                n_preflight_refused += 1
                skipped.append(dict(sid=s["sid"], name=s["name"], reason="identity_mismatch", report=True,
                                    detail=f"{len(reports)} report(s) on disk, none has this search's run set",
                                    ingested_at=s["ingested_at"]))
                continue
            reports = keep
        on_disk = {}
        for nm in s["names"]:
            p = os.path.join(LANCE, _safe(_strip_ts(nm)) + ".lance")
            if p not in registry and os.path.isdir(p) and p not in on_disk:
                n = dataset_rows(p)
                if n is not None:
                    on_disk[p] = n
        job, skip = decide(s, reports, registry, on_disk, taken, state, a.strict, claims=claims)
        if job:
            job["corpus_runs"] = sorted(cruns) if cruns is not None else corpus_runs(conn, s["sid"])
            if not job["corpus_runs"]:
                skipped.append(dict(sid=s["sid"], name=s["name"], reason="no_corpus_runs", report=bool(job.get("report")),
                                    detail="search_raw_files has no rows for this search; identity cannot be checked",
                                    ingested_at=s["ingested_at"]))
                continue
            if job["action"] == "write":
                try:
                    job["bytes"] = os.path.getsize(job["report"])
                except OSError:
                    job["bytes"] = 0
                job["est_gb"] = round(est_gb(job["report"], job["bytes"]), 1)
                if job["est_gb"] > MEM_BUDGET_GB:
                    skipped.append(dict(sid=s["sid"], name=s["name"], reason="too_big", report=True,
                                        detail=f"{job['report']} ({job['bytes'] / 1e9:.0f} GB, est {job['est_gb']} GB RAM)",
                                        ingested_at=s["ingested_at"]))
                    continue
            elif job["action"] == "register":
                job["est_gb"] = round(dir_bytes(job["lance_path"]) / 1e9 * 10.0 + 1.0, 1)   # whole table + md5
            else:
                job["est_gb"] = 1.0                                                          # runs column only
            jobs.append(job)
        else:
            skipped.append(skip)
    why = {}
    for k in skipped:
        why[k["reason"]] = why.get(k["reason"], 0) + 1
    acts = {}
    for j in jobs:
        acts[j["action"]] = acts.get(j["action"], 0) + 1
    # Cheap adoptions first, then writes: smallest report first.
    order = {"link": 0, "register": 1, "write": 2}
    jobs = [j for j in jobs if not j.get("corpus_runs") == []]   # (identity impossible; handled in do_job too)
    jobs.sort(key=lambda j: (order[j["action"]], j.get("bytes", 0)))
    sel = []
    n_write = 0
    for j in jobs:
        if j["action"] == "write":
            if a.max and n_write >= a.max:
                continue
            n_write += 1
        sel.append(j)
    # report_sids: every un-laned search that HAS a usable report (planned or parked): the health scan's RED set.
    report_sids = [j["sid"] for j in jobs if j["action"] == "write"] + \
                  [k["sid"] for k in skipped if k["reason"] == "parked" and k.get("report")]
    summary = dict(at=time.strftime("%Y-%m-%dT%H:%M:%S"), unlaned=len(searches),
                   planned={**acts, "selected_writes": n_write},
                   write_bytes=sum(j.get("bytes", 0) for j in jobs if j["action"] == "write"),
                   skipped=why, apply=bool(a.apply), preflight=bool(a.preflight), max=a.max, job=jid)
    os.makedirs(D, exist_ok=True)
    out = os.path.join(D, f"{'plan' if a.apply else 'dryrun_plan'}_{jid}.json")
    with open(out, "w") as fh:
        json.dump(dict(summary=summary, jobs=sel, all_jobs=len(jobs), skipped=skipped, report_sids=report_sids), fh, indent=1)
    print(json.dumps(summary), flush=True)
    return sel, state, out


# --------------------------------------------------------------------------- child (one report)
def _peak_rss_gb():
    import resource
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(r / (1024 ** 3 if sys.platform == "darwin" else 1024 ** 2), 2)   # KB on Linux, bytes on macOS


def _rm_own(path, jid):
    """Delete a directory this run created: only ever <x>.incoming.<this job id>."""
    if path.endswith(f".incoming.{jid}") and os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def child_write(report, out, name, corpus_n, allow_near, runs, jid):
    """Parse one report with backfill_fragments' own machinery and write the dataset, safely.

    Own process, so a memory kill is attributable to this report and cannot take the others with it.
    Order: refuse if anything is at `out`; parse; the report's runs must equal the corpus runs; the
    precursor count must match; write to <out>.incoming.<jid> (mode create); reopen; rename to `out`.
    Nothing is written to `out` before every check has passed, so a failure leaves nothing there."""
    import backfill_fragments as B
    import pyarrow as pa
    import spectrum_lance as sln
    if os.path.lexists(out):
        return dict(ok=False, error=f"refusing: something already exists at {out}")
    inc = f"{out}.incoming.{jid}"
    _rm_own(inc, jid)                       # this job's own leftover after a requeue
    fdf, pdf = B._extract(report)
    if fdf is None or fdf.empty or pdf is None or pdf.empty:
        return dict(ok=False, error="no fragment-level rows in report")
    if "run" not in pdf.columns:
        return dict(ok=False, error="report has no run column; identity cannot be checked")
    lane_runs = run_set(pdf["run"].dropna().unique())
    want = set(runs)
    if not want or not lane_runs:
        return dict(ok=False, error=f"identity cannot be established: search has {len(want)} runs, report has {len(lane_runs)}")
    if lane_runs != want:
        return dict(ok=False, error=f"identity: report has {len(lane_runs)} runs, the search has {len(want)}; "
                                    f"report-only {sorted(lane_runs - want)[:2]}, search-only {sorted(want - lane_runs)[:2]}")
    tbl, n_frag = B._assemble_table(fdf, pdf)
    del fdf, pdf
    tbl = tbl.set_column(tbl.schema.get_field_index("search_name"), "search_name",
                         pa.array([name] * tbl.num_rows, pa.string()))
    how = counts_match(tbl.num_rows, corpus_n, strict=not allow_near)
    if not how:
        return dict(ok=False, error=f"precursor count mismatch before write: report {tbl.num_rows} vs corpus {corpus_n}"
                                    + ("" if allow_near else " (exact required on a suffixed path)"))
    try:
        n, md5, ver = sln.write_lance(tbl, inc, mode="create")
        back = dataset_rows(inc)
        if back != n:
            _rm_own(inc, jid)
            return dict(ok=False, error=f"incoming dataset reads back {back} rows, wrote {n}")
        if os.path.lexists(out):
            _rm_own(inc, jid)
            return dict(ok=False, error=f"refusing: {out} appeared during the write")
        os.rename(inc, out)
    except Exception as e:  # noqa: BLE001
        _rm_own(inc, jid)
        return dict(ok=False, error=f"write failed: {repr(e)[:300]}")
    return dict(ok=True, n_prec=n, n_frag=int(n_frag), md5=md5, version=int(ver), n_back=dataset_rows(out),
                n_runs=len(lane_runs), peak_rss_gb=_peak_rss_gb())


def child_describe(out, runs_only=False):
    """Open an existing dataset: its distinct runs, and (unless runs_only) what the registry needs."""
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc
    import spectrum_lance as sln
    ds = lance.dataset(out)
    if runs_only:
        runs = sorted(run_set(ds.to_table(columns=["run"]).column("run").unique().to_pylist()))
        n = ds.count_rows()
        return dict(ok=True, n_prec=n, n_back=n, runs=runs, n_runs=len(runs), peak_rss_gb=_peak_rss_gb())
    sub = pa.schema([f for f in sln.SCHEMA if f.name in set(ds.schema.names)])
    tbl = ds.to_table().cast(sub)
    n_frag = int(pc.sum(pc.list_value_length(tbl["frg_mz"])).as_py() or 0) if "frg_mz" in tbl.column_names else 0
    runs = sorted(run_set(tbl["run"].unique().to_pylist())) if "run" in tbl.column_names else []
    return dict(ok=True, n_prec=tbl.num_rows, n_frag=n_frag, md5=sln.content_md5(tbl),
                version=int(ds.version), n_back=tbl.num_rows, runs=runs, n_runs=len(runs), peak_rss_gb=_peak_rss_gb())


def run_child(args, stdin=None):
    r = subprocess.run([sys.executable, "-u", os.path.abspath(__file__)] + args,
                       capture_output=True, text=True, input=stdin)
    for ln in reversed((r.stdout or "").splitlines()):
        if ln.startswith("CHILD_RESULT "):
            try:
                return json.loads(ln[len("CHILD_RESULT "):]), r.returncode
            except ValueError:
                break
    tail = " / ".join(((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-3:])[:400]
    return dict(ok=False, error=f"child rc={r.returncode} (no result; killed?) {tail}"), r.returncode


# --------------------------------------------------------------------------- apply
def inside_lance(path):
    return os.path.abspath(path).startswith(os.path.abspath(LANCE) + os.sep) and path.endswith(".lance")


class MemBudget:
    """Blocks a worker until its estimated RAM fits beside the reports already in flight."""
    def __init__(self, gb):
        self.gb, self.used, self.cv = gb, 0.0, threading.Condition()

    def acquire(self, need):
        with self.cv:
            while self.used > 0 and self.used + need > self.gb:
                self.cv.wait()
            self.used += need

    def release(self, need):
        with self.cv:
            self.used -= need
            self.cv.notify_all()


def do_job(j, strict, jid="local"):
    """Child work for a write/register/link job. Returns an outcome dict (never raises)."""
    t0 = time.time()
    if not inside_lance(j["lance_path"]):
        return dict(sid=j["sid"], name=j.get("name"), action=j["action"], status="failed", error="path outside the lane dir")
    allow_near = not is_suffixed(j["lance_path"]) and not strict
    if j["action"] == "write":
        res, _ = run_child(["_write", "--report", j["report"], "--out", j["lance_path"], "--name", j["name"],
                            "--corpus-n", str(j["corpus_n"]), "--allow-near", "1" if allow_near else "0", "--jid", str(jid)],
                           stdin=json.dumps(j["corpus_runs"]))
    else:
        res, _ = run_child(["_describe", "--out", j["lance_path"]] + (["--runs-only"] if j["action"] == "link" else []))
    out = dict(sid=j["sid"], name=j["name"], action=j["action"], lance_path=j["lance_path"],
               seconds=round(time.time() - t0), peak_rss_gb=res.get("peak_rss_gb"), est_gb=j.get("est_gb"),
               res={k: v for k, v in res.items() if k != "runs"})
    if not res.get("ok"):
        out.update(status="failed", error=res.get("error", "child failed"))
        return out

    def fail(msg):
        # The child already renamed a write into place: take it off the real path, so nothing
        # unverified stays at a final path.
        if j["action"] == "write":
            quarantine(j, jid)
        out.update(status="failed", error=msg)
        return out
    if res.get("n_back") != res["n_prec"]:
        return fail(f"dataset reads back {res.get('n_back')} rows, wrote {res['n_prec']}")
    if j["action"] != "write":
        if not j.get("corpus_runs") or not res.get("runs"):
            return fail(f"identity cannot be established: search has {len(j.get('corpus_runs') or [])} runs, "
                        f"dataset has {len(res.get('runs') or [])}")
        if set(res["runs"]) != set(j["corpus_runs"]):
            return fail(f"identity: dataset runs {res.get('n_runs')} != search runs {len(j['corpus_runs'])}")
    elif not res.get("n_runs") or not j.get("corpus_runs"):
        return fail("identity cannot be established: no runs")
    how = counts_match(res["n_prec"], j["corpus_n"], strict=not allow_near)
    out["count_check"] = how
    if not how:
        return fail(f"precursor count mismatch: lane {res['n_prec']} vs corpus {j['corpus_n']} (not registered)")
    out["status"] = "verified"
    return out


# The fill's OWN registration. spectrum_lance.register() COALESCEs search_id, overwrites counts and md5
# of whatever row is at the path, and commits before anyone can check. This can only insert a new row
# or update a row that is unlinked or already this search's; anything else affects zero rows.
UPSERT_SQL = """INSERT INTO delimp_spectrum_lane
                  (lance_path, search_id, search_name, n_precursors, n_fragments, content_md5,
                   lance_version, writer_version, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (lance_path) DO UPDATE SET
                  search_id = EXCLUDED.search_id, search_name = EXCLUDED.search_name,
                  n_precursors = EXCLUDED.n_precursors, n_fragments = EXCLUDED.n_fragments,
                  content_md5 = EXCLUDED.content_md5, lance_version = EXCLUDED.lance_version,
                  writer_version = EXCLUDED.writer_version, updated_at = now()
                WHERE delimp_spectrum_lane.search_id IS NULL
                   OR delimp_spectrum_lane.search_id = EXCLUDED.search_id"""

LINK_SQL = """UPDATE delimp_spectrum_lane SET search_id = %s, updated_at = now()
               WHERE id = %s AND lance_path = %s AND search_id IS NULL"""


def register_row(conn, j, r):
    from versions import SPECTRUM_LANE_WRITER_VERSION
    cur = conn.cursor()
    cur.execute(UPSERT_SQL, (j["lance_path"], j["sid"], j["name"], int(r["n_prec"]), int(r["n_frag"]),
                             r["md5"], int(r["version"]), SPECTRUM_LANE_WRITER_VERSION))
    if cur.rowcount != 1:
        conn.rollback()
        raise RuntimeError(f"registry row at {j['lance_path']} belongs to another search; nothing was changed")
    conn.commit()


def link_row(conn, j):
    cur = conn.cursor()
    cur.execute(LINK_SQL, (j["sid"], j["row_id"], j["lance_path"]))
    if cur.rowcount != 1:
        # after a reconnect-retry the first attempt may have committed: that is success, not a failure
        cur.execute("SELECT search_id FROM delimp_spectrum_lane WHERE id = %s", (j["row_id"],))
        row = cur.fetchone()
        if not (row and row[0] is not None and str(row[0]) == j["sid"]):
            conn.rollback()
            raise RuntimeError("row was no longer unlinked")
    conn.commit()


def quarantine(j, jid):
    """A dataset this run created could not be registered: take it off the real path (kept, not deleted)."""
    p = j["lance_path"]
    if j["action"] == "write" and os.path.isdir(p):
        try:
            os.rename(p, f"{p}.failed.{jid}")
        except OSError:
            pass


def apply_jobs(db, jobs, state, a, jid, jsonl=None):
    """Run the selected jobs. Returns (outcomes, aborted_reason or None)."""
    outcomes = []
    work = list(jobs)
    budget = MemBudget(MEM_BUDGET_GB)
    db_lock = threading.Lock()      # one psycopg2 connection: serialise its use
    stop = threading.Event()
    why = []

    def gated(j):
        if stop.is_set():
            return dict(sid=j["sid"], name=j["name"], action=j["action"], status="not_run",
                        error="stopped: the database connection was lost")
        need = j.get("est_gb", 4.0)
        budget.acquire(need)
        try:
            if stop.is_set():
                return dict(sid=j["sid"], name=j["name"], action=j["action"], status="not_run",
                            error="stopped: the database connection was lost")
            if j["action"] == "write":
                mark_in_flight(state, j["sid"], jid)     # persisted before the child starts, only for this search
            o = do_job(j, a.strict, jid)
            if o["status"] == "verified":
                try:
                    with db_lock:
                        if j["action"] == "link":
                            db.run(lambda c: link_row(c, j))
                            o["status"] = "linked"
                        else:
                            db.run(lambda c: register_row(c, j, o["res"]))
                            o["status"] = "registered"
                except ConnLost as e:
                    # Not the dataset's fault: leave it at its final path, remember it, stop the job.
                    stop.set()
                    why.append(f"database connection lost: {e}")
                    if j["action"] == "link":
                        o.update(status="not_run", error=f"connection lost: {e}")
                    else:
                        o.update(status="pending_register", error=f"connection lost: {e}")
                except Exception as e:  # noqa: BLE001
                    try:
                        db.conn.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                    o.update(status="failed", error=repr(e)[:400])
                    quarantine(j, jid)
            return o
        finally:
            if j["action"] == "write":
                with _state_lock:                      # the child has ended: it is no longer in flight
                    if state.get(j["sid"], {}).pop("in_flight", None):
                        save_state(state)
            budget.release(need)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futs = {ex.submit(gated, j): j for j in work}
        for f in concurrent.futures.as_completed(futs):
            j = futs[f]
            try:
                o = f.result()
            except Exception as e:  # noqa: BLE001 - recorded; the rest still run
                o = dict(sid=j["sid"], name=j["name"], action=j["action"], status="failed", error=repr(e)[:400])
            with _state_lock:
                if o["status"] == "failed":
                    record_failure(state, j["sid"], o["error"], j.get("lance_path") if j["action"] == "write" else None)
                elif o["status"] == "pending_register":
                    res = o.get("res", {})
                    st = state.setdefault(j["sid"], {})
                    st["pending_register"] = dict(lance_path=j["lance_path"], n_prec=res.get("n_prec"), at=time.strftime("%FT%T"))
                    st.pop("in_flight", None)
                elif o["status"] != "not_run":
                    state.pop(j["sid"], None)
                save_state(state)
            outcomes.append(o)
            if jsonl:
                append_jsonl(jsonl, dict(o, at=time.strftime("%FT%T")))
            print(f"--- {j['name']} [{j['action']}] {o['status']}"
                  + (f" ({o.get('count_check')})" if o.get("count_check") else "")
                  + (f": {o['error']}" if o.get("error") else ""), flush=True)
    return outcomes, ("; ".join(why) or None)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd")
    w = sp.add_parser("_write")
    w.add_argument("--report", required=True)
    w.add_argument("--out", required=True)
    w.add_argument("--name", required=True)
    w.add_argument("--corpus-n", type=int, required=True)
    w.add_argument("--allow-near", type=int, default=0)
    w.add_argument("--jid", required=True)
    d = sp.add_parser("_describe")
    d.add_argument("--out", required=True)
    d.add_argument("--runs-only", action="store_true")
    ap.add_argument("--apply", action="store_true", help="write (default: plan only)")
    ap.add_argument("--max", type=int, default=40, help="at most N lane WRITES per run; 0 = NO CAP (the one-off catch-up)")
    ap.add_argument("--workers", type=int, default=4, help="reports parsed at once (memory-gated by MEM_BUDGET_GB)")
    ap.add_argument("--strict", action="store_true", help="'near' count matches fail too")
    ap.add_argument("--preflight", action="store_true",
                    help="plan: read each PARQUET report's run column and drop reports whose runs are not the search's")
    a = ap.parse_args()
    if a.cmd == "_write":
        try:
            res = child_write(a.report, a.out, a.name, a.corpus_n, bool(a.allow_near), json.load(sys.stdin), a.jid)
        except Exception as e:  # noqa: BLE001
            res = dict(ok=False, error=repr(e)[:400])
        print("CHILD_RESULT " + json.dumps(res))
        return 0
    if a.cmd == "_describe":
        try:
            res = child_describe(a.out, a.runs_only)
        except Exception as e:  # noqa: BLE001
            res = dict(ok=False, error=repr(e)[:400])
        print("CHILD_RESULT " + json.dumps(res))
        return 0

    jid = os.environ.get("SLURM_JOB_ID", "local" + time.strftime("%H%M%S"))
    print(f"fill_spectrum_lanes job {jid}: {'APPLY' if a.apply else 'dry run'}; write cap --max {a.max}"
          f"{' (NO CAP)' if a.max == 0 else ''}; workers {a.workers}; preflight {a.preflight}", flush=True)
    lock = None
    try:
        if a.apply:
            lock = acquire_lock(jid)
        return run_main(a, jid)
    except StateError as e:
        print(f"STOP: {e}", flush=True)
        return 3
    finally:
        if lock:
            release_lock(lock, jid)


def run_main(a, jid):
    db = Db(_connect)
    conn = db.conn
    if not a.apply:
        conn.set_session(readonly=True, autocommit=True)
    jobs, state, plan_path = plan(conn, a, jid)
    if not a.apply:
        print(f"dry run: {len(jobs)} job(s) selected; plan {plan_path}")
        conn.close()
        return 0
    import spectrum_lance as sln
    sln.ensure_registry(conn)
    conn.commit()        # end the planning transaction: no idle-in-transaction session through hours of writes
    jsonl = os.path.join(D, f"outcomes_{jid}.jsonl")
    outcomes, aborted = apply_jobs(db, jobs, state, a, jid, jsonl)
    null_rows = None
    try:
        def q(c):
            cur = c.cursor()
            cur.execute("SELECT count(*) FROM delimp_spectrum_lane WHERE search_id IS NULL")
            n = cur.fetchone()[0]
            c.commit()
            return n
        null_rows = db.run(q)
        db.conn.close()
    except Exception:  # noqa: BLE001
        pass
    n_fail = sum(1 for o in outcomes if o["status"] == "failed")
    counts = {}
    for o in outcomes:
        counts[o["status"]] = counts.get(o["status"], 0) + 1
    parked = [sid for sid, st in state.items() if st.get("parked")]
    near = [dict(name=o["name"], sid=o["sid"], lance_path=o["lance_path"], lane=o["res"].get("n_prec"))
            for o in outcomes if o.get("count_check") == "near"]
    res = dict(summary=dict(at=time.strftime("%Y-%m-%dT%H:%M:%S"), job=jid, outcomes=counts, failed=n_fail,
                            aborted=aborted, parked=len(parked), null_search_id_rows=null_rows, near_lanes=len(near),
                            max_peak_rss_gb=max([o.get("peak_rss_gb") or 0 for o in outcomes] or [0])),
               near_lanes=near, outcomes=outcomes)
    os.makedirs(D, exist_ok=True)
    with open(os.path.join(D, f"result_{jid}.json"), "w") as fh:
        json.dump(res, fh, indent=1)
    print(json.dumps(res["summary"]))
    if aborted:
        print(f"ABORTED: {aborted}; verified datasets stay at their final paths, pending_register is in state.json", flush=True)
        return 2
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
