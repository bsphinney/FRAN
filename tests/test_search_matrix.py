"""search_protein_matrix — the heatmap's data.

Fixture: PROT_0793_search_mouse, 8221f5fc-492e-5c9d-a08d-542cfdb48791 — 480,123 rows,
6,388 proteins x 222 samples, every intensity populated. The largest search in the corpus.

Run:  DELIMP_PG_TOKEN_FILE=/Users/brettphinney/.pgfarm_token python3 tests/test_search_matrix.py
"""
import os, sys, time
os.environ.setdefault("DELIMP_INTERNAL_MODE", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import queries                                      # noqa: E402
from app.db import query as _q                               # noqa: E402

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
check("reports when reach was computed", d.get("reach_computed_at") is not None)

# THE "OF N" THE PANEL SHOWS (F17). This used to assert n_proteins_total == 6388, a
# count(DISTINCT protein_group) over the whole search that ignored the presence floor -- so the
# footer read "Showing 50 of 6,388 proteins" while every row displayed was a GENE that had cleared
# the floor. Three different populations on this fixture: 6,388 protein_groups, 6,340 genes, 4,005
# genes actually rankable. Only the last is consistent with the "in at least 20% of samples" clause
# in the same sentence, and it is the spec's own figure. Cross-checked against an independent
# aggregate rather than a hardcoded 4005, so it cannot rot when the corpus grows.
_floor = 0.2 * len(samps)
_rankable_ind = _q("""WITH per_sample AS (
                        SELECT gene, raw_path FROM delimp_proteins
                         WHERE search_id=%(s)s AND intensity > 0 AND NULLIF(gene,'') IS NOT NULL
                         GROUP BY gene, raw_path)
                      SELECT count(*) AS n FROM (
                        SELECT gene FROM per_sample GROUP BY gene HAVING count(*) >= %(f)s) x""",
                   {"s": SID, "f": _floor}, tables=["delimp_proteins"], fetch="val")
check("reports the RANKABLE population, not every protein_group in the search",
      d.get("n_rankable") == _rankable_ind, f"{d.get('n_rankable')} vs {_rankable_ind} independent")
check("...and that population is genuinely narrower than the raw protein_group count",
      d.get("n_rankable") < 6388, f"n_rankable={d.get('n_rankable')}")

# ACQUISITION ORDER: DO NOT CLAIM WHAT THE DATA CANNOT SUPPORT (F16). The spec justifies the column
# ordering by "acquisition order makes batch drift visible as vertical bands", and the plan asked
# the implementer to report how many samples actually carry a date. On this fixture the answer is
# ZERO of 222 -- the sort falls through to filename order and a vertical band means nothing about
# batch drift. The panel reads n_samples_dated to say which order is really in force.
_dated_ind = _q("""SELECT count(rf.acquisition_date) AS n
                     FROM (SELECT DISTINCT raw_path FROM delimp_proteins WHERE search_id=%(s)s) p
                     LEFT JOIN raw_files rf ON rf.raw_path = p.raw_path""",
                {"s": SID}, tables=["delimp_proteins", "raw_files"], fetch="val")
check("reports how many samples carry an acquisition_date, matching the DB",
      d.get("n_samples_dated") == _dated_ind, f"{d.get('n_samples_dated')} vs {_dated_ind} independent")
check("this fixture is the no-dates case the footer must name (0 of 222)",
      d.get("n_samples_dated") == 0, str(d.get("n_samples_dated")))

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
# Identical arguments to the mode=cv call at the top of this file, so since the F3 fix this is a
# SLOW_CACHE hit rather than a second ~3s round-trip. The cache block at the end of this file
# ASSERTS that (identity + a cold/warm timing witness) instead of merely observing it here.
print(f"  [timing] mode=cv (2nd, cache hit)  end-to-end: {t_cv2:.4f}s")
cvs = [p.get("cv") for p in c["proteins"]]
check("cv mode is sorted descending across the whole page",
      all(cvs[i] >= cvs[i + 1] for i in range(len(cvs) - 1)), str(cvs[:4]))

# THE OPPOSITE REGRESSION (Fix round 3, Major 2; threshold raised in Fix round 4). The "sorted
# descending" check above passes vacuously if every cv is 0 -- 0 >= 0 -- so a future edit that
# collapsed cv to zero everywhere (e.g. grouping per_sample by gene alone, dropping raw_path)
# would leave this entire file green. This is a genuinely multi-sample search (222 samples),
# where cv MUST be a live, non-degenerate signal if the aggregate is doing its job. Proven to
# actually catch the regression it targets, not just assumed to: see the git-stash proof in Fix
# round 3 of task-6-report.md, where stubbing the aggregate to return cv=0 for every row turned
# this check red while every other check in this file (including the one above) stayed green.
#
# Threshold is a measured value witness, not an arbitrary liveness floor: on this fixture
# cvs[0]=7.8122 (Ighg2b) and the whole top-50 page sits above cvs[49]=3.6590, so 5 sits 36% below
# the top row and is still comfortably above the page floor -- enough headroom to never flake, low
# enough to also catch a log-space mistake (CV over log2 intensities lands near 0.1), while an
# 0.5 floor would have missed a uniform 8x-or-smaller shrink entirely.
check("cv is a live signal on a multi-sample search (top row clears 5, not just non-zero)",
      cvs[0] is not None and cvs[0] > 5, str(cvs[:4]))

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

# CONTAMINANT + REACH annotations. These used to be `check("every row carries is_contaminant")`
# and `check("every row carries reach")` -- assertions that a key written unconditionally in a
# dict literal a few lines away is present, which cannot go red for any reason except someone
# deleting the key. Replaced with cross-checks against INDEPENDENTLY computed values, which can.
#
# is_contaminant reaches the payload through a two-level bool_or (per_sample, then agg); the
# query below is a single flat aggregate over the same rows, so a grain error, a lost GROUP BY or
# a dropped column shows up as a mismatch. Measured on this fixture: exactly 2 of the 50 cv rows
# are flagged (TPM2, SERPINA1), so the liveness half is not vacuous either -- an aggregate that
# collapsed every row to False would clear a "matches independent" check trivially if every
# independent value were False too, and it isn't.
_genes = [p["gene"] for p in prots]
_ind_cont = {r["gene"]: bool(r["c"]) for r in _q(
    "SELECT gene, bool_or(is_contaminant) AS c FROM delimp_proteins "
    "WHERE search_id=%(s)s AND gene = ANY(%(g)s) AND intensity > 0 GROUP BY gene",
    {"s": SID, "g": _genes}, tables=["delimp_proteins"])}
_cont_mismatch = [(p["gene"], p["is_contaminant"], _ind_cont.get(p["gene"])) for p in prots
                  if bool(p["is_contaminant"]) != _ind_cont.get(p["gene"])]
check("every row's is_contaminant matches an independently computed bool_or over the same rows",
      not _cont_mismatch, str(_cont_mismatch[:5]))
_n_flagged = sum(1 for p in prots if p["is_contaminant"])
check("the contaminant strip is a live signal on this fixture (some rows flagged, not all)",
      0 < _n_flagged < len(prots), f"{_n_flagged} of {len(prots)} rows flagged")

# reach reaches the payload through `LEFT JOIN delimp_protein_corpus_reach r ON r.gene =
# upper(a.gene)`. The lookup below reads the same table directly, so a dead join, a wrong column
# (n_pct_searches for n_searches) or a stale alias shows up as a mismatch on THIS page -- the
# existing rarity-mode checks only ever exercise mode="rarity". Measured: 50 of 50 cv rows carry
# a non-null reach on this fixture.
_ind_reach = {r["gene"]: r["n"] for r in _q(
    "SELECT gene, n_searches AS n FROM delimp_protein_corpus_reach WHERE gene = ANY(%(g)s)",
    {"g": [g.upper() for g in _genes]}, tables=["delimp_protein_corpus_reach"])}
_reach_mismatch = [(p["gene"], p["reach"], _ind_reach.get((p["gene"] or "").upper())) for p in prots
                   if p["reach"] != _ind_reach.get((p["gene"] or "").upper())]
check("every row's reach matches a direct lookup in delimp_protein_corpus_reach",
      not _reach_mismatch, str(_reach_mismatch[:5]))
check("the reach join is alive on the cv page too (not only in rarity mode)",
      sum(1 for p in prots if p["reach"] is not None) >= 40,
      f"{sum(1 for p in prots if p['reach'] is not None)} of {len(prots)} rows have a reach")

# REDACTION MUST COVER THE PUBLIC VIEW (Fix round 1, 3rd revision). Every check above ran with
# DELIMP_INTERNAL_MODE=1, which sets reveal=True at import (app/main.py:221) — privacy.redact()
# is a no-op under reveal=True. So "ALL PASS" up to this point proves nothing about what an
# anonymous visitor sees. This block is the FIRST coverage in this file of the actual public
# (reveal=False) path — it did not exist before this fix round, which is exactly how the original
# leak got past every check here plus one review round.
import json, re                                               # noqa: E402
from app import privacy                                       # noqa: E402
from app.main import _json_safe                               # noqa: E402

pub = privacy.redact(_json_safe(d), False)          # reveal=False: the anonymous view of `d`
pub_samples, pub_proteins = pub.get("samples") or [], pub.get("proteins") or []

# STRUCTURAL (F6). A sample row is its opaque positional id and NOTHING else. raw_path,
# raw_basename and acquisition_date used to ride along unread; dropping them makes this payload
# structurally incapable of carrying a filename instead of dependent on privacy.redact()
# continuing to know the right key names. This check goes red the moment a field comes back.
check("public view: a sample row carries the opaque id and nothing else",
      all(set(s) == {"id"} for s in pub_samples), str(pub_samples[:2]))

# THE GENERIC SCAN, replacing three checks that could not discriminate or have gone stale:
#   - "no sample carries a bare 'basename' key" caught exactly one historical misspelling; a
#     future field named path/file_name/dir/folder/source is not in privacy._FILE_KEYS, would
#     pass through redact() untouched, and that check would have stayed green.
#   - the two RUN_RE shape checks asserted on raw_path/raw_basename, fields F6 removed.
# Splitting the REAL paths into components and searching the whole serialized payload subsumes
# all three AND the old cells-key discriminator: a filename used as a dict KEY (the ed36e2b bug,
# invisible to redact(), which rewrites values only) lands in this blob just the same, and a full
# path used as a key trips the separator check. It knows nothing about which field names are
# filename-shaped, so it cannot go stale.
real_paths = [r["raw_path"] for r in _q(
    "SELECT DISTINCT raw_path FROM delimp_proteins WHERE search_id=%s",
    (SID,), tables=["delimp_proteins"])]
check("the independent real-path list covers every sample (not a vacuous scan)",
      len(real_paths) == len(samps), f"{len(real_paths)} real paths vs {len(samps)} samples")

blob = json.dumps(pub)
check("public view: no path separator anywhere in the payload",
      "/" not in blob and "\\" not in blob, blob[:200])
comps = {c for rp in real_paths for c in re.split(r"[\\/]+", rp or "") if len(c) > 3}
leaked = sorted(c for c in comps if c in blob)
check(f"public view: none of the {len(comps)} real path components survives redaction",
      not leaked, f"LEAKED: {leaked[:5]}")

# ...and the join must still work: every cells key is a returned public sample id.
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

# F5. A malformed search_id is the caller's mistake (400), not an outage (503). Before this guard
# psycopg2 raised InvalidTextRepresentation deep in the query and app/main.py's generic handler
# turned it into 503 {"detail": "InvalidTextRepresentation: invalid input syntax for type uuid:
# \"not-a-uuid\"\nLINE 4: WHERE p.search_id='not-a-uuid' ..."} -- a fragment of the server's SQL,
# with the anonymous caller's own input echoed back, rendered by the frontend as "Database
# unavailable". The sibling coverage route already had this guard; the new PUBLIC route did not.
# raise_server_exceptions=False so this check observes what a real HTTP client observes. With
# the default (True), removing the guard makes TestClient re-raise psycopg2's
# InvalidTextRepresentation and abort this whole file mid-run instead of reporting one clean red
# -- which hides the very failure the check exists to report, and skips every check after it.
bad_client = TestClient(app, raise_server_exceptions=False)
bad_id = bad_client.get("/api/search/not-a-uuid/matrix")
check("a malformed search_id returns 400, not a 503 'database unavailable'",
      bad_id.status_code == 400, f"{bad_id.status_code}: {bad_id.text[:200]}")
check("...and the 400 body echoes no SQL text back to the caller",
      "SELECT" not in bad_id.text.upper() and "search_id='" not in bad_id.text,
      bad_id.text[:200])

# --- F3: the matrix is cached ------------------------------------------------------------------
# The endpoint is PUBLIC and fires on every search-page view. Measured uncached: 2.8-4.0s here,
# 7.3-8.9s on the largest searches, against a 6-connection pool -- a handful of concurrent
# viewers saturated it. These checks assert the cache is real, that it is keyed so free text
# cannot mint entries, and that a DEGRADED result does not stick for the 30-minute TTL.
from app.db import SLOW_CACHE                                 # noqa: E402

SLOW_CACHE.clear()
t0 = time.monotonic(); cold = queries.search_protein_matrix(SID, mode="cv", limit=7)
t_cold = time.monotonic() - t0
t0 = time.monotonic(); warm = queries.search_protein_matrix(SID, mode="cv", limit=7)
t_warm = time.monotonic() - t0
print(f"  [timing] cache cold: {t_cold:.2f}s   warm: {t_warm:.5f}s")
check("a repeated matrix call returns the cached object itself (no second DB round-trip)",
      warm is cold, f"cold id={id(cold)} warm id={id(warm)}")
check("...and the cached call does no measurable work (< 50 ms against a multi-second cold call)",
      t_warm < 0.05 and t_cold > 0.5, f"cold {t_cold:.2f}s / warm {t_warm:.5f}s")

# THE KEY. mode is normalized to a _MATRIX_MODES member BEFORE it enters the cache key. Keyed on
# the raw string instead, an anonymous caller could mint unbounded entries from free text --
# db.TTLCache has no eviction. Both calls below must land on the SAME entry.
SLOW_CACHE.clear()
junk = queries.search_protein_matrix(SID, mode="; DROP TABLE delimp_proteins --", limit=7)
cv7 = queries.search_protein_matrix(SID, mode="cv", limit=7)
check("an unknown mode shares the cv cache entry (free text cannot mint cache keys)",
      cv7 is junk, f"junk id={id(junk)} cv id={id(cv7)}")

# THE DEGRADED RESULT MUST NOT STICK. An empty matrix (no sample cleared the presence floor, or
# the search does not exist / is mid-ingest) is a TRUTHY dict, so SLOW_CACHE.get_or_set would
# happily cache it for the full 30 minutes -- which is exactly why this uses manual
# cached()/put() gated on a non-empty `proteins` instead. Identity is the witness: two calls that
# return DIFFERENT objects prove nothing was stored between them.
EMPTY_SID = "00000000-0000-0000-0000-000000000000"
e1 = queries.search_protein_matrix(EMPTY_SID, mode="cv", limit=7)
e2 = queries.search_protein_matrix(EMPTY_SID, mode="cv", limit=7)
check("an empty matrix is truthy (so get_or_set WOULD have cached it — this is the trap)",
      bool(e1) and not e1.get("proteins"), str(list(e1)[:4]))
check("a degraded/empty matrix is NOT cached (it self-heals instead of sticking for the TTL)",
      e2 is not e1, "the empty result was cached")

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
