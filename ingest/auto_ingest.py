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


def search_key(path: str) -> str:
    """The SEARCH a candidate export belongs to. Under FRAN_reports/<search>/<export>/ that is the
    parent; elsewhere the directory itself."""
    parts = path.rstrip("/").split("/")
    if "FRAN_reports" in parts:
        i = parts.index("FRAN_reports")
        if i + 1 < len(parts):
            return parts[i + 1]
    return parts[-1]


def pick_one(dirs: list[str]) -> str:
    """Newest export for a search. The export timestamp leads the directory name, so a plain
    descending sort on it is chronological; directories without one sort last and are only chosen
    when nothing else is available."""
    def key(d):
        m = _TS.match(os.path.basename(d.rstrip("/")))
        return (1, m.group(1)) if m else (0, os.path.basename(d))
    return sorted(dirs, key=key, reverse=True)[0]


def select(candidates, skip_failed=True):
    by_search: dict[str, list[str]] = {}
    for c in candidates:
        by_search.setdefault(search_key(c["dir"]), []).append(c["dir"])
    chosen, skipped = [], []
    for name, dirs in sorted(by_search.items()):
        if skip_failed and name.lower().startswith(("fail_", "fail-")):
            skipped.append((name, "named fail*")); continue
        engine = next(c["engine"] for c in candidates if c["dir"] in dirs)
        chosen.append({"search": name, "engine": engine, "dir": pick_one(dirs),
                       "n_exports": len(dirs)})
    return chosen, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually ingest (default: dry run)")
    ap.add_argument("--limit", type=int, default=5, help="max searches to ingest per run")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--candidates", help="reuse a find_uningested.py --json-out instead of rescanning")
    ap.add_argument("--include-failed", action="store_true")
    ap.add_argument("--timeout", type=int, default=10800, help="per-search timeout, seconds")
    a = ap.parse_args()

    print(f"===== auto_ingest {time.strftime('%F %T')} on {os.uname().nodename} =====", flush=True)

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

    todo = chosen[:a.limit]
    if len(chosen) > a.limit:
        print(f"\nlimit={a.limit}: ingesting {len(todo)} now, {len(chosen)-a.limit} left for the "
              f"next run", flush=True)

    ok = dup = fail = 0
    for i, c in enumerate(todo, 1):
        tag = f"[{i}/{len(todo)}] {c['engine']} {c['search'][:52]}"
        if c["n_exports"] > 1:
            print(f"\n{tag}  (newest of {c['n_exports']} exports)", flush=True)
        else:
            print(f"\n{tag}", flush=True)
        print(f"      {c['dir']}", flush=True)
        if not a.apply:
            print("      DRY RUN — not ingesting", flush=True); continue
        cmd = [a.python, os.path.join(HERE, "corpus_ingest.py"), c["dir"],
               "--engine", c["engine"], "--name", c["search"],
               "--output-dir", c["dir"], "--bulk-copy"]
        t0 = time.time()
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=a.timeout)
        except subprocess.TimeoutExpired:
            print(f"      TIMEOUT after {a.timeout}s", flush=True); fail += 1; continue
        tail = (r.stdout or "")[-1500:]
        el = time.time() - t0
        if r.returncode == 0:
            ok += 1
            print(f"      OK in {el:.0f}s", flush=True)
        elif "duplicate" in (r.stdout + r.stderr).lower():
            dup += 1
            print(f"      SKIPPED-DUPLICATE in {el:.0f}s (guard refused — not a failure)", flush=True)
        else:
            fail += 1
            print(f"      FAILED rc={r.returncode} in {el:.0f}s", flush=True)
            print("      --- last output ---", flush=True)
            for line in tail.splitlines()[-12:]:
                print("      | " + line, flush=True)
    print(f"\n===== done: {ok} ingested, {dup} duplicate-skipped, {fail} failed, "
          f"{len(chosen)-len(todo)} still queued — {time.strftime('%F %T')} =====", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
