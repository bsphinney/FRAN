"""Paralog density: different genes only, gene symbols returned, same-gene isoforms excluded.

Run:  python tests/test_denovo_paralog.py     (no pytest needed)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingest"))
from denovo_paralog import build_index, digest, load_reference, paralog_neighbours  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# Tryptic digestion: cut after K/R, not before P.
# NB the R here must NOT be followed by P, or "no cut" is the CORRECT answer and the test fails
# for the wrong reason -- my first version used SAMPLERPEPTIDEK and blamed the code.
d = digest("PEPTIDEKSAMPLERAAAADEK", missed=0, min_len=4)
check("cuts after K", "PEPTIDEK" in d, str(d))
check("cuts after R", "SAMPLER" in d, str(d))
d2 = digest("AAAAAAAKPAAAAAAAAR", missed=0, min_len=4)
check("does not cut before P", "AAAAAAAKPAAAAAAAAR" in d2, str(d2))

# A synthetic reference built on the real keratin collision dataanalysis-16 found:
# ADLEAQVESLK is KRT32/KRT36; SDLEAQVESLK is KRT31/KRT33A/KRT35. One substitution, different genes.
FASTA = """>sp|Q14533|KRT31_HUMAN Keratin, type I cuticular Ha1 OS=Homo sapiens GN=KRT31 PE=1 SV=3
SDLEAQVESLKEELLFLKK
>sp|O76011|KRT34_HUMAN Keratin, type I cuticular Ha4 OS=Homo sapiens GN=KRT33A PE=1 SV=2
SDLEAQVESLKEELLFLKK
>sp|Q14525|KRT32_HUMAN Keratin, type I cuticular Ha3-II OS=Homo sapiens GN=KRT32 PE=1 SV=3
ADLEAQVESLKEELLFLKK
>sp|O76013|KRT36_HUMAN Keratin, type I cuticular Ha6 OS=Homo sapiens GN=KRT36 PE=1 SV=2
ADLEAQVESLKEELLFLKK
>sp|P99999|LONER_HUMAN A protein with no close relatives OS=Homo sapiens GN=LONER PE=1 SV=1
WWWWWWWWWWKEELLFLKK
"""
fh = tempfile.NamedTemporaryFile("w", suffix=".fasta", delete=False)
fh.write(FASTA)
fh.close()

ref = load_reference(fh.name, missed=0)
check("reference digested", len(ref) > 0, str(len(ref)))
check("shared peptide carries every gene",
      ref.get("SDLEAQVESLK") == {"KRT31", "KRT33A"}, str(ref.get("SDLEAQVESLK")))
check("gene parsed from GN=", "KRT32" in ref.get("ADLEAQVESLK", set()))

idx = build_index(ref.keys())

r = paralog_neighbours("ADLEAQVESLK", idx, ref)
check("cross-gene neighbour found", r["n_paralog_neighbours"] == 1, str(r["n_paralog_neighbours"]))
check("neighbour peptide named", r["paralog_peptides"] == ["SDLEAQVESLK"], str(r["paralog_peptides"]))
# Gene symbols, not just a count -- "3 neighbours all KRT3x" differs from "3 unrelated genes".
check("neighbour GENES returned", r["paralog_genes"] == ["KRT31", "KRT33A"], str(r["paralog_genes"]))
check("own genes returned", r["own_genes"] == ["KRT32", "KRT36"], str(r["own_genes"]))

# Same-gene isoforms must NOT count -- different phenomenon, would inflate the number.
SAME = """>sp|A00001|X_HUMAN iso 1 OS=Homo sapiens GN=SAMEGENE PE=1 SV=1
ADLEAQVESLKEELLFLKK
>sp|A00002|X_HUMAN iso 2 OS=Homo sapiens GN=SAMEGENE PE=1 SV=1
SDLEAQVESLKEELLFLKK
"""
fh2 = tempfile.NamedTemporaryFile("w", suffix=".fasta", delete=False)
fh2.write(SAME)
fh2.close()
ref2 = load_reference(fh2.name, missed=0)
r2 = paralog_neighbours("ADLEAQVESLK", build_index(ref2.keys()), ref2)
check("same-gene isoform does NOT count", r2["n_paralog_neighbours"] == 0,
      str(r2["n_paralog_neighbours"]))

# A peptide with no close relative scores zero.
r3 = paralog_neighbours("WWWWWWWWWWK", idx, ref)
check("isolated peptide has no neighbours", r3["n_paralog_neighbours"] == 0)

# A peptide absent from the reference still gets a verdict -- that is the de novo case.
r4 = paralog_neighbours("TDLEAQVESLK", idx, ref)
check("unannotated peptide still profiled", r4["n_paralog_neighbours"] >= 1,
      str(r4["n_paralog_neighbours"]))
check("unannotated peptide has no own genes", r4["own_genes"] == [])

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
