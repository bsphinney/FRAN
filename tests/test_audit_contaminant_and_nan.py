"""Audit 2026-11: contaminant flag by database membership, and no literal "nan" protein groups.

* is_contaminant used to be a NAME regex (KRT|keratin|cont_|contaminant): 42,950 of 42,982 rows of
  the sample's own KRT8/18/19/KRTCAP2 were flagged, and for Spectronaut the flag disagreed with the
  Cont_ accession prefix on 1,011 of 1,493 searches. A protein is a contaminant only when EVERY
  accession of its group comes from the contaminant library (organism.is_contaminant_group).
* A pandas NaN is truthy: `str(r.get(col))` and `str(x.get("protein_group") or "")` wrote the
  literal text "nan" (97,143 precursor rows by 2026-11-18).

Runs under pytest or as a plain script with the project venv.
"""
import importlib.util
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_INGEST = os.path.join(_HERE, "..", "ingest")
sys.path.insert(0, _INGEST)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_INGEST, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _recs():
    nan = float("nan")
    base = {"stripped_seq": "PEPTIDEK", "intensity": 1.0, "normalized_intensity": 1.0, "pg_q_value": 0.001}
    return [
        dict(base, run="r1", protein_group="P05784", gene="KRT18"),                 # sample cytokeratin
        dict(base, run="r1", protein_group="P11679", gene="KRT8"),
        dict(base, run="r1", protein_group="Cont_P02769", gene=None),               # library only
        dict(base, run="r1", protein_group="Cont_P02768;P02768", gene="ALB"),       # explained by sample too
        dict(base, run="r1", protein_group=nan, gene="nan"),                        # float NaN
        dict(base, run="r1", protein_group="nan", gene="nan"),                      # the literal text
        dict(base, run="r1", protein_group=None, gene=None),
        dict(base, run="r1", protein_group="", gene=""),
    ]


def test_sample_keratins_are_not_library_contaminants():
    org = _load("organism")
    for g in ("P05784", "P11679", "P19001"):           # KRT18, KRT8, KRT19 by accession
        assert not org.is_contaminant_group(g)         # prefix-only: organism voting is unchanged


def test_contaminant_flag_is_prefix_then_old_name_rule():
    ci = _load("corpus_ingest")
    src = open(os.path.join(_INGEST, "corpus_ingest.py"), encoding="utf-8").read()
    assert "KRT|keratin|cont_|contaminant" not in src, "the name regex belongs in organism.py only"
    prot = ci._aggregate_proteins(_recs())
    flags = {k[1]: ci.is_contaminant_flag(k[1], a["gene"]) for k, a in prot.items()}
    assert flags["P05784"] is True and flags["P11679"] is True   # 'suspected': KRT by name, no library accession
    assert flags["Cont_P02769"] is True
    assert flags["Cont_P02768;P02768"] is False        # mixed group: the sample database also explains it


def test_missing_protein_group_is_never_a_protein_row():
    ci = _load("corpus_ingest")
    prot = ci._aggregate_proteins(_recs())
    groups = {k[1] for k in prot}
    assert groups == {"P05784", "P11679", "Cont_P02769", "Cont_P02768;P02768"}
    assert not any(g.lower() in ("nan", "none", "", "unknown") for g in groups)


def test_precursor_protein_group_and_gene_become_null():
    ci = _load("corpus_ingest")
    for v in (float("nan"), "nan", "NaN", "None", "", None):
        assert ci._clean_text(v) is None
    assert ci._clean_text("P05784") == "P05784"


def test_spectronaut_missing_text_is_null_not_nan():
    s2c = _load("spectronaut_to_corpus")
    hdr = ["R.FileName", "EG.ModifiedSequence", "FG.Charge", "PG.ProteinGroups", "PG.Genes",
           "EG.Qvalue", "EG.IsDecoy", "PEP.StrippedSequence"]
    rows = [
        ["run1.d", "_PEPTIDEK_", "2", "P05784", "KRT18", "0.001", "False", "PEPTIDEK"],
        ["run1.d", "_PEPTIDER_", "2", "", "", "0.001", "False", "PEPTIDER"],   # empty cell -> pandas NaN
        ["run1.d", "_PEPTIDEA_", "2", "nan", "NaN", "0.001", "False", "PEPTIDEA"],
    ]
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "report.tsv")
        with open(p, "w") as fh:
            fh.write("\t".join(hdr) + "\n")
            for r in rows:
                fh.write("\t".join(r) + "\n")
        out = list(s2c.iter_records(p))
    assert [r["protein_group"] for r in out] == ["P05784", None, None]
    assert [r["gene"] for r in out] == ["KRT18", None, None]


def test_contaminant_class_four_states():
    org = _load("organism")
    cc = org.contaminant_class
    assert cc("Cont_P02769") == "contaminant"
    assert cc("Cont_P60712;Cont_P68103") == "contaminant"
    assert cc("CON__P02769") == "contaminant" and cc("cRAP-001") == "contaminant"
    assert cc("Cont_P02768;P02768") == "mixed"          # both orders
    assert cc("P02768;Cont_P02768") == "mixed"
    assert cc("A0A0G2JV65;Cont_P63103;P63102") == "mixed"
    assert cc("P05784") == "sample" and cc("P05784;P11679") == "sample"
    # separators / whitespace as stored: trailing or doubled ';', padded accessions, lower case
    assert cc(" Cont_P02769 ;  ") == "contaminant"
    assert cc("Cont_A;;Cont_B;") == "contaminant"
    assert cc(" P05784 ; Cont_P02769") == "mixed"
    assert cc("cont_p02769") == "contaminant"
    for empty in (None, "", ";", " ; ", "  "):
        assert cc(empty) is None


def test_suspected_is_the_old_name_rule_without_a_library_accession():
    org = _load("organism")
    cc, flag = org.contaminant_class, org.is_contaminant_flag
    assert cc("P04264", "KRT1") == "suspected" and flag("P04264", "KRT1")
    assert cc("P04264", "krt6b") == "suspected"                 # case-insensitive, as the old regex
    assert cc("P1", "Keratin-like") == "suspected"
    assert cc("Q9XYZ_contaminant", None) == "suspected"         # protein_group is searched too
    assert cc("P1", "ALB") == "sample" and not flag("P1", "ALB")
    assert cc("P1", None) == "sample"                           # None renders as "None": no match
    assert cc("KR", "T1") == "suspected"                        # joined with nothing between, as before
    # the library prefix always wins over the name
    assert cc("Cont_P04264", "KRT1") == "contaminant"
    assert cc("P04264;Cont_P04264", "KRT1") == "mixed" and not flag("P04264;Cont_P04264", "KRT1")
    assert cc("", "KRT1") == "suspected" and cc("", None) is None


def test_is_contaminant_group_stays_prefix_only():
    org = _load("organism")
    for g in ("Cont_A", "Cont_A;B", "B;Cont_A", "B", "", None, ";", "Cont_A;Cont_B", "cRAP001;X", "P04264;KRT1"):
        assert org.is_contaminant_group(g) == (org.contaminant_class(g) == "contaminant")


def test_ingest_writes_class_only_when_the_column_exists():
    src = open(os.path.join(_INGEST, "corpus_ingest.py"), encoding="utf-8").read()
    assert "column_name='contaminant_class'" in src and "_have_cc" in src
    assert '(",contaminant_class" if _have_cc else "")' in src


if __name__ == "__main__":
    fails = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f(); print("PASS", n)
            except Exception as e:  # noqa: BLE001
                fails += 1; print("FAIL", n, type(e).__name__, e)
    sys.exit(1 if fails else 0)
