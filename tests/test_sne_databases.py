"""engine_fasta.sne_databases(): read every protein database a Spectronaut .sne embeds.

Background (verified 2026-10-21 on real files, Spectronaut 13.8 -> 20.1): a .sne stores each
database it searched -- contaminant lists included -- as length-prefixed UTF-16 strings naming it,
then one gzip member of alternating ">header" / sequence records. From Spectronaut 19 the whole
stream is wrapped again in a chain of [int32 length][gzip member]. These fixtures build both
layouts from scratch, so the reader is tested on the structure rather than on one lucky file.
"""
import gzip
import importlib.util
import os
import struct
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_INGEST = os.path.join(_HERE, "..", "ingest")
sys.path.insert(0, _INGEST)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_INGEST, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ef = _load("engine_fasta")


def _s(text: str) -> bytes:
    b = text.encode("utf-16le")
    return struct.pack("<i", len(b)) + b


def _db(file_name: str, entries: list[tuple[str, str]], after: tuple[str, ...] = ("Mixed",)) -> bytes:
    """[display name, file name, *after, parsing rule] then the entries, as a real .sne stores
    them. `after` reproduces what follows the name in real files."""
    payload = b"".join(_s(">" + h) + _s(seq) for h, seq in entries)
    return (_s(file_name.rsplit(".", 1)[0]) + _s(file_name) + b"".join(_s(a) for a in after)
            + _s("UniProt FASTA") + gzip.compress(payload))


CONTAM = [("sp|Cont_P00722|BGAL_ECOLI Beta-galactosidase OS=Escherichia coli GN=lacZ", "MTMITDSLAV"),
          ("sp|Cont_P02769|ALBU_BOVIN Albumin OS=Bos taurus GN=ALB", "MKWVTFISLL")]
VIRUS = [("QNS29858.1  nucleoprotein [Kasokero virus]", "MSELSFKDVN")]
# two isoforms of one gene plus a second gene: 3 entries, 2 genes
PROTEOME = [("tr|A0A7J8BC41|X_ROUAE Dehydrogenase OS=Rousettus aegyptiacus OX=9407 GN=G1", "MFPRTRLAAL"),
            ("tr|A0A7J8BC42|Y_ROUAE Dehydrogenase iso OS=Rousettus aegyptiacus OX=9407 GN=G1", "MFPRTRLAAV"),
            ("tr|A0A7J8BC43|Z_ROUAE Kinase OS=Rousettus aegyptiacus OX=9407 GN=G2", "MKKLLPAWQE")]


def _inner(filler: int = 0) -> bytes:
    """Settings strings, three databases (contaminant, virus, proteome -- the bat search's
    order), then unrelated trailing data that happens to contain a gzip member."""
    return (struct.pack("<i", 71) + _s("Experiment") + _s("Initializing Pipeline...")
            + _s("Help text naming no file: Allows to specify the protein database.")
            + _db("Universal Contaminant Protein FASTA.fasta", CONTAM)
            + _db("gg_brook_viri-Kasokero-Marburg.fasta", VIRUS)
            + os.urandom(filler)
            + _db("gg_rousetusAegiptiacus-tUP000593571.fasta", PROTEOME)
            + _s("Carbamidomethyl (C)") + gzip.compress(b"not a database at all " * 50))


def _chained(inner: bytes, header: bytes, piece: int) -> bytes:
    out = [header]
    for k in range(0, len(inner), piece):
        m = gzip.compress(inner[k:k + piece])
        out.append(struct.pack("<i", len(m)) + m)
    return b"".join(out)


def _write(tmp_path, data: bytes) -> str:
    p = tmp_path / "x.sne"
    p.write_bytes(data)
    return str(p)


def _fasta(tmp_path, name, entries, wrap=4) -> str:
    p = tmp_path / name
    with open(p, "w") as f:
        for h, seq in entries:
            f.write(">" + h + "\n")
            for k in range(0, len(seq), wrap):
                f.write(seq[k:k + wrap] + "\n")
    return str(p)


def _check_three(dbs):
    assert [d["file_name"] for d in dbs] == [
        "Universal Contaminant Protein FASTA.fasta",
        "gg_brook_viri-Kasokero-Marburg.fasta",
        "gg_rousetusAegiptiacus-tUP000593571.fasta"]
    assert [d["n_entries"] for d in dbs] == [2, 1, 3]
    assert [d["is_contaminant"] for d in dbs] == [True, False, False]
    assert all(d["parse_ok"] for d in dbs)
    assert dbs[2]["n_genes"] == 2          # two isoforms of G1 count once
    assert dbs[2]["n_gene_entries"] == 3   # entries-per-gene = 3 / 2
    assert dbs[1]["n_genes"] is None       # no GN= at all: not reported as 0 genes
    assert dbs[1]["n_gene_entries"] is None


def test_plain_layout_reads_every_database_in_order(tmp_path):
    _check_three(ef.sne_databases(_write(tmp_path, _inner())))


def test_chained_layout_spectronaut_19(tmp_path):
    inner = _inner(filler=3000)
    header = struct.pack("<i", 76) + _s("SpNd_Experiment") + _s("run1.d")
    # pieces far smaller than a database, so every embedded member spans several outer ones
    _check_three(ef.sne_databases(_write(tmp_path, _chained(inner, header, piece=97))))


def test_name_is_the_nearest_not_the_previous_database(tmp_path):
    # the tiny virus database sits a few bytes before the proteome; the proteome must not be
    # labelled with the virus file's name or vice versa
    dbs = ef.sne_databases(_write(tmp_path, _inner(filler=0)))
    assert dbs[2]["file_name"].startswith("gg_rousetus")
    assert dbs[1]["file_name"].startswith("gg_brook")


def test_embedded_hash_equals_the_file_on_disk(tmp_path):
    dbs = ef.sne_databases(_write(tmp_path, _inner()))
    f = ef.fasta_digest(_fasta(tmp_path, "p.fasta", PROTEOME, wrap=3))
    assert f["content_sha256"] == dbs[2]["content_sha256"]
    assert f["n_entries"] == 3 and f["n_genes"] == 2


def test_hash_ignores_entry_order_and_line_wrapping(tmp_path):
    a = ef.fasta_digest(_fasta(tmp_path, "a.fasta", PROTEOME, wrap=3))
    b = ef.fasta_digest(_fasta(tmp_path, "b.fasta", list(reversed(PROTEOME)), wrap=60))
    assert a["content_sha256"] == b["content_sha256"]
    assert a["md5"] != b["md5"]            # the file checksum still tells the files apart


def test_a_different_database_hashes_differently(tmp_path):
    a = ef.fasta_digest(_fasta(tmp_path, "a.fasta", PROTEOME))
    b = ef.fasta_digest(_fasta(tmp_path, "b.fasta", PROTEOME[:2]))
    assert a["content_sha256"] != b["content_sha256"]


def test_no_database_is_an_empty_list_and_unreadable_is_none(tmp_path):
    p = _write(tmp_path, struct.pack("<i", 71) + _s("Experiment") + gzip.compress(b"x" * 999))
    assert ef.sne_databases(p) == []
    assert ef.sne_databases(str(tmp_path / "missing.sne")) is None


def test_name_followed_by_a_letter_like_length(tmp_path):
    # Coccidioides, from a real 14.10 .sne: the organism string after the file name is 36 chars,
    # so its int32 length (72) reads as UTF-16 'H' -- a pattern match lost the name entirely
    data = (struct.pack("<i", 71) + _s("Experiment")
            + _db("uniprot-proteome_UP000009084.fasta", PROTEOME,
                  after=("Coccidioides posadasii (strain C735)", "Mixed")))
    assert ef.sne_databases(_write(tmp_path, data))[0]["file_name"] == "uniprot-proteome_UP000009084.fasta"


def test_file_name_not_a_later_setting_that_mentions_a_fasta(tmp_path):
    # real 14.10 .sne: ['iRT', 'iRT.fasta', 'Unknown', ..., 'iRT peptides-fusion.fasta Rule']
    data = (struct.pack("<i", 71) + _s("Experiment")
            + _db("iRT.fasta", VIRUS, after=("Unknown", "Accession", "iRT peptides-fusion.fasta Rule")))
    assert ef.sne_databases(_write(tmp_path, data))[0]["file_name"] == "iRT.fasta"


def test_crap_lists_count_as_contaminants():
    assert ef.is_contaminant_db("Crap_uniprot_No_human_MRSonbeadV2.fasta", 0.0, 38)
    assert ef.is_contaminant_db("anything.fasta", 0.9, 50000)
    assert not ef.is_contaminant_db("UP000005640_9606.fasta", 0.01, 20598)


def test_main_database_is_the_largest_real_one_not_the_first_listed(tmp_path):
    dbs = ef.sne_databases(_write(tmp_path, _inner()))
    assert ef.main_database(dbs)["file_name"].startswith("gg_rousetus")
    s = ef.summarize(dbs)
    assert s["fasta_n_proteins"] == 3
    assert s["contaminant_lib"] == "Universal Contaminant Protein FASTA.fasta"


def test_irt_and_crap_are_never_the_main_database():
    dbs = [{"file_name": "Crap_uniprot_No_human_MRSonbeadV2.fasta", "n_entries": 38, "is_contaminant": True},
           {"file_name": "iRT.fasta", "n_entries": 11, "is_contaminant": False},
           {"file_name": "gg_HoSa_rUP00005640.fasta", "n_entries": None, "is_contaminant": False}]
    assert ef.main_database(dbs)["file_name"] == "gg_HoSa_rUP00005640.fasta"


def test_detect_reads_the_sne_when_output_dir_is_one(tmp_path):
    p = _write(tmp_path, _inner())
    _fasta(tmp_path, "gg_rousetusAegiptiacus-tUP000593571.fasta", PROTEOME)   # the same database
    found = ef.detect_all("spectronaut", None, p)
    assert found["source"] == "sne" and len(found["databases"]) == 3
    bat = found["databases"][2]
    assert bat["file_md5"] and bat["file_path"].endswith("tUP000593571.fasta")
    assert found["databases"][1]["file_md5"] is None        # no virus file on disk
    assert ef.detect("spectronaut", None, p)["fasta_path"] == bat["file_path"]


def test_a_same_named_but_different_file_is_not_verified(tmp_path):
    p = _write(tmp_path, _inner())
    _fasta(tmp_path, "gg_rousetusAegiptiacus-tUP000593571.fasta", PROTEOME[:1])  # re-downloaded, differs
    bat = ef.detect_all("spectronaut", None, p)["databases"][2]
    assert bat["file_md5"] is None and bat["file_path"] is None
    assert bat["n_entries"] == 3                             # counts still come from the .sne


def test_diann_log_records_every_fasta(tmp_path):
    a = _fasta(tmp_path, "UP000005640_9606.fasta", PROTEOME)
    (tmp_path / "report.log.txt").write_text(
        "DIA-NN 2.7.0\n\n\n\n\ndiann-linux --f x.d --fasta " + a
        + " --fasta /gone/Universal Contaminant Protein FASTA.fasta --threads 8\n")
    found = ef.detect_all("diann", None, str(tmp_path))
    names = [(d["file_name"], d["is_contaminant"], d["n_entries"]) for d in found["databases"]]
    assert names == [("UP000005640_9606.fasta", False, 3),
                     ("Universal Contaminant Protein FASTA.fasta", True, None)]
    assert ef.detect("diann", None, str(tmp_path))["fasta_path"] == a


def test_a_proteome_with_contaminants_appended_is_the_search_database():
    # real files: 74,792 entries of human + HIV + cRAP; human + vaccinia + contaminants
    assert not ef.is_contaminant_db("Human_HIV_crap_nonredun.fasta", 0.005, 74792)
    assert not ef.is_contaminant_db("bp_UP000005640_9606_oct_2019_vaccinia_contaminants.fasta", 0.01, 20953)
    assert ef.is_contaminant_db("Universal Contaminant Protein FASTA.fasta", None, None)
