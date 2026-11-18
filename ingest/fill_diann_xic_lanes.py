"""fill_diann_xic_lanes.py -- daily: write the XIC lane for every DIA-NN search that has none.

WHY THIS EXISTS. Nothing in the automatic path writes DIA-NN chromatograms to the XIC lane.
corpus_ingest's XIC block reads Spectronaut .xic.db only (--xic-dir), and the corpus drip ingests a
DIA-NN search's precursors while its `--xic` output (xic/t<N>_xic/<run>.xic.parquet, or report_xic/)
sits on disk unread. A one-off backfill on 2026-11-12 (768 searches, 0 failures) made coverage look
like 98-100%; it ended 2026-11-14 and from 2026-11-13 every new DIA-NN search went without a lane
(the health scan's "searches with XIC lane (diann)" RED). This is that backfill, made permanent.

    fill_diann_xic_lanes.py plan [--max N]        read-only: which searches, written to <D>/plan_<job>.json
    fill_diann_xic_lanes.py run <task> <ntasks> <plan.json>
                                                  one array task: diann_xic_to_lance.py --apply per search
    fill_diann_xic_lanes.py prune-drop [--apply]  drop-box chromatogram copies whose lane is written

DROP-BOX PRUNE. fran_deposit.py copies report_xic/ into the drop-box entry (up to 20 GB) so a renamed
session folder cannot strand the lane. Once the lane is written that copy has done its job, and
nothing else ever deletes drop-box entries, so it is removed here: only report_xic/ (the reports
stay; they are small and keep the search re-ingestible), only when delimp_xic_lane has a row for
the search the manifest names AND that row's dataset is on disk. Links are unlinked, never followed.

PLAN RULES (unchanged from the 2026-11-12 runner, which ingested 768 with 0 failures):
  * DIA-NN searches with no delimp_xic_lane row (the registry is the only bookkeeping: a task killed
    mid-search leaves an unregistered dataset, and diann_xic_to_lance's first write is
    mode="overwrite", so the next run simply redoes it);
  * output_dir exists and holds report.parquet and an xic/ or report_xic/ tree -- OR, when
    output_dir is gone (staff renamed or moved the session folder after ingest: Cacho PROT_0835 and
    Loots MuCer18, 2026-11-17), the drop-box entry whose fran_manifest.json names that output_dir
    and holds report.parquet + report_xic/ (fran_deposit.py copies both since 2026-11-17). The job
    records `via: dropbox` so a lane written from the copy is traceable;
  * only chromatogram files that EXIST count: a drop entry's report_xic/ may hold links into the
    moved folder, and a dead link would pass the coverage guard and fail in the writer;
  * STRICT coverage guard: .xic.parquet files / n_raw_files in 0.9..1.1, else SKIP and say why
    (a mismatch means the pairing is wrong; ingesting would attach another run's traces).
One writer per output dataset: each search belongs to exactly one task (slice task::ntasks).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

D = os.environ.get("FRAN_XIC_FILL_DIR", "/quobyte/proteomics-grp/brett/scratch/fran_health/xic_lane")
LANCE = os.environ.get("FRAN_XIC_LANCE_DIR", "/quobyte/proteomics-grp/brett/glendon/xic_lance")
WRITER = os.path.join(HERE, "diann_xic_to_lance.py")
DROP = os.environ.get("FRAN_DROP_DIR", "/quobyte/proteomics-grp/fran/incoming")


def dropbox_index(root=None):
    """output_dir -> drop-box entry, from each entry's fran_manifest.json. Empty when the drop box
    is unreadable: the fallback is a bonus, never a reason for the plan to fail."""
    idx = {}
    try:
        ents = list(os.scandir(root or DROP))
    except OSError:
        return idx
    for e in ents:
        if not e.is_dir(follow_symlinks=False):
            continue
        try:
            with open(os.path.join(e.path, "fran_manifest.json")) as fh:
                od = (json.load(fh).get("output_dir") or "").rstrip("/")
        except (OSError, ValueError, AttributeError):
            continue
        if od:
            idx[od] = e.path
    return idx


def n_xic(xd):
    """.xic.parquet files under xd that can actually be read (a dead link does not count)."""
    return sum(1 for dp, _dn, fns in os.walk(xd) for f in fns
               if f.endswith(".xic.parquet") and os.path.exists(os.path.join(dp, f)))


def plan(a):
    from coreomics_import import _conn
    conn = _conn()
    conn.set_session(readonly=True, autocommit=True)
    cur = conn.cursor()
    cur.execute("""SELECT s.id, s.search_name, s.output_dir, s.n_raw_files, s.ingested_at
                     FROM delimp_searches s
                    WHERE s.search_engine = 'diann'
                      AND NOT EXISTS (SELECT 1 FROM delimp_xic_lane l WHERE l.search_id = s.id)
                    ORDER BY s.ingested_at""")
    rows = cur.fetchall()
    conn.close()
    jobs, skipped, why = [], [], {}
    drop = dropbox_index()

    def skip(reason, sid, name, detail=""):
        why[reason] = why.get(reason, 0) + 1
        skipped.append(dict(sid=str(sid), name=name, reason=reason, detail=detail))

    for sid, name, od, nraw, ing in rows:
        sid = str(sid)
        src, via = od, "output_dir"
        if not od or not os.path.isdir(od):
            ent = drop.get((od or "").rstrip("/"))
            if not ent:
                skip("no_output_dir", sid, name, od or "")
                continue
            src, via = ent, "dropbox"
        rep = os.path.join(src, "report.parquet")
        xd = next((x for x in (os.path.join(src, "xic"), os.path.join(src, "report_xic")) if os.path.isdir(x)), None)
        if not os.path.isfile(rep) or not xd:
            skip("no_report_or_xic", sid, name, src)
            continue
        nx = n_xic(xd)
        cov = nx / nraw if nraw else 0.0
        if not 0.9 <= cov <= 1.1:
            skip("coverage_mismatch", sid, name, f"{nx} xic / {nraw} runs")
            continue
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", name or "search")[:70]
        jobs.append(dict(sid=sid, name=name, dir=src, via=via, report=rep, xic=xd, n_xic=nx,
                         ingested_at=ing.isoformat() if ing else None,
                         out=f"{LANCE}/{safe}__{sid[:8]}.diann.xic.lance"))
        if a.max and len(jobs) >= a.max:
            break
    os.makedirs(D, exist_ok=True)
    out = os.path.join(D, f"plan_{os.environ.get('SLURM_JOB_ID', 'local')}.json")
    summary = dict(at=time.strftime("%Y-%m-%dT%H:%M:%S"), unlaned=len(rows), planned=len(jobs),
                   skipped=why, out=out)
    json.dump(dict(summary=summary, jobs=jobs, skipped=skipped), open(out, "w"), indent=1)
    print(json.dumps(summary))
    return out, len(jobs)


def _tree_bytes(p):
    if os.path.islink(p):
        return 0
    return sum(os.lstat(os.path.join(dp, f)).st_size for dp, _dn, fns in os.walk(p) for f in fns)


def prune_drop(a, laned=None, root=None):
    """Remove report_xic/ from drop-box entries whose search already has its XIC lane on disk."""
    import shutil
    if laned is None:
        from coreomics_import import _conn
        conn = _conn()
        conn.set_session(readonly=True, autocommit=True)
        cur = conn.cursor()
        cur.execute("""SELECT s.output_dir, l.lance_path FROM delimp_xic_lane l
                         JOIN delimp_searches s ON s.id = l.search_id
                        WHERE s.search_engine = 'diann' AND s.output_dir IS NOT NULL""")
        laned = {od.rstrip("/"): lp for od, lp in cur.fetchall()}
        conn.close()
    n = freed = 0
    for od, ent in sorted(dropbox_index(root).items()):
        lp = laned.get(od)
        x = os.path.join(ent, "report_xic")
        if not lp or not os.path.isdir(lp) or not os.path.lexists(x):
            continue
        b = _tree_bytes(x)
        print(f"{'PRUNE' if a.apply else 'would prune'} {x} ({b / 1e9:.2f} GB; lane {lp})", flush=True)
        if a.apply:
            if os.path.islink(x):
                os.unlink(x)
            else:
                shutil.rmtree(x)
        n += 1
        freed += b
    print(f"PRUNED {n} {freed}" if a.apply else f"dry run: {n} entr(ies), {freed / 1e9:.2f} GB")
    return n, freed


def run(a):
    data = json.load(open(a.plan))
    jobs = data["jobs"][a.task::a.ntasks]
    jid = os.environ.get("SLURM_ARRAY_JOB_ID", os.environ.get("SLURM_JOB_ID", "local"))
    res = os.path.join(D, f"result_{jid}_{a.task}.tsv")       # one writer per file
    ok = fail = 0
    with open(res, "w") as out:
        out.write("search_id\tsearch_name\trc\tseconds\ttail\n")
        for j in jobs:
            t0 = time.time()
            cmd = [sys.executable, "-u", WRITER, "--dir", j["dir"], "--xic-dir", j["xic"],
                   "--report", j["report"], "--out", j["out"], "--search-id", j["sid"],
                   "--search-name", j["name"], "--apply"]
            r = subprocess.run(cmd, capture_output=True, text=True)
            txt = r.stdout + r.stderr
            # The writer refuses when the XIC dir holds a run the report doesn't (a run with no IDs,
            # e.g. a failed injection). If EVERY report run has its XIC file, restricting to the
            # report's runs is exact, not a guess: retry with --runs. Any other mismatch stays a failure.
            if (r.returncode != 0 and "XIC files with no report run" in txt
                    and "report/--runs runs with no XIC file (0)" in txt):
                import pyarrow.parquet as pq
                runs = sorted(set(pq.read_table(j["report"], columns=["Run"]).column("Run").to_pylist()))
                r = subprocess.run(cmd + ["--runs", ",".join(runs)], capture_output=True, text=True)
                txt = "[retried with the report's %d runs] " % len(runs) + r.stdout + r.stderr
            tail = " / ".join(txt.strip().splitlines()[-3:])[:600]
            out.write(f"{j['sid']}\t{j['name']}\t{r.returncode}\t{time.time()-t0:.0f}\t{tail}\n")
            out.flush()
            print(f"--- {j['name']} ({j['n_xic']} xic{', via drop box' if j.get('via') == 'dropbox' else ''}) rc={r.returncode} {time.time()-t0:.0f}s\n    {tail}", flush=True)
            ok += r.returncode == 0
            fail += r.returncode != 0
    print(f"task {a.task}/{a.ntasks}: ok={ok} fail={fail} -> {res}")
    return 0 if fail == 0 else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("plan")
    p.add_argument("--max", type=int, default=0, help="plan at most N searches (0 = all)")
    r = sp.add_parser("run")
    r.add_argument("task", type=int)
    r.add_argument("ntasks", type=int)
    r.add_argument("plan")
    pr = sp.add_parser("prune-drop")
    pr.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if a.cmd == "prune-drop":
        prune_drop(a)
        return 0
    if a.cmd == "plan":
        out, n = plan(a)
        print(f"PLAN {out} {n}")
        return 0
    return run(a)


if __name__ == "__main__":
    sys.exit(main())
