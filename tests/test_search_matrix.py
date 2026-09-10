"""search_protein_matrix — the heatmap's data.

Fixture: PROT_0793_search_mouse, 8221f5fc-492e-5c9d-a08d-542cfdb48791 — 480,123 rows,
6,388 proteins x 222 samples, every intensity populated. The largest search in the corpus.

Run:  DELIMP_PG_TOKEN_FILE=/Users/brettphinney/.pgfarm_token python3 tests/test_search_matrix.py
"""
import os, sys, time
os.environ.setdefault("DELIMP_INTERNAL_MODE", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import queries                                      # noqa: E402

SID = "8221f5fc-492e-5c9d-a08d-542cfdb48791"
FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

t0 = time.monotonic()
d = queries.search_protein_matrix(SID, mode="cv", limit=50)
t_cv = time.monotonic() - t0
print(f"  [timing] mode=cv     end-to-end: {t_cv:.2f}s")
prots, samps = d.get("proteins") or [], d.get("samples") or []
check("returns 50 proteins", len(prots) == 50, str(len(prots)))
check("returns all 222 samples", len(samps) == 222, str(len(samps)))
check("reports the true protein total", d.get("n_proteins_total") == 6388, str(d.get("n_proteins_total")))
check("reports when reach was computed", d.get("reach_computed_at") is not None)

# THE PRESENCE FLOOR. Without it Or6c75 (1 of 222 samples, 85.4e9 mean) outranks albumin.
# Assert the floor held, not that some arbitrary gene is absent.
floor = 0.2 * len(samps)
worst = min(p["n_samples"] for p in prots)
check(f"every row clears the {int(floor)}-sample floor", worst >= floor, f"worst row has {worst}")

# ABUNDANCE MODE must be led by real abundant proteins, not sparse ones.
t0 = time.monotonic()
a = queries.search_protein_matrix(SID, mode="abundance", limit=50)
t_abundance = time.monotonic() - t0
print(f"  [timing] mode=abundance  end-to-end: {t_abundance:.2f}s")
names = [p["gene"] for p in a["proteins"]]
check("abundance mode contains albumin", "Alb" in names, str(names[:8]))
check("abundance mode excludes the 1-sample outlier Or6c75", "Or6c75" not in names, str(names[:8]))

# CV MODE is ordered by variance, descending, over the whole returned page.
t0 = time.monotonic()
c = queries.search_protein_matrix(SID, mode="cv", limit=50)
t_cv2 = time.monotonic() - t0
print(f"  [timing] mode=cv (2nd)  end-to-end: {t_cv2:.2f}s")
cvs = [p.get("cv") for p in c["proteins"]]
check("cv mode is sorted descending across the whole page",
      all(cvs[i] >= cvs[i + 1] for i in range(len(cvs) - 1)), str(cvs[:4]))

# THE CV GRAIN BUG (Fix round 2, CRITICAL). delimp_proteins is one row per (search, sample,
# protein_group) -- a gene with more than one protein_group contributes multiple rows per
# sample. Aggregating stddev_pop/avg directly over those rows mixes between-SAMPLE variation
# with between-PROTEIN-GROUP variation of the same gene. This is the DISCRIMINATOR: on a
# 1-sample search, between-sample variation must be EXACTLY zero for every row -- there is only
# one sample. Before the fix, Hnrnpll came back cv=0.998 on this exact fixture (two protein
# groups, Q921F4=385 and V9GXB6=342,620, in its one sample) despite there being no second sample
# for it to vary across. Any non-zero, non-null cv on a 1-sample search proves the aggregate is
# measuring the wrong thing.
SID_1SAMPLE = "29a34214-8861-5831-8b7a-6af3e4fc405b"
one = queries.search_protein_matrix(SID_1SAMPLE, mode="cv", limit=50)
one_prots = one.get("proteins") or []
check("1-sample fixture returns rows to check", len(one_prots) > 0, str(len(one_prots)))
check("1-sample fixture really has 1 sample", one.get("n_samples_total") == 1,
      str(one.get("n_samples_total")))
bad_cv = [(p["gene"], p["cv"]) for p in one_prots if p.get("cv") not in (None, 0, 0.0)]
check("a single-sample search yields no non-zero, non-null cv (cv must measure between-SAMPLE "
      "variation, not between-protein-group variation within one sample)",
      not bad_cv, str(bad_cv[:5]))

# RARITY MODE must be corpus-scoped and ascending.
t0 = time.monotonic()
r = queries.search_protein_matrix(SID, mode="rarity", limit=50)
t_rarity = time.monotonic() - t0
print(f"  [timing] mode=rarity     end-to-end: {t_rarity:.2f}s")
reach = [p.get("reach") for p in r["proteins"]]

# CORRECTION 1: `or 0` maps None to 0, so if the join dead-ends (every reach None), every
# comparison becomes "0 <= 0" and this would PASS while the corpus dimension is silently
# missing. Check the non-null reaches are ascending AND that the join actually matched most rows.
non_null_reach = [v for v in reach if v is not None]
check("rarity mode is sorted ascending by corpus reach (non-null values)",
      all(non_null_reach[i] <= non_null_reach[i + 1] for i in range(len(non_null_reach) - 1)),
      str(reach[:6]))
# NOT SUFFICIENT ALONE (measured, Fix round 1): this fixture's gene column already carries
# plenty of all-uppercase symbols (MHC genes, contaminant keratins, ACTB, ALB) that case-match
# under EITHER `r.gene = upper(a.gene)` or a broken `r.gene = a.gene`, so a case-sensitive join
# still clears 40/50 here. It still catches a totally dead/unjoinable reach table — keep it —
# but the Title-case check below is the one that actually discriminates the two joins.
check("rarity mode: at least 40 of 50 rows carry a non-null reach (join is not dead)",
      len(non_null_reach) >= 40, f"{len(non_null_reach)} of {len(reach)} rows have a reach")
# THE DISCRIMINATOR (Fix round 1). Measured under the correct join, rarity mode's top-10 is
# 10/10 Title-case genes (Rps18-ps6, Mims1, H2bc9, Nat8f3, Gm3404, Fmo13, Ighg, Gbp8, Zfp68,
# Try4) — real rare mouse-specific genes, which are written Title-case. An all-caps symbol
# case-matches under EITHER join (see above), so it can never witness case-sensitivity; only a
# Title-case gene surfacing with a real (non-null) reach proves the join is doing the upper()
# normalization. Under `r.gene = a.gene` every Title-case gene gets reach=None and sorts last
# via NULLS LAST, so this goes to 0/10. Threshold >= 6 of 10 has headroom against the measured
# 10/10.
top10_titlecase = sum(1 for p in r["proteins"][:10] if p["gene"] and p["gene"] != p["gene"].upper())
check("rarity mode top-10 is majority Title-case genes (case-sensitive join witness)",
      top10_titlecase >= 6, f"{top10_titlecase} of 10 top rows are Title-case: "
      f"{[p['gene'] for p in r['proteins'][:10]]}")

# CORPUS-ABUNDANCE mode is the mean within-search percentile, descending, and must be led by
# genuinely abundant proteins. Measured corpus-wide: H4c1 .909, Hsp90ab1 .906, Gapdh .897, Alb .873.
t0 = time.monotonic()
ca = queries.search_protein_matrix(SID, mode="corpus_abundance", limit=50)
t_corpus_abundance = time.monotonic() - t0
print(f"  [timing] mode=corpus_abundance  end-to-end: {t_corpus_abundance:.2f}s")
canames = [p["gene"] for p in ca["proteins"]]
check("corpus-abundance mode is led by housekeeping proteins",
      any(g in canames[:15] for g in ("Hsp90ab1", "Gapdh", "Alb", "Atp5f1a")), str(canames[:8]))

# CELLS. A missing (protein, sample) pair must be ABSENT, never 0 — absence is not zero.
# CORRECTION 2: sum() over nothing is 0 and all() over nothing is True — if `cells` came back
# empty for every protein (second query returning nothing, gene mismatch, differing basename
# derivation between code paths), BOTH of the checks below would pass vacuously. Assert the
# matrix is genuinely populated FIRST.
total_cells = sum(len(p["cells"]) for p in prots)
check("every returned protein has at least one cell",
      all(len(p["cells"]) > 0 for p in prots), "some protein has zero cells")
check("the matrix is genuinely populated (>5000 cells across 50 proteins x 222 samples)",
      total_cells > 5000, f"{total_cells} total cells")

zeros = sum(1 for p in prots for v in p["cells"].values() if v == 0)
check("no cell is stored as a literal zero", zeros == 0, f"{zeros} zero cells")
check("cells reference real sample keys",
      all(k in {s["id"] for s in samps} for p in prots for k in p["cells"]),
      "a cell key is not a returned sample")

# CONTAMINANT + REACH annotations are present on every row.
check("every row carries is_contaminant", all("is_contaminant" in p for p in prots))
check("every row carries reach (may be None)", all("reach" in p for p in prots))

# REDACTION MUST COVER THE PUBLIC VIEW (Fix round 1, 3rd revision). Every check above ran with
# DELIMP_INTERNAL_MODE=1, which sets reveal=True at import (app/main.py:221) — privacy.redact()
# is a no-op under reveal=True. So "ALL PASS" up to this point proves nothing about what an
# anonymous visitor sees. This block is the FIRST coverage in this file of the actual public
# (reveal=False) path — it did not exist before this fix round, which is exactly how the original
# leak got past every check here plus one review round.
import re                                                     # noqa: E402
from app import privacy                                       # noqa: E402
from app.main import _json_safe                               # noqa: E402

pub = privacy.redact(_json_safe(d), False)          # reveal=False: the anonymous view of `d`
pub_samples, pub_proteins = pub.get("samples") or [], pub.get("proteins") or []

check("public view: no sample carries a bare 'basename' key",
      all("basename" not in s for s in pub_samples))

RUN_RE = re.compile(r"^run-[0-9a-f]{6}(\.[A-Za-z0-9]+)?$")
check("public view: raw_basename is sanitized to the run-xxxxxx shape",
      all(RUN_RE.match(s.get("raw_basename") or "") for s in pub_samples),
      str([s.get("raw_basename") for s in pub_samples[:4]]))
check("public view: raw_path is sanitized to the run-xxxxxx shape",
      all(RUN_RE.match(s.get("raw_path") or "") for s in pub_samples),
      str([s.get("raw_path") for s in pub_samples[:4]]))

# THE DISCRIMINATOR. Compare cell keys against the REAL (unredacted) basenames from `samps`, not
# against pub_samples — real filenames obviously don't appear there if redaction worked. This is
# the check that would have caught the actual bug: cells keyed by filename are invisible to
# redact() because redact() rewrites string VALUES under known keys, never dict KEYS.
real_basenames = {s["raw_basename"] for s in samps}
cell_keys = {k for p in pub_proteins for k in p["cells"]}
check("public view: no cells key is a real acquisition filename",
      not (cell_keys & real_basenames), str(list(cell_keys & real_basenames)[:5]))

# ...and the join must still work under redaction: every cells key is a returned public sample id.
pub_ids = {s["id"] for s in pub_samples}
check("public view: cells keys still match returned sample ids after redaction",
      all(k in pub_ids for p in pub_proteins for k in p["cells"]),
      "a cell key is not a public sample id")

# --- the endpoint ----------------------------------------------------------------------------
from fastapi.testclient import TestClient                    # noqa: E402
from app.main import app                                     # noqa: E402

client = TestClient(app)
rr = client.get(f"/api/search/{SID}/matrix", params={"mode": "cv", "limit": 10})
check("matrix endpoint returns 200", rr.status_code == 200, str(rr.status_code))
body = rr.json()
body = body.get("data", body)
check("endpoint returns 10 proteins", len(body.get("proteins") or []) == 10,
      str(len(body.get("proteins") or [])))
check("endpoint echoes the mode", body.get("mode") == "cv", str(body.get("mode")))

bad = client.get(f"/api/search/{SID}/matrix", params={"mode": "; DROP TABLE delimp_proteins --"})
check("an unknown mode falls back rather than erroring", bad.status_code == 200, str(bad.status_code))
check("...and falls back to cv", (bad.json().get("data") or bad.json()).get("mode") == "cv")

missing = client.get("/api/search/00000000-0000-0000-0000-000000000000/matrix")
check("an unknown search returns 200 with an empty matrix",
      missing.status_code == 200
      and not ((missing.json().get("data") or missing.json()).get("proteins")),
      str(missing.status_code))

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
