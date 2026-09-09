"""delimp_protein_corpus_reach — the two corpus-wide facts the heatmap needs.

Keyed on UPPERCASED gene, not protein_group and not raw gene. Measured 2026-09-09:
47% of protein_group strings are seen in exactly one search (they vary between FASTAs), and
Aldoa=278 vs ALDOA=1278 because symbols are capitalised per species. Either mistake makes
"rarity" measure annotation style instead of biology.

Run:  DELIMP_PG_TOKEN_FILE=/Users/brettphinney/.pgfarm_token python3 tests/test_corpus_reach.py
"""
import os, sys
os.environ.setdefault("DELIMP_INTERNAL_MODE", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.db import query                                    # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

T = "delimp_protein_corpus_reach"
cols = {r["column_name"]: r for r in query(
    "SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name=%s",
    (T,), tables=[T])}
for c in ("gene", "n_searches", "n_samples", "mean_pct_rank", "n_pct_searches", "computed_at"):
    check(f"{c} column exists", c in cols, sorted(cols))

idx = query("SELECT indexdef FROM pg_indexes WHERE tablename=%s", (T,), tables=[T])
check("gene is the primary key",
      any("UNIQUE" in r["indexdef"] and "(gene)" in r["indexdef"] for r in idx),
      " | ".join(r["indexdef"] for r in idx)[:200])

n = query(f"SELECT count(*) AS n FROM {T}", tables=[T], fetch="val") or 0
check("table is populated", n > 100_000, str(n))

# Keys are UPPERCASE. A single lowercase key means the build forgot upper().
lower = query(f"SELECT count(*) AS n FROM {T} WHERE gene <> upper(gene)", tables=[T], fetch="val") or 0
check("every key is uppercased", lower == 0, f"{lower} non-uppercase keys")

# Case-merging actually happened: ALB must exceed either spelling alone (273 / 1789 measured).
alb = query(f"SELECT n_searches FROM {T} WHERE gene='ALB'", tables=[T], fetch="val")
check("ALB reach is case-merged (>1000, not ~273)", (alb or 0) > 1000, str(alb))

# ...and genuinely mouse-specific genes stay rare — the merge must not flatten everything.
mup2 = query(f"SELECT n_searches FROM {T} WHERE gene='MUP2'", tables=[T], fetch="val")
check("MUP2 stays rare after merging (<200)", 0 < (mup2 or 0) < 200, str(mup2))

# The percentile is only meaningful with enough contributing searches.
alb_pct = query(f"SELECT mean_pct_rank FROM {T} WHERE gene='ALB'", tables=[T], fetch="val")
check("ALB sits near the top of a typical run (mean_pct_rank > 0.8)", (alb_pct or 0) > 0.8, str(alb_pct))

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
