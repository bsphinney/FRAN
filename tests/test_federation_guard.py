"""Anti-exfiltration controls, tested against attack shapes rather than happy paths.

The question these answer is not "does a lookup work" but "can a determined peer walk the corpus".
Uses a fake connection so it runs anywhere, with no database.

Run:  python tests/test_federation_guard.py
"""
import os, sys
os.environ.setdefault("FRAN_FED_ROWS_PER_DAY", "200000")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import federation_guard as g          # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

def denied(fn, *a, **k):
    try:
        fn(*a, **k); return None
    except g.Denied as d:
        return d

class FakeCur:
    def __init__(self, outer): self.outer = outer
    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "delimp_federation_quota" in s: self.rows = [self.outer.quota]
        elif "sum(n_rows)" in s: self.rows = [self.outer.spend]
        else: self.rows = [None]
    def fetchone(self): return self.rows[0]
class FakeConn:
    def __init__(self, spend=(0,0,0,0), quota=None): self.spend, self.quota = spend, quota
    def cursor(self): return FakeCur(self)
    def commit(self): pass

print("1. query SHAPE — enumeration must be inexpressible")
check("empty peptide query refused", denied(g.check_shape, "peptide", {}) is not None)
check("2-char probe refused", denied(g.check_shape, "peptide", {"seq": "AK"}) is not None)
check("wildcard refused", denied(g.check_shape, "peptide", {"seq": "LVNEL%"}) is not None)
check("regex/metachar refused", denied(g.check_shape, "peptide", {"seq": "LVN.*AK"}) is not None)
check("SQL-ish payload refused", denied(g.check_shape, "peptide", {"seq": "A' OR '1'='1"}) is not None)
check("bare '%' protein refused", denied(g.check_shape, "protein", {"protein_group": "%"}) is not None)
check("unknown endpoint refused", denied(g.check_shape, "dump_everything", {"seq": "LVNELTEFAK"}) is not None)
check("deep offset refused (corpus paging)",
      denied(g.check_shape, "peptide", {"seq": "LVNELTEFAK", "offset": 500000}) is not None)
try:
    g.check_shape("peptide", {"seq": "LVNELTEFAK"}); ok = True
except g.Denied: ok = False
check("a genuine peptide lookup is ALLOWED", ok)
try:
    g.check_shape("protein", {"protein_group": "P02769"}); ok = True
except g.Denied: ok = False
check("a genuine protein lookup is ALLOWED", ok)

print("\n2. RESPONSE cap — one call cannot be huge")
check("caps an absurd request", g.cap_rows(10**9) == g.MAX_ROWS_PER_RESPONSE)
check("caps a negative/zero request to >=1", g.cap_rows(0) >= 1)
check("honours a smaller request", g.cap_rows(10) == 10)
check("per-peer override tightens", g.cap_rows(10**9, {"max_rows_per_response": 50}) == 50)

print("\n3. BUDGET — the control that stops the patient scraper")
d = denied(g.check_budget, FakeConn(spend=(200000, 500000, 10, 10)), "greedy")
check("daily budget exhaustion denies", d is not None and d.reason == "deny_budget")
check("denial tells the peer when to retry", d is not None and d.retry_after)
d = denied(g.check_budget, FakeConn(spend=(10, 2000000, 10, 10)), "greedy")
check("monthly budget exhaustion denies", d is not None and d.reason == "deny_budget")
st = g.check_budget(FakeConn(spend=(100, 1000, 10, 10)), "polite")
check("a modest peer is allowed", st["remaining_today"] > 0)
check("remaining budget reported", st["remaining_today"] == st["day_cap"] - 100)
# The scenario that motivated the whole module: slow, patient, never bursty.
slow = 86400  # 1 request/second for a day
d = denied(g.check_budget, FakeConn(spend=(200000, 900000, slow, slow)), "patient")
check("1 req/s for 24h is stopped (by budget, not by rate)", d is not None)

print("\n4. NOVELTY — enumeration looks different from research")
d = denied(g.check_budget, FakeConn(spend=(1000, 1000, 5000, 5000)), "walker")
check("5000 requests, all distinct -> denied as enumeration",
      d is not None and "enumeration" in d.message)
st = g.check_budget(FakeConn(spend=(1000, 1000, 5000, 800)), "researcher")
check("5000 requests, 800 distinct (repeats) -> allowed", st["remaining_today"] > 0)
st = g.check_budget(FakeConn(spend=(10, 10, 20, 20)), "newcomer")
check("small all-distinct volume is NOT punished (noise floor)", st["remaining_today"] > 0)

print("\n5. query hashing — paging must not look like novelty")
h1 = g.query_hash("peptide", {"seq": "LVNELTEFAK", "limit": 100, "offset": 0})
h2 = g.query_hash("peptide", {"seq": "LVNELTEFAK", "limit": 500, "offset": 100})
check("same question, different page -> same hash", h1 == h2)
check("different question -> different hash",
      h1 != g.query_hash("peptide", {"seq": "SAMPLERPEPK"}))
check("case/whitespace normalised",
      h1 == g.query_hash("peptide", {"seq": " lvneltefak "}))

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
