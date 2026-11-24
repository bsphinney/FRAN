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
                set its search_id (the exact-match case; link_spectrum_lane.py then runs for the
                rest).
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
import subprocess
import sys
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


def est_gb(path, nbytes):
    return nbytes / 1e9 * (10.0 if str(path).lower().endswith(".parquet") else 1.5) + 1.0


def counts_match(lane_n, corpus_n, strict=False):
    """-> 'exact' | 'near' | None. See the module docstring for why 'near' exists."""
    if lane_n is None or not corpus_n:
        return None
    if int(lane_n) == int(corpus_n):
        return "exact"
    if strict:
        return None
    return "near" if abs(int(lane_n) - int(corpus_n)) <= max(TOL_ABS, TOL_REL * int(corpus_n)) else None


def lance_path_for(name, sid, taken):
    """Plain <name>.lance when nobody holds it; else <name>__<sid8>.lance. `taken` is a set of paths
    held by the registry, by a dataset on disk, or by an earlier search in this plan."""
    base = os.path.join(LANCE, _safe(_strip_ts(name)) + ".lance")
    if base not in taken:
        return base
    return os.path.join(LANCE, f"{_safe(_strip_ts(name))}__{str(sid)[:8]}.lance")


def decide(s, reports, registry, on_disk_rows, taken, state, strict=False):
    """One un-laned search -> a job dict (action write|register|link) or a skip dict.

    s            dict: sid, name, corpus_n, ingested_at, names
    reports      find_reports() result for it (newest first), may be empty
    registry     {lance_path: (row_id, search_id, n_precursors)} for every delimp_spectrum_lane row
    on_disk_rows {lance_path: row count} for datasets that exist on disk (only paths asked about)
    taken        set of paths already claimed (mutated)
    state        {sid: {"attempts": n, "parked": bool, ...}}
    """
    sid, corpus_n = s["sid"], s["corpus_n"]
    st = state.get(sid, {})
    if st.get("parked"):
        return None, dict(sid=sid, name=s["name"], reason="parked", report=bool(reports),
                          detail=st.get("last_error", ""), ingested_at=s["ingested_at"])
    # 1. A dataset by this search's name that is registered with NULL search_id, or already on disk
    #    unregistered, and whose row count is this search's: adopt it rather than write another.
    for nm in s["names"]:
        p = os.path.join(LANCE, _safe(_strip_ts(nm)) + ".lance")
        if p in taken:
            continue
        reg = registry.get(p)
        if reg is not None and reg[1] is None and counts_match(reg[2], corpus_n, strict):
            taken.add(p)
            return dict(sid=sid, name=s["name"], action="link", row_id=reg[0], lance_path=p,
                        corpus_n=corpus_n, ingested_at=s["ingested_at"]), None
        if reg is None and p in on_disk_rows and counts_match(on_disk_rows[p], corpus_n, strict):
            taken.add(p)
            return dict(sid=sid, name=s["name"], action="register", lance_path=p, corpus_n=corpus_n,
                        report=reports[0] if reports else None, ingested_at=s["ingested_at"]), None
    # 2. Needs a write. No report -> the search cannot get a lane without a Windows re-export.
    if not reports:
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
    # A retry reuses the path its failed attempt used (the dataset there is unregistered and untrusted).
    prev = st.get("lance_path")
    path = prev if prev and prev not in registry else \
        lance_path_for(name, sid, taken | set(registry) | set(on_disk_rows))
    taken.add(path)
    return dict(sid=sid, name=s["name"], action="write", report=rep, lance_path=path,
                corpus_n=corpus_n, n_candidates=len(reports), ingested_at=s["ingested_at"]), None


# --------------------------------------------------------------------------- state
def load_state():
    try:
        return json.load(open(os.path.join(D, "state.json")))
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(D, exist_ok=True)
    tmp = os.path.join(D, f"state.json.tmp{os.getpid()}")
    json.dump(state, open(tmp, "w"), indent=1)
    os.replace(tmp, os.path.join(D, "state.json"))


def settle_in_flight(state, jid):
    """A search left in_flight by a DIFFERENT job died mid-write (OOM, time limit, node loss): count
    it as an attempt. The same job id means a requeue after pre-emption, which is not the data's fault."""
    for sid, st in state.items():
        f = st.get("in_flight")
        if f and f != jid:
            st["attempts"] = st.get("attempts", 0) + 1
            st["last_error"] = f"job {f} died while writing this lane"
            st["parked"] = st["attempts"] >= MAX_ATTEMPTS
        if f and f != jid:
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


# --------------------------------------------------------------------------- plan
def _connect():
    from coreomics_import import _conn
    return _conn()


def gather(conn, fs=None):
    """Read-only: every un-laned Spectronaut search, the registry, and what is on disk for them."""
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


def dataset_rows(path):
    try:
        import lance
        return lance.dataset(path).count_rows()
    except Exception:  # noqa: BLE001 - an unopenable dir is "not a dataset"
        return None


def plan(conn, a, jid):
    searches, registry = gather(conn)
    fill_corpus_counts(conn, searches)
    state = load_state()
    if a.apply:
        settle_in_flight(state, jid)
    taken, jobs, skipped = set(), [], []
    for s in searches:
        reports = find_reports(s["names"], s["output_dir"])
        on_disk = {}
        for nm in s["names"]:
            p = os.path.join(LANCE, _safe(_strip_ts(nm)) + ".lance")
            if p not in registry and os.path.isdir(p) and p not in on_disk:
                n = dataset_rows(p)
                if n is not None:
                    on_disk[p] = n
        job, skip = decide(s, reports, registry, on_disk, taken, state, a.strict)
        if job:
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
            jobs.append(job)
        else:
            skipped.append(skip)
    why = {}
    for k in skipped:
        why[k["reason"]] = why.get(k["reason"], 0) + 1
    acts = {}
    for j in jobs:
        acts[j["action"]] = acts.get(j["action"], 0) + 1
    # Cheap adoptions first, then writes: newest searches first, smallest report first within a day.
    order = {"link": 0, "register": 1, "write": 2}
    jobs.sort(key=lambda j: (order[j["action"]], j.get("bytes", 0)))
    sel = []
    n_write = 0
    for j in jobs:
        if j["action"] == "write":
            if a.max and n_write >= a.max:
                continue
            n_write += 1
        sel.append(j)
    # report_sids: every un-laned search that HAS a report (planned or parked): the health scan's RED set.
    report_sids = [j["sid"] for j in jobs if j["action"] == "write"] + \
                  [k["sid"] for k in skipped if k["reason"] == "parked" and k.get("report")]
    summary = dict(at=time.strftime("%Y-%m-%dT%H:%M:%S"), unlaned=len(searches),
                   planned={**acts, "selected_writes": n_write},
                   write_bytes=sum(j.get("bytes", 0) for j in jobs if j["action"] == "write"),
                   skipped=why, apply=bool(a.apply), job=jid)
    os.makedirs(D, exist_ok=True)
    out = os.path.join(D, f"{'plan' if a.apply else 'dryrun_plan'}_{jid}.json")
    json.dump(dict(summary=summary, jobs=sel, all_jobs=len(jobs), skipped=skipped,
                   report_sids=report_sids), open(out, "w"), indent=1)
    print(json.dumps(summary), flush=True)
    return sel, state, out


# --------------------------------------------------------------------------- child (one report)
def child_write(report, out, name):
    """Parse one report with backfill_fragments' own machinery and write the dataset. Own process, so
    a memory kill is attributable to this report and cannot take the others with it."""
    import backfill_fragments as B
    import pyarrow as pa
    import spectrum_lance as sln
    fdf, pdf = B._extract(report)
    if fdf is None or fdf.empty or pdf is None or pdf.empty:
        return dict(ok=False, error="no fragment-level rows in report")
    tbl, n_frag = B._assemble_table(fdf, pdf)
    del fdf, pdf
    tbl = tbl.set_column(tbl.schema.get_field_index("search_name"), "search_name",
                         pa.array([name] * tbl.num_rows, pa.string()))
    n, md5, ver = sln.write_lance(tbl, out, mode="overwrite")
    back = dataset_rows(out)
    return dict(ok=True, n_prec=n, n_frag=int(n_frag), md5=md5, version=int(ver), n_back=back)


def child_describe(out):
    """Open an existing dataset and compute what the registry needs (row/fragment counts, md5)."""
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc
    import spectrum_lance as sln
    ds = lance.dataset(out)
    sub = pa.schema([f for f in sln.SCHEMA if f.name in set(ds.schema.names)])
    tbl = ds.to_table().cast(sub)
    n_frag = int(pc.sum(pc.list_value_length(tbl["frg_mz"])).as_py() or 0) if "frg_mz" in tbl.column_names else 0
    return dict(ok=True, n_prec=tbl.num_rows, n_frag=n_frag, md5=sln.content_md5(tbl),
                version=int(ds.version), n_back=tbl.num_rows)


def run_child(args):
    r = subprocess.run([sys.executable, "-u", os.path.abspath(__file__)] + args,
                       capture_output=True, text=True)
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
        import threading
        self.gb, self.used, self.cv = gb, 0.0, threading.Condition()

    def __enter__(self):
        return self

    def acquire(self, need):
        with self.cv:
            while self.used > 0 and self.used + need > self.gb:
                self.cv.wait()
            self.used += need

    def release(self, need):
        with self.cv:
            self.used -= need
            self.cv.notify_all()


def do_job(j, strict):
    """Child work for a write/register job. Returns an outcome dict (never raises)."""
    t0 = time.time()
    if not inside_lance(j["lance_path"]):
        return dict(sid=j["sid"], action=j["action"], status="failed", error="path outside the lane dir")
    if j["action"] == "write":
        res, _ = run_child(["_write", "--report", j["report"], "--out", j["lance_path"], "--name", j["name"]])
    else:
        res, _ = run_child(["_describe", "--out", j["lance_path"]])
    out = dict(sid=j["sid"], name=j["name"], action=j["action"], lance_path=j["lance_path"],
               seconds=round(time.time() - t0), res=res)
    if not res.get("ok"):
        out.update(status="failed", error=res.get("error", "child failed"))
        return out
    n_back = res.get("n_back")
    if n_back != res["n_prec"]:
        out.update(status="failed", error=f"dataset reads back {n_back} rows, wrote {res['n_prec']}")
        return out
    how = counts_match(res["n_prec"], j["corpus_n"], strict)
    out["count_check"] = how
    if not how:
        out.update(status="failed",
                   error=f"precursor count mismatch: lane {res['n_prec']} vs corpus {j['corpus_n']} (not registered)")
        return out
    out["status"] = "verified"
    return out


def register_outcome(conn, j, o):
    import spectrum_lance as sln
    r = o["res"]
    sln.register(conn, j["sid"], j["name"], j["lance_path"], r["n_prec"], r["n_frag"], r["md5"], r["version"])
    cur = conn.cursor()
    cur.execute("SELECT search_id FROM delimp_spectrum_lane WHERE lance_path = %s", (j["lance_path"],))
    row = cur.fetchone()
    if not row or str(row[0]) != j["sid"]:
        raise RuntimeError(f"registry row at {j['lance_path']} has search_id {row and row[0]}, expected {j['sid']}")


def apply_jobs(conn, jobs, state, a, jid):
    outcomes = []
    cur = conn.cursor()
    # links: cheap, in the parent
    for j in [x for x in jobs if x["action"] == "link"]:
        try:
            cur.execute("""UPDATE delimp_spectrum_lane SET search_id = %s, updated_at = now()
                            WHERE id = %s AND search_id IS NULL""", (j["sid"], j["row_id"]))
            ok = cur.rowcount == 1
            conn.commit()
            outcomes.append(dict(sid=j["sid"], name=j["name"], action="link", status="linked" if ok else "failed",
                                 error=None if ok else "row was no longer unlinked"))
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            outcomes.append(dict(sid=j["sid"], name=j["name"], action="link", status="failed", error=repr(e)[:300]))
    work = [x for x in jobs if x["action"] in ("write", "register")]
    for j in work:
        if j["action"] == "write":
            state.setdefault(j["sid"], {})["in_flight"] = jid     # so a kill is counted next run
    save_state(state)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        budget = MemBudget(MEM_BUDGET_GB)

        def gated(j):
            need = j.get("est_gb", 4.0)
            budget.acquire(need)
            try:
                return do_job(j, a.strict)
            finally:
                budget.release(need)
        futs = {ex.submit(gated, j): j for j in work}
        for f in concurrent.futures.as_completed(futs):
            j = futs[f]
            try:
                o = f.result()
                if o["status"] == "verified":
                    register_outcome(conn, j, o)
                    o["status"] = "registered"
            except Exception as e:  # noqa: BLE001 - recorded; the rest still run
                conn.rollback()
                o = dict(sid=j["sid"], name=j["name"], action=j["action"], status="failed", error=repr(e)[:400])
            if o["status"] == "failed":
                record_failure(state, j["sid"], o["error"], j.get("lance_path") if j["action"] == "write" else None)
            else:
                state.pop(j["sid"], None)
            save_state(state)
            outcomes.append(o)
            print(f"--- {j['name']} [{j['action']}] {o['status']}"
                  + (f" ({o.get('count_check')}, {o['res']['n_prec']:,} precursors)" if o["status"] == "registered" else "")
                  + (f": {o['error']}" if o.get("error") else ""), flush=True)
    return outcomes


def run_linker(apply):
    """link_spectrum_lane.py for the rest: it links only unambiguous names (never mislinks)."""
    r = subprocess.run([sys.executable, "-u", os.path.join(HERE, "link_spectrum_lane.py")]
                       + ([] if apply else ["--dry-run"]), capture_output=True, text=True)
    return dict(rc=r.returncode, tail=((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-4:])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd")
    w = sp.add_parser("_write")
    w.add_argument("--report", required=True)
    w.add_argument("--out", required=True)
    w.add_argument("--name", required=True)
    d = sp.add_parser("_describe")
    d.add_argument("--out", required=True)
    ap.add_argument("--apply", action="store_true", help="write (default: plan only)")
    ap.add_argument("--max", type=int, default=40, help="at most N lane WRITES per run (0 = no cap)")
    ap.add_argument("--workers", type=int, default=4, help="reports parsed at once (memory: ~25 GB each, worst case)")
    ap.add_argument("--strict", action="store_true", help="'near' count matches fail too")
    a = ap.parse_args()
    if a.cmd == "_write":
        try:
            res = child_write(a.report, a.out, a.name)
        except Exception as e:  # noqa: BLE001
            res = dict(ok=False, error=repr(e)[:400])
        print("CHILD_RESULT " + json.dumps(res))
        return 0
    if a.cmd == "_describe":
        try:
            res = child_describe(a.out)
        except Exception as e:  # noqa: BLE001
            res = dict(ok=False, error=repr(e)[:400])
        print("CHILD_RESULT " + json.dumps(res))
        return 0

    jid = os.environ.get("SLURM_JOB_ID", "local" + time.strftime("%H%M%S"))
    conn = _connect()
    if not a.apply:
        conn.set_session(readonly=True, autocommit=True)
    jobs, state, plan_path = plan(conn, a, jid)
    if not a.apply:
        print(f"dry run: {len(jobs)} job(s) selected; plan {plan_path}; linker preview: {run_linker(False)}")
        conn.close()
        return 0
    import spectrum_lance as sln
    sln.ensure_registry(conn)
    outcomes = apply_jobs(conn, jobs, state, a, jid)
    linker = run_linker(True)
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM delimp_spectrum_lane WHERE search_id IS NULL")
    null_rows = cur.fetchone()[0]
    conn.close()
    n_fail = sum(1 for o in outcomes if o["status"] == "failed")
    counts = {}
    for o in outcomes:
        counts[o["status"]] = counts.get(o["status"], 0) + 1
    parked = [sid for sid, st in state.items() if st.get("parked")]
    res = dict(summary=dict(at=time.strftime("%Y-%m-%dT%H:%M:%S"), job=jid, outcomes=counts, failed=n_fail,
                            parked=len(parked), null_search_id_rows=null_rows, linker=linker),
               outcomes=outcomes)
    os.makedirs(D, exist_ok=True)
    out = os.path.join(D, f"result_{jid}.json")
    json.dump(res, open(out, "w"), indent=1)
    print(json.dumps(res["summary"]))
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
