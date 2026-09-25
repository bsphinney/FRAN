"""Compare the repo's ingest/ against a deployment target and report what is MISSING, not just
what DIFFERS. Both, always, in that order.

THE FAILURE THIS EXISTS FOR (2026-09-23). A sync copied the 15 files that DIFFERED between the
repo and Hive and missed the 5 that were ABSENT there. Nothing complained: the differing files
were now identical, the manifest gate compares md5s of files it can find, and a file that does
not exist has no md5 to disagree with. The ingest then died on every one of 53 searches with
`ModuleNotFoundError` -- raw_metadata.py imported tdf_safe, which had never been copied. A whole
run, lost to a diff that only looked at the intersection.

`diff -r` would have shown it. `md5sum`-per-file, the natural way to write a sync check, does not,
because you iterate the files you have on the far side. The asymmetry is the bug: the authoritative
set is the REPO's, and the question is "what does the target lack", which is a set difference in
one direction only.

IMPORT CLOSURE. Reporting all 87 files equally would bury the one that matters. corpus_ingest.py
imports a specific set of siblings, several of them lazily inside functions (`import versions`,
`from provenance import ...`), so a missing module surfaces hours into a run rather than at start-up
-- which is exactly how tdf_safe got through. This walks the transitive closure from the entry
points and flags a gap in it as FATAL, separately from the rest.

USAGE -- two sides, because the repo is on the laptop and the targets are on Hive:

    # on the laptop, in the repo
    python3 ingest/audit_deploy_sync.py --emit /tmp/ingest_manifest.json

    # on Hive, after scp'ing that file over
    python3 audit_deploy_sync.py --check /tmp/ingest_manifest.json \\
        --target /quobyte/proteomics-grp/brett/glendon/fran_ingest
    python3 audit_deploy_sync.py --check /tmp/ingest_manifest.json \\
        --target /nfs/lssc0/flinders/proteomics/Data/FRAN_SNE_export

Exit 1 if anything in the import closure is missing or differs.
"""
from __future__ import annotations
import argparse
import ast
import re
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# What actually gets run on a deployment target. The closure is walked from these.
# An entry point is a script that is RUN ON ITS OWN and can write to the corpus. Everything
# reachable from one -- by import or by subprocess -- must be deployed for that path to work.
# sne_export.py added 2026-09-24: it has its own CLI and launches corpus_ingest.py and
# sne_xic_ingest.py by filename, so without it those two sat outside the closure and the audit
# called them "not fatal to an ingest". Deliberately NOT every script with a __main__ -- the
# backfills and audits are run by hand by someone who will see the traceback, and adding them
# would grow the closure to most of the directory and make the distinction meaningless.
ENTRY_POINTS = ("corpus_ingest.py", "auto_ingest.py", "spectronaut_to_corpus.py",
                "radiant_to_corpus.py", "publish_manifest.py", "versions.py",
                "sne_export.py")


def md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def local_imports(path: str, known: set[str]) -> set[str]:
    """Sibling modules this file imports, including imports nested inside functions.

    ast.walk, not a top-level scan: corpus_ingest.py does `import versions as _V` inside
    _stale_ingest_files() and `from provenance import record_provenance` inside ingest(). Those
    are the dangerous ones -- they raise hours in, not at import time."""
    out = set()
    try:
        tree = ast.parse(open(path, encoding="utf-8", errors="replace").read())
    except SyntaxError:
        return out
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for al in n.names:
                base = al.name.split(".")[0]
                if base + ".py" in known:
                    out.add(base + ".py")
        elif isinstance(n, ast.ImportFrom):
            if n.level == 0 and n.module:
                base = n.module.split(".")[0]
                if base + ".py" in known:
                    out.add(base + ".py")
    return out


def subprocess_targets(path: str, known: set[str]) -> set[str]:
    """Sibling scripts this file RUNS as a subprocess, named as a string literal.

    Imports are not the only way one ingest file depends on another. auto_ingest.py launches
    find_uningested.py, diann_xic_to_lance.py and corpus_ingest.py with
    `[python, os.path.join(HERE, "<name>.py"), ...]`, and sne_export.py launches corpus_ingest.py
    and sne_xic_ingest.py the same way. An AST import walk cannot see any of them, so before this
    existed the audit reported five reachable files as "missing but NOT in the closure -- not fatal
    to an ingest". They are exactly fatal: the subprocess dies with "can't open file", hours in,
    after the parent has already started work.

    That is the same false reassurance this tool was written to stop -- a check that looks only
    where it already knows to look. Found 2026-09-24 by the session reviewing the auto-ingest
    starvation fix, which hit it on diann_xic_to_lance.py -> ingest_perrun_xic.py.

    Matching any "<name>.py" literal is deliberately broad: a filename in a string is evidence of
    a dependency whether it is exec'd, passed as an argument or logged, and over-including a file
    costs one unnecessary copy while under-including one costs a run.
    """
    out = set()
    try:
        src = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return out
    for m in re.finditer(r'["\']([A-Za-z_][A-Za-z0-9_]*\.py)["\']', src):
        if m.group(1) in known:
            out.add(m.group(1))
    return out


def closure(d: str, files: set[str]) -> set[str]:
    """Everything reachable from an entry point, by import OR by subprocess."""
    seen, stack = set(), [e for e in ENTRY_POINTS if e in files]
    while stack:
        f = stack.pop()
        if f in seen:
            continue
        seen.add(f)
        fp = os.path.join(d, f)
        stack.extend((local_imports(fp, files) | subprocess_targets(fp, files)) - seen)
    return seen


def emit(out_path: str) -> int:
    files = {f for f in os.listdir(HERE) if f.endswith(".py")}
    cl = closure(HERE, files)
    man = {"files": {f: md5(os.path.join(HERE, f)) for f in sorted(files)},
           "closure": sorted(cl), "entry_points": [e for e in ENTRY_POINTS if e in files]}
    with open(out_path, "w") as fh:
        json.dump(man, fh, indent=1)
    print(f"wrote {out_path}: {len(man['files'])} files, "
          f"{len(cl)} in the import closure of {len(man['entry_points'])} entry point(s)")
    return 0


def check(manifest_path: str, target: str) -> int:
    man = json.load(open(manifest_path))
    want, cl = man["files"], set(man["closure"])
    if not os.path.isdir(target):
        print(f"TARGET MISSING: {target} is not a directory")
        return 1
    have = {f for f in os.listdir(target) if f.endswith(".py")}

    missing = sorted(f for f in want if f not in have)
    differing = sorted(f for f in want if f in have and md5(os.path.join(target, f)) != want[f])
    extra = sorted(have - set(want))

    print(f"target: {target}")
    print(f"  repo files          : {len(want)}")
    print(f"  present on target   : {len(want) - len(missing)}")
    print(f"  MISSING from target : {len(missing)}")
    print(f"  DIFFERING           : {len(differing)}")
    print(f"  extra on target     : {len(extra)} (not in the repo; stale or local)")

    fatal_missing = [f for f in missing if f in cl]
    fatal_diff = [f for f in differing if f in cl]

    if fatal_missing:
        print(f"\n  *** FATAL — in the import closure and ABSENT ({len(fatal_missing)}):")
        for f in fatal_missing:
            print(f"        {f}")
        print("      An ingest using these will die with ModuleNotFoundError, and for a lazy")
        print("      import it dies mid-run, not at start-up.")
    if fatal_diff:
        print(f"\n  *** in the import closure and STALE ({len(fatal_diff)}):")
        for f in fatal_diff:
            print(f"        {f}")
    other_missing = [f for f in missing if f not in cl]
    if other_missing:
        print(f"\n  missing but NOT in the closure ({len(other_missing)}) — "
              f"not fatal to an ingest, but still not deployed:")
        print("      " + ", ".join(other_missing))
    other_diff = [f for f in differing if f not in cl]
    if other_diff:
        print(f"\n  differing but NOT in the closure ({len(other_diff)}):")
        print("      " + ", ".join(other_diff))

    if fatal_missing or fatal_diff:
        # A target still running OLDER code may not import every file in the CURRENT closure --
        # the share's raw_metadata.py predates the tdf_safe import, so it is stale rather than
        # broken today. That is not a reason to wave this through: the moment you sync the code
        # that does the importing, the import it needs has to already be there. Sync the whole
        # closure or none of it; a half-synced closure is the 2026-09-23 failure exactly.
        need = sorted(set(fatal_missing) | set(fatal_diff))
        print(f"\n  to make the closure whole, copy these {len(need)} file(s):")
        print("      " + " ".join(need))
        print(f"\n  e.g.  scp {' '.join(need)} <host>:{target}/")
        print(f"\nRESULT: NOT SAFE TO INGEST from {target} with the repo's current code")
        return 1
    if missing or differing:
        print(f"\nRESULT: import closure is intact; {len(missing) + len(differing)} "
              f"non-closure file(s) out of sync")
        return 0
    print("\nRESULT: fully in sync")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit", metavar="OUT.json", help="run in the repo: write the file manifest")
    ap.add_argument("--check", metavar="MANIFEST.json", help="run on the target host")
    ap.add_argument("--target", help="deployment directory to check")
    a = ap.parse_args()
    if a.emit:
        return emit(a.emit)
    if a.check:
        if not a.target:
            ap.error("--check requires --target")
        return check(a.check, a.target)
    ap.error("one of --emit or --check is required")


if __name__ == "__main__":
    raise SystemExit(main())
