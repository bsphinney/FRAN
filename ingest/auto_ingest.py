"""auto_ingest.py — find un-ingested searches and ingest them, for the Hive cron.

DRY RUN unless --apply. Wraps find_uningested.py (which only proposes) and corpus_ingest.py (which
does the work), adding the selection and safety rules an unattended run needs.

WHAT THE SELECTION RULES ARE FOR, measured on the first real scan (113 candidate dirs):

  * 113 dirs were only 63 DISTINCT searches. FRAN_reports keeps EVERY re-export --
    "20260121_125024_PJ" had 7 -- and each has a different path, so each would ingest as its own
    search. ONE export per search is taken, the newest by directory name (the export timestamp is
    the leading component). Ingesting all 113 would have created ~50 redundant searches.
  * 3 searches are named "fail*" -- exports the operator marked as failed. Skipped by default.
  * A per-run cap (--limit) exists so a bad scan cannot start 60 ingests unattended. The cron sets
    it low and simply catches up on the next tick.

The duplicate guard in corpus_ingest is the backstop, not the plan: it refuses a write when another
output_dir already holds the same raw-file set and precursor count. Selection above is what keeps
the guard from being the only thing standing between a re-export and a duplicate row. When the guard
does fire, that is logged as SKIPPED-DUPLICATE, not as a failure.

Deliberately NOT enabled here: --lance-dir/--xic-dir. Lane writes are GB-scale per search and this
runs unattended on a database already at 228 GB; enabling them is a storage decision for a human.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))

_TS = re.compile(r"^(\d{8}_\d{4,6})_")
_SN_REPORT = re.compile(r"_Report.*\.(tsv|parquet)$", re.I)


def resolve_input(d: str, engine: str) -> str | None:
    """What to pass to corpus_ingest as its positional `searchdir`.

    Spectronaut is the exception and it is documented (INSTALL.md step 5): it takes the report
    FILE, not the directory. Handing it the directory dies with
    `IsADirectoryError` inside pandas.read_csv. Every other engine takes the directory.

    --output-dir stays the DIRECTORY regardless, because that is the search's identity
    (search_id = uuid5(namespace, output_dir)) and must not change with the report's filename."""
    if engine != "spectronaut":
        return d
    try:
        cands = [f for f in os.listdir(d) if _SN_REPORT.search(f)]
    except OSError:
        return None
    if not cands:
        return None
    # Prefer the FRAN.rs schema export when a directory holds more than one report: a BGS report has
    # no genes, no ion mobility and no fragment columns. See ingest/SPECTRONAUT_FRAN_INGEST.md.
    cands.sort(key=lambda f: (0 if "fran" in f.lower() else 1, -len(f)))
    return os.path.join(d, cands[0])


def search_key(path: str) -> str:
    """The SEARCH a candidate export belongs to. Under FRAN_reports/<search>/<export>/ that is the
    parent; elsewhere the directory itself."""
    parts = path.rstrip("/").split("/")
    if "FRAN_reports" in parts:
        i = parts.index("FRAN_reports")
        if i + 1 < len(parts):
            return parts[i + 1]
    return parts[-1]


def _export_ts(d: str):
    m = _TS.match(os.path.basename(d.rstrip("/")))
    return (1, m.group(1)) if m else (0, os.path.basename(d))


def usable(d: str, engine: str) -> bool:
    """Does this export actually contain a report worth reading?

    16 of the 60 searches in the first real scan resolve to a ZERO-BYTE report -- a failed export
    that still left a stub file behind. They are not ingestable and there is nothing to retry, so
    they must not consume the per-run limit every single run, forever."""
    t = resolve_input(d, engine)
    if not t:
        return False
    try:
        return os.path.getsize(t) > 1024 if os.path.isfile(t) else os.path.isdir(t)
    except OSError:
        return False


def pick_one(dirs: list[str], engine: str = "spectronaut") -> str | None:
    """Newest USABLE export for a search, or None if none is usable.

    Newest-overall is the wrong choice on its own: FRAN_reports keeps every attempt, and for
    searches like 20241202_133750_22Feb2024_tryingBi2GAIN (6 exports) the NEWEST is the empty one
    while an older export is fine. Filtering first rescues those instead of discarding the search."""
    good = [d for d in dirs if usable(d, engine)]
    if not good:
        return None
    return sorted(good, key=_export_ts, reverse=True)[0]


def select(candidates, skip_failed=True):
    by_search: dict[str, list[str]] = {}
    for c in candidates:
        by_search.setdefault(search_key(c["dir"]), []).append(c["dir"])
    chosen, skipped = [], []
    for name, dirs in sorted(by_search.items()):
        if skip_failed and name.lower().startswith(("fail_", "fail-")):
            skipped.append((name, "named fail*")); continue
        engine = next(c["engine"] for c in candidates if c["dir"] in dirs)
        best = pick_one(dirs, engine)
        if best is None:
            skipped.append((name, f"no usable report in {len(dirs)} export(s) — empty/failed"))
            continue
        chosen.append({"search": name, "engine": engine, "dir": best,
                       "identity": os.path.realpath(best),
                       "n_exports": len(dirs), "n_usable": sum(1 for d in dirs if usable(d, engine))})
    return chosen, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually ingest (default: dry run)")
    ap.add_argument("--limit", type=int, default=5, help="max searches to ingest per run")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--candidates", help="reuse a find_uningested.py --json-out instead of rescanning")
    ap.add_argument("--include-failed", action="store_true")
    ap.add_argument("--direct", metavar="JOBS.json",
                    help="ingest an explicit job list [{report, identity, name, engine}] instead "
                         "of scanning — used for .sne experiments whose report is already on disk")
    ap.add_argument("--timeout", type=int, default=10800, help="per-search timeout, seconds")
    a = ap.parse_args()

    print(f"===== auto_ingest {time.strftime('%F %T')} on {os.uname().nodename} =====", flush=True)

    if a.direct:
        jobs = json.load(open(a.direct))
        chosen = [{"search": j["name"], "engine": j.get("engine", "spectronaut"),
                   "dir": os.path.dirname(j["report"]), "report": j["report"],
                   "identity": j["identity"], "identity_from": "direct",
                   "n_exports": 1, "n_usable": 1,
                   **({"organism": j["organism"]} if j.get("organism") else {})}
                  for j in jobs]
        print(f"direct mode: {len(chosen)} job(s) from {a.direct}", flush=True)
        skipped = []
        return _run(a, chosen, skipped)

    if a.candidates and os.path.exists(a.candidates):
        candidates = json.load(open(a.candidates))
        print(f"reusing {len(candidates)} candidates from {a.candidates}", flush=True)
    else:
        out = a.candidates or "/tmp/uningested_%d.json" % os.getpid()
        cmd = [a.python, os.path.join(HERE, "find_uningested.py"), "--json-out", out]
        print("scanning: " + " ".join(cmd), flush=True)
        r = subprocess.run(cmd, capture_output=True, text=True)
        sys.stdout.write(r.stdout[-4000:])
        if r.returncode != 0:
            print(f"SCAN FAILED rc={r.returncode}\n{r.stderr[-2000:]}"); return 1
        candidates = json.load(open(out))

    chosen, skipped = select(candidates, skip_failed=not a.include_failed)
    print(f"\n{len(candidates)} dirs -> {len(chosen)} distinct searches "
          f"({len(skipped)} skipped)", flush=True)
    for name, why in skipped:
        print(f"  SKIP {name[:60]}  ({why})", flush=True)

    return _run(a, chosen, skipped)


def _run(a, chosen, skipped):
    ok = dup = fail = 0
    todo = chosen[:a.limit]
    if len(chosen) > a.limit:
        print(f"\nlimit={a.limit}: ingesting {len(todo)} now, {len(chosen)-a.limit} left for the "
              f"next run", flush=True)

    for i, c in enumerate(todo, 1):
        tag = f"[{i}/{len(todo)}] {c['engine']} {c['search'][:52]}"
        if c["n_exports"] > 1:
            print(f"\n{tag}  (newest usable of {c['n_exports']} exports, "
                  f"{c.get('n_usable', '?')} usable)", flush=True)
        else:
            print(f"\n{tag}", flush=True)
        print(f"      {c['dir']}", flush=True)
        if c.get("identity") and c["identity"] != c["dir"]:
            # symlinked in via the drop box; the real path is the search's identity
            print(f"      -> {c['identity']}", flush=True)
        if not a.apply:
            print("      DRY RUN — not ingesting", flush=True); continue
        target = c.get("report") or resolve_input(c["dir"], c["engine"])
        if not target:
            fail += 1
            print("      FAILED: no report file found in the directory", flush=True)
            continue
        if target != c["dir"]:
            print(f"      report: {os.path.basename(target)}", flush=True)
        cmd = [a.python, os.path.join(HERE, "corpus_ingest.py"), target,
               "--engine", c["engine"], "--name", c["search"],
               "--output-dir", c.get("identity") or c["dir"], "--bulk-copy"]
        t0 = time.time()
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=a.timeout)
        except subprocess.TimeoutExpired:
            print(f"      TIMEOUT after {a.timeout}s", flush=True); fail += 1; continue
        tail = ((r.stdout or "") + "\n--- stderr ---\n" + (r.stderr or ""))[-2500:]
        el = time.time() - t0
        # ORDER MATTERS. The duplicate guard `return`s rather than sys.exit(1), so a refused
        # ingest exits 0 -- checking returncode first would log it as "OK" and the run summary would
        # claim it ingested searches it did not touch. Match the guard's own message, and match it
        # narrowly: the substring "duplicate" alone also appears in corpus_ingest's --allow-duplicate
        # help text.
        blob = (r.stdout or "") + (r.stderr or "")
        if "DUPLICATE of an already-ingested search" in blob:
            dup += 1
            print(f"      SKIPPED-DUPLICATE in {el:.0f}s (guard refused — not a failure)", flush=True)
            for line in blob.splitlines():
                if line.strip().startswith("exists:"):
                    print(f"      {line.strip()}", flush=True)
        elif r.returncode == 0:
            ok += 1
            print(f"      OK in {el:.0f}s", flush=True)
        else:
            fail += 1
            print(f"      FAILED rc={r.returncode} in {el:.0f}s", flush=True)
            print("      --- last output ---", flush=True)
            for line in [x for x in tail.splitlines() if x.strip()][-14:]:
                print("      | " + line, flush=True)
    print(f"\n===== done: {ok} ingested, {dup} duplicate-skipped, {fail} failed, "
          f"{len(chosen)-len(todo)} still queued — {time.strftime('%F %T')} =====", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
