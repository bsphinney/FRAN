"""Search organism must come from the search database, never from the contaminant library.

Background (measured 2026-09-16): a Spectronaut search of human cell samples ran against a custom
ORF FASTA plus Spectronaut's Universal Contaminant Protein FASTA. 15,371 of its 15,503 precursors
were contaminant-library hits (11,763 bovine serum proteins); the ORF database contributed 132, all
"Unknown". corpus_ingest took the most common PEP.AllOccurringOrganisms value and recorded Bos taurus
for every run -- and for 224 runs of two sibling searches of the same samples.
"""
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_INGEST = os.path.join(_HERE, "..", "ingest")
sys.path.insert(0, _INGEST)   # engine_fasta imports organism the same way corpus_ingest does


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_INGEST, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ---------------------------------------------------------------------------
# is_contaminant_group
# ---------------------------------------------------------------------------

def test_contaminant_library_prefixes():
    org = _load("organism")
    assert org.is_contaminant_group("Cont_P02769")                 # Spectronaut universal FASTA
    assert org.is_contaminant_group("Cont_P60712;Cont_P68103")
    assert org.is_contaminant_group("CON__P02769")                 # MaxQuant contaminants.fasta
    assert org.is_contaminant_group("cRAP-001")


def test_mixed_group_is_not_a_contaminant():
    # The peptide is also explained by the search database, so it still describes the sample.
    org = _load("organism")
    assert not org.is_contaminant_group("Cont_P02768;P02768")
    assert not org.is_contaminant_group("P02769")
    assert not org.is_contaminant_group(None)
    assert not org.is_contaminant_group("")


# ---------------------------------------------------------------------------
# vote_organism
# ---------------------------------------------------------------------------

def test_contaminant_dominated_search_is_null_not_bos_taurus():
    # The measured shape: bovine + other contaminant hits swamp a database whose own entries
    # carry no species.
    org = _load("organism")
    pairs = ([("Bos taurus", "Cont_P60712")] * 11763
             + [("Homo sapiens", "Cont_P35908")] * 1227
             + [("Sus scrofa", "Cont_P00761")] * 472
             + [("Unknown", ">c19norep157")] * 132)
    v = org.vote_organism(pairs)
    assert v["organism"] is None
    assert v["n_contaminant"] == 11763 + 1227 + 472
    assert v["n_unlabelled"] == 132


def test_real_proteome_wins_over_more_numerous_contaminants():
    org = _load("organism")
    pairs = ([("Bos taurus", "Cont_P02769")] * 900
             + [("Homo sapiens", "P04637")] * 300
             + [("Homo sapiens (Human)", "P38398")] * 10)
    v = org.vote_organism(pairs)
    assert v["organism"] == "Homo sapiens"
    assert v["n_contaminant"] == 900


def test_bovine_sample_is_still_bovine():
    # Excluding the contaminant LIBRARY must not erase a genuinely bovine search: its own database
    # entries are not Cont_-prefixed.
    org = _load("organism")
    v = org.vote_organism([("Bos taurus", "P02769")] * 50 + [("Homo sapiens", "Cont_P35908")] * 80)
    assert v["organism"] == "Bos taurus"


def test_unique_peptides_decide_shared_do_not():
    org = _load("organism")
    pairs = ([("Cicer arietinum;Homo sapiens", "Q1;P1")] * 500
             + [("Cicer arietinum", "Q2")] * 40 + [("Homo sapiens", "P2")] * 5)
    assert org.vote_organism(pairs)["organism"] == "Cicer arietinum"


def test_shared_only_falls_back_to_parts():
    org = _load("organism")
    v = org.vote_organism([("Macaca mulatta;Macaca fascicularis", "P1")] * 3)
    assert v["organism"] in ("Macaca mulatta", "Macaca fascicularis")
    assert v["n_shared"] == 3


def test_sentinels_never_vote():
    org = _load("organism")
    v = org.vote_organism([("Unknown", "P1"), ("nan", "P2"), (None, "P3"), ("", "P4")])
    assert v["organism"] is None and v["n_unlabelled"] == 4


def test_no_organism_column_at_all():
    # DIA-NN records carry no organism: unchanged behaviour, NULL.
    org = _load("organism")
    assert org.vote_organism([(None, "P1")] * 10)["organism"] is None


# ---------------------------------------------------------------------------
# engine_fasta.fasta_species
# ---------------------------------------------------------------------------

def _fasta(tmp, name, headers):
    p = os.path.join(tmp, name)
    with open(p, "w") as fh:
        for h in headers:
            fh.write(h + "\nPEPTIDEK\n")
    return p


def test_species_from_uniprot_headers_ignores_contaminant_entries():
    import tempfile
    ef = _load("engine_fasta")
    with tempfile.TemporaryDirectory() as tmp:
        p = _fasta(tmp, "gg_HoSa_rUP5640.fasta",
                   [f">sp|P{i:05d}|X{i}_HUMAN Protein OS=Homo sapiens OX=9606 GN=G{i} PE=1 SV=1"
                    for i in range(90)]
                   + [f">Cont_P{i:05d} Serum albumin OS=Bos taurus OX=9913" for i in range(200)])
        assert ef.fasta_species(p) == ("Homo sapiens", 9606, 1.0)


def test_custom_orf_database_has_no_species():
    import tempfile
    ef = _load("engine_fasta")
    with tempfile.TemporaryDirectory() as tmp:
        p = _fasta(tmp, "custom-ORFs.fasta", [f">c10norep{i}" for i in range(50)])
        assert ef.fasta_species(p) is None


def test_multi_organism_database_is_left_to_the_vote():
    import tempfile
    ef = _load("engine_fasta")
    with tempfile.TemporaryDirectory() as tmp:
        p = _fasta(tmp, "dog_plus_yeast.fasta",
                   [f">sp|A{i}|A_CANLF x OS=Canis lupus familiaris OX=9615" for i in range(50)]
                   + [f">sp|B{i}|B_YEAST x OS=Saccharomyces cerevisiae (strain ATCC 204508 / S288c) "
                      f"OX=559292" for i in range(50)])
        assert ef.fasta_species(p) is None


def test_unreadable_fasta_is_none():
    ef = _load("engine_fasta")
    assert ef.fasta_species(None) is None
    assert ef.fasta_species("gg_HoSa_rUP5640.fasta") is None   # Spectronaut's bare filename
