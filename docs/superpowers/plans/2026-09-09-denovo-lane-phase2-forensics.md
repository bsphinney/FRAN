# De novo lane Phase 2 — GVP, dbSNP and amelogenin sex estimation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** The forensic payload — find genetically variant peptides in de novo data, link them to
dbSNP and a population allele frequency, and estimate sex from tooth enamel.

**Architecture:** Three independent capabilities on the Phase 1 foundation. (a) GVP detection by
pigeonhole blocking against the corpus, with three controls that must all pass. (b) Variant
annotation from the EBI Proteins API, cached in Postgres because a protein's record is ~1.2 MB.
(c) Amelogenin sex estimation by mapping peptides onto AMELX/AMELY positions and reading the
diagnostic residues, with MS1 intensity for the female probability.

**Tech Stack:** Python 3.11, psycopg2, PostgreSQL, `xml.etree.ElementTree.iterparse` for mzML.
Tests are plain scripts run as `python tests/test_x.py` — **not** pytest.

**Spec:** `docs/superpowers/specs/2026-09-08-denovo-homology-lane-design.md` §7, §7b, §7c

**Depends on:** Phase 1 Tasks 1–6 (`docs/superpowers/plans/2026-09-09-denovo-lane-phase1.md`).

## Global Constraints

Everything in Phase 1's Global Constraints, plus:

- **A GVP candidate must pass all three controls**, and each is recorded separately so a later
  reader can see which one was doing the work: single-nucleotide reachability, non-isobaric, and
  (where available) a catalogued variant match.
- **`N→D` and `Q→E` are NOT variant evidence on mass alone.** Both are +0.98402, identical to
  deamidation, and Casanovo carries `N[Deamidated]` and `D` at the same mass — so writing `D` is a
  learned prior, not a measurement. The reverse direction (`D→N`, `E→Q`) is chemistry-proof and
  stays valid.
- **Score at the SITE**, from `aa_scores`, never the whole-peptide product.
- **A female sex call requires a denominator.** "No AMELY" and "no AMELY against demonstrated AMELX
  coverage" are different claims. Below the coverage floor the answer is INCONCLUSIVE, never female.
- **The Parker 2019 logistic coefficients are fitted to CI/mg from a PEAKS database search.** They
  must not be applied to de novo intensities without refitting. Compute and store the inputs;
  report the probability only when calibration data is supplied.

---

### Task 1: GVP detection

**Files:**
- Create: `ingest/denovo_gvp.py`
- Test: `tests/test_denovo_gvp.py`

**Interfaces:**
- Consumes: `il()` from `ingest/denovo_corpus_match.py` (Phase 1 Task 3).
- Produces: `build_index(corpus_keys) -> dict`; `find_gvp(peptide, index, corpus) -> list[dict]`
  with keys `corpus_stripped_seq`, `position`, `aa_from`, `aa_to`, `mass_delta`,
  `one_nt_reachable`, `is_isobaric`, `is_deamidation_shaped`, `is_terminal`, `n_neighbours`.

- [ ] **Step 1: Write the failing test**

```python
"""GVP detection: exact 1-substitution search, and the controls that keep it honest."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingest"))
from denovo_gvp import build_index, find_gvp  # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

corpus = {"LNDLEDALQQAK": ["LNDLEDALQQAK"], "PEPTLDEK": ["PEPTIDEK"],
          "AAAAAAAA": ["AAAAAAAA"]}
idx = build_index(corpus)

# One substitution -> a candidate. A454S on KRT1 is a real rs17678945 variant.
hits = find_gvp("LNDLEDALQQSK", idx, corpus)
check("one substitution found", len(hits) == 1, f"got {len(hits)}")
if hits:
    h = hits[0]
    check("position is 0-based index 10", h["position"] == 10, str(h["position"]))
    check("aa_from is the corpus residue", h["aa_from"] == "A")
    check("aa_to is the de novo residue", h["aa_to"] == "S")
    check("A->S is one nucleotide", h["one_nt_reachable"] is True)
    check("A->S is not isobaric", h["is_isobaric"] is False)
    check("A->S is not deamidation-shaped", h["is_deamidation_shaped"] is False)
    check("interior, not terminal", h["is_terminal"] is False)

# An exact corpus peptide is CONSERVED, not a variant.
check("exact match is not a GVP", find_gvp("LNDLEDALQQAK", idx, corpus) == [])
# I/L differences are invisible to de novo -- never a variant.
check("I/L is not a GVP", find_gvp("PEPTLDEK", idx, corpus) == [])
# Two substitutions is not a single-SNP candidate.
check("two substitutions rejected", find_gvp("LNDLEDALQQSR", idx, corpus) == [])

# CONTROL 1 -- S->E needs two nucleotide changes, so it cannot be a single SNP.
h = find_gvp("EAAAAAAA", idx, corpus)
check("S->E style: flagged not-one-nt", h and h[0]["one_nt_reachable"] is False)

# CONTROL 2 -- deamidation. N->D and Q->E are +0.98402, identical to the chemistry.
corpus2 = {"PEPTNDEK": ["PEPTNDEK"], "PEPTQTLK": ["PEPTQTLK"]}
idx2 = build_index(corpus2)
h = find_gvp("PEPTDDEK", idx2, corpus2)
check("N->D flagged deamidation-shaped", h and h[0]["is_deamidation_shaped"] is True)
h = find_gvp("PEPTETLK", idx2, corpus2)
check("Q->E flagged deamidation-shaped", h and h[0]["is_deamidation_shaped"] is True)
# The REVERSE direction cannot be produced by deamidation, so it stays valid.
corpus3 = {"PEPTDDEK": ["PEPTDDEK"]}
h = find_gvp("PEPTNDEK", build_index(corpus3), corpus3)
check("D->N is NOT deamidation-shaped", h and h[0]["is_deamidation_shaped"] is False)

# Determinism: neighbours sorted, or counts drift with the hash seed.
c4 = {"AAAAAAAA": ["AAAAAAAA"], "AAAAAAAB": ["AAAAAAAB"]}
r1 = [h["corpus_stripped_seq"] for h in find_gvp("AAAAAAAC", build_index(c4), c4)]
r2 = [h["corpus_stripped_seq"] for h in find_gvp("AAAAAAAC", build_index(c4), c4)]
check("deterministic order", r1 == r2 == sorted(r1))

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_denovo_gvp.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'denovo_gvp'`

- [ ] **Step 3: Write the implementation**

```python
"""Find genetically variant peptide candidates: de novo peptides one substitution from the corpus.

THE SEARCH. Comparing 15k peptides against 2.9M naively is 43 billion character comparisons.
Pigeonhole instead: two equal-length strings differing in <=1 position must agree EXACTLY on at
least one half, so index the corpus by (len, left half) and (len, right half), probe both, and
verify with a real Hamming distance. Exact -- no false negatives. Measured: 6.6 s to build the
index, 0.1 s to classify 15,512 peptides against 2.9M.

THE THREE CONTROLS. A one-residue difference in de novo data is as likely to be a sequencing error
as a variant, so a candidate carries all three verdicts rather than being silently filtered:

  1. one_nt_reachable -- a real GVP comes from a single nucleotide polymorphism, so the
     substitution must be reachable by one nucleotide change in the codon. S->E is not.
  2. is_isobaric -- near-isobaric swaps are what de novo cannot resolve from the spectrum.
  3. is_deamidation_shaped -- N->D and Q->E are +0.98402, EXACTLY the deamidation delta. Casanovo
     carries N[Deamidated] and D at the same mass, so writing D is a learned prior from modern
     training data, not a measurement -- and it is systematically wrong on archaeological samples.
     The REVERSE direction (D->N, E->Q) cannot be produced by deamidation and stays valid.

A terminal substitution is reported but flagged: de novo confidence is lowest at the termini, which
is why Casanovo's own feature set weights N- and C-terminal confidence separately.
"""
from __future__ import annotations

import collections
import itertools

_BASES = "TCAG"
_AAS = "FFLLSSSSYY**CC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG"
_CODON: dict[str, list[str]] = {}
for _i, _c in enumerate(itertools.product(_BASES, repeat=3)):
    _CODON.setdefault(_AAS[_i], []).append("".join(_c))

MASS = {"G": 57.02146, "A": 71.03711, "S": 87.03203, "P": 97.05276, "V": 99.06841,
        "T": 101.04768, "C": 103.00919, "L": 113.08406, "I": 113.08406, "N": 114.04293,
        "D": 115.02694, "Q": 128.05858, "K": 128.09496, "E": 129.04259, "M": 131.04049,
        "H": 137.05891, "F": 147.06841, "R": 156.10111, "Y": 163.06333, "W": 186.07931}

DEAMIDATION = {("N", "D"), ("Q", "E")}      # reference residue -> what de novo reads
ISOBARIC_TOL = 0.06


def one_nt_apart(x: str, y: str) -> bool:
    """Is residue y reachable from x by exactly one nucleotide substitution?"""
    return any(sum(a != b for a, b in zip(cx, cy)) == 1
               for cx in _CODON.get(x, ()) for cy in _CODON.get(y, ()))


def build_index(corpus: dict[str, list[str]]) -> dict:
    """(length, half, text) -> [corpus I/L keys]. Pigeonhole blocking on halves."""
    idx = collections.defaultdict(list)
    for key in corpus:
        h = len(key) // 2
        idx[(len(key), 0, key[:h])].append(key)
        idx[(len(key), 1, key[h:])].append(key)
    return idx


def find_gvp(peptide: str, index: dict, corpus: dict[str, list[str]]) -> list[dict]:
    """One-substitution neighbours of `peptide` in the corpus, with all three control verdicts.

    An exact (or I/L-equivalent) corpus peptide is CONSERVED, not a variant, and returns [].
    """
    q = peptide.upper()
    if q in corpus:
        return []
    h = len(q) // 2
    cands = set(index.get((len(q), 0, q[:h]), ())) | set(index.get((len(q), 1, q[h:]), ()))

    out = []
    for cand in sorted(cands):          # sorted: unsorted set order depends on the hash seed
        diff = [i for i, (a, b) in enumerate(zip(q, cand)) if a != b]
        if len(diff) != 1:
            continue
        i = diff[0]
        frm, to = cand[i], q[i]
        dm = abs(MASS.get(frm, 0.0) - MASS.get(to, 0.0))
        for spelling in sorted(corpus[cand]):
            out.append({
                "corpus_stripped_seq": spelling,
                "position": i,
                "aa_from": frm,
                "aa_to": to,
                "mass_delta": dm,
                "one_nt_reachable": one_nt_apart(frm, to),
                "is_isobaric": dm < ISOBARIC_TOL,
                "is_deamidation_shaped": (frm, to) in DEAMIDATION,
                "is_terminal": i == 0 or i == len(q) - 1,
                "n_neighbours": len(cands),
            })
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_denovo_gvp.py`
Expected: PASS, `0 failure(s)`

- [ ] **Step 5: Reproduce the measured crane numbers**

Run:
```bash
ssh hive "cd /quobyte/proteomics-grp/brett/teeth_pilot2 && python3 -c \"
import sys; sys.path.insert(0,'.')
from denovo_gvp import build_index, find_gvp
from denovo_corpus_match import il
corpus={}
for l in open('corpus_peptides.txt'):
    s=l.strip().upper()
    if s: corpus.setdefault(il(s),[]).append(s)
idx=build_index(corpus)
dn=sorted({il(l.strip()) for l in open('crane_peptides.txt') if l.strip()})
hits=[h for q in dn for h in find_gvp(q,idx,corpus)[:1]]
cred=[h for h in hits if h['one_nt_reachable'] and not h['is_isobaric']]
print('1-substitution', len(hits))
print('credible      ', len(cred))
print('interior      ', sum(1 for h in cred if not h['is_terminal']))
print('deamid-shaped ', sum(1 for h in cred if h['is_deamidation_shaped']))\""
```
Expected: 1-substitution **1,662**; credible **~1,140**; interior **~897**; deamidation-shaped
**~159**. A materially different number means the detector changed behaviour — investigate before
proceeding.

- [ ] **Step 6: Commit**

```bash
git add ingest/denovo_gvp.py tests/test_denovo_gvp.py
git commit -m "ingest: GVP detection by pigeonhole blocking, with three controls"
```

---

### Task 2: GVP schema and ingest

**Files:**
- Modify: `schema/denovo.sql` (append)
- Modify: `ingest/denovo_ingest.py` (call `find_gvp`, write the rows)
- Test: `tests/test_denovo_gvp_ingest.py`

**Interfaces:**
- Consumes: `find_gvp()` from Task 1; `build_rows()` from Phase 1 Task 6.
- Produces: table `delimp_denovo_gvp`; `build_rows()` gains a `gvp` key.

- [ ] **Step 1: Write the failing test**

```python
"""GVP rows are built, scored at the site, and classify the peptide."""
import os, re, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingest"))
from denovo_ingest import build_rows  # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

SQL = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                        "schema", "denovo.sql")).read()
check("gvp table created", "CREATE TABLE IF NOT EXISTS delimp_denovo_gvp" in SQL)
for col in ("aa_score_at_site", "aa_score_flank_mean", "one_nt_reachable",
            "is_deamidation_shaped", "is_terminal"):
    check(f"gvp.{col} present", col in SQL)

MZTAB = ("MTD\tsoftware[1]\t[MS, MS:1003281, Casanovo, 5.2.1]\n"
         "PSH\topt_global_cv_MS:1003169_proforma_peptidoform_sequence\tPSM_ID\t"
         "search_engine_score[1]\tcharge\texp_mass_to_charge\tspectra_ref\topt_global_aa_scores\n"
         "PSM\tLNDLEDALQQSK\t1\t0.9\t2\t500.5\t"
         "ms_run[1]:controllerType=0 controllerNumber=1 scan=1\t"
         "0.9,0.9,0.9,0.9,0.9,0.9,0.9,0.9,0.9,0.9,0.4,0.9\n")
fh = tempfile.NamedTemporaryFile("w", suffix=".mztab", delete=False,
                                 prefix="QEPlus2_01012020_10_ZG1_")
fh.write(MZTAB); fh.close()

corpus = {"LNDLEDALQQAK": ["LNDLEDALQQAK"]}
out = build_rows(fh.name, cohort="t", corpus=corpus)
check("one gvp row", len(out["gvp"]) == 1, f"got {len(out['gvp'])}")
if out["gvp"]:
    g = out["gvp"][0]
    check("aa_from/aa_to", g["aa_from"] == "A" and g["aa_to"] == "S")
    # The per-residue score AT the substituted position, not the whole-peptide product.
    check("aa_score_at_site is the site residue", abs(g["aa_score_at_site"] - 0.4) < 1e-9,
          str(g.get("aa_score_at_site")))
    check("flank mean excludes the site",
          g["aa_score_flank_mean"] > g["aa_score_at_site"])
cls = {p["stripped_seq"]: p["corpus_class"] for p in out["peptides"]}
check("peptide classified gvp", cls["LNDLEDALQQSK"] == "gvp")

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_denovo_gvp_ingest.py`
Expected: FAIL — `gvp table created`

- [ ] **Step 3: Append the DDL**

Append to `schema/denovo.sql`:

```sql
-- Variant candidates: de novo peptides ONE substitution from a corpus peptide.
-- Every control verdict is stored rather than applied as a filter, so a later reader can see
-- which one was doing the work -- and so the thresholds can move without a re-ingest.
CREATE TABLE IF NOT EXISTS delimp_denovo_gvp (
    run_id              uuid NOT NULL REFERENCES delimp_denovo_run(run_id) ON DELETE CASCADE,
    stripped_seq        text NOT NULL,
    corpus_stripped_seq text NOT NULL,
    position            smallint NOT NULL,      -- 0-based, within the peptide
    aa_from             char(1) NOT NULL,       -- corpus residue
    aa_to               char(1) NOT NULL,       -- de novo residue
    mass_delta          real NOT NULL,
    -- Control 1: a real GVP comes from a SNP, so one nucleotide change must suffice.
    one_nt_reachable    boolean NOT NULL,
    -- Control 2: near-isobaric swaps are what de novo cannot resolve.
    is_isobaric         boolean NOT NULL,
    -- Control 3: N->D and Q->E are +0.98402 -- identical to deamidation, so not evidence of a
    -- variant on mass alone. The reverse direction is chemistry-proof.
    is_deamidation_shaped boolean NOT NULL,
    is_terminal         boolean NOT NULL,
    n_neighbours        smallint NOT NULL,
    -- Score AT the site, from aa_scores. The whole-peptide score is a product over residues and
    -- says nothing about the substituted position.
    aa_score_at_site    real,
    aa_score_flank_mean real,
    n_psms              integer NOT NULL DEFAULT 1,
    PRIMARY KEY (run_id, stripped_seq, corpus_stripped_seq, position)
);
-- Substitution leads, not run_id: "show me every Q->E in the cohort" is the shape of the query.
CREATE INDEX IF NOT EXISTS idx_denovo_gvp_sub
    ON delimp_denovo_gvp (aa_from, aa_to, position);
CREATE INDEX IF NOT EXISTS idx_denovo_gvp_seq ON delimp_denovo_gvp (stripped_seq);

COMMENT ON COLUMN delimp_denovo_gvp.is_deamidation_shaped IS
    'N->D or Q->E: +0.98402, exactly the deamidation delta. Casanovo carries N[Deamidated] and D '
    'at the same mass, so writing D is a learned prior from modern training data, not a '
    'measurement -- and it is systematically wrong on archaeological samples.';
```

- [ ] **Step 4: Wire it into the ingestor**

In `ingest/denovo_ingest.py`, add the import and extend `build_rows`:

```python
from denovo_gvp import build_index, find_gvp                   # noqa: E402


def _site_scores(psms, peptide, position):
    """Per-residue confidence at the substituted position, and the mean of its +/-2 flank.

    The whole-peptide score is a product over residues and says nothing about one position, so
    this is the number a variant call actually rests on.
    """
    for p in psms:
        if p["stripped_seq"] == peptide and p.get("aa_scores"):
            aa = p["aa_scores"]
            if position < len(aa):
                lo, hi = max(0, position - 2), min(len(aa), position + 3)
                flank = [aa[j] for j in range(lo, hi) if j != position]
                return aa[position], (sum(flank) / len(flank) if flank else None)
    return None, None
```

Inside `build_rows`, after the `matches` block:

```python
    index = build_index(corpus)
    gvp = []
    for p in peptides:
        if p["corpus_class"] == "conserved":
            continue
        for h in find_gvp(p["stripped_seq"], index, corpus):
            at, flank = _site_scores(d["psms"], p["stripped_seq"], h["position"])
            gvp.append({**h, "stripped_seq": p["stripped_seq"],
                        "aa_score_at_site": at, "aa_score_flank_mean": flank,
                        "n_psms": p["n_psms"]})
    gvp_seqs = {g["stripped_seq"] for g in gvp}
    for p in peptides:
        if p["stripped_seq"] in gvp_seqs:
            p["corpus_class"] = "gvp"
```

Add `"gvp": gvp` to the returned dict, and in `main()` under `--apply` add
`DELETE FROM delimp_denovo_gvp WHERE run_id = %s` to the delete list plus:

```python
    psycopg2.extras.execute_values(
        cur, "INSERT INTO delimp_denovo_gvp (run_id,stripped_seq,corpus_stripped_seq,position,"
             "aa_from,aa_to,mass_delta,one_nt_reachable,is_isobaric,is_deamidation_shaped,"
             "is_terminal,n_neighbours,aa_score_at_site,aa_score_flank_mean,n_psms) VALUES %s",
        [(rid, g["stripped_seq"], g["corpus_stripped_seq"], g["position"], g["aa_from"],
          g["aa_to"], g["mass_delta"], g["one_nt_reachable"], g["is_isobaric"],
          g["is_deamidation_shaped"], g["is_terminal"], g["n_neighbours"],
          g["aa_score_at_site"], g["aa_score_flank_mean"], g["n_psms"]) for g in out["gvp"]])
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python tests/test_denovo_gvp_ingest.py`
Expected: PASS, `0 failure(s)`

- [ ] **Step 6: Commit**

```bash
git add schema/denovo.sql ingest/denovo_ingest.py ingest/denovo_gvp.py \
        tests/test_denovo_gvp_ingest.py
git commit -m "ingest: write GVP candidates, scored at the substituted site"
```

---

### Task 3: Variant annotation — dbSNP and allele frequency

**Files:**
- Create: `ingest/variant_annotation.py`
- Modify: `schema/denovo.sql` (append)
- Test: `tests/test_variant_annotation.py`

**Interfaces:**
- Produces: `fetch_variants(accession) -> list[dict]`; `annotate(accession, position, aa_wt, aa_alt,
  variants) -> dict|None` with `rsid`, `maf`, `maf_source`, `clinical_significance`; table
  `delimp_variant_annotation`.

This is the GPMDB capability: mass shift → dbSNP → minor allele frequency, rendered inline.

- [ ] **Step 1: Write the failing test**

```python
"""Variant annotation: parse the EBI Proteins payload; do not hit the network in tests."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingest"))
from variant_annotation import annotate, parse_features  # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

# The shape the EBI Proteins API actually returns (trimmed to the fields used).
PAYLOAD = {"accession": "P04264", "sequence": "M" * 500, "features": [
    {"type": "VARIANT", "begin": "454", "end": "454", "wildType": "A",
     "alternativeSequence": "S",
     "xrefs": [{"name": "dbSNP", "id": "rs17678945"}],
     "populationFrequencies": [{"source": "gnomAD v4.1.0 Exomes", "frequency": 0.0222635}],
     "clinicalSignificances": [{"type": "Benign"}]},
    {"type": "VARIANT", "begin": "236", "end": "236", "wildType": "N",
     "alternativeSequence": "D", "xrefs": [], "populationFrequencies": []},
]}

feats = parse_features(PAYLOAD)
check("two features parsed", len(feats) == 2, f"got {len(feats)}")

a = annotate("P04264", 454, "A", "S", feats)
check("variant matched", a is not None)
if a:
    check("rsid", a["rsid"] == "rs17678945")
    check("maf", abs(a["maf"] - 0.0222635) < 1e-9)
    check("maf source", "gnomAD" in a["maf_source"])
    check("significance", a["clinical_significance"] == "Benign")

b = annotate("P04264", 236, "N", "D", feats)
check("catalogued without rsid still matches", b is not None)
if b:
    check("no rsid is None, not a crash", b["rsid"] is None)
    check("no maf is None", b["maf"] is None)

check("wrong residue does not match", annotate("P04264", 454, "A", "T", feats) is None)
check("wrong position does not match", annotate("P04264", 999, "A", "S", feats) is None)

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_variant_annotation.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'variant_annotation'`

- [ ] **Step 3: Append the DDL**

```sql
-- Cached variant annotation from the EBI Proteins API. A CACHE, not a mirror: the API returns
-- ~1.2 MB per protein, and allele frequencies are revised as gnomAD releases, so rows carry a
-- fetch timestamp and are refreshed on a TTL rather than treated as permanent.
CREATE TABLE IF NOT EXISTS delimp_variant_annotation (
    accession             text NOT NULL,
    position              integer NOT NULL,      -- 1-based, in the protein
    aa_wt                 char(1) NOT NULL,
    aa_alt                char(1) NOT NULL,
    rsid                  text,
    maf                   double precision,
    maf_source            text,
    clinical_significance text,
    fetched_at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (accession, position, aa_wt, aa_alt)
);
CREATE INDEX IF NOT EXISTS idx_variant_rsid ON delimp_variant_annotation (rsid);
```

- [ ] **Step 4: Write the implementation**

```python
"""Annotate a substitution with dbSNP and a population allele frequency.

This is the GPMDB capability: from a mass shift to a minor allele frequency. Casanovo makes it
easier than GPMDB had it -- de novo reads the substituted residue directly, so there is no
inference from a delta.

Measured coverage on KRT1 (P04264), the protein that matters most for hair: 875 variant features,
848 single-residue substitutions, 757 carrying a dbSNP rsID, 436 carrying a population frequency
(gnomAD v4.1.0 Exomes, ClinVar, 1000 Genomes).

SCOPE. dbSNP is human-only: for non-human samples homology remains the route. And a catalogued
match is EVIDENCE, not proof -- a de novo error can coincide with a known variant. But it is the
only one of the controls that can CONFIRM rather than merely fail to reject, which makes it the
strongest of them.
"""
from __future__ import annotations

import json
import urllib.request

API = "https://www.ebi.ac.uk/proteins/api/variation/{acc}"


def fetch_variants(accession: str, timeout: int = 45) -> dict:
    req = urllib.request.Request(API.format(acc=accession),
                                 headers={"Accept": "application/json",
                                          "User-Agent": "fran-denovo-lane"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())


def parse_features(payload: dict) -> list[dict]:
    """Single-residue substitutions only; indels are a different problem."""
    out = []
    for f in payload.get("features", []):
        wt, alt, beg = f.get("wildType"), f.get("alternativeSequence"), f.get("begin")
        if wt and alt and len(wt) == 1 and len(alt) == 1 and beg:
            out.append({"position": int(beg), "wt": wt, "alt": alt, "raw": f})
    return out


def annotate(accession: str, position: int, aa_wt: str, aa_alt: str,
             features: list[dict]) -> dict | None:
    """Exact match on position AND both residues. A position-only match would be a false positive."""
    for f in features:
        if f["position"] != position or f["wt"] != aa_wt or f["alt"] != aa_alt:
            continue
        raw = f["raw"]
        rs = [x.get("id") for x in (raw.get("xrefs") or [])
              if "dbSNP" in (x.get("name") or "")]
        pops = raw.get("populationFrequencies") or []
        best = max(pops, key=lambda p: p.get("frequency") or 0) if pops else None
        sig = raw.get("clinicalSignificances") or []
        return {"accession": accession, "position": position, "aa_wt": aa_wt, "aa_alt": aa_alt,
                "rsid": rs[0] if rs else None,
                "maf": best.get("frequency") if best else None,
                "maf_source": best.get("source") if best else None,
                "clinical_significance": sig[0].get("type") if sig else None}
    return None
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python tests/test_variant_annotation.py`
Expected: PASS, `0 failure(s)`

- [ ] **Step 6: Verify against the live API**

Run:
```bash
python3 -c "
import sys; sys.path.insert(0,'ingest')
from variant_annotation import fetch_variants, parse_features, annotate
f = parse_features(fetch_variants('P04264'))
print('features:', len(f))
print(annotate('P04264', 454, 'A', 'S', f))"
```
Expected: ~848 features, and `rs17678945` with `maf` ≈ 0.0223.

- [ ] **Step 7: Commit**

```bash
git add ingest/variant_annotation.py schema/denovo.sql tests/test_variant_annotation.py
git commit -m "ingest: dbSNP and allele-frequency annotation, cached with a TTL"
```

---

### Task 4: Amelogenin sex estimation

**Files:**
- Create: `ingest/amel_call.py`
- Test: `tests/test_amel_call.py`

**Interfaces:**
- Produces: `Amel(x_fasta, y_fasta)` with `.sites` and `.y_insertions`;
  `call_sample(peptides, amel) -> dict`; `verdict(result, min_x_cov_pct, min_y_sites) -> (str, str)`.

- [ ] **Step 1: Write the failing test**

```python
"""Sex estimation from AMELX/AMELY, by SITE rather than by whole-peptide matching."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingest"))
from amel_call import Amel, call_sample, verdict  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
X = os.path.join(HERE, "fixtures", "Q99217.fasta")   # AMELX
Y = os.path.join(HERE, "fixtures", "Q99218.fasta")   # AMELY
if not (os.path.exists(X) and os.path.exists(Y)):
    print("SKIP: fetch the AMELX/AMELY FASTAs into tests/fixtures/ first")
    sys.exit(0)

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

amel = Amel(X, Y)
check("19 usable discriminating sites", len(amel.sites) == 19, str(len(amel.sites)))
check("AMELY insertion found", any(len(s) == 14 for _, _, s in amel.y_insertions))

# NOT ONE diagnostic site may be I/L, N/D or Q/E -- that is what makes deamidation-tolerant
# alignment safe. If this ever fails, the tolerance is unsound and must be removed.
bad = [s for s in amel.sites
       if {s["xr"], s["yr"]} in ({"I", "L"}, {"N", "D"}, {"Q", "E"})]
check("no site is I/L, N/D or Q/E", not bad, str(bad))

# A peptide inside the AMELY-only insertion is unambiguous: AMELX has no such region.
r = call_sample({"SEALDVDRTALVLTPLK": 3}, amel)
check("insertion peptide gives Y evidence", len(r["ins"]) >= 1)

# Absence of AMELY with NO AMELX coverage is INCONCLUSIVE, never female.
r0 = call_sample({"WWWWWWWWWW": 1}, amel)
v, why = verdict(r0, min_x_cov_pct=15.0, min_y_sites=2)
check("no coverage -> INCONCLUSIVE", v == "INCONCLUSIVE", v)
check("verdict states the denominator", "coverage" in why.lower())

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Fetch the fixtures and run the test**

Run:
```bash
mkdir -p tests/fixtures
curl -s "https://rest.uniprot.org/uniprotkb/Q99217.fasta" -o tests/fixtures/Q99217.fasta
curl -s "https://rest.uniprot.org/uniprotkb/Q99218.fasta" -o tests/fixtures/Q99218.fasta
python tests/test_amel_call.py
```
Expected: FAIL — `ModuleNotFoundError: No module named 'amel_call'`

- [ ] **Step 3: Write the implementation**

```python
"""Sex estimation from de novo peptides: AMELX vs AMELY, by SITE not by peptide.

WHY SITE-LEVEL. Matching de novo peptides against a reference peptide LIST fails twice on
archaeological enamel: the peptides are non-tryptic so their termini match no tryptic reference,
and they are deamidated so a single event anywhere in the peptide breaks a whole-peptide match.
Measured on the real sequences, 12 of the 19 discriminating sites sit within 3 residues of an N or
Q, so deamidation near a diagnostic site is the expected case, not a corner case. Mapping the
peptide onto the protein and reading the diagnostic POSITION is immune to both.

DEAMIDATION-TOLERANT ALIGNMENT, AND WHY IT IS SAFE HERE. During alignment N is treated as
equivalent to D and Q to E, because de novo cannot distinguish them. That would be dangerous if any
diagnostic site were an N/D or Q/E pair -- it is not. All 19 sites are verified free of N/D, Q/E,
I/L and near-isobaric ambiguity, so tolerance raises mapping sensitivity without touching the call.
The test asserts this; if it ever fails, remove the tolerance.

THE DENOMINATOR. A female call is an ABSENCE of evidence, so it is only meaningful against
demonstrated AMELX coverage in the same sample -- see Parker et al. 2019 (J Archaeol Sci
101:169-180) sec 3.4, which models Pr(female) as a function of the AMELX signal precisely because
"no AMELY" is also what a low-signal male looks like. Below the coverage floor this returns
INCONCLUSIVE, never FEMALE.
"""
from __future__ import annotations

import collections
import difflib

_IL = str.maketrans("I", "L")


def _read_fasta(path: str) -> str:
    return "".join(l.strip() for l in open(path) if not l.startswith(">"))


def equiv(a: str, b: str) -> bool:
    """Residue identity as de novo can actually resolve it."""
    return a == b or {a, b} in ({"I", "L"}, {"N", "D"}, {"Q", "E"})


class Amel:
    """AMELX/AMELY with their discriminating sites, derived from the sequences, not hardcoded."""

    def __init__(self, x_path: str, y_path: str):
        self.X, self.Y = _read_fasta(x_path), _read_fasta(y_path)
        self.sites: list[dict] = []
        self.y_insertions: list[tuple[int, int, str]] = []
        sm = difflib.SequenceMatcher(None, self.X, self.Y, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            if tag == "replace" and (i2 - i1) == (j2 - j1):
                for k in range(i2 - i1):
                    xr, yr = self.X[i1 + k], self.Y[j1 + k]
                    if not equiv(xr, yr):          # unreadable by de novo -> excluded on purpose
                        self.sites.append({"x": i1 + k, "y": j1 + k, "xr": xr, "yr": yr})
            elif tag == "insert":
                self.y_insertions.append((j1, j2, self.Y[j1:j2]))


def alignments(pep: str, prot: str, max_mm: int = 1) -> list[int]:
    out = []
    n = len(pep)
    for off in range(0, len(prot) - n + 1):
        mm = 0
        for k in range(n):
            if not equiv(pep[k], prot[off + k]):
                mm += 1
                if mm > max_mm:
                    break
        else:
            out.append(off)
    return out


def call_sample(peptides: dict[str, int], amel: Amel, max_mm: int = 1,
                min_len: int = 7) -> dict:
    """peptides maps an I/L-normalised peptide -> its PSM count."""
    px, py = amel.X.translate(_IL), amel.Y.translate(_IL)
    x_cov, y_cov = set(), set()
    x_sites, y_sites = collections.Counter(), collections.Counter()
    ins = []
    for pep, n in peptides.items():
        if len(pep) < min_len:
            continue
        ax, ay = alignments(pep, px, max_mm), alignments(pep, py, max_mm)
        if not ax and not ay:
            continue
        for off in ax:
            x_cov.update(range(off, off + len(pep)))
        for off in ay:
            y_cov.update(range(off, off + len(pep)))
        for si, s in enumerate(amel.sites):
            for off in ay:
                k = s["y"] - off
                if 0 <= k < len(pep) and pep[k] == s["yr"]:
                    y_sites[si] += n
            for off in ax:
                k = s["x"] - off
                if 0 <= k < len(pep) and pep[k] == s["xr"]:
                    x_sites[si] += n
        for a, b, _s in amel.y_insertions:
            if b - a < 6:
                continue
            for off in ay:
                ov = min(off + len(pep), b) - max(off, a)
                if ov >= 6:
                    ins.append((pep, ov, n))
                    break
    return {"x_cov": len(x_cov), "x_pct": 100.0 * len(x_cov) / len(amel.X),
            "y_cov": len(y_cov), "y_pct": 100.0 * len(y_cov) / len(amel.Y),
            "x_sites": x_sites, "y_sites": y_sites, "ins": ins}


def verdict(r: dict, min_x_cov_pct: float = 15.0, min_y_sites: int = 2) -> tuple[str, str]:
    """A call WITH its denominator. Absence of AMELY is only female if AMELX was actually seen.

    min_x_cov_pct is a provisional floor, NOT a calibrated threshold. Parker 2019 replaces it with
    a logistic on the AMELX signal; see ingest/amel_intensity.py. Until that is refit for de novo
    input, treat a FEMALE call near the floor as inconclusive.
    """
    n_y = len(r["y_sites"])
    strong_ins = sum(1 for _p, ov, _n in r["ins"] if ov >= 8)
    if n_y >= min_y_sites or strong_ins:
        why = []
        if n_y:
            why.append(f"{n_y} AMELY-specific site(s)")
        if strong_ins:
            why.append(f"{strong_ins} peptide(s) inside the AMELY insertion")
        return "MALE", " + ".join(why)
    if r["x_pct"] >= min_x_cov_pct:
        return "FEMALE", (f"no AMELY evidence against {r['x_pct']:.0f}% AMELX coverage "
                          f"({len(r['x_sites'])} X-specific site(s))")
    return "INCONCLUSIVE", (f"only {r['x_pct']:.0f}% AMELX coverage -- too little amelogenin to "
                            f"call absence of AMELY")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_amel_call.py`
Expected: PASS, `0 failure(s)`

- [ ] **Step 5: Verify against the real teeth samples**

Run the caller over the four `teeth_pilot2` mzTabs.
Expected, matching the 2026-09-08 pilot:

| sample | AMELX cov | Y-sites | call |
|---|---|---|---|
| `QEPlus2_12212017_28_62` | 60% | 6 | MALE |
| `QEplus2_04212017_13_T12` | 43% | 1 | MALE |
| `QEPlus2_12212017_14_58` | 20% | 0 | FEMALE (weak — see Task 5) |
| `QEplus2_04212017_17_T20` | 0% | 0 | INCONCLUSIVE |

- [ ] **Step 6: Commit**

```bash
git add ingest/amel_call.py tests/test_amel_call.py tests/fixtures/Q9921*.fasta
git commit -m "ingest: amelogenin sex estimation by site, with the coverage denominator"
```

---

### Task 5: Amelogenin intensity and the female probability

**Files:**
- Create: `ingest/amel_intensity.py`
- Modify: `schema/denovo.sql` (append)
- Test: `tests/test_amel_intensity.py`

**Interfaces:**
- Consumes: `Amel`, `alignments` from Task 4.
- Produces: `scan_intensities(mzml) -> dict[int, float]`;
  `combined_intensity(psms, amel) -> {'x': float, 'y': float, 'shared': float}`;
  `pr_female(amelx_ci_per_mg) -> float`; table `delimp_denovo_sex_call`.

- [ ] **Step 1: Write the failing test**

```python
"""Amelogenin intensity, the Y:X ratio, and the Parker 2019 logistic -- with its guard rails."""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingest"))
from amel_intensity import pr_female, scan_intensities  # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

# Parker et al. 2019 sec 3.4: ln(Pr(F)/(1-Pr(F))) = -12.8622 + 0.7496*ln(AMELX+1)
# Their stated landmark: AMELX > 5.31e8 CI/mg gives >90% probability of female.
check("landmark: 5.31e8 -> >0.90", pr_female(5.31e8) > 0.90, f"{pr_female(5.31e8):.3f}")
check("monotonic in AMELX", pr_female(1e9) > pr_female(1e7))
check("low signal is near a coin flip", 0.3 < pr_female(3.2e7) < 0.7,
      f"{pr_female(3.2e7):.3f}")
check("zero signal does not crash", 0.0 <= pr_female(0.0) <= 1.0)

# Precursor intensity comes from cvParam MS:1000042, keyed by scan.
MZML = """<?xml version="1.0"?>
<mzML xmlns="http://psi.hupo.org/ms/mzml"><run><spectrumList count="1">
<spectrum index="0" id="controllerType=0 controllerNumber=1 scan=42">
 <cvParam accession="MS:1000511" name="ms level" value="2"/>
 <precursorList count="1"><precursor><selectedIonList count="1"><selectedIon>
  <cvParam accession="MS:1000744" name="selected ion m/z" value="500.5"/>
  <cvParam accession="MS:1000042" name="peak intensity" value="12345.6"/>
 </selectedIon></selectedIonList></precursor></precursorList>
</spectrum></spectrumList></run></mzML>"""
fh = tempfile.NamedTemporaryFile("w", suffix=".mzML", delete=False)
fh.write(MZML); fh.close()
inten = scan_intensities(fh.name)
check("intensity keyed by scan", inten.get(42) is not None)
check("intensity value", abs(inten.get(42, 0) - 12345.6) < 1e-6)

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_amel_intensity.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'amel_intensity'`

- [ ] **Step 3: Append the DDL**

```sql
-- One sex call per de novo run. The INPUTS are stored, not just the verdict, so a call can be
-- re-derived when the model is recalibrated without re-reading any raw data.
CREATE TABLE IF NOT EXISTS delimp_denovo_sex_call (
    run_id            uuid PRIMARY KEY REFERENCES delimp_denovo_run(run_id) ON DELETE CASCADE,
    call              text NOT NULL CHECK (call IN ('MALE','FEMALE','INCONCLUSIVE')),
    amelx_cov_pct     real,
    amely_cov_pct     real,
    n_x_sites         smallint,
    n_y_sites         smallint,
    n_insertion_peptides smallint,
    amelx_intensity   double precision,   -- combined precursor intensity, AMELX-specific peptides
    amely_intensity   double precision,
    enamel_mg         real,               -- NULL unless the wet-lab mass was recorded
    -- Only meaningful when enamel_mg is present: the Parker 2019 logistic takes CI/mg.
    pr_female         real,
    -- Mass-independent, so comparable to the published 8.54% +/- 6.35% male range even without
    -- enamel_mg. This is the number that validates a de novo pipeline against the paper.
    amely_amelx_ratio real,
    model_note        text,
    called_at         timestamptz NOT NULL DEFAULT now()
);

COMMENT ON COLUMN delimp_denovo_sex_call.pr_female IS
    'Parker et al. 2019 (J Archaeol Sci 101:169-180) sec 3.4 logistic, applicable ONLY when AMELY '
    'is absent AND enamel_mg is known. Coefficients are fitted to CI/mg from a PEAKS database '
    'search and must be refit before being trusted on de novo intensities.';
```

- [ ] **Step 4: Write the implementation**

```python
"""AMELX/AMELY combined ion intensity, and the probability of female sex.

WHY. Parker et al. 2019 estimate FEMALE as a probability rather than as "AMELY absent", because
absence of AMELY is also what a low-signal MALE looks like. Their model needs a QUANTITY --
combined peak ion intensity per protein -- which a Casanovo mzTab does not carry. This supplies it
by going back to the mzML and summing the precursor peak intensity of every spectrum whose de novo
peptide is SPECIFIC to AMELX or to AMELY.

SPECIFICITY IS THE WHOLE PROBLEM. AMELX and AMELY are ~90% identical, so most amelogenin peptides
map to both and carry no sex information. A peptide counts here only when it places a residue at a
discriminating position or lands inside an AMELY-only insertion. Everything else is tallied as
"shared" and never fed to the model.

WHAT THIS IS NOT. Parker 2019 normalises to enamel mass (CI/mg) and quantifies from a PEAKS search
at 1% FDR. Without the mass and the FDR the ABSOLUTE numbers are not on the published scale and the
published coefficients must NOT be applied to them. What IS directly comparable is the RATIO Y/X,
which is mass-independent: the paper reports 8.54% +/- 6.35% in males. Measured on two teeth
samples 2026-09-08: 5.60% (ancient) and 9.58% (modern) -- both inside that range, from a de novo
pipeline. That agreement is the bridge that would justify a refit.
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET

# Parker et al. 2019 sec 3.4, for AMELY-negative samples only.
B0, B1 = -12.8622, 0.7496
AMELX_90PCT_FEMALE = 5.31e8          # CI/mg above which Pr(F) > 0.90

_SCAN = re.compile(r"scan=(\d+)")
_MZML_NS = "{http://psi.hupo.org/ms/mzml}"


def pr_female(amelx_ci_per_mg: float) -> float:
    """Probability of female sex GIVEN no AMELY detected. Caller must check that first."""
    lo = B0 + B1 * math.log(max(0.0, amelx_ci_per_mg) + 1.0)
    return 1.0 / (1.0 + math.exp(-lo))


def scan_intensities(mzml_path: str) -> dict[int, float]:
    """scan -> precursor peak intensity (cvParam MS:1000042), streamed.

    iterparse with el.clear(): these files are ~500 MB and must not be loaded whole.
    """
    out: dict[int, float] = {}
    cur_scan, inten = None, None
    for ev, el in ET.iterparse(mzml_path, events=("start", "end")):
        tag = el.tag.replace(_MZML_NS, "")
        if ev == "start" and tag == "spectrum":
            m = _SCAN.search(el.get("id") or "")
            cur_scan, inten = (int(m.group(1)) if m else None), None
        elif ev == "end":
            if tag == "cvParam" and el.get("accession") == "MS:1000042":
                try:
                    inten = float(el.get("value"))
                except (TypeError, ValueError):
                    pass
            elif tag == "spectrum":
                if cur_scan is not None and inten is not None:
                    out[cur_scan] = inten
                el.clear()
    return out
```

Add `combined_intensity(psms, amel, intensities)` alongside, classifying each PSM's peptide as
`X` / `Y` / `shared` with the same site logic as Task 4 and summing intensities per class.

- [ ] **Step 5: Run test to verify it passes**

Run: `python tests/test_amel_intensity.py`
Expected: PASS, `0 failure(s)`

- [ ] **Step 6: Verify the ratio against the paper**

Run the extractor over the two male teeth samples.
Expected: `28_62` ≈ **5.60%**, `T12` ≈ **9.58%** — both inside Parker 2019's 8.54% ± 6.35%.
A ratio far outside that range means the specificity logic has regressed.

- [ ] **Step 7: Commit**

```bash
git add ingest/amel_intensity.py schema/denovo.sql tests/test_amel_intensity.py
git commit -m "ingest: amelogenin intensity, the Y:X ratio, and the Parker 2019 logistic"
```

---

### Task 6: The forensic read layer and UI

**Files:**
- Modify: `app/denovo.py`, `app/db.py`, `app/main.py`, `app/static/app.js`
- Test: `tests/test_denovo_gvp_api.py`

**Interfaces:**
- Produces: `gvp_candidates(...)`, `sex_calls(cohort=None)`; routes `GET /api/denovo/gvp`,
  `GET /api/denovo/sex`; SPA route `#/denovo/gvp`.

- [ ] **Step 1: Write the failing test**

```python
"""The GVP query surface: allowlisting, and filters that do not silently drop candidates."""
import os, re, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import db  # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

for t in ("delimp_denovo_gvp", "delimp_variant_annotation", "delimp_denovo_sex_call"):
    check(f"{t} allowlisted", t in db.PUBLIC_TABLES)

src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "denovo.py")).read()
check("gvp_candidates defined", "def gvp_candidates(" in src)
check("sex_calls defined", "def sex_calls(" in src)
check("filters on the controls", "one_nt_reachable" in src and "is_deamidation_shaped" in src)
check("no interpolated SQL", not re.search(r'(?:execute|query)\(\s*f["\']', src))
# The controls must default to SHOWING everything with its verdicts, not pre-filtering.
check("controls are optional filters, not hardcoded",
      "one_nt_reachable = true" not in src.lower().replace("%(", ""))

from app.main import app  # noqa: E402
paths = {r.path for r in app.routes}
check("/api/denovo/gvp registered", "/api/denovo/gvp" in paths)
check("/api/denovo/sex registered", "/api/denovo/sex" in paths)

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_denovo_gvp_api.py`
Expected: FAIL — `delimp_denovo_gvp allowlisted`

- [ ] **Step 3: Add the tables, queries, routes and view**

Add to `PUBLIC_TABLES`:

```python
        "delimp_denovo_gvp",
        "delimp_variant_annotation",
        "delimp_denovo_sex_call",
```

Add to `app/denovo.py`:

```python
def gvp_candidates(cohort: str | None = None, aa_from: str | None = None,
                   aa_to: str | None = None, interior_only: bool = False,
                   exclude_deamidation: bool = False, min_site_score: float | None = None,
                   with_rsid: bool = False, limit: int = 200, offset: int = 0) -> dict[str, Any]:
    """Variant candidates across every de novo run.

    The three controls are OPTIONAL FILTERS, not defaults: every candidate is returned with its
    verdicts so the caller can see what was excluded and why. Defaulting them on would hide the
    deamidation-shaped calls rather than labelling them.
    """
    lim, off = _page(limit, offset)
    params: dict[str, Any] = {"lim": lim, "off": off}
    w = ["1=1"]
    if cohort:
        w.append("r.cohort = %(cohort)s"); params["cohort"] = cohort
    if aa_from:
        w.append("g.aa_from = %(aa_from)s"); params["aa_from"] = aa_from.upper()[:1]
    if aa_to:
        w.append("g.aa_to = %(aa_to)s"); params["aa_to"] = aa_to.upper()[:1]
    if interior_only:
        w.append("NOT g.is_terminal")
    if exclude_deamidation:
        w.append("NOT g.is_deamidation_shaped")
    if min_site_score is not None:
        w.append("g.aa_score_at_site >= %(mss)s"); params["mss"] = float(min_site_score)
    if with_rsid:
        w.append("v.rsid IS NOT NULL")
    where = " AND ".join(w)
    rows = query(
        f"""SELECT g.stripped_seq, g.corpus_stripped_seq, g.position, g.aa_from, g.aa_to,
                   g.mass_delta, g.one_nt_reachable, g.is_isobaric, g.is_deamidation_shaped,
                   g.is_terminal, g.aa_score_at_site, g.aa_score_flank_mean, g.n_psms,
                   r.run_id, r.run_name, r.cohort, r.donor_id, r.sample_role,
                   v.rsid, v.maf, v.maf_source, v.clinical_significance
              FROM delimp_denovo_gvp g
              JOIN delimp_denovo_run r ON r.run_id = g.run_id
              LEFT JOIN delimp_variant_annotation v
                     ON v.aa_wt = g.aa_from AND v.aa_alt = g.aa_to
             WHERE {where}
             ORDER BY (v.rsid IS NOT NULL) DESC, g.n_psms DESC, g.stripped_seq
             LIMIT %(lim)s OFFSET %(off)s""",
        params, tables=["delimp_denovo_gvp", "delimp_denovo_run", "delimp_variant_annotation"])
    total = query(
        f"""SELECT count(*) FROM delimp_denovo_gvp g
              JOIN delimp_denovo_run r ON r.run_id = g.run_id
              LEFT JOIN delimp_variant_annotation v
                     ON v.aa_wt = g.aa_from AND v.aa_alt = g.aa_to
             WHERE {where}""",
        params, tables=["delimp_denovo_gvp", "delimp_denovo_run", "delimp_variant_annotation"],
        fetch="val")
    return {"rows": rows, "total": total}


def sex_calls(cohort: str | None = None) -> dict[str, Any]:
    where, params = "", {}
    if cohort:
        where, params["cohort"] = "WHERE r.cohort = %(cohort)s", cohort
    rows = query(
        f"""SELECT r.run_name, r.cohort, r.donor_id, r.sample_role, s.call, s.amelx_cov_pct,
                   s.n_x_sites, s.n_y_sites, s.n_insertion_peptides, s.amely_amelx_ratio,
                   s.pr_female, s.enamel_mg, s.model_note
              FROM delimp_denovo_sex_call s
              JOIN delimp_denovo_run r ON r.run_id = s.run_id {where}
             ORDER BY r.run_name""",
        params, tables=["delimp_denovo_sex_call", "delimp_denovo_run"])
    return {"rows": rows}
```

Add to `app/main.py`:

```python
@app.get("/api/denovo/gvp")
def api_denovo_gvp(cohort: str | None = None, aa_from: str | None = None,
                   aa_to: str | None = None, interior_only: bool = False,
                   exclude_deamidation: bool = False, min_site_score: float | None = None,
                   with_rsid: bool = False, limit: int = 200, offset: int = 0):
    return denovo.gvp_candidates(cohort, aa_from, aa_to, interior_only, exclude_deamidation,
                                 min_site_score, with_rsid, limit, offset)


@app.get("/api/denovo/sex")
def api_denovo_sex(cohort: str | None = None):
    return denovo.sex_calls(cohort)
```

Add a `#/denovo/gvp` SPA view rendering one row per candidate:
`peptide · corpus peptide · Q→E @ 9 · site score · rsID · MAF · significance`, with toggles for
`interior_only`, `exclude_deamidation` and `with_rsid`, and each control shown as a chip so an
excluded candidate is visibly excluded rather than absent.

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_denovo_gvp_api.py` then `python scripts/predeploy_check.py`
Expected: both PASS

- [ ] **Step 5: Commit**

```bash
git add app/denovo.py app/db.py app/main.py app/static/app.js tests/test_denovo_gvp_api.py
git commit -m "app: the variant-hunt surface, with controls as visible filters"
```

---

## Deferred beyond Phase 2

- **MCP tools** (`search_denovo_peptides`, `find_gvp_candidates`, `compare_denovo_runs`) — Phase 3.
  `app/mcp_server.py` already exists and inherits the allowlist, so this is additive.
- **Homology + LCA ingest** from a full `delimp_denovo_v1` bundle (DIAMOND hits, species calls).
- **Peptidoform abundance ratio** — the machinery is generic and can be built from
  `amel_intensity.scan_intensities`; the interpretation is a collaborator's unpublished work and
  must not be written into either public repo. See the gitignored working document.

## Open questions that block validation, not implementation

- **Enamel masses** for the teeth samples. Without them `pr_female` cannot be computed on the
  published scale, and the Y:X ratio is the only comparable number.
- **Known sex** for a calibration set. Without it the logistic cannot be refit for de novo input,
  and a FEMALE call near the coverage floor stays unsupported.
- **RR (6), JE (3) and the 32 unattributable runs** — ingest as `sample_role='unknown'` until
  someone who knows the naming says otherwise. A validation set with unknown donors in it is not
  quite a validation set.
