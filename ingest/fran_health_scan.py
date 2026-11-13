"""fran_health_scan.py - weekly check that FRAN is still filling everything it used to fill.

WHY. On 2026-11-13 a survey found that since ~September almost none of FRAN's metadata had been
filled for new data -- raw-file locations, instrument facts, PTM rollups, provenance -- because the
scripts that fill them were never scheduled. Every individual job reported success. Nothing
compared "how complete is NEW data" with "how complete was OLD data", and that comparison is the
one that would have caught it in the first week. This script makes it, every week.

WHAT IT CHECKS (each finding is RED / AMBER / ok):
  fill      per-column fill rate of rows from the last 30 days vs the 90 days before that
  coverage  per-search side tables (PTM rollup, provenance, FASTAs, XIC / spectrum lane) the same way
  backlog   absolute counts still missing (is the filler catching up?)
  filler    did fran_fill.py run in the last 2 days, and did every step succeed?
  drip      parked / failed search sets, chains stranded on DependencyNeverSatisfied
  slurm     jobs held by a launch failure, and which nodes caused them this week
  cron      the expected crontab lines are present on login2 (from a snapshot the cron wrapper writes)
  ingest    when the last search was ingested

--heal does the safe, mechanical fixes: release held jobs off their bad node (slurm_heal.py) and
resubmit the filler if it has not run. Everything else is reported, because it needs a decision.

    python fran_health_scan.py [--heal] [--post] [--out DIR]

Read-only on the database. Run as a SLURM job (fran_health.sbatch), not on the login node.
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

OUT_DIR = "/quobyte/proteomics-grp/brett/scratch/fran_health"
FILL_DIR = os.path.join(OUT_DIR, "fill")
CRON_SNAPSHOT = os.path.join(OUT_DIR, "crontab_login2.txt")
DRIP_STATE = "/nfs/lssc0/flinders/proteomics/Data/FRAN_diann/corpus_2026/state.json"
HEAL_LOG = "/quobyte/proteomics-grp/brett/scratch/corpus_drip_logs/slurm_heal_nodes.tsv"
WEBHOOK_FILE = "/quobyte/proteomics-grp/.config/skill_slack_webhook"
FILL_SBATCH = os.path.join(HERE, "fran_fill.sbatch")

RECENT, BASE_FROM, BASE_TO = 30, 30, 120     # days: recent window, baseline window
MIN_ROWS = 20                                 # don't judge a window with fewer rows than this
# FIXED floors, not only a rolling baseline. The first test run proved why: the 90-day baseline was
# itself half-decayed (hive_path 49%, PTM rollup 21%), so a slow decay only ever looked like a
# modest drop against its own recent past. Below its floor a column is RED whatever came before.
# Floors are what the corpus reached when the fillers were run (June-August 2026).
FLOORS = {"raw_files.hive_path": 0.90, "raw_files.instrument_model": 0.90,
          "raw_files.acquisition_date": 0.90, "raw_files.n_ms2_frames": 0.85,
          "raw_files.cycle_time_sec": 0.85, "sample_metadata.organism_taxon_id": 0.90,
          "searches with PTM rollup": 0.90, "searches with provenance row": 0.90,
          "searches with FASTA record": 0.90, "searches with raw-file links": 0.95}

# (label, FROM clause, date expression, [(column label, filled-expression)])
FILL_CHECKS = [
    ("raw_files", "raw_files r", "r.first_seen_at", [
        ("hive_path", "nullif(btrim(r.hive_path),'') IS NOT NULL"),
        ("instrument_model", "nullif(btrim(r.instrument_model),'') IS NOT NULL"),
        ("acquisition_date", "r.acquisition_date IS NOT NULL"),
        ("n_ms2_frames", "r.n_ms2_frames IS NOT NULL"),
        ("cycle_time_sec", "r.cycle_time_sec IS NOT NULL"),
        ("file_size_bytes", "r.file_size_bytes IS NOT NULL"),
        ("md5", "nullif(btrim(r.md5),'') IS NOT NULL"),
    ]),
    ("sample_metadata", "delimp_sample_metadata t JOIN raw_files r ON r.raw_path = t.raw_path",
     "r.first_seen_at", [
        ("organism_taxon_id", "t.organism_taxon_id IS NOT NULL"),
    ]),
    ("search_provenance", "delimp_search_provenance t", "t.recorded_at", [
        ("pi", "nullif(btrim(t.pi),'') IS NOT NULL"),
        ("client", "nullif(btrim(t.client),'') IS NOT NULL"),
        ("project", "nullif(btrim(t.project),'') IS NOT NULL"),
    ]),
]

# Per-search side tables: (label, engine filter or None, EXISTS subquery on s.id)
COVERAGE_CHECKS = [
    ("PTM rollup", None, "SELECT 1 FROM delimp_search_protein_ptm x WHERE x.search_id = s.id"),
    ("provenance row", None, "SELECT 1 FROM delimp_search_provenance x WHERE x.search_id = s.id"),
    ("FASTA record", None, "SELECT 1 FROM delimp_search_fastas x WHERE x.search_id = s.id"),
    ("raw-file links", None, "SELECT 1 FROM search_raw_files x WHERE x.search_id = s.id"),
    ("XIC lane", "diann", "SELECT 1 FROM delimp_xic_lane x WHERE x.search_id = s.id"),
    ("spectrum lane", "spectronaut", "SELECT 1 FROM delimp_spectrum_lane x WHERE x.search_id = s.id"),
]

EXPECTED_CRON = ["corpus_drip.sh", "corpus_ingest_drip.sh", "cron_auto_ingest.sh",
                 "fran_fill_cron.sh", "fran_health_cron.sh"]

findings = []   # (severity, area, message)
metrics = {"fill": {}, "coverage": {}}   # structured numbers for delimp_qc_scan / the FRAN-QC page
HEAL_EVENTS = "/quobyte/proteomics-grp/brett/scratch/corpus_drip_logs/slurm_heal_events.jsonl"
PG_DISK_GB = float(os.environ.get("FRAN_PG_DISK_GB", "1600"))   # PG Farm volume since 2026-11-06


def add(sev, area, msg):
    findings.append((sev, area, msg))
    print(f"[{sev}] {area}: {msg}", flush=True)


def sh(cmd):
    return subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True)


def judge(area, what, recent, base, n_recent, n_base):
    if n_recent < MIN_ROWS:
        return
    floor = FLOORS.get(what.split(" (")[0])
    if area in metrics:
        metrics[area][what] = {"recent": round(recent, 4), "base": round(base, 4), "floor": floor,
                               "n_recent": n_recent, "n_base": n_base}
    txt = (f"{what}: {recent:.0%} of the last {RECENT} d vs {base:.0%} before "
           f"(n={n_recent:,}/{n_base:,})" + (f"; floor {floor:.0%}" if floor else ""))
    if floor and recent < floor:
        add("RED", area, txt)
        return
    if n_base < MIN_ROWS:
        return
    drop = base - recent
    if base >= 0.5 and drop >= 0.30:
        add("RED", area, txt)
    elif drop >= 0.15:
        add("AMBER", area, txt)


def columns_of(c, table):
    c.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,))
    return {r[0] for r in c.fetchall()}


def check_fill(c):
    for label, frm, dexpr, cols in FILL_CHECKS:
        have = columns_of(c, frm.split()[0])
        cols = [(n, e) for n, e in cols if n in have]
        if not cols:
            add("AMBER", "fill", f"{label}: none of the checked columns exist any more")
            continue
        sel = ", ".join(
            f"avg(({e})::int) FILTER (WHERE {dexpr} > now() - interval '{RECENT} days'), "
            f"avg(({e})::int) FILTER (WHERE {dexpr} <= now() - interval '{BASE_FROM} days' "
            f"AND {dexpr} > now() - interval '{BASE_TO} days')" for _, e in cols)
        c.execute(f"""SELECT count(*) FILTER (WHERE {dexpr} > now() - interval '{RECENT} days'),
                             count(*) FILTER (WHERE {dexpr} <= now() - interval '{BASE_FROM} days'
                                              AND {dexpr} > now() - interval '{BASE_TO} days'),
                             {sel} FROM {frm} WHERE {dexpr} > now() - interval '{BASE_TO} days'""")
        row = c.fetchone()
        n_r, n_b = row[0], row[1]
        for i, (name, _) in enumerate(cols):
            r, b = row[2 + 2 * i], row[3 + 2 * i]
            if r is None:
                continue
            b = r if b is None else b
            judge("fill", f"{label}.{name}", float(r), float(b), n_r, n_b)


def check_coverage(c):
    for label, engine, sub in COVERAGE_CHECKS:
        eng = f"AND s.search_engine = '{engine}'" if engine else ""
        c.execute(f"""SELECT count(*) FILTER (WHERE s.ingested_at > now() - interval '{RECENT} days'),
                             avg((EXISTS ({sub}))::int) FILTER (WHERE s.ingested_at > now() - interval '{RECENT} days'),
                             count(*) FILTER (WHERE s.ingested_at <= now() - interval '{BASE_FROM} days'),
                             avg((EXISTS ({sub}))::int) FILTER (WHERE s.ingested_at <= now() - interval '{BASE_FROM} days')
                        FROM delimp_searches s
                       WHERE s.ingested_at > now() - interval '{BASE_TO} days' {eng}""")
        n_r, r, n_b, b = c.fetchone()
        if r is not None:
            b = r if b is None else b
            judge("coverage", f"searches with {label}" + (f" ({engine})" if engine else ""),
                  float(r), float(b), n_r, n_b)


def check_backlog(c):
    c.execute("""SELECT count(*) FILTER (WHERE nullif(btrim(hive_path),'') IS NULL),
                        count(*) FILTER (WHERE instrument_model IS NULL),
                        count(DISTINCT raw_basename), count(*) FROM raw_files""")
    no_path, no_inst, n_base, n = c.fetchone()
    c.execute("""SELECT count(*) FROM delimp_searches s
                  WHERE s.ingested_at < now() - interval '2 days'
                    AND NOT EXISTS (SELECT 1 FROM delimp_search_protein_ptm x WHERE x.search_id = s.id)
                    AND EXISTS (SELECT 1 FROM delimp_precursors p WHERE p.search_id = s.id
                                AND p.protein_group IS NOT NULL LIMIT 1)""")
    no_ptm = c.fetchone()[0]
    metrics["backlog"] = {"raw_rows": n, "distinct_runs": n_base, "no_hive_path": no_path,
                          "no_instrument": no_inst, "no_ptm": no_ptm}
    msg = (f"raw_files {n:,} rows / {n_base:,} distinct runs: no hive_path {no_path:,}, "
           f"no instrument {no_inst:,}; searches >2 d old with no PTM rollup {no_ptm:,}")
    add("AMBER" if (no_ptm > 50 or no_path > 0.1 * n) else "ok", "backlog", msg)


def check_ingest(c):
    c.execute("SELECT max(ingested_at), count(*) FILTER (WHERE ingested_at > now() - interval '7 days') "
              "FROM delimp_searches")
    last, n7 = c.fetchone()
    if last is None:
        add("RED", "ingest", "no ingested searches at all?")
        return
    age_h = (time.time() - last.timestamp()) / 3600
    metrics["ingest"] = {"last_age_h": round(age_h, 1), "n_7d": n7}
    add("AMBER" if age_h > 48 else "ok", "ingest",
        f"last search ingested {age_h:.0f} h ago; {n7:,} in the last 7 days")


def check_db(c):
    """Database size against the PG Farm volume. The service account cannot see the disk itself,
    so free space is estimated the way the PG Farm admin does (relation sizes + ~5 GB for the OS).
    The disk filling on 2026-10-05 took FRAN down for a month; this is the early warning."""
    c.execute("SELECT pg_database_size(current_database())")
    size = c.fetchone()[0]
    free = PG_DISK_GB * 1024 ** 3 - size - 5 * 1024 ** 3
    pct_free = free / (PG_DISK_GB * 1024 ** 3)
    metrics["db"] = {"size_bytes": size, "disk_gb": PG_DISK_GB, "est_free_bytes": int(free)}
    msg = (f"database {size / 1024 ** 3:,.0f} GiB of a {PG_DISK_GB:,.0f} GB volume; "
           f"~{free / 1024 ** 3:,.0f} GiB free ({pct_free:.0%})")
    add("RED" if pct_free < 0.05 else "AMBER" if pct_free < 0.15 else "ok", "database", msg)


def check_filler():
    runs = sorted(glob.glob(os.path.join(FILL_DIR, "fill_*.json")), key=os.path.getmtime)
    if not runs:
        add("RED", "filler", f"fran_fill.py has never written a status file in {FILL_DIR}")
        return "stale"
    newest = runs[-1]
    age_h = (time.time() - os.path.getmtime(newest)) / 3600
    try:
        st = json.load(open(newest))
    except (OSError, ValueError) as e:
        add("AMBER", "filler", f"unreadable status {newest}: {e}")
        return "stale" if age_h > 48 else "ok"
    bad = [k for k, v in st.get("steps", {}).items() if v.get("rc") not in (0, None)]
    if age_h > 48:
        add("RED", "filler", f"last fill run {age_h:.0f} h ago ({os.path.basename(newest)})")
        return "stale"
    if bad:
        add("AMBER", "filler", f"last run ({os.path.basename(newest)}) failed step(s): {', '.join(bad)}")
    else:
        add("ok", "filler", f"last run {age_h:.0f} h ago, all {len(st.get('steps', {}))} steps rc=0")
    return "ok"


def check_drip():
    try:
        st = json.load(open(DRIP_STATE))
    except (OSError, ValueError) as e:
        add("AMBER", "drip", f"cannot read {DRIP_STATE}: {e}")
        return
    counts = {}
    for v in st.values():
        counts[v.get("status", "?")] = counts.get(v.get("status", "?"), 0) + 1
    parked = [(v.get("name") or k)[:60] + " -- " + (v.get("reason") or "")[:120]
              for k, v in st.items() if v.get("status") == "parked"]
    metrics["drip"] = {"status_counts": counts,
                       "parked": [{"name": (v.get("name") or k)[:80], "reason": (v.get("reason") or "")[:240]}
                                  for k, v in st.items() if v.get("status") == "parked"][:100]}
    add("AMBER" if parked else "ok", "drip", f"state {counts}" +
        (f"; parked sets need a human:\n    " + "\n    ".join(parked[:15]) if parked else ""))
    r = sh("squeue -u $(id -un) -h -t PD -o '%r'")
    reasons = r.stdout.splitlines()
    dns = sum(1 for x in reasons if x.strip() == "DependencyNeverSatisfied")
    held = sum(1 for x in reasons if "launch failed" in x)
    metrics.setdefault("slurm", {}).update({"held": held, "stranded": dns})
    if dns:
        add("AMBER", "slurm", f"{dns} job(s) stranded on DependencyNeverSatisfied (the drip cancels "
                              f"its own each firing; any left belong to something else)")
    if held:
        add("AMBER", "slurm", f"{held} job(s) held by a launch failure (slurm_heal.py releases them)")


def check_bad_nodes():
    if not os.path.exists(HEAL_LOG):
        return
    cutoff = time.strftime("%FT%T", time.localtime(time.time() - 7 * 86400))
    per = {}
    for line in open(HEAL_LOG):
        p = line.rstrip("\n").split("\t")
        if len(p) == 3 and p[0] >= cutoff and p[1] != "node":
            per[p[1]] = per.get(p[1], 0) + int(p[2] or 0)
    metrics.setdefault("slurm", {})["bad_nodes"] = per
    if per:
        add("AMBER", "slurm", "nodes that failed job launches this week (report to "
                              f"hpc-help@ucdavis.edu if they persist): {per}")


def check_cron():
    if not os.path.exists(CRON_SNAPSHOT):
        add("AMBER", "cron", f"no crontab snapshot at {CRON_SNAPSHOT} (fran_health_cron.sh writes it)")
        return
    age_h = (time.time() - os.path.getmtime(CRON_SNAPSHOT)) / 3600
    txt = open(CRON_SNAPSHOT).read()
    live = [ln for ln in txt.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    missing = [x for x in EXPECTED_CRON if not any(x in ln for ln in live)]
    if missing:
        add("RED", "cron", f"not scheduled (or commented out) on login2: {', '.join(missing)}")
    else:
        add("ok", "cron", f"all {len(EXPECTED_CRON)} FRAN cron lines live (snapshot {age_h:.0f} h old)")


def heal(filler_state):
    r = subprocess.run([sys.executable, os.path.join(HERE, "slurm_heal.py")], capture_output=True, text=True)
    out = (r.stdout or "").strip().splitlines()
    add("ok", "heal", out[-1] if out else "no held jobs")
    if filler_state == "stale":
        r = sh(f"sbatch --parsable {FILL_SBATCH}")
        if r.returncode == 0:
            add("ok", "heal", f"resubmitted the filler: job {r.stdout.strip()}")
        else:
            add("RED", "heal", f"could not resubmit the filler: {r.stderr.strip()[:200]}")


def import_events(conn):
    """Copy what the self-healing layer did into delimp_qc_event (dedupe_key makes it idempotent)."""
    ev = []
    try:
        for i, line in enumerate(open(HEAL_EVENTS)):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            ev.append((d["at"], "heal", d.get("kind", "?"), d.get("summary", ""),
                       json.dumps(d.get("detail", {})), f"heal:{d['at']}:{d.get('kind')}:{i}"))
    except OSError:
        pass
    for path in glob.glob(os.path.join(FILL_DIR, "fill_*.json")):
        try:
            st = json.load(open(path))
        except (OSError, ValueError):
            continue
        tag = os.path.basename(path)
        steps = st.get("steps", {})
        bad = {k: v for k, v in steps.items() if v.get("rc") not in (0, None)}
        ev.append((st.get("finished") or st.get("started"), "fill", "fill_run",
                   f"fill run{' (weekly)' if st.get('weekly') else ''}{' DRY' if st.get('dry_run') else ''}: "
                   f"{len(steps)} step(s), {len(bad)} failed",
                   json.dumps({k: {"rc": v.get("rc"), "seconds": v.get("seconds"),
                                   "skipped": v.get("skipped")} for k, v in steps.items()}),
                   f"fill:{tag}"))
        for k, v in bad.items():
            ev.append((st.get("finished") or st.get("started"), "fill", "fill_step_failed",
                       f"{k} rc={v.get('rc')}: {(v.get('err_tail') or v.get('tail') or '')[-160:]}",
                       json.dumps({"step": k, "rc": v.get("rc")}), f"fill:{tag}:{k}"))
    try:
        st = json.load(open(DRIP_STATE))
        for k, v in st.items():
            if v.get("status") == "parked" and v.get("parked_at"):
                ev.append((v["parked_at"], "drip", "parked", f"parked {(v.get('name') or k)[:60]}: "
                           f"{(v.get('reason') or '')[:200]}", json.dumps({"setkey": k}),
                           f"drip:parked:{k}:{v['parked_at']}"))
            elif v.get("status") == "failed" and v.get("completed_at") and "stranded" in (v.get("reason") or ""):
                ev.append((v["completed_at"], "drip", "stranded", f"cancelled stranded chain "
                           f"{(v.get('name') or k)[:60]}", json.dumps({"setkey": k}),
                           f"drip:stranded:{k}:{v['completed_at']}"))
    except (OSError, ValueError):
        pass
    if not ev:
        return 0
    from psycopg2.extras import execute_values
    with conn.cursor() as c:
        execute_values(c, """INSERT INTO delimp_qc_event (at, source, kind, summary, detail, dedupe_key)
                             VALUES %s ON CONFLICT (dedupe_key) DO NOTHING""", ev,
                       template="(%s::timestamptz,%s,%s,%s,%s::jsonb,%s)", page_size=500)
        n = c.rowcount
    conn.commit()
    return n


def record_scan(conn, kind):
    with conn.cursor() as c:
        c.execute("""INSERT INTO delimp_qc_scan (kind, n_red, n_amber, findings, metrics, job_id, host)
                     VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s) RETURNING id""",
                  (kind, sum(1 for f in findings if f[0] == "RED"),
                   sum(1 for f in findings if f[0] == "AMBER"),
                   json.dumps([{"sev": s_, "area": a_, "msg": m} for s_, a_, m in findings]),
                   json.dumps(metrics, default=str), os.environ.get("SLURM_JOB_ID"),
                   os.uname().nodename))
        sid = c.fetchone()[0]
    conn.commit()
    return sid


def post(text):
    try:
        hook = open(WEBHOOK_FILE).read().strip()
        req = urllib.request.Request(hook, data=json.dumps({"text": text}).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20).read()
        print("posted to Slack")
    except (OSError, urllib.error.URLError) as e:
        print(f"Slack post failed ({str(e)[:120]}) -- the report file is still written")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--heal", action="store_true")
    ap.add_argument("--post", action="store_true")
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--kind", default="manual", choices=["daily", "weekly", "manual"])
    ap.add_argument("--no-db", action="store_true", help="don't write delimp_qc_scan / delimp_qc_event")
    a = ap.parse_args()

    global _conn
    from coreomics_import import _conn
    conn = _conn()
    conn.set_session(readonly=True, autocommit=True)
    with conn.cursor() as c:
        c.execute("SET statement_timeout = '15min'")
        for fn in (check_db, check_fill, check_coverage, check_backlog, check_ingest):
            try:
                fn(c)
            except Exception as e:  # noqa: BLE001 - one broken check must not hide the others
                add("AMBER", fn.__name__.replace("check_", ""), f"check errored: {repr(e)[:200]}")
    conn.close()
    filler_state = check_filler()
    for fn in (check_drip, check_bad_nodes, check_cron):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            add("AMBER", fn.__name__.replace("check_", ""), f"check errored: {repr(e)[:200]}")
    if a.heal:
        heal(filler_state)

    order = {"RED": 0, "AMBER": 1, "ok": 2}
    findings.sort(key=lambda f: order.get(f[0], 3))
    n_red = sum(1 for f in findings if f[0] == "RED")
    n_amb = sum(1 for f in findings if f[0] == "AMBER")
    head = f"FRAN weekly health scan {time.strftime('%F')}: {n_red} RED, {n_amb} AMBER"
    body = "\n".join(f"- *{s}* {a_}: {m}" for s, a_, m in findings)
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, f"scan_{time.strftime('%Y%m%d_%H%M%S')}.md")
    with open(path, "w") as fh:
        fh.write(f"# {head}\n\n{body}\n")
    print(f"\n{head}\nreport: {path}")
    if not a.no_db:
        try:
            w = _conn()
            n_ev = import_events(w)
            sid = record_scan(w, a.kind)
            w.close()
            print(f"recorded scan {sid} and {n_ev} new event(s) for FRAN-QC")
        except Exception as e:  # noqa: BLE001 - the report file and Slack post still happen
            print(f"could not record to the QC tables: {repr(e)[:300]}")
    if a.post:
        short = "\n".join(f"- *{s}* {a_}: {m.splitlines()[0][:220]}" for s, a_, m in findings if s != "ok")
        post(f"{head}\n{short or 'everything filling normally'}\nfull report: {path}")
    return 1 if n_red else 0


if __name__ == "__main__":
    sys.exit(main())
