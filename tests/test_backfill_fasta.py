"""backfill_fasta.py's decisions: where a recorded path lives on Hive, which .sne may stand in for
a search, and what happens to the delimp_searches summary.

The case that motivates `correct`: 59 searches recorded a cRAP list or iRT.fasta as their database
because the old detector took the first non-"contaminant"-named file. The case that motivates
`conflict`: a stored path that names none of the search's databases is a disagreement between two
sources, and neither is proof, so it is reported and left alone.
"""
import gzip
import importlib.util
import os
import struct
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_INGEST = os.path.join(_HERE, "..", "ingest")
sys.path.insert(0, _INGEST)

_spec = importlib.util.spec_from_file_location("backfill_fasta", os.path.join(_INGEST, "backfill_fasta.py"))
bf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bf)

CRAP = {"file_name": "Crap_uniprot_No_human_MRSonbeadV2.fasta", "n_entries": 38, "is_contaminant": True}
IRT = {"file_name": "iRT.fasta", "n_entries": 11, "is_contaminant": False}
HUMAN = {"file_name": "gg_HoSa_rUP00005640.fasta", "n_entries": 20366, "is_contaminant": False}


def test_windows_and_mac_paths_map_onto_hive():
    assert bf.hive_path(r"R:\Data\lab\service\x\a.sne") == "/nfs/lssc0/flinders/proteomics/Data/lab/service/x/a.sne"
    assert bf.hive_path(r"K:\Data\lab\b.sne") == "/nfs/lssc0/flinders/proteomics/Data/lab/b.sne"
    assert bf.hive_path(r"E:\\BRETT\c.sne") is None                 # a local Windows disk
    assert bf.hive_path("/Volumes/proteomics/Data/x") == "/nfs/lssc0/flinders/proteomics/Data/x"
    assert bf.hive_path("/Volumes/proteomics-grp/SERVICE/y") == "/quobyte/proteomics-grp/SERVICE/y"


def test_summary_fill_same_correct_conflict():
    dbs = [CRAP, IRT, HUMAN]
    assert bf.summary_action({"fasta_path": None}, dbs)[0] == "fill"
    assert bf.summary_action({"fasta_path": "gg_HoSa_rUP00005640.fasta"}, dbs)[0] == "same"
    act, summ = bf.summary_action({"fasta_path": "Crap_uniprot_No_human_MRSonbeadV2.fasta"}, dbs)
    assert act == "correct" and summ["fasta_path"] == "gg_HoSa_rUP00005640.fasta"
    assert summ["fasta_n_proteins"] == 20366
    assert bf.summary_action({"fasta_path": "iRT.fasta"}, dbs)[0] == "correct"
    assert bf.summary_action({"fasta_path": "something_else.fasta"}, dbs)[0] == "conflict"


def test_a_tiny_bait_construct_is_corrected():
    bait = {"file_name": "KimTurboID.fasta", "n_entries": 1, "is_contaminant": False}
    assert bf.summary_action({"fasta_path": "KimTurboID.fasta"}, [bait, HUMAN])[0] == "correct"


def test_a_second_real_proteome_is_kept_not_corrected():
    # host-pathogen: the stored pathogen database is a real choice, not an error
    fungus = {"file_name": "C_posadasii_ref-seq-ramona.fasta", "n_entries": 8516,
              "is_contaminant": False, "file_md5": "abc"}
    act, vals = bf.summary_action({"fasta_path": "C_posadasii_ref-seq-ramona.fasta"}, [CRAP, fungus, HUMAN])
    assert act == "keep"
    assert vals["fasta_path"] == "C_posadasii_ref-seq-ramona.fasta"
    assert vals["fasta_n_proteins"] == 8516 and vals["fasta_md5"] == "abc"   # its own numbers


def test_raw_stems_drop_the_extension():
    assert bf.raw_stems(["30une-2025__60SPD_DIA-MT3_S3-F3_1_14480.d", r"C:\x\DERO_S6-D5_1_9089.htrms"]) == \
        ["30une-2025__60SPD_DIA-MT3_S3-F3_1_14480", "DERO_S6-D5_1_9089"]


def _s(t):
    b = t.encode("utf-16le")
    return struct.pack("<i", len(b)) + b


def test_a_name_only_match_must_name_one_of_the_search_raws(tmp_path):
    p = tmp_path / "20241105_134105_DERO direct DIA.sne"
    p.write_bytes(struct.pack("<i", 71) + _s("DERO direct DIA") + _s("DERO_S6-D5_1_9089.htrms")
                  + gzip.compress(b"x" * 100))
    assert bf.sne_mentions_raws(str(p), ["DERO_S6-D5_1_9089"])
    assert not bf.sne_mentions_raws(str(p), ["SomeOtherRun_1_1234"])
    assert not bf.sne_mentions_raws(str(p), [])


def test_candidates_by_name_rank_the_closest_path_first(tmp_path):
    idx = {"a.sne": [(10, "/nfs/x/other/a.sne"), (10, "/nfs/x/projA/sub/a.sne")]}
    got = bf.sne_candidates(r"B:\Automatic_SNE_storage\projA\sub\a.sne", idx)
    assert got[0] == ("/nfs/x/projA/sub/a.sne", "name")
    assert all(how == "name" for _, how in got)


def test_a_fran_reports_export_looks_for_its_original_sne():
    idx = {"spn_x_apr24.sne": [(5, "/nfs/lssc0/flinders/proteomics/Data/lab/service/q/SpN_x_apr24.sne")]}
    got = bf.sne_candidates(bf.FRAN_REPORTS + "SpN_x_apr24/20260627_174949_SpN_x_apr24", idx)
    assert got == [("/nfs/lssc0/flinders/proteomics/Data/lab/service/q/SpN_x_apr24.sne", "name")]


def test_a_recorded_path_that_exists_here_is_used_as_is(tmp_path):
    # on the Windows node, B:\Automatic_SNE_storage\x.sne is local and has no Hive mapping
    p = tmp_path / "local.sne"
    p.write_bytes(b"")
    assert bf.hive_path(r"B:\Automatic_SNE_storage\x.sne") is None
    assert bf.local_path(str(p)) == str(p)
    assert bf.sne_candidates(str(p), {}) == [(str(p), "path")]
    assert bf.local_path(r"R:\Data\lab\a.sne") == "/nfs/lssc0/flinders/proteomics/Data/lab/a.sne"
