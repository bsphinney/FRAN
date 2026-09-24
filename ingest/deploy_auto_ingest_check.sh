#!/bin/bash
# deploy_auto_ingest_check.sh -- PRINT-ONLY pre-deploy check for the auto-ingest starvation fix.
#
# Changes nothing, anywhere. It compares this checkout's ingest/ with the Hive copy the cron runs
# (a loose scp copy, not a checkout -- see HIVE_SYNC.md) and prints the exact commands to deploy
# and to smoke-test. Copying is left to a human on purpose.
#
# WHY IT CHECKS "MISSING" SEPARATELY FROM "DIFFERS". On 2026-09-23 a sync copied the 15 files that
# DIFFERED and missed the 5 that were ABSENT on Hive; every ingest then died with
# ModuleNotFoundError. A per-file md5 loop over the files the far side HAS cannot see that. This
# fix adds two modules auto_ingest.py imports at start-up, so forgetting them is exactly that
# failure -- and ingest/audit_deploy_sync.py (not yet in the repo when this was written) is the
# general version of this check, for the whole directory.
#
# Usage, from the repo root on the laptop:   bash ingest/deploy_auto_ingest_check.sh [ssh-host]
# Runs under macOS /bin/bash 3.2: no associative arrays, no `timeout`.
set -uo pipefail

HOST="${1:-brettsp@hive.hpc.ucdavis.edu}"
DEST=/quobyte/proteomics-grp/brett/glendon/fran_ingest
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
HERE="$(cd "$(dirname "$0")" && pwd)"

# What this change ships. Nothing else is copied: corpus_ingest.py & co. on Hive belong to other
# work in flight, and a partial "sync everything that differs" is how the 09-23 run was lost.
SHIP="auto_ingest.py auto_ingest_state.py auto_ingest_alert.py find_uningested.py"

# What auto_ingest.py needs to FIND on Hive but does not ship: the transitive closure of sibling
# modules, walked from auto_ingest.py AND from every script it runs as a subprocess (corpus_ingest.py
# first of all -- its lazy `import versions` / `from provenance import ...` inside function bodies
# are exactly how tdf_safe went missing on 09-23). ast.walk sees imports nested in functions;
# importlib.import_module("x") / __import__("x") string arguments count; and any string literal
# that IS a sibling's filename ("corpus_ingest.py", "diann_xic_to_lance.py") counts, which is how a
# subprocess target is named. An import built from a variable cannot be resolved statically.
NEED=$(python3 - "$HERE" <<'EOF'
import ast, os, sys
here = sys.argv[1]
known = {f for f in os.listdir(here) if f.endswith(".py")}
def deps(f):
    out = set()
    try:
        tree = ast.parse(open(os.path.join(here, f), encoding="utf-8", errors="replace").read())
    except SyntaxError:
        return out
    for n in ast.walk(tree):
        names = []
        if isinstance(n, ast.Import):
            names = [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            names = [n.module]
        elif (isinstance(n, ast.Call) and n.args and isinstance(n.args[0], ast.Constant)
              and isinstance(n.args[0].value, str)
              and getattr(n.func, "attr", getattr(n.func, "id", "")) in ("import_module", "__import__")):
            names = [n.args[0].value]
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in known:
            out.add(n.value)
        out |= {x.split(".")[0] + ".py" for x in names if x.split(".")[0] + ".py" in known}
    return out
seen, stack = set(), ["auto_ingest.py", "corpus_ingest.py", "find_uningested.py",
                      "diann_xic_to_lance.py"]
while stack:
    f = stack.pop()
    if f in seen or f not in known:
        continue
    seen.add(f)
    stack += sorted(deps(f) - seen)
print(" ".join(sorted(seen)))
EOF
)

lmd5() { if command -v md5sum >/dev/null 2>&1; then md5sum "$1" | cut -d' ' -f1; else md5 -q "$1"; fi; }

echo "local : $HERE"
echo "remote: $HOST:$DEST"
REMOTE=$(ssh -o BatchMode=yes -o ConnectTimeout=20 "$HOST" \
  "cd $DEST 2>/dev/null && for f in $NEED; do if [ -f \"\$f\" ]; then md5sum \"\$f\"; else echo \"MISSING \$f\"; fi; done") \
  || { echo "ABORT: cannot list $DEST on $HOST"; exit 1; }

copy="" fatal=0
echo
printf "%-28s %-9s %s\n" FILE STATE NOTE
for f in $NEED; do
  line=$(printf '%s\n' "$REMOTE" | grep -E "(^MISSING $f\$|  $f\$)" | head -1)
  shipped=no; case " $SHIP " in *" $f "*) shipped=yes;; esac
  if [ "${line%% *}" = "MISSING" ]; then
    state=MISSING
    if [ $shipped = yes ]; then copy="$copy $f"; note="shipped by this change -> copy"
    else fatal=1; note="NEEDED and absent -- an ingest would die on it"; fi
  elif [ "${line%% *}" = "$(lmd5 "$HERE/$f")" ]; then
    state=same; note=""
  else
    state=DIFFERS
    if [ $shipped = yes ]; then copy="$copy $f"; note="shipped by this change -> copy"
    else note="not this change's file; left alone (other work in flight?)"; fi
  fi
  printf "%-28s %-9s %s\n" "$f" "$state" "$note"
done

echo
if [ $fatal -ne 0 ]; then
  echo "NOT SAFE: a file auto_ingest.py needs is absent on Hive and is not part of this change."
  echo "Resolve that first (ingest/audit_deploy_sync.py shows the whole picture)."
  exit 1
fi
if [ -z "$copy" ]; then
  echo "Nothing to copy: Hive already runs this change."
else
  stamp=$(date +%Y%m%d)
  echo "To deploy (NOT run by this script):"
  echo
  echo "  # 1. keep the running copies for rollback"
  echo "  ssh $HOST 'for f in auto_ingest.py find_uningested.py; do cp -p $DEST/\$f $DEST/\$f.bak.$stamp; done'"
  echo "  # 2. copy ALL of these together -- auto_ingest.py imports the others at start-up"
  echo "  (cd $HERE && scp$copy $HOST:$DEST/)"
  echo "  # 3. re-run this script: every row must read 'same'"
  echo "  bash $0 $HOST"
fi
echo
echo "Smoke test after copying (imports + the memory CLI only: no scan, no database, no ingest):"
echo "  ssh $HOST 'cd $DEST && $PY -c \"import auto_ingest, auto_ingest_state, auto_ingest_alert, find_uningested; print(\\\"imports ok\\\")\" && $PY auto_ingest.py --list-quarantine --state-file /tmp/aiq_smoke_\$\$.json'"
echo
echo "Rollback: ssh $HOST 'cp -p $DEST/auto_ingest.py.bak.<date> $DEST/auto_ingest.py'"
echo "  and the same for find_uningested.py (.bak of it too, before step 2); the two new modules"
echo "  are inert without the new auto_ingest.py, and the state file can stay."
