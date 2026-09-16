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


# ---------------------------------------------------------------------------------------------
# THE REFRESH JOB MUST CONVERGE. Five real searches hold delimp_proteins rows and render heatmaps
# but have NO delimp_precursors row with a non-NULL protein_group, so the INSERT groups nothing for
# them and they can never gain a rollup row. A PENDING predicate that means only "has no rows"
# re-selects them on every weekly run forever: "nothing to do" can never print. PENDING must mean
# "CAN produce rows and has not yet".
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingest"))
from refresh_search_ptm import PENDING                  # noqa: E402

TBL = ["delimp_searches", "delimp_search_protein_ptm", "delimp_precursors"]
LIM = {"lim": 100000}

# Anything PENDING hands back must be able to produce at least one row. This is the convergence
# property itself, asked of the real predicate rather than a paraphrase of it.
stuck = query(f"""SELECT count(*) AS n FROM ({PENDING}) q
                   WHERE NOT EXISTS (SELECT 1 FROM delimp_precursors p
                                      WHERE p.search_id = q.id AND p.protein_group IS NOT NULL)""",
              LIM, tables=TBL, fetch="val", timeout_ms=600_000)
check("every search PENDING selects can actually produce a rollup row", (stuck or 0) == 0,
      f"{stuck} search(es) would be re-selected forever — the job never converges")

# ...and the exclusion is not vacuous: the searches PENDING now skips are exactly the ones that
# cannot produce rows, not a predicate that quietly drops work. Derived, never pinned.
n_norows = query("""SELECT count(*) AS n FROM delimp_searches s
                     WHERE NOT EXISTS (SELECT 1 FROM delimp_search_protein_ptm t
                                        WHERE t.search_id = s.id)""",
                 tables=TBL, fetch="val", timeout_ms=600_000)
n_pending = query(f"SELECT count(*) AS n FROM ({PENDING}) q", LIM, tables=TBL, fetch="val",
                  timeout_ms=600_000)
n_uncomputable = query("""SELECT count(*) AS n FROM delimp_searches s
                           WHERE NOT EXISTS (SELECT 1 FROM delimp_search_protein_ptm t
                                              WHERE t.search_id = s.id)
                             AND NOT EXISTS (SELECT 1 FROM delimp_precursors p
                                              WHERE p.search_id = s.id
                                                AND p.protein_group IS NOT NULL)""",
                       tables=TBL, fetch="val", timeout_ms=600_000)
check("PENDING skips exactly the searches that cannot produce rows, and no others",
      n_pending + n_uncomputable == n_norows,
      f"pending={n_pending} + uncomputable={n_uncomputable} != no-rows={n_norows}")
check("the uncomputable set is real, so this suite is testing something",
      (n_uncomputable or 0) > 0,
      "no uncomputable searches left — the convergence bug is unreproducible here, "
      "which makes the check above vacuous")
print(f"  [measured] searches lacking rollup rows={n_norows}, PENDING selects={n_pending}, "
      f"cannot ever produce rows={n_uncomputable}")

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
