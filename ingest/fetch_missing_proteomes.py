"""fetch_missing_proteomes.py -- prepare the proteomes the corpus-wide run is blocked on.

204 of the 1,337 sets cannot be searched because no prepared FASTA exists for their organism: 76
taxa, headed by Saccharomyces cerevisiae (27 sets) and Vibrio cholerae (22). The drip's fasta_for()
deliberately NEVER fetches -- "a drip firing is not the place to discover the internet is down" --
so this is the deliberate, separate step it refers to.

IT DOES NOT BUILD LIBRARIES, and that is a decision rather than an omission. A predicted library is
keyed on (FASTA md5, DIA-NN version, the step-1 flags), and the m/z range varies by cohort, so 76
proteomes imply ~106 libraries. The drip already builds and caches those on demand with 12.2x
reuse; pre-building would move the same ~300 CPU-hours earlier and risk building libraries for sets
that later skip for some other reason.

Nothing here is reimplemented: fetch_fasta.py does the actual preparation (one protein per gene,
the universal contaminant set appended and prefixed so DIA-NN excludes it from quant, md5, dated
snapshot, meta.json). This only decides WHICH taxa and WHERE, so a prepared proteome is
indistinguishable from the 25 already on disk.

    python3 fetch_missing_proteomes.py                  # list what is missing, fetch nothing
    python3 fetch_missing_proteomes.py --fetch
    python3 fetch_missing_proteomes.py --fetch --limit 10
"""
import argparse
import csv
import collections
import functools
import json
import os
import subprocess
import sys
import time

print = functools.partial(print, flush=True)                      # noqa: A001

ROOT = "/nfs/lssc0/flinders/proteomics/Data/FRAN_diann"
FASTA_ROOT = os.path.join(ROOT, "_fasta")
WORKLIST = os.path.join(ROOT, "CORPUS_WORKLIST.tsv")
STATE = os.path.join(ROOT, "corpus_2026", "state.json")
STACK = "/quobyte/proteomics-grp/fran/pipeline-skill/stack-libcache/scripts"
PY = "/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python"


def prepared_taxa():
    """taxon -> the newest version dir that actually holds a search.fasta."""
    out = {}
    for d in sorted(os.listdir(FASTA_ROOT)):
        if "_" not in d:
            continue
        taxon = d.rsplit("_", 1)[-1]
        for v in sorted(os.listdir(os.path.join(FASTA_ROOT, d)), reverse=True):
            if os.path.exists(os.path.join(FASTA_ROOT, d, v, "search.fasta")):
                out[taxon] = os.path.join(FASTA_ROOT, d, v)
                break
    return out


def missing():
    """(taxon, organism) -> number of sets blocked on it, for sets not already done."""
    have = prepared_taxa()
    try:
        done = {k for k, v in json.load(open(STATE)).items()
                if v.get("status") in ("complete", "submitted")}
    except (OSError, ValueError):
        done = set()
    need = collections.Counter()
    notax = 0
    with open(WORKLIST) as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            if r["setkey"] in done:
                continue
            t = str(r.get("taxon") or "").strip()
            if not t:
                notax += 1                       # no organism recorded: a fetch cannot fix it
                continue
            if t not in have:
                need[(t, (r.get("organism") or "?").strip())] += 1
    return need, notax, have


def resolve_proteome(taxon):
    """(proteome_id, note) for a taxon, or (None, why). fetch_fasta's `fetch` takes a proteome id,
    not a taxid -- `resolve` is the step that turns one into the other."""
    r = subprocess.run([PY, os.path.join(STACK, "fetch_fasta.py"), "resolve",
                        "--taxid", str(taxon)], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        return None, "resolve failed: " + (r.stderr or "")[-140:].replace("\n", " ")
    try:
        cands = (json.loads(r.stdout) or {}).get("candidates") or []
    except ValueError:
        return None, "resolve returned no JSON"
    if not cands:
        return None, "no proteome candidate for taxid %s" % taxon
    # Reference proteomes first, then the largest. UniProt often returns several strains; the
    # reference one is what a search should use unless somebody deliberately chose otherwise.
    best = sorted(cands, key=lambda c: (not c.get("is_reference"),
                                        -int(c.get("protein_count") or 0)))[0]
    return best.get("proteome_id"), "%s %s (%s proteins%s)" % (
        best.get("proteome_id"), (best.get("organism") or "?")[:40],
        best.get("protein_count"), ", reference" if best.get("is_reference") else "")


def fetch_one(taxon, organism, stamp):
    """One prepared proteome, via fetch_fasta.py. Returns (ok, note)."""
    up, note = resolve_proteome(taxon)
    if not up:
        return False, note
    stage = os.path.join(FASTA_ROOT, ".staging_%s_%s" % (taxon, os.getpid()))
    os.makedirs(stage, exist_ok=True)
    out = os.path.join(stage, "search.fasta")
    r = subprocess.run(
        [PY, os.path.join(STACK, "fetch_fasta.py"), "fetch", "--proteome", up,
         "--content", "one_per_gene", "--contaminants", "universal", "--add-contaminants",
         "--out", out],
        capture_output=True, text=True, timeout=900)
    if r.returncode != 0 or not os.path.exists(out):
        return False, "%s | %s" % (note, (r.stderr or r.stdout or "")[-160:].replace("\n", " "))
    # NAME THE DIRECTORY BY THE WORKLIST'S TAXON, not the proteome's own. They differ whenever
    # UniProt's reference is a strain: taxid 4932 (S. cerevisiae) resolves to UP000002311, whose
    # taxid is 559292 (strain S288c). The drip's fasta_for() scans for a directory ending
    # "_<taxon>" using the taxon ON THE SET, so naming it 559292 would prepare a proteome that
    # nothing could ever find -- a silent no-op that still looks like a successful fetch.
    dest = os.path.join(FASTA_ROOT, "%s_%s" % (up, taxon), stamp)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest):
        return False, "destination already exists: %s" % dest
    os.rename(stage, dest)
    return True, dest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="actually fetch (otherwise just report)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N taxa")
    a = ap.parse_args()

    need, notax, have = missing()
    blocked = sum(need.values())
    print(f"prepared taxa on disk        : {len(have)}")
    print(f"sets blocked on a missing one: {blocked} across {len(need)} taxa")
    print(f"sets with NO taxon recorded  : {notax}  (a fetch cannot fix these)")
    if not need:
        print("nothing to fetch")
        return 0
    print()
    for (t, o), n in need.most_common(15):
        print("  %-9s %-40s %3d sets" % (t, o[:40], n))
    if not a.fetch:
        print(f"\n(dry run — {len(need)} taxa would be fetched; pass --fetch)")
        return 0

    stamp = time.strftime("%Y_%m")
    ok = fail = 0
    # Most-blocking first, so an interrupted run has still unblocked the most sets.
    for (t, o), n in need.most_common(a.limit or None):
        print(f"  fetching {t:<9} {o[:38]:<40} ({n} sets)…")
        good, note = fetch_one(t, o, stamp)
        if good:
            ok += 1
            print(f"      -> {note}")
        else:
            fail += 1
            print(f"      FAILED: {note}")
    print(f"\nprepared {ok}, failed {fail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
