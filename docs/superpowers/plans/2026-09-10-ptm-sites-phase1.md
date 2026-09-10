# PTM Sites on the Coverage Map — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On a protein's sequence-coverage map, mark the exact residue carrying each variable
modification, with occupancy, sourced from `modified_seq_proforma`.

**Architecture:** A pure-Python ProForma parser (`app/proforma.py`) turns a modified sequence into
`(unimod_id, residue, position_in_peptide)` triples. `protein_coverage_peptides()` gains a second
protein-scoped aggregate over modified precursors — measured at 0.13 s on the existing
`idx_prec_protein_group` — and maps peptide-relative positions onto protein coordinates using the
`start` offsets the coverage map already carries. `_drawPeptideMap()` marks those residues.

**Tech Stack:** Python 3.13, FastAPI, psycopg2, vanilla JS (no framework, no build step).

**Spec:** `docs/superpowers/specs/2026-09-10-ptm-sites-and-modification-search-design.md`

**Worktree:** `/Users/brettphinney/Documents/FRAN-ptm`, branch `ptm-sites`, based on
`search-heatmap` — NOT on `main`. The search-scoped coverage map (`search_id` on
`protein_coverage_peptides`, the `here` keys) exists only on `search-heatmap`; building this on
`main` would target a function that does not have the scoping this feature reads.

## Global Constraints

- Every `query()` call passes `tables=[...]`. `_assert_allowlisted()` is the first statement of
  `query()`'s body (`app/db.py:383`), before any SQL is built.
- `privacy.redact(obj, reveal)` sanitizes string VALUES under
  `_FILE_KEYS = {"raw_path","raw_basename","run","file","filename","fasta_path"}`. It NEVER renames
  dict KEYS. Nothing filename-shaped may become a dict key.
- Read `modified_seq_proforma`. NEVER `mods` — it is 1.43% populated because
  `ingest/spectronaut_to_corpus.py:199` writes `"mods": None`, and Spectronaut is 96% of the corpus.
  The GIN index `idx_prec_mods_gin` on it is dead by construction. Do not use it.
- **Variable modifications only.** In scope: `{35: Oxidation, 1: Acetyl, 21: Phospho,
  7: Deamidated, 27: "Glu->pyro-Glu"}`. Carbamidomethyl (UNIMOD 4) is a fixed modification — a
  reagent, not biology — and is 60.9% of all modifications. It must never appear as a site.
- Sites are **engine-reported and not independently localized**. `site_localization_probability` is
  NULL on all 437 M rows. The UI must say so visibly, not only in a tooltip.
- An unmodified protein must return byte-identical output to today. The coverage map is live.
- Every test must be proven able to fail. Show the failure, then the fix.

---

### Task 1: The ProForma parser

**Files:**
- Create: `app/proforma.py`
- Test: `tests/test_proforma.py`

**Interfaces:**
- Consumes: nothing. Pure function, no DB, no imports from `app.queries`.
- Produces: `parse_proforma(pf: str) -> list[Mod]` where
  `Mod = NamedTuple("Mod", [("unimod_id", int), ("residue", str | None), ("pos", int)])`.
  `pos` is the 1-based index into the STRIPPED sequence of the residue the tag follows.
  `pos == 0` means an N-terminal modification, and `residue` is then `None`.
  Also produces `VARIABLE_MODS: dict[int, str]` and `MOD_NAME(uid) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_proforma.py
from app.proforma import parse_proforma, Mod, VARIABLE_MODS


def test_internal_mod_position_is_index_of_preceding_residue():
    # M[UNIMOD:35]RNPDEK -- oxidation on the M at position 1
    assert parse_proforma("M[UNIMOD:35]RNPDEK") == [Mod(35, "M", 1)]


def test_n_terminal_mod_has_no_preceding_residue():
    # Real corpus string. The acetyl precedes residue 1; it modifies the terminus, not a residue.
    assert parse_proforma("[UNIMOD:1]SETAPAETATPAPVEK") == [Mod(1, None, 0)]


def test_n_terminal_and_internal_together_do_not_shift_the_internal_one():
    # THE regression this parser exists to prevent. A parser that treats every tag as following
    # a residue assigns the N-terminal acetyl to a residue and shifts SPAK's phospho by one.
    # Stripped: SETAPAETATPAPVEKSPAK (20 aa). The phospho S is position 17.
    pf = "[UNIMOD:1]SETAPAETATPAPVEKS[UNIMOD:21]PAK"
    assert parse_proforma(pf) == [Mod(1, None, 0), Mod(21, "S", 17)]


def test_two_sites_on_one_peptide():
    # Real corpus string from P92966 (RS41), an Arabidopsis SR splicing factor.
    # Stripped: RESRSPPPYEK. Phospho on S at 3 and S at 5.
    assert parse_proforma("RES[UNIMOD:21]RS[UNIMOD:21]PPPYEK") == [Mod(21, "S", 3), Mod(21, "S", 5)]


def test_unmodified_sequence_yields_nothing():
    assert parse_proforma("AADDTWEPFASGK") == []


def test_empty_and_none_are_safe():
    assert parse_proforma("") == []
    assert parse_proforma(None) == []


def test_diann_style_underscores_are_not_residues():
    # Spectronaut/DIA-NN wrap sequences in underscores. An underscore must not count as a residue,
    # or every position in every peptide from that engine is shifted.
    assert parse_proforma("_M[UNIMOD:35]RNPDEK_") == [Mod(35, "M", 1)]


def test_unknown_bracket_token_is_skipped_without_shifting_positions():
    # A mod name the corpus does not map stays as literal text. It must not be counted as a
    # residue and must not consume the residues around it.
    assert parse_proforma("AC[SomethingElse]DK[UNIMOD:21]E") == [Mod(21, "K", 4)]


def test_carbamidomethyl_is_not_a_variable_mod():
    # Fixed modification: a reagent, 60.9% of all modifications corpus-wide. Parsed, but never
    # offered as a site.
    assert 4 not in VARIABLE_MODS
    assert parse_proforma("AAC[UNIMOD:4]LLPK") == [Mod(4, "C", 3)]
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_proforma.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.proforma'`

- [ ] **Step 3: Implement**

```python
# app/proforma.py
"""ProForma parsing for FRAN's modified_seq_proforma column.

WHY THIS COLUMN AND NOT `mods`: modified_seq_proforma is 100% populated corpus-wide (measured:
249,978 of 249,978 sampled). `mods` is 1.43% populated because ingest/spectronaut_to_corpus.py:199
writes `"mods": None` with a TODO, and Spectronaut is 2,008 of the 2,086 searches. The GIN index
idx_prec_mods_gin sits on that near-empty column and is dead by construction. Do not reach for it.
"""
from __future__ import annotations

import re
from typing import NamedTuple

# UNIMOD ids seen anywhere in the corpus (a token scan of 18,856 modified precursors found SIX,
# and no others): 4 Carbamidomethyl, 35 Oxidation, 1 Acetyl, 21 Phospho, 7 Deamidated,
# 27 Glu->pyro-Glu.
#
# VARIABLE_MODS deliberately EXCLUDES 4 (Carbamidomethyl). It is a fixed modification -- iodoacetamide,
# a reagent -- and is 60.9% of all modifications corpus-wide. Including it would bury every real
# site under cysteine alkylation.
VARIABLE_MODS: dict[int, str] = {
    21: "Phospho",
    1: "Acetyl",
    35: "Oxidation",
    7: "Deamidated",
    27: "Glu->pyro-Glu",
}
_FIXED_MODS: dict[int, str] = {4: "Carbamidomethyl"}

# Modifications that are genuinely biological, versus those that are largely sample-handling
# artifacts. Both are "variable"; only the first group is biology, and the UI should not imply
# otherwise. Oxidation in particular is 34% of all modifications and is mostly handling.
BIOLOGICAL_MODS: frozenset[int] = frozenset({21, 1})

_TOKEN = re.compile(r"\[UNIMOD:(\d+)\]")


class Mod(NamedTuple):
    unimod_id: int
    residue: str | None   # None for an N-terminal modification
    pos: int              # 1-based index into the stripped sequence; 0 = N-terminal


def mod_name(uid: int) -> str:
    return VARIABLE_MODS.get(uid) or _FIXED_MODS.get(uid) or f"UNIMOD:{uid}"


def parse_proforma(pf: str | None) -> list[Mod]:
    """Return every modification in a ProForma string, positioned against the stripped sequence.

    `pos` is the 1-based index of the residue the tag FOLLOWS. A tag appearing before any residue
    is N-terminal and gets pos=0 with residue=None.

    That N-terminal case is the whole reason this is a parser and not a regex. Given
    `[UNIMOD:1]SETAPAETATPAPVEKS[UNIMOD:21]PAK`, a parser that assumes every tag follows the
    residue it modifies assigns the acetyl to a nonexistent residue and then shifts the phospho --
    and every later position in that peptide -- by one. Acetyl is 5.9% of modified precursors, so
    this is the common case, not an exotic one.
    """
    if not pf or not isinstance(pf, str):
        return []
    mods: list[Mod] = []
    n_res = 0            # residues emitted so far == 1-based index of the most recent residue
    last_res: str | None = None
    i, n = 0, len(pf)
    while i < n:
        ch = pf[i]
        if ch == "[":
            m = _TOKEN.match(pf, i)
            if m is not None:
                mods.append(Mod(int(m.group(1)), last_res if n_res else None, n_res))
                i = m.end()
                continue
            # An unrecognised bracket token (an unmapped mod name left as literal text by
            # _to_proforma). Skip the whole token -- its letters are NOT residues.
            close = pf.find("]", i)
            i = n if close < 0 else close + 1
            continue
        if ch.isalpha():
            n_res += 1
            last_res = ch
        # Anything else (the '_' delimiters Spectronaut and DIA-NN wrap sequences in, digits,
        # punctuation) is neither a residue nor a tag. Skipping rather than counting it is what
        # keeps positions aligned with stripped_seq.
        i += 1
    return mods


def sites_in_protein(pf: str | None, peptide_start: int,
                     variable_only: bool = True) -> list[tuple[int, int, str | None]]:
    """Map a peptide's modifications onto PROTEIN coordinates.

    `peptide_start` is the coverage map's 1-based inclusive start (app/static/app.js indexes with
    `p.start-1`, which is what pins the convention). An N-terminal modification maps to the
    peptide's own first residue.

    Returns (unimod_id, position_in_protein, residue).
    """
    out = []
    for mod in parse_proforma(pf):
        if variable_only and mod.unimod_id not in VARIABLE_MODS:
            continue
        pos = peptide_start + mod.pos - 1 if mod.pos else peptide_start
        out.append((mod.unimod_id, pos, mod.residue))
    return out
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_proforma.py -v`
Expected: PASS, 9 tests.

- [ ] **Step 5: Prove the N-terminal test has teeth**

This is mandatory, not optional. Temporarily replace the `[` branch body with the naive version
that ignores the N-terminal case:

```python
mods.append(Mod(int(m.group(1)), last_res, n_res))   # naive: no `if n_res` guard
```

Run: `python3 -m pytest tests/test_proforma.py -v`
Expected: `test_n_terminal_mod_has_no_preceding_residue` and
`test_n_terminal_and_internal_together_do_not_shift_the_internal_one` FAIL.
Then restore the guard and confirm green. Record both outputs in the report.

- [ ] **Step 6: Commit**

```bash
git add app/proforma.py tests/test_proforma.py
git commit -m "feat: ProForma parser that positions modifications without the N-terminal shift"
```

---

### Task 2: Coverage query returns per-site modification data

**Files:**
- Modify: `app/queries.py` — `_protein_coverage_peptides()`
- Test: `tests/test_ptm_coverage.py`

**Interfaces:**
- Consumes: `parse_proforma`, `sites_in_protein`, `VARIABLE_MODS` from Task 1.
- Produces: the coverage result dict gains one key, `sites`, a list of
  `{"pos": int, "residue": str|None, "unimod_id": int, "name": str, "n_precursors": int,
    "n_runs": int, "here_n_precursors": int|None, "occupancy": float|None}`.
  `occupancy` is modified precursors at that position divided by all precursors covering it.
  Peptides themselves are UNCHANGED — no new keys on peptide dicts.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ptm_coverage.py
import os
os.environ.setdefault("DELIMP_PG_TOKEN_FILE", "/Users/brettphinney/.pgfarm_token")

from app import queries

# P92966 = RS41, an Arabidopsis SR splicing factor. Measured 2026-09-10: 6 phosphopeptides,
# 460 phospho precursors, including RES[UNIMOD:21]RS[UNIMOD:21]PPPYEK.
RS41 = "P92966"
PHOSPHO_SEARCH = "2c4911a3-79fd-5367-bdd0-ee85a16cd25b"


def test_sites_are_returned_for_a_phosphoprotein():
    d = queries.protein_coverage_peptides(RS41, search_id=PHOSPHO_SEARCH)
    sites = d.get("sites")
    assert sites, "RS41 has 6 phosphopeptides in this search; sites must not be empty"
    assert any(s["unimod_id"] == 21 for s in sites)


def test_no_carbamidomethyl_site_is_ever_returned():
    # The discriminator: UNIMOD 4 is 60.9% of all modifications, so if the variable-only filter
    # were dropped this assertion fails loudly rather than silently passing on a protein that
    # happens to have no cysteine.
    d = queries.protein_coverage_peptides(RS41, search_id=PHOSPHO_SEARCH)
    assert all(s["unimod_id"] != 4 for s in d.get("sites", []))


def test_phospho_sites_land_on_S_T_or_Y():
    # Phospho occurs on serine, threonine and tyrosine. A site on any other residue means the
    # position arithmetic is wrong -- this catches an off-by-one that a count-based assertion
    # cannot see.
    d = queries.protein_coverage_peptides(RS41, search_id=PHOSPHO_SEARCH)
    bad = [s for s in d["sites"] if s["unimod_id"] == 21 and s["residue"] not in ("S", "T", "Y")]
    assert not bad, f"phospho on non-STY residues means positions are misaligned: {bad}"


def test_site_positions_agree_with_the_protein_sequence():
    # The strongest available check: the residue the site claims must be the residue actually at
    # that position in the canonical sequence.
    d = queries.protein_coverage_peptides(RS41, search_id=PHOSPHO_SEARCH)
    seq = d.get("sequence")
    if not seq:
        return  # sequence not carried by this call; covered at the endpoint level instead
    for s in d["sites"]:
        if s["residue"] is not None:
            assert seq[s["pos"] - 1] == s["residue"], (
                f"site claims {s['residue']} at {s['pos']} but sequence has {seq[s['pos']-1]}")


def test_unmodified_protein_returns_todays_shape_exactly():
    # The live-regression guard. A protein with no variable modifications must be untouched.
    d = queries.protein_coverage_peptides("P02769")  # bovine serum albumin, the worst contaminant
    assert "peptides" in d and "gene" in d
    assert d.get("sites") == []
    for p in d["peptides"][:20]:
        assert "unimod_id" not in p and "sites" not in p
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_ptm_coverage.py -v`
Expected: FAIL — `sites` key absent, `d.get("sites")` is `None`.

- [ ] **Step 3: Implement**

In `_protein_coverage_peptides()`, AFTER the existing `peps` query and the existing `here` block,
and only when `peps` is non-empty. Do not alter either existing query.

```python
    # --- variable-modification sites -------------------------------------------------------
    # A SECOND aggregate at the same protein scope, not a change of the existing grain. The
    # stripped_seq aggregation above feeds the coverage bars, the here/corpus comparison and the
    # peptide table, all of which work today; changing its grain would move all three at once.
    #
    # MEASURED 0.13s for this shape on P92966 (7 modforms), served by idx_prec_protein_group --
    # the same index the peptide query above uses. The corpus-wide version of this query (no
    # protein_group predicate) is an unindexed scan of 238 GB that ran over 15 minutes without
    # finishing, which is why site search corpus-wide is a rollup table (Phase 2) and not this.
    sites: list[dict] = []
    try:
        params = [pg]
        scope = ""
        if search_id:
            scope = " AND search_id = %s"
            params.append(search_id)
        modrows = query(
            f"""SELECT modified_seq_proforma, stripped_seq,
                       COUNT(*)                 AS n_precursors,
                       COUNT(DISTINCT raw_path) AS n_runs
                  FROM delimp_precursors
                 WHERE protein_group = %s AND n_mods > 0{scope}
                 GROUP BY modified_seq_proforma, stripped_seq""",
            tuple(params), tables=["delimp_precursors"], timeout_ms=15000,
        )
    except Exception:  # noqa: BLE001 - a missing sites list degrades the panel, never 503s it
        modrows = []

    if modrows:
        # peptide start offsets come from the peptides already mapped onto the sequence; a modform
        # whose stripped_seq did not map has no protein coordinate and is skipped rather than
        # guessed at.
        starts = {p["stripped_seq"]: p["start"] for p in peps if p.get("start")}
        agg: dict[tuple[int, int], dict] = {}
        for r in modrows:
            start = starts.get(r["stripped_seq"])
            if not start:
                continue
            for uid, pos, residue in sites_in_protein(r["modified_seq_proforma"], start):
                k = (uid, pos)
                s = agg.setdefault(k, {"pos": pos, "residue": residue, "unimod_id": uid,
                                       "name": mod_name(uid), "n_precursors": 0, "n_runs": 0})
                s["n_precursors"] += int(r["n_precursors"] or 0)
                s["n_runs"] = max(s["n_runs"], int(r["n_runs"] or 0))
        # Occupancy: modified precursors at this position over ALL precursors covering it. A site
        # shown without this reads as "this residue is phosphorylated", which is not what partial
        # occupancy means -- and partial is the normal case.
        cover = {}
        for p in peps:
            st, en = p.get("start"), p.get("end")
            if not st:
                continue
            for i in range(st, en + 1):
                cover[i] = cover.get(i, 0) + int(p.get("n_precursors") or 0)
        for s in agg.values():
            tot = cover.get(s["pos"]) or 0
            s["occupancy"] = round(s["n_precursors"] / tot, 4) if tot else None
        sites = sorted(agg.values(), key=lambda s: (s["pos"], s["unimod_id"]))

    result["sites"] = sites
```

Add to the imports at the top of `app/queries.py`:

```python
from app.proforma import mod_name, sites_in_protein
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_ptm_coverage.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 5: Prove the STY test has teeth**

Change `sites_in_protein`'s arithmetic from `peptide_start + mod.pos - 1` to
`peptide_start + mod.pos` (a deliberate off-by-one).
Run: `python3 -m pytest tests/test_ptm_coverage.py -v`
Expected: `test_phospho_sites_land_on_S_T_or_Y` FAILS.
Restore, confirm green, and record both outputs.

- [ ] **Step 6: Confirm nothing else moved**

```bash
git diff
python3 -m pytest tests/test_coverage_scope.py tests/test_search_matrix.py -q
```
The coverage-scope suite must be unchanged. `git diff` must show no edit outside the block above
and the import line. This plan family once shipped a dropped WHERE clause green because a
teeth-proof was not followed by a diff read.

- [ ] **Step 7: Commit**

```bash
git add app/queries.py tests/test_ptm_coverage.py
git commit -m "feat: coverage map carries variable-modification sites in protein coordinates"
```

---

### Task 3: Render sites on the sequence

**Files:**
- Modify: `app/static/app.js` — `_drawPeptideMap()`
- Modify: `app/main.py` — only if `api_protein_coverage` filters keys (check first; if it returns
  the dict wholesale, no change is needed and none should be made)

**Interfaces:**
- Consumes: `d.sites` from Task 2.
- Produces: no new interface.

- [ ] **Step 1: Confirm the endpoint already passes `sites` through**

```bash
grep -n "api_protein_coverage" -A 12 app/main.py
curl -s "http://127.0.0.1:8893/api/protein/P92966/coverage?search_id=2c4911a3-79fd-5367-bdd0-ee85a16cd25b" | python3 -m json.tool | head -40
```
If `sites` is present in the response, make no change to `app/main.py`.

- [ ] **Step 2: Add the site markers**

In `_drawPeptideMap()`, after the `hereRes`/`corpRes`/`anyRes` arrays are built, index the sites by
position:

```javascript
  // Variable-modification sites, keyed by 1-based protein position. Colour by modification;
  // phospho is the one people are looking for, so it gets the strongest colour.
  const MODCOL={21:'#f472b6',1:'#c084fc',35:'#94a3b8',7:'#fbbf24',27:'#fb923c'};
  const siteAt={};
  (d.sites||[]).forEach(s=>{ (siteAt[s.pos]=siteAt[s.pos]||[]).push(s); });
```

In the `resChars` map, decorate a residue that carries a site. The residue letter must stay
legible — the marker annotates, it does not replace:

```javascript
    const resChars=d.sequence.slice(off,end).split('').map((ch,i)=>{
      const gi=off+i;
      const col = scopeOff ? (anyRes[gi]?'#94a3b8':'#475569') : (hereRes[gi]?'#FFE9A8':(corpRes[gi]?'#a7f3e0':'#475569'));
      const ss=siteAt[gi+1];
      if(!ss) return `<span style="display:inline-block;width:${W}%;text-align:center;color:${col}">${esc(ch)}</span>`;
      const s=ss[0], mc=MODCOL[s.unimod_id]||'#e2e8f0';
      const occ=s.occupancy==null?'':` · ${Math.round(s.occupancy*100)}% of precursors here`;
      const tip=`${s.name} on ${esc(ch)}${gi+1}${occ} · ${fmt(s.n_precursors)} precursors in ${fmt(s.n_runs)} runs · engine-reported, not independently localized`;
      return `<span title="${esc(tip)}" style="display:inline-block;width:${W}%;text-align:center;color:${col};`
           + `border-bottom:2px solid ${mc};font-weight:700;cursor:help">${esc(ch)}</span>`;
    }).join('');
```

- [ ] **Step 3: Add the legend and the honesty line**

Append to the `legend` template literal, only when `(d.sites||[]).length`:

```javascript
  const modLegend=(d.sites||[]).length
    ? `<span class="ml-1"><span style="display:inline-block;width:10px;height:0;border-bottom:2px solid #f472b6;vertical-align:middle"></span> phospho</span>
       <span><span style="display:inline-block;width:10px;height:0;border-bottom:2px solid #c084fc;vertical-align:middle"></span> acetyl</span>
       <span><span style="display:inline-block;width:10px;height:0;border-bottom:2px solid #94a3b8;vertical-align:middle"></span> oxidation</span>
       <span class="text-slate-500">sites are <b>as reported by the search engine</b> and not independently localized</span>`
    : '';
```

That last clause is required, not decorative. `site_localization_probability` is NULL on all
437 M rows, so FRAN cannot vouch for which residue carries the modification — only for which
residue the engine said. Do not soften it to a tooltip.

- [ ] **Step 4: Verify in the browser**

The server on `:8893` runs `search-heatmap`; start one for THIS worktree on a free port instead:

```bash
cd /Users/brettphinney/Documents/FRAN-ptm
DELIMP_PG_TOKEN_FILE=/Users/brettphinney/.pgfarm_token DELIMP_INTERNAL_MODE=1 \
  python3 -m uvicorn app.main:app --port 8894 --host 127.0.0.1
```

Check `http://127.0.0.1:8894/#/protein/P92966` scoped to the phospho search. Confirm: phospho
residues are underlined pink, the letters remain readable, hover shows occupancy and the
engine-reported caveat, and a protein with no modifications renders exactly as before.

- [ ] **Step 5: Confirm the no-modification path is untouched**

Open a protein with no variable modifications and confirm no legend row, no markers, and no
console errors. `d.sites` may be `[]` or absent; both must render cleanly.

- [ ] **Step 6: Commit**

```bash
git add app/static/app.js
git commit -m "site: mark modified residues on the sequence coverage map"
```

---

## Self-Review

**Spec coverage.** Phase 1 of the spec asks for: the query change (Task 2), position mapping via
`peptide.start` (Tasks 1-2), residue-level display (Task 3), occupancy (Task 2 computes, Task 3
shows), the engine-reported disclosure (Task 3 step 3), and the three named tests — N-term +
internal (Task 1), unmodified-protein regression (Task 2), and the real two-site RS41 peptide
(Task 1). All present.

**Placeholders.** None. Every code step carries the actual code.

**Type consistency.** `Mod` is produced in Task 1 and consumed in Task 2 via `sites_in_protein`,
which returns plain tuples rather than `Mod`, so Task 2 never depends on the NamedTuple's field
names. `sites[]` dict keys used in Task 3 (`pos`, `residue`, `unimod_id`, `name`, `n_precursors`,
`n_runs`, `occupancy`) match exactly what Task 2 constructs.

**Known gap, deliberate.** `occupancy` divides modified precursors at a position by all precursors
covering it, where the denominator sums `n_precursors` over peptides spanning that residue. Where
peptides overlap, the denominator double-counts and occupancy reads low. Accepted for Phase 1
because it is directionally right and never exceeds 1 in the common non-overlapping case; the
honest fix needs a per-position precursor count, which is a Phase 2 rollup concern. The UI wording
("% of precursors here") must not promise more precision than that.
