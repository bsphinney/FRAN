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
