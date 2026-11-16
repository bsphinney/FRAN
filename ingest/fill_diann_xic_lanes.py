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

PLAN RULES (unchanged from the 2026-11-12 runner, which ingested 768 with 0 failures):
  * DIA-NN searches with no delimp_xic_lane row (the registry is the only bookkeeping: a task killed
    mid-search leaves an unregistered dataset, and diann_xic_to_lance's first write is
    mode="overwrite", so the next run simply redoes it);
  * output_dir exists and holds report.parquet and an xic/ or report_xic/ tree;
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

    def skip(reason, sid, name, detail=""):
        why[reason] = why.get(reason, 0) + 1
        skipped.append(dict(sid=str(sid), name=name, reason=reason, detail=detail))

    for sid, name, od, nraw, ing in rows:
        sid = str(sid)
        if not od or not os.path.isdir(od):
            skip("no_output_dir", sid, name, od or "")
            continue
        rep = os.path.join(od, "report.parquet")
        xd = next((x for x in (os.path.join(od, "xic"), os.path.join(od, "report_xic")) if os.path.isdir(x)), None)
        if not os.path.isfile(rep) or not xd:
            skip("no_report_or_xic", sid, name, od)
            continue
        nx = sum(1 for _dp, _dn, fns in os.walk(xd) for f in fns if f.endswith(".xic.parquet"))
        cov = nx / nraw if nraw else 0.0
        if not 0.9 <= cov <= 1.1:
            skip("coverage_mismatch", sid, name, f"{nx} xic / {nraw} runs")
            continue
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", name or "search")[:70]
        jobs.append(dict(sid=sid, name=name, dir=od, report=rep, xic=xd, n_xic=nx,
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
            r = subprocess.run([sys.executable, "-u", WRITER, "--dir", j["dir"], "--xic-dir", j["xic"],
                                "--report", j["report"], "--out", j["out"], "--search-id", j["sid"],
                                "--search-name", j["name"], "--apply"], capture_output=True, text=True)
            tail = " / ".join((r.stdout + r.stderr).strip().splitlines()[-3:])[:600]
            out.write(f"{j['sid']}\t{j['name']}\t{r.returncode}\t{time.time()-t0:.0f}\t{tail}\n")
            out.flush()
            print(f"--- {j['name']} ({j['n_xic']} xic) rc={r.returncode} {time.time()-t0:.0f}s\n    {tail}", flush=True)
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
    a = ap.parse_args()
    if a.cmd == "plan":
        out, n = plan(a)
        print(f"PLAN {out} {n}")
        return 0
    return run(a)


if __name__ == "__main__":
    sys.exit(main())
