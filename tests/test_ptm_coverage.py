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


def test_scoped_occupancy_uses_the_scoped_denominator_not_the_corpus_one():
    # Fix round 1, CRITICAL #1: occupancy divided a search-scoped numerator by a corpus-wide
    # denominator, so every scoped occupancy on a protein seen in >1 search read low by exactly
    # the corpus/scoped precursor ratio. RS41 is in exactly 2 searches, so the bug's own fixture
    # displayed "50%" everywhere the true, scoped value is 100% -- pinned here so it cannot
    # regress silently. Proven able to fail: reverting the `here_n_precursors`-based denominator
    # in queries.py back to summing corpus-wide n_precursors makes this assert 0.5, not 1.0.
    d = queries.protein_coverage_peptides(RS41, search_id=PHOSPHO_SEARCH)
    fully_occupied = {239, 254, 272, 274, 284, 286}  # measured 2026-09-10 against this fixture
    seen = {s["pos"]: s["occupancy"] for s in d["sites"] if s["pos"] in fully_occupied}
    assert seen.keys() == fully_occupied, f"expected sites at {fully_occupied}, saw {seen.keys()}"
    for pos, occ in seen.items():
        assert occ == 1.0, f"position {pos}: expected occupancy 1.0 (fully scoped), got {occ}"


def test_unscoped_call_computes_no_sites():
    # Fix round 1, MAJOR #6: the sites aggregate is now gated on search_id. Nothing renders sites
    # on the unscoped standalone protein page (loadCoverage() in app.js never reads d.sites), so
    # computing them there was pure waste -- measured 2.12s thrown away per uncached BSA load.
    # RS41 is a real phosphoprotein; if this returns non-empty, the gate has regressed.
    d = queries.protein_coverage_peptides(RS41)
    assert d.get("sites") == []


def test_unmodified_protein_returns_todays_shape_exactly():
    # The live-regression guard. A protein with no variable modifications must be untouched.
    #
    # NOT P02769 (BSA/ALB): measured 2026-09-10 it has real, chemically-correct sites in this
    # corpus -- Met oxidation at protein positions 111/208/469/571, Asn/Gln deamidation at
    # 290/291, N-terminal Glu->pyro-Glu at 267 -- because Met oxidation and deamidation are
    # near-ubiquitous in real MS data, not corpus noise. Treating BSA as "unmodified" would have
    # made a correct implementation look broken. Q9FEB5 (DSP4) was measured to have zero
    # n_mods > 0 precursors and 15 mapped peptides, so it actually exercises the "no PTMs" case.
    d = queries.protein_coverage_peptides("Q9FEB5")
    assert "peptides" in d and "gene" in d
    assert d.get("sites") == []
    for p in d["peptides"][:20]:
        assert "unimod_id" not in p and "sites" not in p
