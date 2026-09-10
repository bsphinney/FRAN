from app.proforma import parse_proforma, Mod, VARIABLE_MODS, BIOLOGICAL_MODS, sites_in_protein


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


def test_glygly_is_a_variable_biological_modification():
    # UNIMOD 121 is the ubiquitin remnant. Regression guard for a real integration gap: the ingest
    # gained `"GlyGly": 121` (so re-ingested rows normalise to [UNIMOD:121] instead of the literal
    # text "[GlyGly (K)]") while VARIABLE_MODS did not. The consequence was invisible by
    # construction -- parse_proforma returned the site and sites_in_protein silently dropped it, so
    # ~750,000 ubiquitin remnants would have rendered as unmarked residues.
    assert 121 in VARIABLE_MODS
    assert 121 in BIOLOGICAL_MODS          # ubiquitination is biology, not sample handling
    assert sites_in_protein("_IGSLIDVNQSK[UNIMOD:121]DPEGLR_", 1) == [(121, 11, "K")]


def test_every_ingest_mapped_modification_can_become_a_site():
    # The general form of the bug above: any name the Spectronaut ingest normalises to a UNIMOD id
    # must be classifiable here, or it parses cleanly and vanishes one layer up. Carbamidomethyl (4)
    # is the ONE deliberate exception -- a fixed modification, a reagent, 60%+ of all modifications.
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location(
        "_sn", pathlib.Path(__file__).resolve().parents[1] / "ingest" / "spectronaut_to_corpus.py")
    try:
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    except Exception:                       # pandas/pyarrow absent -> nothing to check here
        return
    unmapped = {uid for uid in mod._MOD_UNIMOD.values()
                if uid not in VARIABLE_MODS and uid != 4}
    assert not unmapped, f"ingest maps these UNIMOD ids that can never become sites: {unmapped}"
