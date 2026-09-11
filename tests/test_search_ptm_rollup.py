import os, sys
os.environ.setdefault("DELIMP_PG_TOKEN_FILE", "/Users/brettphinney/.pgfarm_token")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.db import query                               # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

PHOS_SEARCH = "2c4911a3-79fd-5367-bdd0-ee85a16cd25b"   # Arabidopsis STY phospho
GG_SEARCH   = "5d629050-4f68-5e43-8e1f-48bbc3fc0b8f"   # Bennett_Penn_Ubiq, 61.6% GlyGly
RS41        = "P92966"                                  # 6 phosphopeptides, 460 precursors

rows = query("SELECT * FROM delimp_search_protein_ptm WHERE search_id=%s AND protein_group=%s",
             (PHOS_SEARCH, RS41), tables=["delimp_search_protein_ptm"])
check("RS41 has a row in the phospho search", len(rows) == 1, str(len(rows)))
if rows:
    check("RS41 is flagged has_phospho", rows[0]["has_phospho"] is True, str(rows[0]))
    check("RS41 is flagged has_ptm", rows[0]["has_ptm"] is True, str(rows[0]))

# THE GLYGLY WITNESS. Historical rows store the literal `[GlyGly (K)]`, NOT [UNIMOD:121]. A
# predicate matching only the normalised form finds ZERO here while the search is 61.6% GG.
n_gg = query("""SELECT count(*) AS n FROM delimp_search_protein_ptm
                 WHERE search_id=%s AND has_glygly""", (GG_SEARCH,),
             tables=["delimp_search_protein_ptm"], fetch="val")
check("the Bennett_Penn ubiquitin search has thousands of GlyGly proteins",
      (n_gg or 0) > 1000, f"has_glygly rows={n_gg}")

# DISCRIMINATOR: flags must not be uniformly true. A rollup that set has_ptm on every row would
# pass every check above and make the filter useless.
tot, with_ptm = query("""SELECT count(*) AS t, count(*) FILTER (WHERE has_ptm) AS w
                           FROM delimp_search_protein_ptm WHERE search_id=%s""",
                      (PHOS_SEARCH,), tables=["delimp_search_protein_ptm"], fetch="one").values()
check("has_ptm is selective, not uniformly true", 0 < with_ptm < tot, f"{with_ptm} of {tot}")
check("has_phospho is rarer than has_ptm (phospho is a subset)",
      query("""SELECT count(*) FILTER (WHERE has_phospho) < count(*) FILTER (WHERE has_ptm) AS ok
                 FROM delimp_search_protein_ptm WHERE search_id=%s""",
            (PHOS_SEARCH,), tables=["delimp_search_protein_ptm"], fetch="val") is True)

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
