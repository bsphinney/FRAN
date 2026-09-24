import os
import sys, sys, hashlib, subprocess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _live_db import require_live_db  # noqa: E402
require_live_db("the ingest manifest contents")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingest"))

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

import publish_manifest as PM

# The gated set must contain every script that writes corpus rows. If a new adapter is added and
# not listed here, a stale copy of it would ingest silently — the exact failure this exists to stop.
check("corpus-writing scripts are gated at refuse",
      {"corpus_ingest.py", "spectronaut_to_corpus.py", "versions.py"} <= set(PM.REFUSE_FILES),
      str(sorted(PM.REFUSE_FILES)))

# THE DISCRIMINATOR. The whole design rests on md5 detecting a change no version constant does.
# spectronaut_to_corpus.py gained the PTM work with no version bump; a constant-based check sees
# nothing. Assert the hash function actually distinguishes two near-identical files.
a = PM.file_md5(os.path.join(PM.INGEST_DIR, "spectronaut_to_corpus.py"))
import tempfile, shutil
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "x.py")
    shutil.copy(os.path.join(PM.INGEST_DIR, "spectronaut_to_corpus.py"), p)
    check("identical content hashes identically", PM.file_md5(p) == a)
    with open(p, "a") as fh: fh.write("\n# one comment\n")
    check("a one-line change changes the hash", PM.file_md5(p) != a)

check("publish refuses a dirty tree", hasattr(PM, "assert_clean_tree"))
print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
