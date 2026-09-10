"""PTM site localization ingest -- the columns Spectronaut already exports and
ingest/spectronaut_to_corpus.py never read.

Background (measured 2026-09-10): site_localization_probability is populated on 0 of 437M
delimp_precursors rows, not because the data is missing but because COLMAP never asked for
EG.PTMLocalizationProbabilities and _MOD_UNIMOD had no entry for GlyGly (UniMod 121, the
ubiquitin/NEDD8/ISG15 remnant), so _to_proforma() silently passed "[GlyGly (K)]" through as
literal text for every one of ~750,000 GG precursors.
"""
import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "ingest", "spectronaut_to_corpus.py")


def _mod():
    spec = importlib.util.spec_from_file_location("spectronaut_to_corpus", _SRC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ---------------------------------------------------------------------------
# _parse_localization_min: EG.PTMLocalizationProbabilities -> a 0-1 fraction
# ---------------------------------------------------------------------------

def test_single_site_full_confidence():
    # Real corpus string (Bennett_Penn_Ubiq_July_2023 report, verified on Hive).
    s = "_IGSLIDVNQSK[GlyGly (K): 100%]DPEGLR_"
    assert _mod()._parse_localization_min(s) == 1.0


def test_multi_site_takes_the_minimum_not_the_mean_or_max():
    # THE discriminator a single-site test cannot catch: one GlyGly mark, split by Spectronaut
    # across two candidate K's with DIFFERENT probabilities (95.4% / 4.6%, real string verified
    # on Hive from Bennett_Penn_Ubiq_July_2023). Mean would report ~50%, max would report 95.4%
    # -- both would overstate how confidently this precursor's mod is actually placed. Only the
    # minimum (4.6%) reflects that the true site is genuinely unresolved.
    s = "_K[GlyGly (K): 95.4%]IQSSLSVNNDISK[GlyGly (K): 4.6%]K_"
    prob = _mod()._parse_localization_min(s)
    assert prob is not None
    assert abs(prob - 0.046) < 1e-9


def test_minimum_spans_different_modification_names_on_the_same_precursor():
    # Real corpus string: a confidently localized GlyGly (100%) alongside an UNRELATED oxidation
    # the search could not place between two candidate methionines (100% / 0%). The whole-
    # precursor gate in fran_schema.sql (`n_mods = 0 OR site_localization_probability > 0.75`)
    # exists to flag "some mod position on this precursor is not trustworthy" -- scoping the
    # minimum to GlyGly-only would have reported 100% and missed the unresolved oxidation.
    s = "_AAM[Oxidation (M): 100%]EALVVEVSK[GlyGly (K): 100%]QPNIISQLDPVNEHM[Oxidation (M): 0%]LNTIR_"
    assert _mod()._parse_localization_min(s) == 0.0


def test_no_probability_annotation_returns_none():
    assert _mod()._parse_localization_min("_IGSLIDVNQSKDPEGLR_") is None
    assert _mod()._parse_localization_min(None) is None
    assert _mod()._parse_localization_min("") is None


# ---------------------------------------------------------------------------
# COLMAP resolution for the three unparameterized PTM localization columns
# ---------------------------------------------------------------------------

_PTM_HDR_TSV = [
    "R.FileName", "PEP.StrippedSequence", "EG.ModifiedSequence", "FG.Charge", "EG.Qvalue",
    "EG.PTMLocalizationProbabilities", "EG.PTMAssayProbability", "EG.HasLocalizationInformation",
    "EG.PTMPositions [GlyGly (K)]", "EG.PTMProbabilities [GlyGly (K)]",
]
_PTM_HDR_PARQUET = [c.replace(".", "_", 1).replace(" [", "_[").replace(" (", "_(") for c in _PTM_HDR_TSV]


def test_resolves_the_unparameterized_localization_columns():
    cols = _mod().resolve_columns(_PTM_HDR_TSV)
    assert cols.get("ptm_localization") == "EG.PTMLocalizationProbabilities"
    assert cols.get("ptm_assay_probability") == "EG.PTMAssayProbability"
    assert cols.get("has_localization") == "EG.HasLocalizationInformation"


def test_does_not_match_the_mod_name_parameterized_columns():
    # These vary per search (one column per modification actually searched for) and are
    # deliberately out of scope -- a fixed regex would be brittle against them.
    cols = _mod().resolve_columns(_PTM_HDR_TSV)
    assert cols.get("ptm_localization") != "EG.PTMPositions [GlyGly (K)]"


# ---------------------------------------------------------------------------
# _to_proforma: GlyGly must map to UNIMOD:121, and any unmapped name must be COUNTED
# ---------------------------------------------------------------------------

def test_glygly_maps_to_unimod_121_and_leaves_no_literal_text():
    pf = _mod()._to_proforma("_IGSLIDVNQSK[GlyGly (K)]DPEGLR_")
    assert pf == "IGSLIDVNQSK[UNIMOD:121]DPEGLR"
    assert "GlyGly" not in pf


def test_an_unmapped_modification_name_is_counted_not_silently_passed_through():
    unmapped = {}
    pf = _mod()._to_proforma("_PEPTIDE[Sumo (K)]K_", unmapped)
    # It's still passed through (never a hard failure mid-ingest) ...
    assert "Sumo" in pf
    # ... but it must be COUNTED, so an ingest run reports it instead of going silent.
    assert unmapped == {"Sumo": 1}


def test_unmapped_counter_accumulates_across_multiple_calls():
    unmapped = {}
    _mod()._to_proforma("_A[Sumo (K)]B_", unmapped)
    _mod()._to_proforma("_C[Sumo (K)]D[Sumo (K)]E_", unmapped)
    assert unmapped == {"Sumo": 3}
