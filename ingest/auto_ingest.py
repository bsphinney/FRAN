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

Lanes are NOT enabled for scan candidates or --direct jobs. Lane writes are GB-scale per search and
this runs unattended on a database already at 228 GB; enabling them is a storage decision, made per
search by whoever REGISTERS it. A diann queue row with xic_dir set (fran_queue.py add --xic-dir) has
DIA-NN's native *.xic.parquet written to the XIC lane by _run_xic_lane() once its precursors ingest OK
(not on SKIPPED-DUPLICATE); the outcome goes to xic_status and never re-queues the row. xic_dir on a
non-diann row is recorded as xic_status 'unsupported' and ignored.

ORDER AND MEMORY (2026-09-24). For a week every run re-tried the same five alphabetically-first
FRAN_reports candidates -- three duplicates, a truncated export and a header-only one -- and ingested
nothing, while drop-box searches sorted behind "2022..." names were never reached. So:

  * drop-box entries (incoming/) go before the FRAN_reports backlog, oldest-staged first within
    each group; registered queue rows still go before both. A drop-box entry takes its identity
    from a VALID fran_manifest.json (a legacy bare-symlink entry from its target) and is skipped --
    never charged -- when the manifest is malformed, when FRAN policy excludes it as QC, or when it
    contradicts its own search's FASTA record (find_uningested.read_manifest / qc_reason: the one
    definition of the contract and of the QC rule);
  * every scan outcome is remembered (auto_ingest_state.py): ok/duplicate never again, a failure
    backs off 4 h -> 1 d and is quarantined after 3; a systemic failure (stale code, database
    down, CLI skew) is not charged -- it stops the run, or, for an import failure, holds back that
    engine only;
  * every ingest is re-checked against the corpus immediately before it runs, under a lease on its
    output_dir, because corpus_ingest deletes and re-inserts an existing one;
  * whatever needs a person -- N runs with work and no progress (a crash or a killed run counts),
    an engine blocked N runs running, a manifest that contradicts its search -- posts ONE Slack
    message (auto_ingest_alert.py).

    python auto_ingest.py --list-quarantine
    python auto_ingest.py --clear <key or unique part of it>
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import platform
import re
import subprocess
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))

# Imported at start-up, not lazily, so a deploy that forgot to copy them fails the job in its first
# second rather than hours in (ingest/DEPLOY_auto_ingest.md).
import auto_ingest_alert as aia  # noqa: E402
import auto_ingest_state as ais  # noqa: E402
import find_uningested as fu  # noqa: E402  -- the drop-box contract: DROPBOX_ROOT, read_manifest

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


def report_problem(d: str, engine: str) -> str | None:
    """Why this export cannot be ingested, or None if it looks ingestable.

    Each rejection is a failed EXPORT: nothing to retry, so it must not consume the per-run limit
    every single run, forever.

      * zero-byte / stub report -- 16 of the 60 searches in the first real scan.
      * header only -- "Number of rows: 0" in the export's .params; the header alone is ~2 KB, so the
        size test never caught it (20220330_153755_Chicken_DIA..., 2026-09).
      * TRUNCATED -- the export was interrupted. Measured 2026-09-24 over all 241 FRAN_reports TSVs:
        237 end in a newline and have a non-empty .params; the other 4 end mid-line and have a
        0-byte .params, all from the 2026-08-28/29 export batch. One of them (Nuciser, 22.9 GB)
        spent 75 min per run parsing before its last, 16-of-128-field line reached COPY as a row
        of NaNs and died on NOT NULL "charge". A parquet file must end with its PAR1 magic.

    Costs two small reads per export, against a candidate list of a few hundred."""
    t = resolve_input(d, engine)
    if not t:
        return "no report file"
    try:
        if os.path.isdir(t):
            return None
        size = os.path.getsize(t)
        if size <= 1024:
            return "empty/stub report"
        with open(t, "rb") as fh:
            fh.seek(-4, os.SEEK_END)
            tail = fh.read(4)
            if t.lower().endswith(".parquet"):
                return None if tail == b"PAR1" else "truncated parquet (no PAR1 footer)"
            if not tail.endswith(b"\n"):
                return "truncated export (ends mid-line)"
            fh.seek(0)
            head = fh.read(1 << 20)
    except OSError as e:
        return f"unreadable report ({type(e).__name__})"
    nl = head.find(b"\n")
    if nl != -1 and nl + 1 >= size:
        return "header only (the export has no rows)"
    return None


def usable(d: str, engine: str) -> bool:
    """Does this export actually contain a report worth reading? See report_problem."""
    return report_problem(d, engine) is None


def pick_one(dirs: list[str], engine: str = "spectronaut") -> str | None:
    """Newest USABLE export for a search, or None if none is usable.

    Newest-overall is the wrong choice on its own: FRAN_reports keeps every attempt, and for
    searches like 20241202_133750_22Feb2024_tryingBi2GAIN (6 exports) the NEWEST is the empty one
    while an older export is fine. Filtering first rescues those instead of discarding the search."""
    good = [d for d in dirs if usable(d, engine)]
    if not good:
        return None
    return sorted(good, key=_export_ts, reverse=True)[0]


def in_dropbox(path: str) -> bool:
    return path.rstrip("/").startswith(fu.DROPBOX_ROOT.rstrip("/") + "/")


def staged_at(d: str, declared: float | None = None) -> float:
    """When a candidate arrived, in epoch seconds, for oldest-first ordering.

    Drop box: the manifest's staged_at when the skill writes one, else the mtime of
    fran_manifest.json (written once, at staging), else -- a legacy bare-symlink entry -- the
    link's own mtime. FRAN_reports: the export timestamp that leads the export directory's name
    ("20260826_212930_..."), else the directory's mtime. Unknown sorts last, so a directory that
    cannot even be stat'ed never holds the head."""
    if declared is not None:
        return declared
    if in_dropbox(d):
        for p in (os.path.join(d, fu.MANIFEST), d):
            try:
                return os.lstat(p).st_mtime
            except OSError:
                continue
        return float("inf")
    m = _TS.match(os.path.basename(d.rstrip("/")))
    if m:
        try:
            return time.mktime(time.strptime(m.group(1).ljust(15, "0"), "%Y%m%d_%H%M%S"))
        except (ValueError, OverflowError):
            pass
    try:
        return os.stat(d).st_mtime
    except OSError:
        return float("inf")


def _apply_manifest(c: dict, m: dict) -> None:
    """Take a drop-box entry's identity from its (validated) manifest.

    fran_deposit.py stages a REAL directory, incoming/<name>__<hash>/, holding fran_manifest.json
    and links to the report -- not, as when this was first written, a link to the output directory.
    realpath() of that directory is therefore the drop-box path itself, and ingesting from it would
    record the search under output_dir=/quobyte/proteomics-grp/fran/incoming/search_out__e14aac29,
    search_name "search_out__e14aac29" and an INFERRED organism. The manifest carries the true
    output_dir, search_name, organism and taxon (its suggested_ingest says exactly that): the same
    four values a registered queue row carries. The ingest reads from the real output directory, as
    the queue path does, whenever it is still there."""
    c["identity"] = m["output_dir"]
    c["identity_from"] = "manifest"
    if os.path.isdir(m["output_dir"]):
        c["dir"] = m["output_dir"]
    for src, dst in (("search_name", "search"), ("organism", "organism"), ("taxon", "taxon")):
        if m.get(src):
            c[dst] = m[src]


_FASTA_ARG = re.compile(r"--fasta[ =]+(\S+)")
NEEDS_HUMAN = ("manifest_fasta_mismatch", "manifest_organism_mismatch")


def _search_record(d: str):
    """(fasta basenames, organism) that a search's OWN records say it used; each None if unrecorded.

    Two independent sources, both written when the search RAN, not when it was staged:
      * search_provenance.json -- "fasta" (a path or a list) and, if present, "organism";
      * report.log.txt -- DIA-NN writes its command line at the top, one `--fasta <path>` per
        database (`--fasta-search` is a different flag and does not match).
    fran_deposit.json is deliberately NOT a source: the stage step writes it from the same inputs as
    the manifest, so agreeing with it proves nothing."""
    names, org = set(), None
    try:
        with open(os.path.join(d, "search_provenance.json"), encoding="utf-8") as fh:
            prov = json.load(fh)
        if isinstance(prov, dict):
            v = prov.get("fasta")
            for p in (v if isinstance(v, list) else [v]):
                if isinstance(p, str) and p.strip():
                    names.add(os.path.basename(p.strip().rstrip("/")))
            if isinstance(prov.get("organism"), str) and prov["organism"].strip():
                org = prov["organism"].strip()
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(d, "report.log.txt"), "rb") as fh:
            head = fh.read(256 * 1024).decode("utf-8", "replace")
        names |= {os.path.basename(p) for p in _FASTA_ARG.findall(head)}
    except OSError:
        pass
    return (names or None), org


def manifest_mismatch(m: dict, search_dir: str) -> str | None:
    """Why a drop-box manifest contradicts its own search, or None (agrees, or nothing to compare).

    The manifest is what the ingest trusts for organism and taxon, so a wrong one files a whole
    search under the wrong species. Found live on 2026-09-24: incoming/search_mouse_mousecont's
    manifest names human_UP000005640.fasta for a MOUSE search (DIA-NN ran
    mouse_UP000000589_mousecont.fasta) -- the old stage code took whichever FASTA sidecar sorted
    first. Compared by basename, because the same file is staged from a laptop path and searched
    from a Hive path. When the search keeps no record of its own, the manifest stands."""
    fasta_names, org = _search_record(search_dir)
    mf = m.get("fasta_path")
    if mf and fasta_names:
        mine = os.path.basename(mf.rstrip("/"))
        if mine not in fasta_names:
            return (f"manifest_fasta_mismatch: manifest={mine} "
                    f"search={','.join(sorted(fasta_names))}")
    if org and m.get("organism") and org.lower() != m["organism"].lower():
        return f"manifest_organism_mismatch: manifest={m['organism']} search={org}"
    return None


def select(candidates, skip_failed=True):
    """One candidate per search, ordered for the run: drop box first, then oldest-staged first.

    Returns (chosen, skipped). Each chosen candidate carries `attempt_key` -- the real path of the
    export it was read from -- which is what auto_ingest_state remembers outcomes under, and
    `identity` -- the output_dir corpus_ingest will write -- which is what runs lease."""
    by_search: dict[str, list[str]] = {}
    for c in candidates:
        by_search.setdefault(search_key(c["dir"]), []).append(c["dir"])
    chosen, skipped = [], []
    for name, dirs in sorted(by_search.items()):
        if skip_failed and name.lower().startswith(("fail_", "fail-")):
            skipped.append((name, "named fail*")); continue
        engine = next(c["engine"] for c in candidates if c["dir"] in dirs)
        probs = {d: report_problem(d, engine) for d in dirs}
        good = [d for d in dirs if probs[d] is None]
        if not good:
            why = "; ".join(sorted({p for p in probs.values() if p}))
            skipped.append((name, f"no usable report in {len(dirs)} export(s) — {why}"))
            continue
        best = sorted(good, key=_export_ts, reverse=True)[0]
        c = {"search": name, "engine": engine, "dir": best,
             "identity": os.path.realpath(best).rstrip("/"), "attempt_key": os.path.realpath(best),
             "dropbox": in_dropbox(best), "n_exports": len(dirs), "n_usable": len(good)}
        declared = None
        if c["dropbox"]:
            # Every skip below is "not attempted", never a failure: nothing is charged.
            m, why = fu.read_manifest(best, engine)
            if why == f"no {fu.MANIFEST}" and os.path.islink(best.rstrip("/")):
                m = None      # the ORIGINAL staging format: a bare link to the output dir, whose
                              # realpath is the identity, as it always was
            elif why:
                skipped.append((name, f"drop box: {why}"))
                continue
            # FRAN keeps QC runs out of the corpus (find_uningested.qc_reason: qc/exclude true,
            # then a QC/scratch root, then qc: false, then the name rule). The entry stays put.
            why = fu.qc_reason(m["output_dir"] if m else c["identity"],
                               m.get("search_name") if m else name,
                               m.get("qc") if m else None, m.get("exclude") if m else None)
            if why:
                skipped.append((name, f"qc: {why}"))
                continue
            if m:
                _apply_manifest(c, m)
                why = manifest_mismatch(m, c["dir"] if os.path.isdir(c["dir"]) else best)
                if why:
                    skipped.append((name, why))      # a person must repair the manifest
                    continue
                declared = m.get("staged_at")
        c["staged_at"] = staged_at(best, declared)
        chosen.append(c)
    chosen.sort(key=lambda c: (0 if c["dropbox"] else 1, c["staged_at"], c["search"]))
    return chosen, skipped


def _claim_queue(a):
    """Claim rows from delimp_ingest_queue and shape them like scan candidates.

    Returns (candidates, conn). Rows are claimed ONLY under --apply: a dry run must not take a
    lease it will never release.

    These are prepended to the scan's candidate list, which is what makes the queue authoritative
    for Hive-produced searches -- _run() takes chosen[:limit] off the front, so a registered search
    can never be starved behind the alphabetically-sorted FRAN_reports backlog. That starvation is
    why two DIA-NN searches dropped into incoming/ on 2026-08-26 were still uningested 13 days
    later.

    A queue that cannot be reached must not take the whole run down with it -- the scan half still
    works -- so failures here are reported and skipped.
    """
    if not a.apply:
        return [], None
    try:
        import fran_queue
        con = fran_queue._conn()
        rows = fran_queue.claim_batch(con, a.limit, platform.node())
    except Exception as e:  # noqa: BLE001
        print(f"  queue unavailable ({type(e).__name__}: {e}); continuing with scan only",
              flush=True)
        return [], None
    cands = [{"search": r["search_name"] or os.path.basename(r["searchdir"].rstrip("/")),
              "engine": r["engine"],
              "dir": r["searchdir"],
              "identity": r["output_dir"],
              "identity_from": "queue",
              "n_exports": 1, "n_usable": 1,
              "queue_id": r["id"],
              **({"organism": r["organism_name"]} if r.get("organism_name") else {}),
              **({"taxon": r["taxon"]} if r.get("taxon") else {}),
              # The row's XIC declaration. Without these two keys _run_xic_lane() returns early,
              # so no DIA-NN trace was ever ingested from the queue (fixed 2026-09-16).
              **({"xic_dir": r["xic_dir"]} if r.get("xic_dir") else {}),
              **({"lance_dir": r["lance_dir"]} if r.get("lance_dir") else {})}
             for r in rows]
    if cands:
        print(f"\nqueue: claimed {len(cands)} registered search(es) — these run first", flush=True)
        for c in cands:
            print(f"  Q{c['queue_id']:<5} {c['engine']:<12} {c['dir']}", flush=True)
    return cands, con


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
    ap.add_argument("--state-file", default=ais.DEFAULT_STATE_FILE,
                    help="attempt memory (JSON; env FRAN_AUTO_INGEST_STATE) [%(default)s]")
    ap.add_argument("--max-failures", type=int, default=ais.DEFAULT_MAX_FAILURES,
                    help="failed attempts before a candidate is quarantined [%(default)s]")
    ap.add_argument("--backoff-hours", default=",".join(f"{h:g}" for h in ais.DEFAULT_BACKOFF_HOURS),
                    help="wait after the 1st, 2nd, ... failure, comma-separated [%(default)s]")
    ap.add_argument("--stuck-runs", type=int, default=ais.DEFAULT_STUCK_RUNS,
                    help="consecutive no-progress runs (with work queued) before Slack is told "
                         "[%(default)s]")
    ap.add_argument("--no-alert", action="store_true", help="never post to Slack")
    ap.add_argument("--list-quarantine", action="store_true",
                    help="list quarantined candidates and exit (no scan, no database)")
    ap.add_argument("--clear", metavar="KEY",
                    help="forget one candidate (exact key, or a unique part of it) so the next run "
                         "may pick it; exit")
    a = ap.parse_args()
    try:
        backoff = tuple(float(x) for x in a.backoff_hours.split(",") if x.strip())
    except ValueError:
        ap.error("--backoff-hours takes numbers, e.g. 4,24,72")
    store = ais.AttemptStore(a.state_file, backoff_hours=backoff, max_failures=a.max_failures)

    if a.list_quarantine:
        return _list_quarantine(store)
    if a.clear:
        key, hits = store.clear(a.clear)
        if key:
            print(f"cleared: {key}\n  (eligible again on the next run)")
            return 0
        print(f"nothing cleared: {len(hits)} record(s) match {a.clear!r}"
              + ("; give the exact key" if hits else ""))
        for k in hits[:20]:
            print(f"  {k}")
        return 1

    print(f"===== auto_ingest {time.strftime('%F %T')} on {platform.node()} =====", flush=True)
    a.owner = f"{platform.node()}:{os.getpid()}"

    if a.direct:
        jobs = json.load(open(a.direct))
        chosen = [{"search": j["name"], "engine": j.get("engine", "spectronaut"),
                   "dir": os.path.dirname(j["report"]), "report": j["report"],
                   "identity": j["identity"], "identity_from": "direct",
                   "n_exports": 1, "n_usable": 1,
                   **({"organism": j["organism"]} if j.get("organism") else {})}
                  for j in jobs]
        print(f"direct mode: {len(chosen)} job(s) from {a.direct}", flush=True)
        # --direct never uses the queue or the attempt memory, but it IS re-checked against the
        # corpus before each ingest (see _ingest_one), so it needs a connection to ask with.
        return _run(a, chosen, [], _check_conn() if a.apply else None)

    if a.apply:
        # A run that never reaches the end -- killed at the 8 h SLURM wall -- would otherwise never
        # be counted, however many times it happened.
        for o, started in _mem(store, "begin_run", a.owner, default=[]) or []:
            print(f"previous run {o} (started {started}) never finished -- killed at the SLURM "
                  f"wall? Counted as a run with no progress.", flush=True)
    try:
        return _scan_and_run(a, store)
    except Exception as e:  # noqa: BLE001 -- recorded, then re-raised
        # A crash is the loudest way of ingesting nothing: the 09-23 partial deploy (an import or
        # attribute error), or a bug in select/_run. It must count toward the stuck alert like any
        # other run that made no progress, not fail silently every four hours.
        traceback.print_exc()
        print(f"\n===== CRASHED: {type(e).__name__}: {e} — {time.strftime('%F %T')} =====",
              flush=True)
        if a.apply:
            _finish_run(a, store, 0, None,
                        {"ok": 0, "dup": 0, "fail": 0, "systemic": 0, "crashed": 1},
                        f"crashed: {type(e).__name__}: {e}")
        raise


def _scan_and_run(a, store):
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
            print(f"SCAN FAILED rc={r.returncode}\n{r.stderr[-2000:]}")
            # A scan that cannot run ingests nothing, every run, as surely as a wedged head does.
            # The queue size is unknown, which the stuck detector treats as "work exists".
            if a.apply:
                _finish_run(a, store, 0, None, {"ok": 0, "dup": 0, "fail": 0, "systemic": 0},
                            f"the candidate scan failed (rc={r.returncode})")
            return 1
        candidates = json.load(open(out))

    chosen, skipped = select(candidates, skip_failed=not a.include_failed)
    print(f"\n{len(candidates)} dirs -> {len(chosen)} distinct searches "
          f"({len(skipped)} skipped)", flush=True)
    for name, why in skipped:
        # Drop-box skips read "skipped (qc: ...)" / "skipped (manifest_fasta_mismatch: ...)" -- the
        # form the skill side greps for.
        form = "skipped ({})" if why.startswith(("qc: ",) + NEEDS_HUMAN) else "({})"
        print(f"  SKIP {name[:60]}  " + form.format(why), flush=True)

    eligible, held = _mem(store, "gate", chosen, default=(chosen, []))
    _print_held(held)
    queued, qcon = _claim_queue(a)
    return _run(a, queued + eligible, skipped, qcon, store=store, held=held)


def _list_quarantine(store) -> int:
    data = store.load()
    q = store.by_status(ais.QUARANTINED, data)
    counts = collections.Counter(r.get("status") or "leased" for r in data["candidates"].values())
    print(f"attempt memory: {store.path}")
    print("  " + (", ".join(f"{n} {st}" for st, n in sorted(counts.items())) or "empty"))
    print(f"\n{len(q)} quarantined (a human decides; `--clear <key>` makes one eligible again):")
    for key, r in q:
        last = [ln for ln in (r.get("last_error_tail") or "").splitlines() if ln.strip()]
        print(f"\n  {key}")
        print(f"    search {r.get('search', '?')}  engine {r.get('engine', '?')}  "
              f"attempts {r.get('attempts')}  quarantined {r.get('quarantined_at')}")
        print(f"    last outcome: {r.get('last_outcome')}")
        for ln in last[-3:]:
            print(f"    | {ln[:160]}")
    return 0


def _print_held(held) -> None:
    """What the attempt memory kept out of this run, and why. Resolved ones are only counted."""
    if not held:
        return
    by = collections.Counter(st for _, st, _ in held)
    print(f"\nattempt memory: holding back {len(held)} candidate(s) — "
          + ", ".join(f"{n} {st}" for st, n in sorted(by.items())), flush=True)
    for c, st, rec in held:
        if st == ais.DONE:
            continue
        rec = rec or {}
        when = (f"next try after {rec.get('next_eligible')}"
                if st in (ais.BACKOFF, ais.DEFERRED) else
                "needs --clear" if st == ais.QUARANTINED else "")
        print(f"  HOLD {st:<11} {c['search'][:56]}  (attempts {rec.get('attempts', 0)}; {when})",
              flush=True)


DEFAULT_XIC_LANCE_DIR = os.environ.get(
    "FRAN_XIC_LANCE_DIR", "/quobyte/proteomics-grp/brett/glendon/xic_lance")


def _run_xic_lane(a, c, qcon):
    """Run the observed-chromatogram lane for a queue row that asked for it.

    Opt-in per row: only fires when the registration set xic_dir. The unattended cron has always
    withheld the lanes because they are GB-scale (PROT_0793 alone is 15 GB of *.xic.parquet), and
    that is a storage decision -- so the producer who wrote the traces declares them, rather than a
    scanner guessing.

    NEVER fails the queue row. Precursors are committed by the time this runs; sending the row back
    to 'queued' over a lane error would re-ingest them. The outcome is recorded in xic_status /
    xic_error instead, on its own axis.
    """
    xic_dir = c.get("xic_dir")
    if not xic_dir or qcon is None or not c.get("queue_id"):
        return
    import fran_queue
    out_dir = c.get("lance_dir") or DEFAULT_XIC_LANCE_DIR
    try:
        cur = qcon.cursor()
        cur.execute("SELECT id FROM delimp_searches WHERE output_dir = %s",
                    (c.get("identity") or c["dir"],))
        row = cur.fetchone()
        # psycopg2 does not autocommit, so that SELECT opened a transaction. Close it now: left
        # open across the lane subprocess (up to --timeout) it holds a lock on delimp_searches
        # that any ALTER would queue behind, and every page read behind the ALTER.
        qcon.commit()
        if not row:
            fran_queue.mark_xic(qcon, c["queue_id"], "failed",
                                "no delimp_searches row for this output_dir")
            print("      xic lane: SKIPPED (no corpus row to attach traces to)", flush=True)
            return
        sid = str(row[0])
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(c["search"]))[:80]
        out = os.path.join(out_dir, f"{safe}__{sid[:8]}.diann.xic.lance")
        cmd = [a.python, os.path.join(HERE, "diann_xic_to_lance.py"),
               "--dir", c["dir"], "--xic-dir", xic_dir, "--out", out,
               "--search-id", sid, "--search-name", str(c["search"]), "--apply"]
        print(f"      xic lane: {xic_dir} -> {out}", flush=True)
        t0 = time.time()
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=a.timeout)
        el = time.time() - t0
        if r.returncode == 0:
            fran_queue.mark_xic(qcon, c["queue_id"], "done")
            print(f"      xic lane: OK in {el:.0f}s", flush=True)
            for line in (r.stdout or "").splitlines():
                if line.startswith("xic layout:") or line.startswith("wrote"):
                    print(f"      {line}", flush=True)
        else:
            tail = ((r.stdout or "") + "\n" + (r.stderr or ""))[-800:]
            fran_queue.mark_xic(qcon, c["queue_id"], "failed", tail)
            print(f"      xic lane: FAILED rc={r.returncode} in {el:.0f}s "
                  f"(precursors are already ingested; row stays done)", flush=True)
    except Exception as e:  # noqa: BLE001
        try:
            fran_queue.mark_xic(qcon, c["queue_id"], "failed", str(e))
        except Exception:  # noqa: BLE001
            pass
        print(f"      xic lane: FAILED ({type(e).__name__}: {e})", flush=True)


def _mem(store, method, *args, default=None, **kw):
    """Call the attempt memory; a bug or IO failure there costs a warning, never the ingest run."""
    if store is None:
        return default
    try:
        return getattr(store, method)(*args, **kw)
    except Exception as e:  # noqa: BLE001
        print(f"      WARNING (attempt memory): {method} failed — {type(e).__name__}: {e}",
              flush=True)
        return default


def _ident(c) -> str:
    """The output_dir a candidate would be ingested as -- what runs lease and de-duplicate on."""
    return str(c.get("identity") or c["dir"]).rstrip("/")


def _check_conn():
    """A connection to re-check the corpus with (--direct has no queue connection), or None."""
    try:
        import fran_queue
        return fran_queue._conn()
    except Exception as e:  # noqa: BLE001
        print(f"  no database connection for the pre-ingest corpus check ({type(e).__name__}: {e})",
              flush=True)
        return None


def _corpus_has(qcon, output_dir):
    """(search_id or None, None), or (None, reason) when the corpus cannot be asked.

    Uses fran_queue._already_ingested -- the one definition of "this output_dir is a corpus row",
    the same check `fran_queue.py add` makes -- and closes the read transaction at once for the
    reason _run_xic_lane gives."""
    if qcon is None:
        return None, "no database connection to re-check the corpus before ingesting"
    try:
        import fran_queue
        sid = fran_queue._already_ingested(qcon.cursor(), output_dir)
        qcon.commit()
        return sid, None
    except Exception as e:  # noqa: BLE001
        try:
            qcon.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None, f"could not re-check the corpus before ingesting ({type(e).__name__})"


def _ingest_one(a, c, qcon) -> dict:
    """Re-check the corpus, then corpus_ingest one candidate. Returns {"outcome": ...}:
    already | duplicate | ok | fail | timeout | systemic (with "scope" and "reason").

    THE CORPUS RE-CHECK RUNS IMMEDIATELY BEFORE EVERY INGEST -- scan, drop-box, queue and --direct
    alike -- and under the run's lease on this output_dir. For an output_dir already in the corpus
    corpus_ingest does not refuse: it DELETES and re-inserts the whole search. The scan filters
    known paths, but the corpus can change between the scan and this moment (another run, a
    registered row for the same output_dir, a hand ingest), so the scan's answer is not enough.
    This also means a `fran_queue.py add --force` registration of an existing output_dir is marked
    done, not re-ingested: a deliberate re-ingest is run by hand."""
    od = c.get("identity") or c["dir"]
    sid, why = _corpus_has(qcon, od)
    if why:
        return {"outcome": "systemic", "scope": "global", "reason": why, "tail": "", "el": 0,
                "rc": None}
    if sid:
        return {"outcome": "already", "sid": sid}
    target = c.get("report") or resolve_input(c["dir"], c["engine"])
    if not target:
        return {"outcome": "fail", "rc": None, "el": 0,
                "tail": "no report file found in the directory"}
    if target != c["dir"]:
        print(f"      report: {os.path.basename(target)}", flush=True)
    cmd = [a.python, os.path.join(HERE, "corpus_ingest.py"), target,
           "--engine", c["engine"], "--name", c["search"],
           "--output-dir", od, "--bulk-copy"]
    # A producer that resolved the organism (the drop-box manifest records it, and the queue
    # stores it) knows better than corpus_ingest's own inference. Both this and --direct set
    # these keys; until 2026-09-08 the command never passed them and they were dead fields.
    if c.get("organism"):
        cmd += ["--organism-name", str(c["organism"])]
    if c.get("taxon"):
        cmd += ["--taxon", str(c["taxon"])]
    # DIA-NN's chromatograms are *.xic.parquet and go through diann_xic_to_lance AFTER the
    # ingest (see _run_xic_lane). Spectronaut XICs are NOT taken from the queue: corpus_ingest's
    # --lance-dir means the OBSERVED-SPECTRUM lane, so passing a row's lance_dir there wrote an
    # unrequested spectrum lane into the XIC directory, inside this subprocess -- where a kill
    # after COMMIT sends the row back to 'queued' and re-ingests the whole search.
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=a.timeout)
    except subprocess.TimeoutExpired:
        return {"outcome": "timeout", "rc": None, "el": a.timeout,
                "tail": f"timeout after {a.timeout}s"}
    el = time.time() - t0
    tail = ((r.stdout or "") + "\n--- stderr ---\n" + (r.stderr or ""))[-2500:]
    # ORDER MATTERS. The duplicate guard `return`s rather than sys.exit(1), so a refused
    # ingest exits 0 -- checking returncode first would log it as "OK" and the run summary would
    # claim it ingested searches it did not touch. Match the guard's own message, and match it
    # narrowly: the substring "duplicate" alone also appears in corpus_ingest's --allow-duplicate
    # help text.
    blob = (r.stdout or "") + (r.stderr or "")
    if "DUPLICATE of an already-ingested search" in blob:
        return {"outcome": "duplicate", "el": el, "blob": blob}
    if r.returncode == 0:
        return {"outcome": "ok", "el": el}
    scope, reason = ais.classify_failure(r.stdout, r.stderr)
    return {"outcome": "systemic" if scope else "fail", "scope": scope, "reason": reason,
            "rc": r.returncode, "el": el, "tail": tail}


def _run(a, chosen, skipped, qcon=None, store=None, held=()):
    ok = dup = fail = systemic = 0
    stopped = None                      # why the run stopped early, if it did
    blocked: dict[str, str] = {}        # engine -> why its remaining candidates are held back
    progressed: set[str] = set()        # engines with an ingest or a resolved duplicate this run
    handled: set[str] = set()           # output_dirs already dealt with in this run
    owner = getattr(a, "owner", None) or f"{platform.node()}:{os.getpid()}"

    def _mark(c, outcome, err=None):
        """Write a queue row's terminal state back. No-op for scan candidates.

        A duplicate counts as DONE, not as a failure: the guard refusing the write means the search
        is already in the corpus, so the registration is satisfied and must not burn a retry.
        """
        if qcon is None or not c.get("queue_id"):
            return
        try:
            import fran_queue
            if outcome in ("ok", "duplicate"):
                fran_queue.mark_done(qcon, c["queue_id"])
            else:
                st = fran_queue.mark_failed(qcon, c["queue_id"], err or outcome)
                print(f"      queue Q{c['queue_id']} -> {st}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"      WARNING: could not update queue Q{c.get('queue_id')}: {e}", flush=True)

    def _remember(c, outcome, tail="", reason=None):
        """Record a SCAN candidate's outcome in the attempt memory. Queue rows and --direct jobs
        have no attempt_key: the queue keeps its own attempts, and a direct job is an operator's."""
        key = c.get("attempt_key") if store is not None else None
        if not key:
            return
        rec = _mem(store, "record", key, outcome, tail, reason=reason,
                   meta={"search": c["search"], "engine": c["engine"], "dir": c["dir"]})
        if rec and outcome not in ("ok", "duplicate"):
            st = rec.get("status")
            nxt = (f" until {rec.get('next_eligible')}"
                   if st in (ais.BACKOFF, ais.DEFERRED) else "")
            print(f"      attempt memory: {st}{nxt} (attempts {rec.get('attempts', 0)}"
                  f"/{store.max_failures})", flush=True)

    def _resolved(c):
        progressed.add(c["engine"])
        _mark(c, "duplicate")
        _remember(c, "duplicate")

    todo = chosen[:a.limit]
    if len(chosen) > a.limit:
        print(f"\nlimit={a.limit}: ingesting {len(todo)} now, {len(chosen)-a.limit} left for the "
              f"next run", flush=True)

    i = 0
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
        if c.get("dropbox"):
            print(f"      staged as {c['attempt_key']}", flush=True)
        if not a.apply:
            print("      DRY RUN — not ingesting", flush=True); continue
        ident = _ident(c)
        if ident in handled:
            # e.g. a queue row and a staged drop-box entry for the same search, in one run
            print("      SKIPPED — this output_dir was already handled earlier in this run",
                  flush=True)
            continue
        if c["engine"] in blocked:
            print(f"      SKIPPED — {c['engine']} candidates are held back this run: "
                  f"{blocked[c['engine']]} (not charged)", flush=True)
            continue
        if store is not None:
            # A lease on the OUTPUT_DIR, so no two runs corpus_ingest one search at once, whatever
            # kind of candidate brought it. Memory unavailable -> proceed: bookkeeping never blocks.
            got, why = _mem(store, "claim", ident, owner, a.timeout + 900, c.get("attempt_key"),
                            default=(True, None))
            if not got:
                print(f"      SKIPPED — {why}", flush=True)
                continue
        handled.add(ident)
        try:
            res = _ingest_one(a, c, qcon)
        finally:
            if store is not None:
                _mem(store, "release", ident, owner)
        out, el = res["outcome"], res.get("el", 0)

        if out == "already":
            dup += 1
            print(f"      ALREADY IN THE CORPUS as search_id={res['sid']} under this output_dir — "
                  f"not re-ingesting (corpus_ingest would delete and re-insert it)", flush=True)
            _resolved(c)
        elif out == "duplicate":
            dup += 1
            print(f"      SKIPPED-DUPLICATE in {el:.0f}s (guard refused — not a failure)", flush=True)
            for line in res["blob"].splitlines():
                if line.strip().startswith("exists:"):
                    print(f"      {line.strip()}", flush=True)
            _resolved(c)
        elif out == "ok":
            ok += 1
            progressed.add(c["engine"])
            print(f"      OK in {el:.0f}s", flush=True)
            _mark(c, "ok")
            _remember(c, "ok")
            if c["engine"] == "diann":
                _run_xic_lane(a, c, qcon)
            elif c.get("xic_dir") and qcon is not None and c.get("queue_id"):
                try:
                    import fran_queue
                    fran_queue.mark_xic(qcon, c["queue_id"], "unsupported",
                                        f"auto_ingest writes XIC lanes for diann rows only, not "
                                        f"{c['engine']}")
                except Exception as e:  # noqa: BLE001
                    print(f"      WARNING: could not record xic status: {e}", flush=True)
                print(f"      xic lane: UNSUPPORTED for {c['engine']} queue rows "
                      f"(precursors ingested; xic_dir ignored)", flush=True)
        elif out == "timeout":
            fail += 1
            print(f"      TIMEOUT after {a.timeout}s", flush=True)
            _mark(c, "fail", res["tail"])
            _remember(c, "timeout", res["tail"])
        elif out == "fail" and res.get("rc") is None:
            fail += 1
            print(f"      FAILED: {res['tail']}", flush=True)
            _mark(c, "fail", res["tail"])
            _remember(c, "fail", res["tail"])
        else:
            tail = res.get("tail", "")
            if out == "systemic":
                systemic += 1
                bar = "      " + "#" * 90
                rc = f"rc={res['rc']} after {el:.0f}s" if res.get("rc") is not None else "before ingest"
                reach = ("stopping the run" if res["scope"] == "global" else
                         f"holding back the other {c['engine']} candidates; other engines continue")
                print(f"{bar}\n      ### SYSTEMIC FAILURE {rc}: {res['reason']}\n"
                      f"      ### NOT charged to this candidate — {reach}.\n{bar}", flush=True)
            else:
                fail += 1
                print(f"      FAILED rc={res['rc']} in {el:.0f}s", flush=True)
            if tail.strip():
                print("      --- last output ---", flush=True)
                for line in [x for x in tail.splitlines() if x.strip()][-14:]:
                    print("      | " + line, flush=True)
            if out != "systemic":
                _mark(c, "fail", f"rc={res['rc']}: " + tail[-800:])
                _remember(c, "fail", tail)
                continue
            _remember(c, "systemic", tail, reason=res["reason"])
            if qcon is not None and c.get("queue_id"):
                # Deliberately NOT mark_failed: that burns one of the row's MAX_ATTEMPTS on a fault
                # that is not the row's. Left 'claimed', the queue's own lease returns it to
                # 'queued' after STALE_CLAIM_H, uncharged -- the same deferral a scan candidate
                # gets. Rows claimed later in a stopped batch are left the same way.
                print(f"      queue Q{c['queue_id']} left claimed; the claim lapses back to "
                      f"'queued' by itself, no attempt charged", flush=True)
            if res["scope"] == "engine":
                blocked[c["engine"]] = res["reason"]
                continue
            stopped = res["reason"]
            print("      stopping this run: the remaining candidates would fail the same way",
                  flush=True)
            break

    not_reached = len(todo) - i if (stopped and todo) else 0
    left = len(chosen) - len(todo) + not_reached
    n_q = n_b = 0
    if store is not None:
        # Counted over EVERY scan candidate this run saw, after its outcomes: the backlog's state.
        keys = [c["attempt_key"] for c in chosen if c.get("attempt_key")]
        keys += [c["attempt_key"] for c, _, _ in held if c.get("attempt_key")]
        data = _mem(store, "load")
        sts = collections.Counter(_mem(store, "status", k, data=data, default=("?", None))[0]
                                  for k in keys) if data is not None else collections.Counter()
        n_q, n_b = sts[ais.QUARANTINED], sts[ais.BACKOFF] + sts[ais.DEFERRED]
    notes = ""
    if stopped:
        notes += f", {systemic} stopped by a systemic error ({stopped})"
    elif systemic:
        notes += f", {systemic} systemic"
    if blocked:
        notes += ", held back: " + "; ".join(f"{e} ({w})" for e, w in sorted(blocked.items()))
    print(f"\n===== done: {ok} ingested, {dup} duplicate-skipped, {fail} failed, "
          f"{left} still queued, {n_q} quarantined, {n_b} backed-off{notes} — "
          f"{time.strftime('%F %T')} =====", flush=True)
    if a.apply and store is not None:
        needs_human = {n: w for n, w in skipped if w.startswith(NEEDS_HUMAN)}
        _finish_run(a, store, ok + dup, len(chosen),
                    {"ok": ok, "dup": dup, "fail": fail, "systemic": systemic}, stopped,
                    n_quarantined=n_q, engines_progressed=progressed, blocked_engines=blocked,
                    needs_human=needs_human)
    return 0


def _finish_run(a, store, progress, n_eligible, summary, stopped, n_quarantined=0,
                engines_progressed=(), blocked_engines=None, needs_human=None):
    """Count this run, and tell Slack -- once per episode -- whatever needs a person.

    See AttemptStore.record_run: the pipeline stuck (no progress with work queued, a failed scan, a
    crash, a killed run), one engine blocked while others progress, or a drop-box entry whose
    manifest contradicts its own search. An empty queue is silent."""
    stuck_runs = getattr(a, "stuck_runs", ais.DEFAULT_STUCK_RUNS)
    res = _mem(store, "record_run", progress, n_eligible, summary, stuck_runs=stuck_runs,
               owner=getattr(a, "owner", None), engines_progressed=sorted(engines_progressed),
               blocked_engines=blocked_engines or {}, needs_human=needs_human or {})
    if not res:
        return
    due = res.get("due") or []
    if not due:
        if res["consecutive_zero"] and n_eligible != 0:
            print(f"stuck-watch: {res['consecutive_zero']} consecutive run(s) with work and no "
                  f"progress (Slack is told at {stuck_runs})", flush=True)
        return
    keys = [d["key"] for d in due]
    if getattr(a, "no_alert", False):
        print(f"stuck-watch: ALERT DUE ({', '.join(keys)}) — not sent (--no-alert)", flush=True)
        return
    job = os.environ.get("SLURM_JOB_ID")
    text = aia.compose(platform.node(), due, res, n_eligible, stopped, n_quarantined,
                       (f"/quobyte/proteomics-grp/de-limp/fran_refresh/logs/auto_ingest_{job}.out"
                        if job else None))
    sent, note = aia.post(text)
    print(f"stuck-watch: Slack alert ({', '.join(keys)}) "
          f"{'SENT' if sent else 'NOT sent: ' + note}", flush=True)
    if sent:
        _mem(store, "mark_alert_sent", keys=keys)


if __name__ == "__main__":
    sys.exit(main())
