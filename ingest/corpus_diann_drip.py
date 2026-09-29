"""corpus_diann_drip.py — submit the corpus-wide DIA-NN re-search as a paced drip, resumably.

THE JOB. 1,337 Spectronaut raw sets / 14,478 runs / ~140,400 CPU-hours, from
CORPUS_WORKLIST.tsv. At 100 sustained CPUs that is about six weeks of wall clock, so this is
not a script you run once and watch: it is fired repeatedly by cron, does a little each time,
and survives a reboot, a cancelled job, a full queue and a month of elapsed time.

WHY A DRIP AND NOT ONE BIG SUBMISSION
  * A monolith occupies a shared cluster continuously with no natural pause, and a failure
    part-way means restarting it.
  * The 9.7 CPU-h/run figure comes from 26 pilot searches with a median of 14 runs, against a
    corpus median of 6. Small sets amortise step 1 worse, so the real cost may be higher — a
    paced run discovers that in week one instead of after committing everything.
  * Backpressure means the rate self-adjusts to how busy the partition is, rather than us
    guessing right once.

WHY THE UNIT IS A RAW SET, NOT A SEARCH. 2,094 Spectronaut searches collapse to 1,462 distinct
sets of raws; 415 sets have been searched two or more times, one of them eight. DIA-NN's output
depends on the raws and the parameters, not on how many times Spectronaut was pointed at them,
so searching per-search would run identical work up to eight times. That is ~73,600 CPU-hours.

WHY THE ORDER IS BY COHORT. A cohort is (instrument, measured DIA isolation range) — searches
that can share one predicted spectral library. plan_speclib_batch.py deduplicates the library
build across a batch, so a batch drawn from ONE cohort builds the library once and every other
member waits on that build instead of repeating it. A batch drawn in arrival order shares
nothing. CORPUS_WORKLIST.tsv is pre-ordered largest-cohort-first for exactly this reason, and
this script takes contiguous slices of it.

THE m/z RANGE IS THE MEASURED ONE. --min-pr-mz/--max-pr-mz come from CORPUS_PROBE.tsv, which
reads the isolation windows out of each raw. They are NOT raw_files.mass_range_min/max: on
Bruker that column is the instrument's full acquisition range, on Thermo the MS1 scan range.
Measured on one timsTOF Pro run the column says 100–1700 and the method isolated 268.59–1218.59.
The cache keys on these values EXACTLY, so a wrong one silently reuses a library covering a span
that was never acquired — nothing fails, identifications are simply missing.

STATE. One row per raw set in a JSON file, so progress is answerable at any moment and a
re-fire never resubmits what is already running. Never infer "done" from report.parquet
existing: it exists while step 5 is still writing it, which nearly caused a partial ingest on
2026-09-22. Completion means no job from the set's jobs.txt is still in the queue.

    python ingest/corpus_diann_drip.py --status
    python ingest/corpus_diann_drip.py --dry-run
    python ingest/corpus_diann_drip.py --submit          # one firing; safe under cron with flock
"""
import argparse
import csv
import functools
import json
import os
import shlex
import subprocess
import sys
import time

print = functools.partial(print, flush=True)                      # noqa: A001

ROOT = "/nfs/lssc0/flinders/proteomics/Data/FRAN_diann"
WORKLIST = os.path.join(ROOT, "CORPUS_WORKLIST.tsv")
PROBE = os.path.join(ROOT, "CORPUS_PROBE.tsv")
OUT_ROOT = os.path.join(ROOT, "corpus_2026")
STATE = os.path.join(OUT_ROOT, "state.json")
FASTA_ROOT = os.path.join(ROOT, "_fasta")

STACK = "/quobyte/proteomics-grp/fran/pipeline-skill/stack-libcache/scripts"
TOOLS = "/quobyte/proteomics-grp/fran/engines/pilot_tools/tools.json"
# The DIA-NN binary itself. plan_speclib_batch forwards --diann straight through to
# diann_parallel.py, which REQUIRES it -- omitting it fails at batch_submit.sh, after the plan
# looks fine, which is a confusing place to find out. Taken from tools.json's "diann" key rather
# than hardcoded from a pilot sbatch, so the pinned version is the one that actually runs.
DIANN_BIN = "/quobyte/proteomics-grp/fran/engines/pilot_tools/diann/2.7.0/diann-2.7.0/diann-linux"
PY = "/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python"
DIANN_VERSION = "2.7.0"

# Budget. `low` is the contended partition and the right home for a month-long background job;
# the pacing note measured 1,343 idle CPUs there. 100 sustained is a small fraction and leaves
# the cluster usable by everyone else, which matters over six weeks.
PARTITION, ACCOUNT, QOS = "low", "publicgrp", "publicgrp-low-qos"
# 100 was wrong, and wrong in the way that matters: it was BELOW the footprint of a single
# search, so the drip could never submit a second one while any search was running. Measured on
# the live run -- 3 searches submitted, 632 CPUs occupied, 1,325 of 1,337 sets never touched
# because every firing after the first found itself over the ceiling.
#
# The error was in the note below, which claimed a chain "never runs more than 64 at once". That
# is true of s3_assembly and s5_report, which are single 64-CPU jobs, and false of the steps that
# dominate: s2_firstpass and s4_finalpass are ARRAYS throttled at %20, each task taking
# THREADS_PER_FILE, so one search peaks near 20 x 16 = 320 CPUs. A ceiling of 100 against a peak
# of 320 is not a throttle, it is a stop.
#
# 800 clears one search's peak comfortably, lets several small sets (the corpus median is 6 runs,
# about 96 CPUs at step 2) run together, and still leaves most of a partition the pacing note
# measured at 1,343 idle CPUs. It is an emergency brake, which is what it should always have been.
CPU_CEILING = 800
# Separate from the CPU ceiling and doing a different job: the CPU count bounds what we OCCUPY,
# this bounds what we QUEUE.
# Raised from 12 after measuring what 12 actually occupies: 176 CPUs against a ceiling of 800,
# with 4,157 CPUs idle in the partition and every one of our pending jobs waiting on its OWN chain
# (squeue reason "Dependency"), not on the cluster. A search is five SEQUENTIAL steps, so it idles
# between them -- twelve in flight averaged ~15 active CPUs each. The queue-depth cap was sized as
# if searches occupy CPUs continuously; they do not. 50 is what it takes to approach the 800-CPU
# ceiling, and the ceiling is then the thing that actually throttles, which is what it is for.
MAX_SEARCHES_IN_FLIGHT = 50
# diann_parallel.py defaults --time-per-file to 2 hours, and plan_speclib_batch.py does not expose
# it, so every step-2/step-4 array task got a 2 h wall. That killed five chains on the first two
# days: one run of a 2-file set needed 2:00:10 while its partner finished in 0:29:56, the task hit
# the wall, and afterok then never released s3_assembly -- losing the whole search, including the
# hundreds of first-pass tasks that had already succeeded beside it. Run length varies by more
# than an order of magnitude across this corpus, so a wall sized for the median is a wall that
# silently destroys the tail.
TIME_PER_FILE_H = 8
THREADS_PER_FILE = 16
BATCH_MAX = 10            # sets generated per firing; one cohort slice, so they share a library


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def load_state():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(st):
    os.makedirs(OUT_ROOT, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh, indent=1, sort_keys=True)
    os.replace(tmp, STATE)          # atomic: a killed firing never leaves half a state file


def worklist():
    with open(WORKLIST) as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def probe_index():
    idx = {}
    with open(PROBE) as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            idx[r["run"]] = r
    return idx


def cpus_in_flight():
    """This user's RUNNING CPUs — the backpressure signal.

    RUNNING ONLY, and that is a correction. The first version counted PENDING too, reasoning that
    our own queued work is already committed. Measured on the first real batch: three searches
    reported 564 CPUs against a ceiling of 100, which would have wedged the drip permanently after
    one firing.

    The reasoning was wrong because a 5-step chain is a DEPENDENCY chain. step1 -> 1b -> 2 -> 3 ->
    4 -> 5, each afterok on the last, so at most one step of a search ever runs at a time. Summing
    all five counts 188 CPUs for a search whose real peak is 64 (the assembly step). Four fifths of
    that total is work SLURM is holding back precisely so it does not consume the cluster.

    A pending queue is still a real cost to other users -- it takes scheduling priority -- so it is
    bounded separately by MAX_SEARCHES_IN_FLIGHT rather than by pretending it is occupancy.
    """
    r = sh(["bash", "-lc", "squeue -u $USER -h -t RUNNING -o '%C' 2>/dev/null"])
    n = 0
    for line in (r.stdout or "").splitlines():
        try:
            n += int(line.strip())
        except ValueError:
            pass
    return n


def searches_in_flight(st):
    """Searches whose chain has not finished. The bound on how much we queue, as opposed to how
    much we occupy -- a queue of our own pending chains still takes scheduling priority from
    everyone else even though it burns no CPU."""
    return sum(1 for v in st.values() if v.get("status") == "submitted")


def still_running(jobs):
    """True while ANY job id from this set's chain is in the queue.

    The completion test, and it is deliberately not "report.parquet exists": that file exists
    while step 5 is still writing it.
    """
    if not jobs:
        return False
    r = sh(["bash", "-lc", "squeue -h -j %s -o %%i 2>/dev/null" % ",".join(str(j) for j in jobs)])
    return bool((r.stdout or "").strip())


def fasta_for(taxon):
    """The prepared FASTA for this taxon, or None. Never fetches: a drip firing is not the
    place to discover the internet is down. Missing taxa are reported by --status and fetched
    deliberately."""
    if not taxon:
        return None
    try:
        for d in sorted(os.listdir(FASTA_ROOT)):
            if d.endswith("_" + str(taxon)):
                vers = sorted(os.listdir(os.path.join(FASTA_ROOT, d)))
                for v in reversed(vers):
                    f = os.path.join(FASTA_ROOT, d, v, "search.fasta")
                    if os.path.exists(f):
                        return f
    except OSError:
        pass
    return None


def prepare(row, probes):
    """Generate one search's params.cfg and raw list. Returns a batch entry, or (None, reason)."""
    name = "%s__%s" % (row["search_name"][:60].replace("/", "_").replace(" ", "_"),
                       row["setkey"][:8])
    dest = os.path.join(OUT_ROOT, name)
    wf = os.path.join(dest, "wf")
    os.makedirs(wf, exist_ok=True)

    fasta = fasta_for(row.get("taxon"))
    if not fasta:
        return None, "no prepared FASTA for taxon %s" % row.get("taxon")

    raws = [p["path"] for b, p in probes.items() if b in row["_runs"]]
    if not raws:
        return None, "no probed raw paths"
    rl = os.path.join(wf, "files.txt")
    with open(rl, "w") as fh:
        fh.write("\n".join(raws) + "\n")

    cfg = os.path.join(wf, "params.cfg")
    r = sh([PY, os.path.join(STACK, "estimate_params.py"),
            "--engine", "diann", "--acquisition", "DIA",
            "--instrument", row["instrument"] or "unknown",
            "--var-mods", "ox",
            # mc=2 matches what Spectronaut plainly allowed; the arm-1/arm-2 comparison showed
            # --missed-cleavages 1 accounted for 21.5% of the peptides DIA-NN appeared to miss.
            "--overrides", json.dumps({"--missed-cleavages": 2}),
            "--precursor-mz-range", row["dia_mz_lo"], row["dia_mz_hi"],
            "--fasta-meta", fasta + ".meta.json",
            "--out", cfg])
    if r.returncode != 0 or not os.path.exists(cfg):
        return None, "estimate_params failed rc=%s %s" % (r.returncode, (r.stderr or "")[:120])
    return {"name": name, "fasta": fasta, "cfg": cfg, "raw_list": rl,
            "out": os.path.join(dest, "output", "search")}, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--submit", action="store_true")
    ap.add_argument("--ceiling", type=int, default=CPU_CEILING)
    ap.add_argument("--batch-max", type=int, default=BATCH_MAX)
    ap.add_argument("--max-in-flight", type=int, default=MAX_SEARCHES_IN_FLIGHT)
    a = ap.parse_args()

    st = load_state()
    rows = worklist()
    probes = probe_index()

    # refresh what has finished since the last firing
    changed = False
    for sk, s in st.items():
        if s.get("status") == "submitted" and not still_running(s.get("jobs") or []):
            s["status"] = "complete"
            s["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            changed = True
    if changed:
        save_state(st)

    done = {k for k, v in st.items() if v.get("status") in ("complete", "submitted", "skipped")}
    todo = [r for r in rows if r["setkey"] not in done]
    counts = {}
    for v in st.values():
        counts[v.get("status", "?")] = counts.get(v.get("status", "?"), 0) + 1

    if a.status or not (a.dry_run or a.submit):
        inflight = cpus_in_flight()
        print(f"worklist   : {len(rows):,} raw sets")
        print(f"state      : {counts or '(nothing started)'}")
        print(f"remaining  : {len(todo):,}")
        print(f"CPUs RUNNING  : {inflight:,} / ceiling {a.ceiling:,}")
        print(f"searches in flight: {searches_in_flight(st)} / {a.max_in_flight}")
        miss = sorted({r['taxon'] for r in todo if not fasta_for(r.get('taxon'))})
        if miss:
            print(f"taxa with no prepared FASTA ({len(miss)}): {miss[:12]}")
        return 0

    inflight = cpus_in_flight()
    if inflight >= a.ceiling:
        print(f"{inflight:,} CPUs RUNNING >= ceiling {a.ceiling:,} — nothing submitted")
        return 0
    nflight = searches_in_flight(st)
    if nflight >= a.max_in_flight:
        print(f"{nflight} searches already in flight >= {a.max_in_flight} — nothing submitted")
        return 0
    a.batch_max = min(a.batch_max, a.max_in_flight - nflight)
    if not todo:
        print("worklist complete")
        return 0

    # Record EVERY single-run set now, in one pass, and take them out of contention.
    #
    # Skipping them one batch at a time was not enough. The batch is the ten smallest sets in the
    # cohort, and the ten smallest are all single-run, so a firing skipped ten and generated
    # nothing -- and the next firing would have done the same, for the eighteen firings it takes
    # to walk past 181 of them at ten a time. Six hours of doing nothing, correctly.
    #
    # They are still recorded individually, with the reason, so the progress report can count
    # them; what changes is that the drip stops rediscovering them and gets on with the 1,156
    # sets it can actually run.
    singles = [r for r in todo if int(r["n_runs"] or 0) < 2]
    if singles:
        why = "single-run set -- needs the single-shot run_search.py, not the parallel chain"
        for r in singles:
            st[r["setkey"]] = {"status": "skipped", "reason": why, "cohort": r["cohort"],
                               "n_runs": int(r["n_runs"] or 0)}
        save_state(st)
        print(f"recorded {len(singles)} single-run set(s) as skipped — they need run_search.py")
        todo = [r for r in todo if int(r["n_runs"] or 0) >= 2]
        if not todo:
            print("nothing left that the parallel chain can run")
            return 0

    # One contiguous slice, so the batch sits inside a single cohort and shares a library. Cohort
    # ORDER is left alone -- largest-cohort-first still amortises a library build over the most
    # searches -- but WITHIN the chosen cohort the smallest sets go first.
    #
    # Ordering by size costs nothing in library reuse, since every set here shares the cohort's
    # one library, and it fixes the thing that actually hurt: the first firing drew the three
    # largest sets in the corpus (125, 224 and 220 runs), saturated the cluster with multi-day
    # searches, and left nothing finishing for days. Smallest-first means completed searches --
    # and therefore ingested results, and a progress report that moves -- within hours.
    cohort = todo[0]["cohort"]
    in_cohort = [r for r in todo if r["cohort"] == cohort]
    slice_ = sorted(in_cohort, key=lambda r: int(r["n_runs"] or 0))[: a.batch_max]
    print(f"cohort {cohort}: taking {len(slice_)} of {len(in_cohort)}")

    # map setkey -> its run basenames, needed to pick raw paths out of the probe table
    import collections
    runs_of = collections.defaultdict(set)
    sys.path.insert(0, "/quobyte/proteomics-grp/brett/glendon/fran_ingest")
    import plan_spectrum_backfill as PB
    c = PB._conn(); cur = c.cursor()
    cur.execute("""
      WITH sn AS (SELECT s.id, md5(string_agg(rf.raw_basename, ',' ORDER BY rf.raw_basename)) AS setkey
                  FROM delimp_searches s JOIN search_raw_files srf ON srf.search_id=s.id
                  JOIN raw_files rf ON rf.raw_path=srf.raw_path
                  WHERE s.search_engine='spectronaut' GROUP BY s.id),
           pick AS (SELECT DISTINCT ON (setkey) setkey, id FROM sn ORDER BY setkey, id)
      SELECT p.setkey, rf.raw_basename FROM pick p
      JOIN search_raw_files srf ON srf.search_id=p.id
      JOIN raw_files rf ON rf.raw_path=srf.raw_path""")
    for sk, base in cur.fetchall():
        b = base.rstrip("/")
        for e in (".d", ".raw"):
            if b.lower().endswith(e):
                b = b[: -len(e)]
        runs_of[sk].add(os.path.basename(b))
    c.close()

    batch, skipped = [], []
    for r in slice_:
        # A one-run set cannot go through this pipeline at all: plan_speclib_batch/batch_submit
        # refuse it outright -- "Parallel search needs >= 2 raw files ... use the single-shot
        # run_search.py for 1" -- because the 5-step chain exists to split runs across an array
        # and there is nothing to split. 181 of the 1,337 sets (13.5%) are single-run.
        #
        # Recorded as a skip WITH ITS REASON rather than quietly passed over, so the progress
        # report counts them and names why. They are real work that still needs doing, by the
        # single-shot path, and a silent omission here is exactly how a worklist shrinks without
        # anyone noticing. Found within one firing of ordering smallest-first, which put ten of
        # them in the first batch -- the old ordering would have hidden it for weeks.
        if int(r["n_runs"] or 0) < 2:
            why = "single-run set -- needs the single-shot run_search.py, not the parallel chain"
            skipped.append((r["setkey"], why))
            st[r["setkey"]] = {"status": "skipped", "reason": why, "cohort": r["cohort"],
                               "n_runs": int(r["n_runs"] or 0)}
            continue
        r["_runs"] = runs_of.get(r["setkey"], set())
        entry, why = prepare(r, probes)
        if entry:
            batch.append(entry)
            st[r["setkey"]] = {"status": "generated", "name": entry["name"],
                               "cohort": r["cohort"], "n_runs": int(r["n_runs"])}
        else:
            skipped.append((r["setkey"], why))
            st[r["setkey"]] = {"status": "skipped", "reason": why, "cohort": r["cohort"]}
    save_state(st)

    for sk, why in skipped:
        print(f"  SKIP {sk[:10]} — {why}")
    if not batch:
        print("nothing generated this firing")
        return 0

    bjson = os.path.join(OUT_ROOT, "batch_%s.json" % time.strftime("%Y%m%d_%H%M%S"))
    with open(bjson, "w") as fh:
        json.dump(batch, fh, indent=1)
    plan_cmd = [PY, os.path.join(STACK, "plan_speclib_batch.py"),
                "--batch", bjson, "--diann-version", DIANN_VERSION,
                "--diann", DIANN_BIN,
                "--out-root", OUT_ROOT,
                "--threads-per-file", str(THREADS_PER_FILE),
                "--partition", PARTITION, "--account", ACCOUNT, "--qos", QOS]
    print("plan: " + " ".join(shlex.quote(x) for x in plan_cmd))
    if a.dry_run:
        print(f"(dry run — {len(batch)} searches prepared, nothing planned or submitted)")
        return 0

    r = sh(plan_cmd)
    print((r.stdout or "")[-1500:])
    if r.returncode != 0:
        print("plan_speclib_batch FAILED:\n" + (r.stderr or "")[-1200:])
        return 1
    submit = os.path.join(OUT_ROOT, "batch_submit.sh")
    if not os.path.exists(submit):
        print("no batch_submit.sh produced")
        return 1
    # Raise the per-file wall in the generated batch_submit.sh before running it.
    #
    # It has to happen HERE and not earlier: plan_speclib_batch.py writes batch_submit.sh, which
    # invokes diann_parallel.py, which is what generates the step sbatch files -- so the sbatch
    # files do not exist until this script runs, and there is nothing to edit before it. Adding
    # the flag to the diann_parallel.py invocations is the only point where the wall is still
    # changeable from FRAN's side. plan_speclib_batch.py lives in the pipeline skill's stack, not
    # this repo; if it ever forwards --time-per-file, delete this and pass it in plan_cmd instead.
    try:
        txt = open(submit).read()
        lines = txt.splitlines(True)
        n = 0
        for i, ln in enumerate(lines):
            if "diann_parallel.py" in ln and "--time-per-file" not in ln:
                lines[i] = ln.rstrip("\n") + f" --time-per-file {TIME_PER_FILE_H}\n"
                n += 1
        if n:
            with open(submit, "w") as fh:
                fh.writelines(lines)
            print(f"raised --time-per-file to {TIME_PER_FILE_H} h on {n} chain(s)")
    except OSError as e:
        # Not fatal: a 2 h wall still searches most sets. Losing the batch would be worse.
        print(f"could not raise the wall ({e}); chains keep the 2 h default")

    r = sh(["bash", submit])
    print((r.stdout or "")[-2000:])
    if r.returncode != 0:
        print("batch_submit FAILED:\n" + (r.stderr or "")[-1200:])
        return 1

    for e in batch:
        jf = os.path.join(e["out"], "jobs.txt")
        jobs = []
        try:
            jobs = [ln.strip() for ln in open(jf) if ln.strip()]
        except OSError:
            pass
        for sk, v in st.items():
            if v.get("name") == e["name"]:
                v["status"] = "submitted" if jobs else "failed"
                v["jobs"] = jobs
                v["submitted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_state(st)
    print(f"\nsubmitted {sum(1 for e in batch)} searches; state -> {STATE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
