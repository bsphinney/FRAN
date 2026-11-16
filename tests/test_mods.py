"""Offline tests for app/mods.py (modification-notation canonicalisation)."""
from app.mods import UNIMOD, canonicalize, merge_forms, resolve_token


def test_the_owner_examples_get_readable_names():
    assert canonicalize("NMQDM[UNIMOD:35]VEDYR")["display"] == "NMQDM[Oxidation]VEDYR"
    assert canonicalize("N[UNIMOD:7]MQDM[UNIMOD:35]VEDYR")["display"] == "N[Deamidated]MQDM[Oxidation]VEDYR"
    assert canonicalize("NM[UNIMOD:35]QDM[Dioxidation (MW)]VEDYR")["display"] == "NM[Oxidation]QDM[Dioxidation]VEDYR"
    assert canonicalize("NM[Dioxidation (MW)]QDM[Dioxidation (MW)]VEDYR")["display"] == "NM[Dioxidation]QDM[Dioxidation]VEDYR"
    assert canonicalize("NM[UNIMOD:35]QD[Cation:Na (DE)]M[UNIMOD:35]VEDYR")["display"] == \
        "NM[Oxidation]QD[Cation:Na]M[Oxidation]VEDYR"


def test_all_notations_of_one_chemistry_share_a_key():
    keys = {canonicalize(s)["key"] for s in (
        "AAC[UNIMOD:4]K", "AAC[Carbamidomethyl (C)]K", "AAC[+C2+H3+N+O]K", "AAC[+57.0215]K", "AAC[+57.02]K")}
    assert keys == {"AAC[UNIMOD:4]K"}
    assert {canonicalize(s)["key"] for s in ("AM[UNIMOD:35]K", "AM[Oxidation (M)]K", "AM[+O]K", "AM[+15.99]K")} \
        == {"AM[UNIMOD:35]K"}


def test_name_variants():
    assert resolve_token("Deamidation")["accession"] == 7
    assert resolve_token("Carbamylation (KR)")["accession"] == 5
    assert resolve_token("MMTS")["accession"] == 39
    assert resolve_token("GlyGly (K): 95.4%")["accession"] == 121
    assert resolve_token("Trimethyl (K)")["accession"] == 37
    assert resolve_token("Carboxymethyl (Any N-term)")["accession"] == 6
    assert resolve_token("Dehydrated (ST)")["accession"] == 23


def test_nested_bracket_name_is_one_token():
    c = canonicalize("PTGK[Hex (K)]RVAE[Cation:Mg[II]]DDEDK")
    assert c["display"] == "PTGK[Hex]RVAE[Cation:Mg[II]]DDEDK"
    assert c["unmapped"] == []


def test_unknown_tokens_pass_through_unchanged_and_flagged():
    c = canonicalize("ALYDNLHPSR[SILAC][SILAC]")
    assert c["unmapped"] == ["SILAC", "SILAC"]       # multiple per residue are kept, not collapsed
    assert c["key"] == "ALYDNLHPSR[SILAC][SILAC]"
    assert c["display"] == "ALYDNLHPSR[SILAC][SILAC]"
    c = canonicalize("K[DEL-gg_Cys137L]AA")
    assert c["unmapped"] == ["DEL-gg_Cys137L"]
    # a UNIMOD accession we hold no name for is also flagged, never renamed
    assert canonicalize("K[UNIMOD:99999]A")["unmapped"] == ["UNIMOD:99999"]


def test_ambiguous_or_imprecise_delta_masses_are_not_guessed():
    # -18.0106 is both Dehydrated (23) and Glu->pyro-Glu (27): a mass cannot choose.
    assert resolve_token("-18.0106")["accession"] is None
    # "+42.0" is within 0.05 of both Acetyl and Trimethyl.
    assert resolve_token("+42.0")["accession"] is None
    # mass in the corpus table with no entry we carry
    assert resolve_token("+999.9")["accession"] is None
    # formula with an element we cannot weigh
    assert resolve_token("+C2+Xx")["accession"] is None
    # real corpus elemental tokens that match no table entry stay as written
    assert resolve_token("+C8+H11+N+O")["accession"] is None


def test_elemental_forms_in_the_corpus():
    assert resolve_token("+C2+H3+N+O")["accession"] == 4
    assert resolve_token("+O")["accession"] == 35
    assert resolve_token("+C2+H2+O")["accession"] == 1
    assert resolve_token("+C12+H20+N2+O2")["accession"] == 739


def test_n_terminal_mod_and_positions():
    c = canonicalize("[UNIMOD:1]SETAPAETATPAPVEKS[UNIMOD:21]PAK")
    assert c["display"] == "[Acetyl]SETAPAETATPAPVEKS[Phospho]PAK"
    assert [(m["pos"], m["residue"], m["accession"]) for m in c["mods"]] == [(0, None, 1), (17, "S", 21)]


def test_stacked_mods_on_one_residue_are_order_independent():
    a = canonicalize("AK[UNIMOD:21][UNIMOD:35]A")["key"]
    b = canonicalize("AK[Oxidation][UNIMOD:21]A")["key"]
    # second string's bare name "Oxidation" resolves too
    assert a == b == "AK[UNIMOD:21][UNIMOD:35]A"


def test_unmodified_none_and_empty():
    assert canonicalize("AADDTWEPFASGK")["key"] == "AADDTWEPFASGK"
    assert canonicalize(None)["key"] is None
    assert canonicalize("")["segments"] == []


def test_isoleucine_leucine_irrelevant():
    # Canonicalisation never touches residues, so I and L keep their spelling.
    assert canonicalize("PEPTIDEIL[UNIMOD:35]")["display"] == "PEPTIDEIL[Oxidation]"


def test_every_table_entry_is_reachable_by_accession_and_name():
    for acc, (name, mass, _comp) in UNIMOD.items():
        assert resolve_token(f"UNIMOD:{acc}")["accession"] == acc
        assert resolve_token(name)["name"] is not None


def _row(raw, charge, n, mz, rt, q, engines, im=None, l2=None):
    return {"modified_seq_proforma": raw, "charge": charge, "n_obs": n, "avg_mz": mz, "avg_rt": rt,
            "avg_im": im, "best_q_value": q, "avg_log2_int": l2, "max_engines": 1, "engines": engines}


def test_merge_sums_obs_weights_averages_and_unions_engines():
    rows = [
        _row("NM[UNIMOD:35]QDMVEDYR", 2, 30, 650.0, 20.0, 1e-3, ["DIA-NN"], l2=20.0),
        _row("NM[Oxidation (M)]QDMVEDYR", 2, 10, 650.0, 24.0, 1e-5, ["Spectronaut"], l2=None),
        _row("NM[UNIMOD:35]QDMVEDYR", 3, 5, 433.0, 20.0, 1e-4, ["DIA-NN"]),
        _row("NMQDM[SILAC]VEDYR", 2, 7, 640.0, 21.0, None, ["Spectronaut"]),
    ]
    out = merge_forms(rows)
    assert len(out) == 3
    ox2 = next(r for r in out if r["charge"] == 2 and r["modified_seq_proforma"] == "NM[UNIMOD:35]QDMVEDYR")
    assert ox2["n_obs"] == 40
    assert ox2["avg_rt"] == (30 * 20.0 + 10 * 24.0) / 40
    assert ox2["avg_log2_int"] == 20.0                 # only the row that has the value contributes
    assert ox2["best_q_value"] == 1e-5
    assert ox2["engines"] == ["DIA-NN", "Spectronaut"]
    assert ox2["n_notations"] == 2 and set(ox2["notations"]) == {"NM[UNIMOD:35]QDMVEDYR", "NM[Oxidation (M)]QDMVEDYR"}
    assert ox2["display"] == "NM[Oxidation]QDMVEDYR"
    # same canonical form at another charge stays separate
    assert next(r for r in out if r["charge"] == 3)["n_obs"] == 5
    # unmapped row kept as-is and flagged
    sil = next(r for r in out if r["has_unmapped"])
    assert sil["display"] == "NMQDM[SILAC]VEDYR" and sil["n_notations"] == 1
    assert [r["n_obs"] for r in out] == sorted((r["n_obs"] for r in out), reverse=True)


def test_merge_does_not_merge_across_unknown_tokens():
    rows = [_row("AK[X1]A", 2, 1, 1, 1, None, []), _row("AK[X2]A", 2, 1, 1, 1, None, [])]
    assert len(merge_forms(rows)) == 2
