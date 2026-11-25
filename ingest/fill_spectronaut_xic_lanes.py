"""fill_spectronaut_xic_lanes.py — write the chromatogram (XIC) lane for Spectronaut searches whose
XIC databases have reached Flinders.

WHY. Measured 2026-11-25: 2 of 2,222 Spectronaut searches had an XIC lane. A Spectronaut XIC lane is
built from the report plus the "All XIC" SQLite files (one <run>.xic.db per raw file). Those files
live on the Windows computers, and nothing brought them to Hive. sne_one.py now copies them to

    FRAN_reports/<sne name>/<export folder>/xic_dbs/   +   COMPLETE.txt (written last)

next to the archived report, and this job turns each complete set into a lane.

WHAT IT DOES, per Spectronaut search with no row in delimp_xic_lane:
  write   an archived report has xic_dbs/COMPLETE.txt beside it -> xic_lance.process_one, in a child
          process (one at a time: the writer holds the whole table in memory), into
          <xic_lance>/.incoming.<job>/, then RENAMED to <name>__<sid8>.xic.lance and registered.
  adopt   the dataset is already at its final path but not in the registry (the job was preempted
          between the rename and the registration): it is re-read and registered, never rewritten.
  free    after a lane is registered and re-read, the *.xic.db copies beside the archived report are
          DELETED (FRAN_SN_XIC_KEEP_DBS=1 keeps them). They are copies: at ~1.6 GB per raw file a
          full set for the corpus is larger than the free space on the share. COMPLETE.txt stays
          and LANE_BUILT.txt is added, so the Windows side does not export them again.
  (none)  no complete XIC set on Flinders: counted, nothing attempted. Most searches: the XICs were
          never exported from Spectronaut.

SAFETY. xic_lance.process_one writes with mode="overwrite" to <search_name>.xic.lance, so two
searches with one name would replace each other. This job never lets it write at a final path: the
dataset is built in this job's own incoming folder and renamed to a path that carries the search id,
and an existing path is never replaced. Dry run unless --apply. A search that fails 3 times is parked.

  python fill_spectronaut_xic_lanes.py                 # plan only
  python fill_spectronaut_xic_lanes.py --apply --max 10
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fill_spectrum_lanes as F  # noqa: E402 -- report lookup, Db wrapper, child runner: one definition

XIC_LANCE = os.environ.get("FRAN_XIC_LANCE_DIR", "/quobyte/proteomics-grp/brett/glendon/xic_lance")
D = os.environ.get("FRAN_SN_XIC_FILL_DIR", "/quobyte/proteomics-grp/brett/scratch/fran_health/sn_xic_lane")
XIC_SUBDIR, XIC_DONE = "xic_dbs", "COMPLETE.txt"       # the names sne_one.py writes
MAX_ATTEMPTS = 3
REPORTS_ROOT = os.environ.get("FRAN_REPORTS_ROOT", "/nfs/lssc0/flinders/proteomics/Data/FRAN_reports")
KEEP_DBS = os.environ.get("FRAN_SN_XIC_KEEP_DBS", "0") == "1"


def free_xic_dbs(xic_dir, lance_path, root=None):
    """Delete the *.xic.db copies in ONE archived xic_dbs folder once its lane exists. Only a folder
    named xic_dbs, under the report archive, holding COMPLETE.txt; only *.xic.db / *.sqlite files.
    Returns (files, GB) removed."""
    root = os.path.realpath(root or REPORTS_ROOT)
    d = os.path.realpath(xic_dir)
    if (os.path.basename(d) != XIC_SUBDIR or not d.startswith(root + os.sep)
            or not os.path.isfile(os.path.join(d, XIC_DONE))):
        return 0, 0.0
    with open(os.path.join(d, "LANE_BUILT.txt.part"), "w") as fh:
        fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\n{lance_path}\n")
    os.replace(os.path.join(d, "LANE_BUILT.txt.part"), os.path.join(d, "LANE_BUILT.txt"))
    n = b = 0
    for fn in os.listdir(d):
        f = os.path.join(d, fn)
        if fn.lower().endswith((".xic.db", ".sqlite")) and os.path.isfile(f) and not os.path.islink(f):
            b += os.path.getsize(f); os.remove(f); n += 1
    return n, b / 1e9


def describe(lance_path, sid):
    """Re-read a dataset at its final path: what register() needs, and proof it opens."""
    import lance
    import pyarrow.compute as pc
    import xic_lance as xln
    ds = lance.dataset(lance_path)
    t = ds.to_table(columns=["search_id", "n_traces", "run"])
    sids = set(t.column("search_id").unique().to_pylist())
    if not t.num_rows or sids != {str(sid)}:
        raise RuntimeError(f"{lance_path}: {t.num_rows} rows, search ids {sorted(map(str, sids))[:3]}, expected {sid}")
    return dict(ok=True, lance_path=lance_path, n_prec=t.num_rows, n_traces=int(pc.sum(t.column("n_traces")).as_py()),
                md5=xln.dataset_md5(ds), version=int(ds.version),
                runs=sorted(F.run_set(t.column("run").unique().to_pylist())))


def xic_set_for(report, isfile=os.path.isfile):
    """The complete XIC-db folder beside an archived report, or None."""
    d = os.path.join(os.path.dirname(report), XIC_SUBDIR)
    return d if isfile(os.path.join(d, XIC_DONE)) else None


def final_path(search_name, sid, root=None):
    return os.path.join(root or XIC_LANCE, f"{F._safe(F._strip_ts(search_name or 'search'))}__{str(sid)[:8]}.xic.lance")


def plan(searches, state, find_reports=F.find_reports, has_set=xic_set_for, exists=os.path.lexists):
    """searches: dicts sid, name, output_dir, n. Returns (jobs, skipped counts)."""
    jobs, skipped = [], {}

    def skip(why):
        skipped[why] = skipped.get(why, 0) + 1

    for s in searches:
        st = state.get(s["sid"], {})
        if st.get("attempts", 0) >= MAX_ATTEMPTS:
            skip("parked"); continue
        names = F.candidate_names(s["name"], None, s["output_dir"], None)
        pair = next(((r, has_set(r)) for r in find_reports(names, s["output_dir"]) if has_set(r)), None)
        if not pair:
            skip("no_xic_dbs_on_flinders"); continue
        out = final_path(s["name"], s["sid"])
        # A dataset only reaches its final path by rename AFTER a complete write, so one found there
        # without a registry row is whole: adopt it (re-read, register), never rewrite it.
        jobs.append(dict(sid=s["sid"], name=s["name"], n=s["n"], report=pair[0], xic_dir=pair[1], out=out,
                         adopt=bool(exists(out))))
    return jobs, skipped


def child(spec, jid):
    """Build one lane in this job's incoming folder and rename it into place (or adopt one already
    there), then re-read it. spec: JSON of a plan() job plus corpus_runs. Prints CHILD_RESULT."""
    import xic_lance as xln
    j = json.loads(spec)
    report, xic_dir, out, sid, name = j["report"], j["xic_dir"], j["out"], j["sid"], j["name"]
    inc = os.path.join(os.path.dirname(out), f".incoming.{jid}")
    os.makedirs(inc, exist_ok=True)
    res = dict(ok=False)
    try:
        if not os.path.lexists(out):
            lpath, n_prec, n_tr, md5, ver = xln.process_one(report, xic_dir, inc, search_id=sid, search_name=name)
            if not lpath or not n_prec:
                raise RuntimeError("no traces matched the report's precursors")
            # The archived report is found by NAME. A lane whose runs the search never had was built
            # from another analysis of the same name: do not keep it.
            import lance
            runs = F.run_set(lance.dataset(lpath).to_table(columns=["run"]).column("run").unique().to_pylist())
            extra = sorted(runs - set(j.get("corpus_runs") or runs))
            if extra:
                raise RuntimeError(f"report/XIC runs the search does not have ({len(extra)}, e.g. {extra[:2]}): "
                                   f"a different analysis of the same name?")
            if os.path.lexists(out):
                raise RuntimeError(f"{out} appeared during the write; never replaced")
            os.rename(lpath, out)
        res = describe(out, sid)
    except Exception as e:  # noqa: BLE001
        res = dict(ok=False, error=repr(e)[:300])
    finally:
        shutil.rmtree(inc, ignore_errors=True)          # only ever this job's own incoming folder
    print("CHILD_RESULT " + json.dumps(res), flush=True)
    return 0 if res["ok"] else 1


def run_child(args):
    """Run THIS script as a child (F.run_child would start fill_spectrum_lanes.py) and read its result."""
    r = subprocess.run([sys.executable, "-u", os.path.abspath(__file__)] + args, capture_output=True, text=True)
    for ln in reversed((r.stdout or "").splitlines()):
        if ln.startswith("CHILD_RESULT "):
            try:
                return json.loads(ln[len("CHILD_RESULT "):])
            except ValueError:
                break
    tail = " / ".join(((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-3:])[:400]
    return dict(ok=False, error=f"child rc={r.returncode} (no result; killed?) {tail}")


def gather(conn):
    cur = conn.cursor()
    cur.execute("""SELECT s.id::text, s.search_name, s.output_dir, s.n_precursors_total
                     FROM delimp_searches s
                    WHERE s.search_engine = 'spectronaut' AND s.n_precursors_total > 0
                      AND NOT EXISTS (SELECT 1 FROM delimp_xic_lane x WHERE x.search_id::text = s.id::text)
                    ORDER BY s.ingested_at""")
    return [dict(sid=r[0], name=r[1], output_dir=r[2], n=r[3]) for r in cur.fetchall()]


def load_state():
    f = os.path.join(D, "state.json")
    if not os.path.exists(f):
        return {}
    with open(f) as fh:                # an unreadable state file stops the run: never start from empty
        st = json.load(fh)
    if not isinstance(st, dict):
        raise ValueError(f"{f} is not a JSON object")
    return st


def save_state(state):
    os.makedirs(D, exist_ok=True)
    tmp = os.path.join(D, f"state.json.tmp.{os.getpid()}")
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1)
    os.replace(tmp, os.path.join(D, "state.json"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--max", type=int, default=10, help="lanes written per run (0 = no cap)")
    ap.add_argument("--child", metavar="JSON", help=argparse.SUPPRESS)
    a = ap.parse_args()
    jid = os.environ.get("SLURM_JOB_ID", f"local{os.getpid()}")
    if a.child:
        sys.exit(child(a.child, jid))

    db = F.Db(F._connect)
    searches = db.run(gather)
    state = load_state()
    jobs, skipped = plan(searches, state)
    todo = jobs[:a.max] if a.max else jobs
    print(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "job": jid, "apply": a.apply,
                      "spectronaut_without_xic_lane": len(searches), "writable": len(jobs),
                      "selected": len(todo), "skipped": skipped}), flush=True)
    if not a.apply:
        for j in todo:
            print(f"  would {'adopt' if j['adopt'] else 'write'} {j['out']}  <- {j['xic_dir']}")
        return 0
    os.makedirs(D, exist_ok=True)
    import xic_lance as xln
    db.run(lambda c: xln.ensure_registry(c))
    n_ok = n_fail = 0
    for j in todo:
        t0 = time.time()
        runs = db.run(lambda c: F.corpus_runs(c, j["sid"]))
        r = run_child(["--child=" + json.dumps(dict(j, corpus_runs=runs))])
        if r.get("ok"):
            try:
                db.run(lambda c: xln.register(c, j["sid"], j["name"], r["lance_path"], r["n_prec"],
                                              r["n_traces"], r["md5"], r["version"]))
            except Exception as e:  # noqa: BLE001
                r = dict(ok=False, error=f"written but NOT registered ({e!r}); left at {j['out']}")
        if r.get("ok"):
            n_ok += 1
            state.pop(j["sid"], None)
            freed = "XIC dbs kept"
            if not KEEP_DBS:
                try:
                    nf, gb = free_xic_dbs(j["xic_dir"], r["lance_path"])
                    freed = f"{nf} XIC dbs removed from the share ({gb:.1f} GB)"
                except OSError as e:
                    freed = f"XIC dbs NOT removed ({e!r})"
            print(f"--- {j['name']} registered: {r['n_prec']:,} of {j['n']:,} precursors, "
                  f"{r['n_traces']:,} traces, {int(time.time()-t0)}s; {freed}", flush=True)
        else:
            n_fail += 1
            st = state.setdefault(j["sid"], {})
            st["attempts"] = st.get("attempts", 0) + 1
            st["last_error"] = r.get("error")
            print(f"--- {j['name']} FAILED (attempt {st['attempts']}): {r.get('error')}", flush=True)
        save_state(state)
        F.append_jsonl(os.path.join(D, f"outcomes_{jid}.jsonl"),
                       dict(sid=j["sid"], name=j["name"], out=j["out"], **r))
    print(json.dumps({"job": jid, "registered": n_ok, "failed": n_fail}), flush=True)
    return 0 if not n_fail else 1


if __name__ == "__main__":
    sys.exit(main())
