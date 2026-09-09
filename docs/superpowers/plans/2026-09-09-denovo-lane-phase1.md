# De novo lane Phase 1 — the corpus link

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land de novo sequencing results in the FRAN corpus so every de novo peptide links into
FRAN's existing peptide page when the corpus knows it.

**Architecture:** DE-LIMP owns the write path — an ingestor reads a Casanovo mzTab, computes the
corpus join in Python against a one-time dump of corpus peptides, and writes the `delimp_denovo_*`
tables directly (the PG Farm service account owns all public tables, so it can DDL). FRAN owns the
read side only: schema DDL, an `app/denovo.py` query module, three endpoints, one SPA view.

**Tech Stack:** Python 3.11, psycopg2, PostgreSQL (PG Farm), FastAPI, vanilla-JS SPA.
Tests are plain scripts run as `python tests/test_x.py` — this repo does **not** use pytest.

**Spec:** `docs/superpowers/specs/2026-09-08-denovo-homology-lane-design.md`

**This is Phase 1 of 3.** It is infrastructure, not the payload. Phase 2 is GVP detection + dbSNP;
Phase 3 is amelogenin sex estimation. Both depend on Tasks 1–6 here.

## Global Constraints

- **Tests are plain Python scripts**, no pytest. Copy the shape of `tests/test_federation_boundary.py`:
  a `check(name, cond, detail)` helper, a module-level `FAILS` list, `sys.exit(1)` if non-empty.
- **Every FRAN query names its `tables=[...]`** so `db.query()` enforces the allowlist. A table not
  in `app/db.py PUBLIC_TABLES` is unreachable, and `scripts/predeploy_check.py` fails the build if a
  query names one that is missing.
- **`%s` / `%(name)s` placeholders only.** Never interpolate input into SQL.
- **I/L normalisation on every cross-modality join.** `seq_il = replace(seq,'I','L')`. Exact
  matching finds 16.3% of crane de novo peptides; I/L-normalised finds 32.8%. Half the signal.
- **Never threshold on `peptide_score`.** It is the product of per-residue scores (≈ p^L), so it
  filters length, not quality. Store it only alongside `length`; compare with
  `conf_geomean = peptide_score ** (1/length)`.
- **Determinism:** sort any candidate set before choosing from it, or counts drift between runs
  (measured: 1,139 vs 1,142, from Python's per-run hash seed).
- **Identity comes from SEARCHING the run name, never positional parsing.** Four attempts at
  positional parsing gave four different wrong answers. `\b` is unsafe in filenames — `_` is a word
  character.
- **`sample_role` defaults to `'unknown'`, never `'donor'`.** Folder membership does not imply donor
  status: a bovine BSA standard sits in the hair cohort with 7,844 PSMs and normal confidence.

---

### Task 1: Schema

**Files:**
- Create: `schema/denovo.sql`
- Test: `tests/test_denovo_schema.py`

**Interfaces:**
- Produces: tables `delimp_denovo_run`, `delimp_denovo_peptide`, `delimp_denovo_psm`,
  `delimp_denovo_corpus_match`; index `idx_consensus_seq_il`.

- [ ] **Step 1: Write the failing test**

```python
"""The de novo schema, asserted rather than eyeballed. Parses the DDL text; needs no DB."""
import os, re, sys
HERE = os.path.dirname(os.path.abspath(__file__))
SQL = open(os.path.join(HERE, "..", "schema", "denovo.sql")).read()

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

for t in ("delimp_denovo_run", "delimp_denovo_peptide", "delimp_denovo_psm",
          "delimp_denovo_corpus_match"):
    check(f"{t} created idempotently", f"CREATE TABLE IF NOT EXISTS {t}" in SQL)

pep = re.search(r"CREATE TABLE IF NOT EXISTS delimp_denovo_peptide \((.*?)\n\);", SQL, re.S)
check("peptide table parses", pep is not None)
if pep:
    body = pep.group(1)
    check("peptide_score stored WITH length", "peptide_score" in body and "length" in body)
    check("conf_geomean stored", "conf_geomean" in body)
    check("seq_il stored", "seq_il" in body)
    check("corpus_class stored", "corpus_class" in body)

check("I/L expression index on the corpus side",
      "idx_consensus_seq_il" in SQL and "replace(stripped_seq" in SQL)
check("sample_role defaults to unknown", "sample_role" in SQL and "'unknown'" in SQL)
check("replicate_kind distinguishes the three kinds", "replicate_kind" in SQL)
check("no DROP statements", "DROP TABLE" not in SQL.upper())

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_denovo_schema.py`
Expected: FAIL — `FileNotFoundError: schema/denovo.sql`

- [ ] **Step 3: Write the DDL**

```sql
-- FRAN de novo lane. Apply AFTER schema/fran_schema.sql:
--     psql "$FRAN_DB_URL" -f schema/denovo.sql
-- Additive and idempotent. Written by the DE-LIMP ingestor; FRAN only reads.

-- One row per de novo run. Deliberately NOT delimp_searches: a de novo run has no protein groups,
-- no q-values and no FASTA, and would be mostly NULL there.
CREATE TABLE IF NOT EXISTS delimp_denovo_run (
    run_id            uuid PRIMARY KEY,
    run_name          text NOT NULL,
    cohort            text,
    source_raw        text,
    denovo_engine     text NOT NULL,
    engine_version    text NOT NULL,
    weights           text,
    gpu_arch          text,
    bundle_format     text NOT NULL,
    donor_id          text,
    sample_role       text NOT NULL DEFAULT 'unknown'
                      CHECK (sample_role IN ('donor','standard','control','unknown')),
    replicate_of      uuid REFERENCES delimp_denovo_run(run_id),
    -- Three phenomena, three downstream behaviours: prefer the good run / average the technical
    -- pair / treat as independent batch corroboration. One flag would flatten them.
    replicate_kind    text CHECK (replicate_kind IN
                      ('failed_reacquisition','technical','cross_batch')),
    n_psms            integer NOT NULL,
    n_peptides        integer NOT NULL,
    len_median        integer,
    conf_geomean_median real,
    ingest_flag       text,
    ingested_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_denovo_run_donor  ON delimp_denovo_run (donor_id);
CREATE INDEX IF NOT EXISTS idx_denovo_run_cohort ON delimp_denovo_run (cohort);

CREATE TABLE IF NOT EXISTS delimp_denovo_peptide (
    run_id        uuid NOT NULL REFERENCES delimp_denovo_run(run_id) ON DELETE CASCADE,
    stripped_seq  text NOT NULL,
    seq_il        text NOT NULL,
    length        smallint NOT NULL,
    n_psms        integer NOT NULL,
    peptide_score real,
    conf_geomean  real,
    corpus_class  text NOT NULL DEFAULT 'novel'
                  CHECK (corpus_class IN ('conserved','gvp','novel')),
    PRIMARY KEY (run_id, stripped_seq)
);
-- seq_il leads, not run_id: cross-run queries are the point, per-run is the special case.
CREATE INDEX IF NOT EXISTS idx_denovo_pep_seqil ON delimp_denovo_peptide (seq_il);
CREATE INDEX IF NOT EXISTS idx_denovo_pep_class ON delimp_denovo_peptide (corpus_class);

CREATE TABLE IF NOT EXISTS delimp_denovo_psm (
    run_id        uuid NOT NULL REFERENCES delimp_denovo_run(run_id) ON DELETE CASCADE,
    scan          integer NOT NULL,
    stripped_seq  text NOT NULL,
    seq_il        text NOT NULL,
    sequence      text,              -- WITH modifications: N+0.984 vs a plain D is unrecoverable
    charge        smallint,
    exp_mz        real,
    peptide_score real,
    aa_scores     real[],            -- per-residue; Phase 2 reads these at the variant site
    PRIMARY KEY (run_id, scan)
);
CREATE INDEX IF NOT EXISTS idx_denovo_psm_seqil ON delimp_denovo_psm (seq_il);

-- A JOIN TABLE: the I/L collapse is one-to-many in reverse, so one de novo peptide can match
-- several real corpus spellings (8.2% of crane matches did).
CREATE TABLE IF NOT EXISTS delimp_denovo_corpus_match (
    run_id              uuid NOT NULL REFERENCES delimp_denovo_run(run_id) ON DELETE CASCADE,
    stripped_seq        text NOT NULL,
    corpus_stripped_seq text NOT NULL,
    match_kind          text NOT NULL CHECK (match_kind IN ('exact','il')),
    n_candidates        smallint NOT NULL,
    corpus_n_obs        bigint,
    corpus_n_searches   integer,
    corpus_irt_mean     double precision,
    corpus_im_mean      double precision,
    PRIMARY KEY (run_id, stripped_seq, corpus_stripped_seq)
);
CREATE INDEX IF NOT EXISTS idx_denovo_match_corpus
    ON delimp_denovo_corpus_match (corpus_stripped_seq);

-- The corpus-side half of the I/L key. An EXPRESSION index avoids adding a column, a writer and a
-- maintenance job to a table FRAN already owns. On delimp_peptide_consensus (2.93M rows, 1,291 MB)
-- and deliberately NOT on delimp_precursors (485M rows) -- tens of GB for no extra reach.
CREATE INDEX IF NOT EXISTS idx_consensus_seq_il
    ON delimp_peptide_consensus (replace(stripped_seq, 'I', 'L'));

COMMENT ON COLUMN delimp_denovo_peptide.peptide_score IS
    'Casanovo peptide score = PRODUCT of per-residue scores, so it is ~p^L and filters LENGTH, not '
    'quality. Never threshold on it. Compare with conf_geomean.';
COMMENT ON COLUMN delimp_denovo_run.sample_role IS
    'Defaults to unknown. Folder membership does not imply donor status: a bovine BSA standard sits '
    'in the hair cohort with 7,844 PSMs and normal confidence, invisible to any yield check.';
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_denovo_schema.py`
Expected: PASS, `0 failure(s)`

- [ ] **Step 5: Commit**

```bash
git add schema/denovo.sql tests/test_denovo_schema.py
git commit -m "schema: de novo lane tables and the corpus-side I/L expression index"
```

---

### Task 2: Run-name identity

**Files:**
- Create: `ingest/denovo_identity.py`
- Test: `tests/test_denovo_identity.py`

**Interfaces:**
- Produces: `parse_identity(run_name: str) -> dict` with keys `donor_id` (str|None),
  `sample_role` (str), `raw_token` (str|None).

Its own task because four independent attempts produced four different wrong answers.

- [ ] **Step 1: Write the failing test**

```python
"""Donor identity from a run name. Every case here is a real filename that broke an attempt."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingest"))
from denovo_identity import parse_identity  # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

donor = lambda r: parse_identity(r)["donor_id"]
role  = lambda r: parse_identity(r)["sample_role"]

check("grammar 1 plain ZG",   donor("QEPlus2_03162018_36_ZG171") == "ZG171")
check("grammar 2 ZG_ underscore", donor("QEPlus2_03262019_54_ZG_313") == "ZG313")
check("grammar 3 no position field", donor("QEPlus2_08162019_ZG377") == "ZG377")
check("grammar 4 the 2G typo", donor("QEPlus2_02162017_10_2G26") == "ZG26")
check("2G after underscore (\\b is unsafe here)", donor("QEplus2_02162017_10_2G11") == "ZG11")
check("case variants",        donor("QEPlus2_01012018_10_Zg99") == "ZG99")
check("RDIL suffix",          donor("QEPlus2_03162018_99_ZG171_RDIL") == "ZG171")
check("timestamp suffix",     donor("QEplus2_08152017_86_ZG125_170821145253") == "ZG125")

check("BSA is a standard",    role("QEPlus2_02162017_52_BSA") == "standard")
check("Pooled is a standard", role("QEPlus2_02162018_78_Pooled100Femto") == "standard")
check("ZG is a donor",        role("QEPlus2_03162018_36_ZG171") == "donor")
check("RR is unknown",        role("QEplus2_09172020_28_RR1") == "unknown")
check("JE is unknown",        role("QEPlus2_02162017_46_JE1") == "unknown")
check("numeric is unknown",   role("QEPlus2_01042017_27_155") == "unknown")
check("numeric has no donor", donor("QEPlus2_01042017_27_155") is None)

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_denovo_identity.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'denovo_identity'`

- [ ] **Step 3: Write the implementation**

```python
"""Donor identity from a de novo run name.

SEARCH the name for a donor token; never parse by position. The Parker hair cohort has at least
four filename grammars and four independent positional-parsing attempts each produced a different
wrong count (353 / 337 / 334 / 318 ZG runs). Two traps:
  * `\\b` is not a token boundary here -- `_` is a word character, so `\\b2G` never matches
    `_10_2G26` and silently drops all 25 of those runs.
  * `2G` is NOT a separate donor series. It is ZG11-35: contiguous, with ZERO number collisions
    against ZG36-463. That zero-collision result is what licenses folding it into ZG. If they were
    different donors, folding would merge two people in a validation set -- worse than a broken
    query.
"""
from __future__ import annotations

import re

_ZG = re.compile(r"(?:^|_)([Zz][Gg]|2[Gg])_?(\d+)")
_OTHER_INITIALS = re.compile(r"(?:^|_)([A-Z]{2})(\d+)(?:_|$)")
_STANDARD = re.compile(r"BSA|Pooled|Femto|HeLa|iRT", re.I)


def parse_identity(run_name: str) -> dict:
    """-> {'donor_id': str|None, 'sample_role': str, 'raw_token': str|None}"""
    if _STANDARD.search(run_name):
        return {"donor_id": None, "sample_role": "standard", "raw_token": None}
    m = _ZG.search(run_name)
    if m:
        return {"donor_id": f"ZG{int(m.group(2))}", "sample_role": "donor",
                "raw_token": m.group(0).lstrip("_")}
    m = _OTHER_INITIALS.search(run_name)
    if m:
        return {"donor_id": None, "sample_role": "unknown",
                "raw_token": f"{m.group(1)}{m.group(2)}"}
    return {"donor_id": None, "sample_role": "unknown", "raw_token": None}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_denovo_identity.py`
Expected: PASS, `0 failure(s)`

- [ ] **Step 5: Verify against the real cohort**

Run:
```bash
scp ingest/denovo_identity.py hive:/quobyte/proteomics-grp/brett/zach_hair_denovo/
ssh hive "cd /quobyte/proteomics-grp/brett/zach_hair_denovo && python3 -c \"
import csv, collections
from denovo_identity import parse_identity
print(collections.Counter(parse_identity(r['run'])['sample_role']
      for r in csv.DictReader(open('manifest.tsv'), delimiter='\t')))\""
```
Expected: `donor` **378**, `standard` **2**, `unknown` **41**. Total 421.
If `donor` is not exactly 378 there is a fifth grammar — stop and investigate, do not proceed.

- [ ] **Step 6: Commit**

```bash
git add ingest/denovo_identity.py tests/test_denovo_identity.py
git commit -m "ingest: donor identity by searching the run name, never by position"
```

---

### Task 3: Corpus matching

**Files:**
- Create: `ingest/denovo_corpus_match.py`
- Test: `tests/test_denovo_corpus_match.py`

**Interfaces:**
- Produces: `il(seq) -> str`; `match_peptides(peptides, corpus) -> list[dict]` with keys
  `stripped_seq`, `corpus_stripped_seq`, `match_kind`, `n_candidates`.

- [ ] **Step 1: Write the failing test**

```python
"""I/L normalisation, and the one-to-many the join table exists for."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingest"))
from denovo_corpus_match import il, match_peptides  # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

check("I->L", il("AIIEVLGR") == "ALLEVLGR")
check("already L unchanged", il("ALLEVLGR") == "ALLEVLGR")

corpus = {"ALLEVLGR": ["AIIEVLGR", "ALIEVLGR", "ALLEVLGR"], "PEPTLDEK": ["PEPTIDEK"]}
rows = match_peptides(["ALLEVLGR"], corpus)
check("one-to-many yields 3 rows", len(rows) == 3, f"got {len(rows)}")
check("n_candidates recorded", all(r["n_candidates"] == 3 for r in rows))
check("own spelling marked exact",
      any(r["corpus_stripped_seq"] == "ALLEVLGR" and r["match_kind"] == "exact" for r in rows))
check("others marked il",
      any(r["corpus_stripped_seq"] == "AIIEVLGR" and r["match_kind"] == "il" for r in rows))

r1 = [r["corpus_stripped_seq"] for r in match_peptides(["ALLEVLGR"], corpus)]
r2 = [r["corpus_stripped_seq"] for r in match_peptides(["ALLEVLGR"], corpus)]
check("deterministic and sorted", r1 == r2 == sorted(r1))

check("no match yields no rows", match_peptides(["WWWWWWWW"], corpus) == [])
check("I/L reach", len(match_peptides(["PEPTIDEK"], corpus)) == 1)

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_denovo_corpus_match.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'denovo_corpus_match'`

- [ ] **Step 3: Write the implementation**

```python
"""Match de novo peptides against the FRAN corpus on an I/L-normalised key.

De novo sequencing cannot distinguish isoleucine from leucine, so the join must be I/L-normalised:
exact matching finds 16.3% of the crane peptides, I/L-normalised finds 32.8%. Half the signal is in
the normalisation.

The result is one row per (de novo peptide x matched corpus spelling), because the collapse is
one-to-many in reverse: ALLEVLGR matches the real corpus peptides AIIEVLGR, ALIEVLGR and ALLEVLGR.
8.2% of crane matches were ambiguous like this. Showing one and hiding the rest would misrepresent
what de novo can actually distinguish.
"""
from __future__ import annotations

_IL = str.maketrans("I", "L")


def il(seq: str) -> str:
    return seq.upper().translate(_IL)


def match_peptides(peptides: list[str], corpus: dict[str, list[str]]) -> list[dict]:
    """corpus maps an I/L-normalised key -> the real corpus spellings under it.

    Candidates are SORTED before emission: unsorted set iteration makes output depend on Python's
    per-run hash seed, which drifted a downstream count by 3 between runs.
    """
    rows = []
    for pep in peptides:
        hits = corpus.get(il(pep))
        if not hits:
            continue
        for spelling in sorted(hits):
            rows.append({"stripped_seq": pep, "corpus_stripped_seq": spelling,
                         "match_kind": "exact" if spelling == pep else "il",
                         "n_candidates": len(hits)})
    return rows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_denovo_corpus_match.py`
Expected: PASS, `0 failure(s)`

- [ ] **Step 5: Commit**

```bash
git add ingest/denovo_corpus_match.py tests/test_denovo_corpus_match.py
git commit -m "ingest: I/L-normalised corpus matching as a one-to-many join"
```

---

### Task 4: mzTab reader

**Files:**
- Create: `ingest/denovo_mztab.py`
- Test: `tests/test_denovo_mztab.py`

**Interfaces:**
- Consumes: `il()` from Task 3.
- Produces: `read_mztab(path) -> {'engine_version': str|None, 'psms': list[dict]}`, each PSM with
  `scan`, `sequence`, `stripped_seq`, `seq_il`, `charge`, `exp_mz`, `peptide_score`, `aa_scores`.

- [ ] **Step 1: Write the failing test**

```python
"""mzTab reading across BOTH casanovo column generations.

Casanovo 5.2.0 renamed the mzTab columns. A reader that knows only the 5.1 names returns ZERO
peptides from a 5.2 file WITHOUT erroring -- indistinguishable from an empty sample. That silent
zero is the failure this test exists to prevent.
"""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingest"))
from denovo_mztab import read_mztab  # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

V51 = ("MTD\tsoftware[1]\t[MS, MS:1003281, Casanovo, 5.1.2]\n"
       "PSH\tsequence\tPSM_ID\tsearch_engine_score[1]\tcharge\texp_mass_to_charge\t"
       "spectra_ref\topt_ms_run[1]_aa_scores\n"
       "PSM\tPEPTIDEK\t1\t0.9\t2\t500.5\tms_run[1]:controllerType=0 controllerNumber=1 scan=42\t"
       "0.9,0.9,0.9,0.9,0.9,0.9,0.9,0.9\n")
V52 = ("MTD\tsoftware[1]\t[MS, MS:1003281, Casanovo, 5.2.1]\n"
       "PSH\topt_global_cv_MS:1003169_proforma_peptidoform_sequence\tPSM_ID\t"
       "search_engine_score[1]\tcharge\texp_mass_to_charge\tspectra_ref\topt_global_aa_scores\n"
       "PSM\tPEPTIDEK\t1\t0.9\t2\t500.5\tms_run[1]:controllerType=0 controllerNumber=1 scan=42\t"
       "0.9,0.9,0.9,0.9,0.9,0.9,0.9,0.9\n")

def write(text):
    fh = tempfile.NamedTemporaryFile("w", suffix=".mztab", delete=False)
    fh.write(text); fh.close(); return fh.name

for label, text, ver in (("5.1.x", V51, "5.1.2"), ("5.2.x", V52, "5.2.1")):
    d = read_mztab(write(text))
    check(f"{label}: one PSM", len(d["psms"]) == 1, f"got {len(d['psms'])}")
    if d["psms"]:
        p = d["psms"][0]
        check(f"{label}: sequence", p["stripped_seq"] == "PEPTIDEK")
        check(f"{label}: seq_il", p["seq_il"] == "PEPTLDEK")
        check(f"{label}: scan from spectra_ref", p["scan"] == 42)
        check(f"{label}: aa_scores parsed", len(p["aa_scores"]) == 8)
    check(f"{label}: engine version", d["engine_version"] == ver)

MOD = V52.replace("PEPTIDEK", "PEPTN+0.984DEK")
d = read_mztab(write(MOD))
check("mods stripped from the key", d["psms"][0]["stripped_seq"] == "PEPTNDEK")
check("raw sequence preserved", "+0.984" in d["psms"][0]["sequence"])

BAD = V52.replace("opt_global_cv_MS:1003169_proforma_peptidoform_sequence", "mystery")
try:
    read_mztab(write(BAD)); check("unknown column raises", False, "returned instead of raising")
except ValueError:
    check("unknown column raises", True)

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_denovo_mztab.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'denovo_mztab'`

- [ ] **Step 3: Write the implementation**

```python
"""Read a Casanovo mzTab, across both column generations.

Casanovo 5.2.0 renamed the columns to the spec:
    sequence                -> opt_global_cv_MS:1003169_proforma_peptidoform_sequence
    opt_ms_run[1]_aa_scores -> opt_global_aa_scores
A reader that knows only the old names returns zero peptides from a 5.2 file without erroring,
which is indistinguishable from an empty sample. So accept both and RAISE when neither is present.

The raw `sequence` is kept WITH its modifications: N+0.984 versus a plain D is a distinction no
mass can recover later, and Phase 2 needs it.
"""
from __future__ import annotations

import re

from denovo_corpus_match import il

_MOD = re.compile(r"[\[(][^\])]*[\])]|[+-]\d+\.\d+")
_SCAN = re.compile(r"scan=(\d+)")
_VER = re.compile(r"Casanovo,\s*([0-9][^\]\s]*)")

SEQ_COLS = ("sequence", "opt_global_cv_MS:1003169_proforma_peptidoform_sequence")
AA_COLS = ("opt_ms_run[1]_aa_scores", "opt_global_aa_scores")


def _pick(header, names):
    for n in names:
        if n in header:
            return header.index(n)
    return None


def read_mztab(path: str) -> dict:
    psms, version = [], None
    seq_i = aa_i = sc_i = ch_i = mz_i = ref_i = None
    with open(path) as fh:
        for line in fh:
            if line.startswith("MTD"):
                m = _VER.search(line)
                if m:
                    version = m.group(1)
            elif line.startswith("PSH"):
                h = line.rstrip("\n").split("\t")
                seq_i = _pick(h, SEQ_COLS)
                if seq_i is None:
                    raise ValueError(
                        f"{path}: no recognised sequence column. Looked for {SEQ_COLS}; found "
                        f"{h[:12]}. A casanovo >=5.2 file needs the opt_global_cv name.")
                aa_i = _pick(h, AA_COLS)
                sc_i = h.index("search_engine_score[1]") if "search_engine_score[1]" in h else None
                ch_i = h.index("charge") if "charge" in h else None
                mz_i = h.index("exp_mass_to_charge") if "exp_mass_to_charge" in h else None
                ref_i = h.index("spectra_ref") if "spectra_ref" in h else None
            elif line.startswith("PSM") and seq_i is not None:
                f = line.rstrip("\n").split("\t")
                if len(f) <= seq_i:
                    continue

                def _f(i):
                    try:
                        return float(f[i])
                    except (TypeError, ValueError, IndexError):
                        return None

                scan = None
                if ref_i is not None and len(f) > ref_i:
                    m = _SCAN.search(f[ref_i])
                    scan = int(m.group(1)) if m else None
                aa = []
                if aa_i is not None and len(f) > aa_i and f[aa_i] not in ("null", ""):
                    try:
                        aa = [float(x) for x in f[aa_i].split(",") if x]
                    except ValueError:
                        aa = []
                raw = f[seq_i]
                stripped = _MOD.sub("", raw).upper()
                ch = _f(ch_i) if ch_i is not None else None
                psms.append({"scan": scan, "sequence": raw, "stripped_seq": stripped,
                             "seq_il": il(stripped), "charge": int(ch) if ch else None,
                             "exp_mz": _f(mz_i) if mz_i is not None else None,
                             "peptide_score": _f(sc_i) if sc_i is not None else None,
                             "aa_scores": aa})
    if seq_i is None:
        raise ValueError(f"{path}: no PSH header line found")
    return {"engine_version": version, "psms": psms}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_denovo_mztab.py`
Expected: PASS, `0 failure(s)`

- [ ] **Step 5: Commit**

```bash
git add ingest/denovo_mztab.py tests/test_denovo_mztab.py
git commit -m "ingest: read casanovo mzTab across both column generations, failing loudly"
```

---

### Task 5: Peptide aggregation

**Files:**
- Create: `ingest/denovo_aggregate.py`
- Test: `tests/test_denovo_aggregate.py`

**Interfaces:**
- Consumes: the `read_mztab()` PSM shape from Task 4.
- Produces: `aggregate_peptides(psms) -> list[dict]` with `stripped_seq`, `seq_il`, `length`,
  `n_psms`, `peptide_score`, `conf_geomean`; `run_stats(peptides) -> dict` with `n_psms`,
  `n_peptides`, `len_median`, `conf_geomean_median`.

- [ ] **Step 1: Write the failing test**

```python
"""Aggregation, and the length-normalised confidence that replaces peptide_score."""
import os, statistics, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingest"))
from denovo_aggregate import aggregate_peptides, run_stats  # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

psms = [
    {"scan": 1, "stripped_seq": "PEPTIDEK", "seq_il": "PEPTLDEK", "peptide_score": 0.5, "aa_scores": []},
    {"scan": 2, "stripped_seq": "PEPTIDEK", "seq_il": "PEPTLDEK", "peptide_score": 0.7, "aa_scores": []},
    {"scan": 3, "stripped_seq": "AAAAAA",   "seq_il": "AAAAAA",   "peptide_score": 0.5, "aa_scores": []},
]
rows = {r["stripped_seq"]: r for r in aggregate_peptides(psms)}
check("two distinct peptides", len(rows) == 2)
check("n_psms counted", rows["PEPTIDEK"]["n_psms"] == 2)
check("best score kept", abs(rows["PEPTIDEK"]["peptide_score"] - 0.7) < 1e-9)
check("length stored", rows["PEPTIDEK"]["length"] == 8)
check("geomean of the 8-mer", abs(rows["PEPTIDEK"]["conf_geomean"] - 0.7 ** (1/8)) < 1e-9)
check("geomean of the 6-mer", abs(rows["AAAAAA"]["conf_geomean"] - 0.5 ** (1/6)) < 1e-9)
check("geomean reorders vs raw score",
      rows["PEPTIDEK"]["conf_geomean"] > rows["AAAAAA"]["conf_geomean"])

st = run_stats(list(rows.values()))
check("run n_psms", st["n_psms"] == 3)
check("run n_peptides", st["n_peptides"] == 2)
check("len_median", st["len_median"] == statistics.median([8, 6]))
check("negative score clamped, not crashed",
      aggregate_peptides([{"scan": 9, "stripped_seq": "AA", "seq_il": "AA",
                           "peptide_score": -0.99, "aa_scores": []}])[0]["conf_geomean"] >= 0)

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_denovo_aggregate.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'denovo_aggregate'`

- [ ] **Step 3: Write the implementation**

```python
"""Aggregate PSMs to peptides, carrying a length-normalised confidence.

WHY conf_geomean EXISTS. Casanovo's peptide score is the PRODUCT of per-residue scores
(model.py _peptide_score), so it is ~p^L and collapses with length whatever the quality. Measured
across 10.76M PSMs, per-residue confidence PEAKS at length 20 while the peptide score ranks those
peptides ~1,400x worse than 7-mers. A raw-score threshold is therefore a length filter that
discards the best-sequenced material first -- fatal for variant work, where short peptides are the
least informative.

conf_geomean = peptide_score ** (1/length) is roughly length-stable and is the only per-peptide
number that compares across lengths. Store it ALONGSIDE length, never instead of it.
"""
from __future__ import annotations

import statistics


def _prob(score):
    """Casanovo emits scores in [-1, 1]; map to [0, 1] for the geometric mean."""
    if score is None:
        return 0.0
    return max(0.0, (score + 1) / 2 if score < 0 else score)


def aggregate_peptides(psms: list[dict]) -> list[dict]:
    best: dict[str, dict] = {}
    for p in psms:
        seq = p["stripped_seq"]
        if not seq:
            continue
        score = _prob(p.get("peptide_score"))
        row = best.get(seq)
        if row is None:
            best[seq] = {"stripped_seq": seq, "seq_il": p["seq_il"], "length": len(seq),
                         "n_psms": 1, "peptide_score": score}
        else:
            row["n_psms"] += 1
            row["peptide_score"] = max(row["peptide_score"], score)
    for row in best.values():
        row["conf_geomean"] = row["peptide_score"] ** (1.0 / row["length"]) if row["length"] else 0.0
    return sorted(best.values(), key=lambda r: r["stripped_seq"])


def run_stats(peptides: list[dict]) -> dict:
    if not peptides:
        return {"n_psms": 0, "n_peptides": 0, "len_median": None, "conf_geomean_median": None}
    return {"n_psms": sum(p["n_psms"] for p in peptides),
            "n_peptides": len(peptides),
            "len_median": int(statistics.median(p["length"] for p in peptides)),
            "conf_geomean_median": float(statistics.median(p["conf_geomean"] for p in peptides))}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_denovo_aggregate.py`
Expected: PASS, `0 failure(s)`

- [ ] **Step 5: Commit**

```bash
git add ingest/denovo_aggregate.py tests/test_denovo_aggregate.py
git commit -m "ingest: aggregate to peptides with a length-normalised confidence"
```

---

### Task 6: The ingestor

**Files:**
- Create: `ingest/denovo_ingest.py`
- Test: `tests/test_denovo_ingest.py`

**Interfaces:**
- Consumes: Tasks 2–5.
- Produces: CLI `python ingest/denovo_ingest.py --mztab <f> --cohort <name> [--apply]`;
  `load_corpus_index(conn) -> dict[str, list[str]]`;
  `build_rows(mztab, cohort, corpus, gpu_arch=None, weights=None) -> dict` with keys `run`,
  `peptides`, `psms`, `matches`.

- [ ] **Step 1: Write the failing test**

```python
"""The ingestor assembles rows correctly and is DRY-RUN by default."""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingest"))
from denovo_ingest import build_rows  # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

MZTAB = ("MTD\tsoftware[1]\t[MS, MS:1003281, Casanovo, 5.2.1]\n"
         "PSH\topt_global_cv_MS:1003169_proforma_peptidoform_sequence\tPSM_ID\t"
         "search_engine_score[1]\tcharge\texp_mass_to_charge\tspectra_ref\topt_global_aa_scores\n"
         "PSM\tPEPTIDEK\t1\t0.9\t2\t500.5\tms_run[1]:controllerType=0 controllerNumber=1 scan=42\t0.9\n"
         "PSM\tWWWWWWWW\t2\t0.8\t2\t600.5\tms_run[1]:controllerType=0 controllerNumber=1 scan=43\t0.8\n")
fh = tempfile.NamedTemporaryFile("w", suffix=".mztab", delete=False,
                                 prefix="QEPlus2_03162018_36_ZG171_")
fh.write(MZTAB); fh.close()

corpus = {"PEPTLDEK": ["PEPTIDEK", "PEPTLDEK"]}
out = build_rows(fh.name, cohort="test", corpus=corpus)

check("run row present", out["run"]["run_name"].startswith("QEPlus2_03162018_36_ZG171"))
check("donor parsed", out["run"]["donor_id"] == "ZG171")
check("role is donor", out["run"]["sample_role"] == "donor")
check("engine version recorded", out["run"]["engine_version"] == "5.2.1")
check("engine recorded", out["run"]["denovo_engine"] == "casanovo")
check("two peptides", len(out["peptides"]) == 2)
check("two psms", len(out["psms"]) == 2)
check("match rows = 2", len(out["matches"]) == 2, f"got {len(out['matches'])}")
check("n_candidates = 2", all(m["n_candidates"] == 2 for m in out["matches"]))

cls = {p["stripped_seq"]: p["corpus_class"] for p in out["peptides"]}
check("matched peptide is conserved", cls["PEPTIDEK"] == "conserved")
check("unmatched peptide is novel", cls["WWWWWWWW"] == "novel")
check("run_id deterministic",
      build_rows(fh.name, cohort="test", corpus=corpus)["run"]["run_id"] == out["run"]["run_id"])

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_denovo_ingest.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'denovo_ingest'`

- [ ] **Step 3: Write the implementation**

```python
"""Ingest one de novo mzTab into the FRAN corpus. DRY RUN unless --apply.

The corpus index is a one-time COPY of delimp_peptide_consensus (2.93M peptides, 44 MB, 5.2 s)
folded to an I/L key in Python. Matching in Python rather than SQL keeps the join off a 262 GB
production database; the whole pass takes ~7 s.

Idempotent per run: run_id = uuid5(NS, run_name) and --apply deletes that run's rows first.
Re-ingest is delete+insert scoped to one run, never a corpus-wide rewrite.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from denovo_aggregate import aggregate_peptides, run_stats     # noqa: E402
from denovo_corpus_match import il, match_peptides             # noqa: E402
from denovo_identity import parse_identity                     # noqa: E402
from denovo_mztab import read_mztab                            # noqa: E402

NS = uuid.UUID("6f5c9b3a-1d2e-4f8a-9c7b-3e5d1a2b4c6d")


def load_corpus_index(conn) -> dict[str, list[str]]:
    idx: dict[str, list[str]] = {}
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT stripped_seq FROM delimp_peptide_consensus")
    for (seq,) in cur:
        idx.setdefault(il(seq), []).append(seq)
    return idx


def build_rows(mztab_path, cohort, corpus, gpu_arch=None, weights=None) -> dict:
    run_name = os.path.basename(mztab_path).replace(".mztab", "")
    ident = parse_identity(run_name)
    d = read_mztab(mztab_path)
    peptides = aggregate_peptides(d["psms"])
    matches = match_peptides([p["stripped_seq"] for p in peptides], corpus)
    matched = {m["stripped_seq"] for m in matches}
    for p in peptides:
        p["corpus_class"] = "conserved" if p["stripped_seq"] in matched else "novel"
    return {
        "run": {"run_id": str(uuid.uuid5(NS, run_name)), "run_name": run_name, "cohort": cohort,
                "denovo_engine": "casanovo", "engine_version": d["engine_version"],
                "weights": weights, "gpu_arch": gpu_arch, "bundle_format": "mztab",
                "donor_id": ident["donor_id"], "sample_role": ident["sample_role"],
                "ingest_flag": None, **run_stats(peptides)},
        "peptides": peptides, "psms": d["psms"], "matches": matches}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mztab", required=True)
    ap.add_argument("--cohort", required=True)
    ap.add_argument("--gpu-arch")
    ap.add_argument("--weights")
    ap.add_argument("--apply", action="store_true", help="write; otherwise dry run")
    a = ap.parse_args()

    import psycopg2, psycopg2.extras
    from refresh_leaderboards import _token
    con = psycopg2.connect(
        host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
        dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30)

    corpus = load_corpus_index(con)
    out = build_rows(a.mztab, a.cohort, corpus, a.gpu_arch, a.weights)
    print(json.dumps({"run": out["run"], "n_matches": len(out["matches"])},
                     indent=2, default=str))
    if not a.apply:
        print("\nDRY RUN -- nothing written. Re-run with --apply.")
        return

    cur = con.cursor()
    rid = out["run"]["run_id"]
    for t in ("delimp_denovo_corpus_match", "delimp_denovo_psm", "delimp_denovo_peptide"):
        cur.execute(f"DELETE FROM {t} WHERE run_id = %s", (rid,))
    cur.execute("DELETE FROM delimp_denovo_run WHERE run_id = %s", (rid,))
    cur.execute(
        "INSERT INTO delimp_denovo_run (run_id,run_name,cohort,denovo_engine,engine_version,"
        "weights,gpu_arch,bundle_format,donor_id,sample_role,n_psms,n_peptides,len_median,"
        "conf_geomean_median,ingest_flag) VALUES (%(run_id)s,%(run_name)s,%(cohort)s,"
        "%(denovo_engine)s,%(engine_version)s,%(weights)s,%(gpu_arch)s,%(bundle_format)s,"
        "%(donor_id)s,%(sample_role)s,%(n_psms)s,%(n_peptides)s,%(len_median)s,"
        "%(conf_geomean_median)s,%(ingest_flag)s)", out["run"])
    psycopg2.extras.execute_values(
        cur, "INSERT INTO delimp_denovo_peptide (run_id,stripped_seq,seq_il,length,n_psms,"
             "peptide_score,conf_geomean,corpus_class) VALUES %s",
        [(rid, p["stripped_seq"], p["seq_il"], p["length"], p["n_psms"], p["peptide_score"],
          p["conf_geomean"], p["corpus_class"]) for p in out["peptides"]])
    psycopg2.extras.execute_values(
        cur, "INSERT INTO delimp_denovo_psm (run_id,scan,stripped_seq,seq_il,sequence,charge,"
             "exp_mz,peptide_score,aa_scores) VALUES %s",
        [(rid, p["scan"], p["stripped_seq"], p["seq_il"], p["sequence"], p["charge"], p["exp_mz"],
          p["peptide_score"], p["aa_scores"]) for p in out["psms"] if p["scan"] is not None])
    psycopg2.extras.execute_values(
        cur, "INSERT INTO delimp_denovo_corpus_match (run_id,stripped_seq,corpus_stripped_seq,"
             "match_kind,n_candidates) VALUES %s",
        [(rid, m["stripped_seq"], m["corpus_stripped_seq"], m["match_kind"], m["n_candidates"])
         for m in out["matches"]])
    con.commit()
    print(f"\nwrote run {rid}: {len(out['peptides'])} peptides, {len(out['matches'])} matches")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_denovo_ingest.py`
Expected: PASS, `0 failure(s)`

- [ ] **Step 5: Dry-run against real data**

Run:
```bash
ssh hive "DELIMP_PG_TOKEN_FILE=/quobyte/proteomics-grp/brett/.pgfarm_token \
  python3 <ingest dir>/denovo_ingest.py --cohort teeth \
  --mztab /quobyte/proteomics-grp/brett/teeth_pilot2/out/QEPlus2_12212017_28_62_v5std.mztab"
```
Expected: JSON with `n_peptides` ≈ 22,000 and non-zero `n_matches`. **Do not pass `--apply` until
this looks right.**

- [ ] **Step 6: Commit**

```bash
git add ingest/denovo_ingest.py tests/test_denovo_ingest.py
git commit -m "ingest: de novo mzTab -> corpus, dry-run by default and idempotent per run"
```

---

### Task 7: Read layer

**Files:**
- Create: `app/denovo.py`
- Modify: `app/db.py` — add four tables to `PUBLIC_TABLES`
- Test: `tests/test_denovo_queries.py`

**Interfaces:**
- Produces: `list_runs(cohort=None, limit=50, offset=0)`;
  `run_peptides(run_id, corpus_class=None, limit=100, offset=0)`; `peptide_denovo(stripped_seq)`.

- [ ] **Step 1: Write the failing test**

```python
"""The de novo read layer: allowlisting, SQL hygiene, and the I/L reverse lookup."""
import os, re, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import db  # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

for t in ("delimp_denovo_run", "delimp_denovo_peptide", "delimp_denovo_psm",
          "delimp_denovo_corpus_match"):
    check(f"{t} allowlisted", t in db.PUBLIC_TABLES)

src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "denovo.py")).read()
check("every query names its tables", src.count("tables=[") >= 3)
check("no interpolated SQL", not re.search(r'(?:execute|query)\(\s*f["\']', src))
check("uses placeholders", "%(" in src)
check("peptide_denovo joins on seq_il", "seq_il" in src)
check("length selected wherever peptide_score is",
      "peptide_score" not in src or "length" in src)

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_denovo_queries.py`
Expected: FAIL — `delimp_denovo_run allowlisted`, then `FileNotFoundError` for `app/denovo.py`

- [ ] **Step 3: Add the tables to the allowlist**

In `app/db.py`, inside the `PUBLIC_TABLES` frozenset, after the `delimp_spectrum_lane` entry:

```python
        # de novo lane (Casanovo). Read-only here; written by the DE-LIMP ingestor. No customer
        # identifiers: run names go through the public tier's existing masking, and donor_id is a
        # lab-internal series label.
        "delimp_denovo_run",
        "delimp_denovo_peptide",
        "delimp_denovo_psm",
        "delimp_denovo_corpus_match",
```

- [ ] **Step 4: Write the query module**

```python
"""Read-side queries for the de novo lane.

FRAN never writes a de novo row -- the DE-LIMP ingestor owns the write path. Every function names
its `tables` so db.query() can enforce the public allowlist.
"""
from __future__ import annotations

from typing import Any

from .db import query


def _page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(int(limit or 50), 500)), max(0, int(offset or 0))


def list_runs(cohort: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    lim, off = _page(limit, offset)
    where, params = "", {}
    if cohort:
        where, params["cohort"] = "WHERE cohort = %(cohort)s", cohort
    params.update(lim=lim, off=off)
    rows = query(
        f"""SELECT run_id, run_name, cohort, denovo_engine, engine_version, donor_id, sample_role,
                   n_psms, n_peptides, len_median, conf_geomean_median, ingest_flag
              FROM delimp_denovo_run {where}
             ORDER BY ingested_at DESC, run_name
             LIMIT %(lim)s OFFSET %(off)s""",
        params, tables=["delimp_denovo_run"])
    total = query(f"SELECT count(*) FROM delimp_denovo_run {where}", params,
                  tables=["delimp_denovo_run"], fetch="val")
    return {"rows": rows, "total": total}


def run_peptides(run_id: str, corpus_class: str | None = None,
                 limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """A run's peptides, each with the corpus spellings it links to.

    corpus_stripped_seq is aggregated into an array because the I/L match is one-to-many: one de
    novo peptide can point at several real corpus peptides, and the UI shows every candidate.
    """
    lim, off = _page(limit, offset)
    params: dict[str, Any] = {"run_id": run_id, "lim": lim, "off": off}
    cls = ""
    if corpus_class in ("conserved", "gvp", "novel"):
        cls, params["cls"] = "AND p.corpus_class = %(cls)s", corpus_class
    rows = query(
        f"""SELECT p.stripped_seq, p.length, p.n_psms, p.peptide_score, p.conf_geomean,
                   p.corpus_class,
                   coalesce(array_agg(m.corpus_stripped_seq ORDER BY m.corpus_stripped_seq)
                            FILTER (WHERE m.corpus_stripped_seq IS NOT NULL), '{{}}') AS corpus_hits,
                   max(m.n_candidates) AS n_candidates
              FROM delimp_denovo_peptide p
              LEFT JOIN delimp_denovo_corpus_match m
                     ON m.run_id = p.run_id AND m.stripped_seq = p.stripped_seq
             WHERE p.run_id = %(run_id)s {cls}
             GROUP BY p.stripped_seq, p.length, p.n_psms, p.peptide_score, p.conf_geomean,
                      p.corpus_class
             ORDER BY p.n_psms DESC, p.stripped_seq
             LIMIT %(lim)s OFFSET %(off)s""",
        params, tables=["delimp_denovo_peptide", "delimp_denovo_corpus_match"])
    total = query(
        f"SELECT count(*) FROM delimp_denovo_peptide p WHERE p.run_id = %(run_id)s {cls}",
        params, tables=["delimp_denovo_peptide"], fetch="val")
    return {"rows": rows, "total": total}


def peptide_denovo(stripped_seq: str) -> dict[str, Any]:
    """Direction B: which de novo runs called this peptide.

    Joins on seq_il, not stripped_seq: a DIA peptide spelled with I must find the de novo peptide
    spelled with L. Matching the raw spelling silently halves the reach.
    """
    seq = (stripped_seq or "").strip().upper()
    if not seq:
        return {"rows": []}
    rows = query(
        """SELECT r.run_id, r.run_name, r.cohort, r.donor_id, r.sample_role,
                  p.stripped_seq, p.length, p.n_psms, p.conf_geomean, p.corpus_class
             FROM delimp_denovo_peptide p
             JOIN delimp_denovo_run r ON r.run_id = p.run_id
            WHERE p.seq_il = replace(%(seq)s, 'I', 'L')
            ORDER BY p.n_psms DESC
            LIMIT 100""",
        {"seq": seq}, tables=["delimp_denovo_peptide", "delimp_denovo_run"])
    return {"rows": rows}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python tests/test_denovo_queries.py`
Expected: PASS, `0 failure(s)`

- [ ] **Step 6: Run the predeploy check**

Run: `python scripts/predeploy_check.py`
Expected: PASS. It checks every `tables=[...]` literal against `PUBLIC_TABLES`, so a table used in
a query but missing from the allowlist fails here rather than at runtime.

- [ ] **Step 7: Commit**

```bash
git add app/denovo.py app/db.py tests/test_denovo_queries.py
git commit -m "app: de novo read layer, allowlisted, joining on the I/L key"
```

---

### Task 8: API endpoints

**Files:**
- Modify: `app/main.py` — three routes after the `/api/peptide/{stripped_seq}/flyability` route
- Test: `tests/test_denovo_api.py`

**Interfaces:**
- Consumes: Task 7.
- Produces: `GET /api/denovo/runs`, `GET /api/denovo/run/{run_id}/peptides`,
  `GET /api/peptide/{stripped_seq}/denovo`.

- [ ] **Step 1: Write the failing test**

```python
"""The de novo routes exist and delegate to app.denovo."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

from app.main import app  # noqa: E402
paths = {r.path for r in app.routes}
check("/api/denovo/runs registered", "/api/denovo/runs" in paths)
check("run peptides registered", "/api/denovo/run/{run_id}/peptides" in paths)
check("peptide denovo registered", "/api/peptide/{stripped_seq}/denovo" in paths)

src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "main.py")).read()
check("delegates to app.denovo", "denovo.list_runs" in src and "denovo.run_peptides" in src)

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_denovo_api.py`
Expected: FAIL — `/api/denovo/runs registered`

- [ ] **Step 3: Add the routes**

Add `denovo` to the existing `from app import ...` block in `app/main.py`, then:

```python
@app.get("/api/denovo/runs")
def api_denovo_runs(cohort: str | None = None, limit: int = 50, offset: int = 0):
    return denovo.list_runs(cohort, limit, offset)


@app.get("/api/denovo/run/{run_id}/peptides")
def api_denovo_run_peptides(run_id: str, corpus_class: str | None = None,
                            limit: int = 100, offset: int = 0):
    return denovo.run_peptides(run_id, corpus_class, limit, offset)


@app.get("/api/peptide/{stripped_seq}/denovo")
def api_peptide_denovo(stripped_seq: str):
    return denovo.peptide_denovo(stripped_seq)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_denovo_api.py`
Expected: PASS, `0 failure(s)`

- [ ] **Step 5: Verify live**

Run `uvicorn app.main:app --port 7860`, then `curl -s localhost:7860/api/denovo/runs | head -c 400`
Expected: JSON with `rows` and `total`. `total: 0` before ingest is correct.

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_denovo_api.py
git commit -m "api: de novo run list, run peptides, and the peptide reverse lookup"
```

---

### Task 9: The peptide-list view

**Files:**
- Modify: `app/static/app.js`
- Test: `tests/test_denovo_ui.py`

**Interfaces:**
- Consumes: Task 8. Produces routes `#/denovo` and `#/denovo/<run_id>`.

- [ ] **Step 1: Write the failing test**

```python
"""The SPA wires the de novo views and reuses the existing peptide chip."""
import os, sys

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

js = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                       "app", "static", "app.js")).read()
check("denovo route", "case 'denovo':" in js)
check("denovo run route", "case 'denovorun':" in js)
check("renderDenovo defined", "function renderDenovo(" in js)
check("renderDenovoRun defined", "function renderDenovoRun(" in js)
check("reuses pepChip", "pepChip(" in js)
check("hits the runs endpoint", "/api/denovo/runs" in js)
check("hits the peptides endpoint", "/api/denovo/run/" in js)
check("length shown alongside confidence", "conf_geomean" in js and "length" in js)

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_denovo_ui.py`
Expected: FAIL — `denovo route`

- [ ] **Step 3: Add the routes and render functions**

In `route()`'s switch, after `case 'engines':`:

```javascript
    case 'denovo': return renderDenovo();
    case 'denovorun': return renderDenovoRun(param);
```

Then near `renderEngines`:

```javascript
function roleChip(role){
  const c = {donor:'bg-emerald-500/15 text-emerald-300',
             standard:'bg-amber-500/15 text-amber-300',
             control:'bg-sky-500/15 text-sky-300',
             unknown:'bg-slate-500/15 text-slate-400'}[role] || 'bg-slate-500/15 text-slate-400';
  return `<span class="px-2 py-0.5 rounded text-[10px] ${c}">${esc(role)}</span>`;
}
function flagChip(f){
  return `<span class="px-2 py-0.5 rounded text-[10px] bg-rose-500/15 text-rose-300"
           title="flagged at ingest">${esc(f)}</span>`;
}

async function renderDenovo(){
  const d = await api('/api/denovo/runs?limit=200');
  const rows = (d.rows||[]).map(r=>`
    <tr class="row-hover border-b border-white/5 cursor-pointer"
        onclick="go('denovorun','${encodeURIComponent(r.run_id)}')">
      <td class="py-2 font-mono text-accent-400">${esc(r.run_name)}</td>
      <td>${esc(r.cohort||'')}</td>
      <td>${esc(r.donor_id||'—')}</td>
      <td>${roleChip(r.sample_role)}</td>
      <td class="text-right tabular-nums">${(r.n_peptides||0).toLocaleString()}</td>
      <td class="text-right tabular-nums">${r.len_median||'—'}</td>
      <td class="text-right tabular-nums">${(r.conf_geomean_median||0).toFixed(3)}</td>
      <td>${r.ingest_flag?flagChip(r.ingest_flag):''}</td>
    </tr>`).join('');
  el('view').innerHTML = `
    <h2 class="text-xl mb-1">De novo runs</h2>
    <p class="text-[11px] text-slate-500 mb-3">Peptides sequenced directly from spectra, with no
      database search. Median confidence is the length-normalised per-residue value — the raw
      peptide score is a product over residues, so it falls with length whatever the quality.</p>
    <div class="overflow-x-auto"><table class="w-full text-sm">
      <thead><tr class="text-[10px] uppercase tracking-wider text-slate-500">
        <th class="text-left py-1">Run</th><th class="text-left">Cohort</th>
        <th class="text-left">Donor</th><th class="text-left">Role</th>
        <th class="text-right">Peptides</th><th class="text-right">Median len</th>
        <th class="text-right">Median conf</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
}

async function renderDenovoRun(runId){
  const d = await api(`/api/denovo/run/${encodeURIComponent(runId)}/peptides?limit=300`);
  const rows = (d.rows||[]).map(r=>{
    const hits = r.corpus_hits||[];
    const linked = hits.length ? hits.map(h=>pepChip(h)).join(' ')
                               : '<span class="text-slate-600">not seen</span>';
    const amb = (r.n_candidates||0) > 1
      ? `<span class="ml-1 text-[10px] text-amber-400"
          title="I and L are indistinguishable to de novo, so this matches ${r.n_candidates} corpus spellings">${r.n_candidates}×</span>`
      : '';
    const cls = r.corpus_class==='conserved'
      ? '<span class="px-2 py-0.5 rounded text-[10px] bg-teal-500/15 text-teal-300">conserved</span>'
      : '<span class="px-2 py-0.5 rounded text-[10px] bg-amber-500/15 text-amber-300">novel</span>';
    return `<tr class="border-b border-white/5">
      <td class="py-2 font-mono">${esc(r.stripped_seq)}</td>
      <td class="text-right tabular-nums text-slate-400">${r.length}</td>
      <td>${cls}</td><td>${linked}${amb}</td>
      <td class="text-right tabular-nums">${r.n_psms}</td>
      <td class="text-right tabular-nums">${(r.conf_geomean||0).toFixed(3)}</td>
    </tr>`;}).join('');
  el('view').innerHTML = `
    <button class="text-xs text-accent-400 mb-2" onclick="go('denovo')">&larr; all de novo runs</button>
    <h2 class="text-xl mb-1">${esc(runId)}</h2>
    <p class="text-[11px] text-slate-500 mb-3">${(d.total||0).toLocaleString()} peptides.
      <b>Conserved</b> = already in the FRAN corpus (contaminant or housekeeping, not evidence of
      this sample). <b>Novel</b> = never seen in the corpus.</p>
    <div class="overflow-x-auto"><table class="w-full text-sm">
      <thead><tr class="text-[10px] uppercase tracking-wider text-slate-500">
        <th class="text-left py-1">Peptide</th><th class="text-right">Len</th>
        <th class="text-left">Class</th><th class="text-left">In FRAN</th>
        <th class="text-right">PSMs</th><th class="text-right">Conf</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
}
```

In `renderPeptide`, after the existing panels:

```javascript
  const dn = await api(`/api/peptide/${encodeURIComponent(seq)}/denovo`).catch(()=>({rows:[]}));
  if ((dn.rows||[]).length){
    el('pep-denovo').innerHTML = `
      <h3 class="text-sm mb-1">Seen de novo</h3>
      <p class="text-[11px] text-slate-500 mb-2">Sequenced directly from spectra, without a
        database search, in ${dn.rows.length} run(s).</p>` +
      dn.rows.slice(0,10).map(r=>`
        <div class="flex justify-between text-xs py-1 border-b border-white/5">
          <span class="cursor-pointer text-accent-400"
                onclick="go('denovorun','${encodeURIComponent(r.run_id)}')">${esc(r.run_name)}</span>
          <span class="text-slate-500">${esc(r.cohort||'')} · ${r.n_psms} PSMs ·
            conf ${(r.conf_geomean||0).toFixed(3)}</span>
        </div>`).join('');
  }
```

Add `<div id="pep-denovo" class="mt-4"></div>` to the peptide view template, and a `De novo`
navbar link to `#/denovo`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_denovo_ui.py`
Expected: PASS, `0 failure(s)`

- [ ] **Step 5: Verify in the browser**

`uvicorn app.main:app --port 7860`, open `http://localhost:7860/#/denovo`.
Expected: the runs table renders. Empty before ingest is correct.

- [ ] **Step 6: Commit**

```bash
git add app/static/app.js tests/test_denovo_ui.py
git commit -m "ui: de novo run list, peptide table, and the seen-de-novo panel"
```

---

### Task 10: End to end on real data

**Files:**
- Create: `tests/test_denovo_e2e.py`

- [ ] **Step 1: Write the test**

```python
"""End-to-end on a real bundle, asserting the relationships the design rests on.

Skips cleanly when the fixture is absent so it does not break on a machine without HIVE.
"""
import os, statistics, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingest"))

FIXTURE = os.environ.get("DENOVO_FIXTURE_MZTAB", "")
if not FIXTURE or not os.path.exists(FIXTURE):
    print("SKIP: set DENOVO_FIXTURE_MZTAB to a casanovo mzTab to run this test")
    sys.exit(0)

from denovo_aggregate import aggregate_peptides   # noqa: E402
from denovo_mztab import read_mztab               # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

d = read_mztab(FIXTURE)
check("PSMs read", len(d["psms"]) > 1000, f"got {len(d['psms'])}")
check("engine version parsed", bool(d["engine_version"]))
peps = aggregate_peptides(d["psms"])
check("peptides aggregated", len(peps) > 500, f"got {len(peps)}")

short = [p for p in peps if p["length"] <= 9]
long_ = [p for p in peps if p["length"] >= 18]
if short and long_:
    gs = statistics.median(p["conf_geomean"] for p in short)
    gl = statistics.median(p["conf_geomean"] for p in long_)
    check("conf_geomean is roughly length-stable", abs(gs - gl) < 0.35,
          f"short {gs:.3f} vs long {gl:.3f}")
    rs = statistics.median(p["peptide_score"] for p in short)
    rl = statistics.median(p["peptide_score"] for p in long_)
    check("raw peptide_score collapses with length (why conf_geomean exists)", rs > rl * 2,
          f"short {rs:.4f} vs long {rl:.4f}")

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Run against real data**

Run:
```bash
DENOVO_FIXTURE_MZTAB=/quobyte/proteomics-grp/brett/teeth_pilot2/out/QEPlus2_12212017_28_62_v5std.mztab \
  python tests/test_denovo_e2e.py
```
Expected: PASS, `0 failure(s)`

- [ ] **Step 3: Ingest for real**

Run the Task 6 command with `--apply` on one teeth run, then
`curl -s localhost:7860/api/denovo/runs | head -c 400`
Expected: one row, `n_peptides` ≈ 22,000.

- [ ] **Step 4: Commit**

```bash
git add tests/test_denovo_e2e.py
git commit -m "test: end-to-end de novo ingest against a real bundle"
```

---

## What Phase 1 deliberately leaves out

Phase 1 stops at "the peptides, each linking into the corpus". The forensic payload is Phase 2
(GVP + dbSNP) and Phase 3 (amelogenin sex estimation), both planned separately and both depending
on Tasks 1–6 here. Also deferred: homology/LCA ingest from a full `delimp_denovo_v1` bundle, and
the MCP tools.

**Awaiting a human answer, not a decision:** the identity of the RR (6) and JE (3) runs, the 32
unattributable runs, and whether `ZG148PEPRT` is a plain replicate of `ZG148` or carries a spiked
retention-time standard. All ingest as `sample_role='unknown'` until someone who knows the naming
says otherwise. The 12 PRM runs are out of scope by decision.
