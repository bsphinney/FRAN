"""GVP detection: exact 1-substitution search, and the controls that keep it honest.

Run:  python tests/test_denovo_gvp.py     (no pytest needed)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingest"))
from denovo_gvp import build_index, find_gvp  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


corpus = {"LNDLEDALQQAK": ["LNDLEDALQQAK"], "PEPTLDEK": ["PEPTIDEK"], "AAAAAAAA": ["AAAAAAAA"]}
idx = build_index(corpus)

# One substitution -> a candidate. KRT1 A454S is the real rs17678945 variant.
hits = find_gvp("LNDLEDALQQSK", idx, corpus)
check("one substitution found", len(hits) == 1, f"got {len(hits)}")
if hits:
    h = hits[0]
    check("position is 0-based index 10", h["position"] == 10, str(h["position"]))
    check("aa_from is the CORPUS residue", h["aa_from"] == "A")
    check("aa_to is the DE NOVO residue", h["aa_to"] == "S")
    check("A->S is one nucleotide", h["one_nt_reachable"] is True)
    check("A->S is not isobaric", h["is_isobaric"] is False)
    check("A->S is not deamidation-shaped", h["is_deamidation_shaped"] is False)
    check("interior, not terminal", h["is_terminal"] is False)

# An exact corpus peptide is CONSERVED, not a variant.
check("exact match is not a GVP", find_gvp("LNDLEDALQQAK", idx, corpus) == [])
# I/L is invisible to de novo -- never a variant.
check("I/L is not a GVP", find_gvp("PEPTLDEK", idx, corpus) == [])
# Two substitutions is not a single-SNP candidate.
check("two substitutions rejected", find_gvp("LNDLEDALQQSR", idx, corpus) == [])

# CONTROL 1 -- S->E needs two nucleotide changes, so it cannot come from one SNP.
# The corpus peptide must actually carry the S, or this tests a different substitution: matching
# EAAAAAAA against AAAAAAAA is A->E, which IS one nucleotide, and the test passes for the wrong
# reason.
corpus_se = {"SAAAAAAA": ["SAAAAAAA"]}
h = find_gvp("EAAAAAAA", build_index(corpus_se), corpus_se)
check("S->E is found as a candidate", len(h) == 1, f"got {len(h)}")
check("S->E flagged not-one-nt", bool(h) and h[0]["one_nt_reachable"] is False)
check("...but is still returned, not filtered", bool(h) and h[0]["aa_from"] == "S")

# CONTROL 2 -- deamidation. N->D and Q->E are +0.98402, exactly the chemistry.
corpus2 = {"PEPTNDEK": ["PEPTNDEK"], "PEPTQTLK": ["PEPTQTLK"]}
idx2 = build_index(corpus2)
h = find_gvp("PEPTDDEK", idx2, corpus2)
check("N->D flagged deamidation-shaped", bool(h) and h[0]["is_deamidation_shaped"] is True)
h = find_gvp("PEPTETLK", idx2, corpus2)
check("Q->E flagged deamidation-shaped", bool(h) and h[0]["is_deamidation_shaped"] is True)
# The REVERSE direction cannot be produced by deamidation, so it stays a valid candidate.
corpus3 = {"PEPTDDEK": ["PEPTDDEK"]}
h = find_gvp("PEPTNDEK", build_index(corpus3), corpus3)
check("D->N is NOT deamidation-shaped", bool(h) and h[0]["is_deamidation_shaped"] is False)

# CONTROL 3 -- methylation-shaped, from the 2026-07 session's isobaric list: +14.016 for
# D->E, S->T, G->A, V->I. (V->I is also I/L-invisible, so it never reaches here.)
corpus4 = {"PEPTDTLK": ["PEPTDTLK"], "PEPSTTLK": ["PEPSTTLK"]}
idx4 = build_index(corpus4)
h = find_gvp("PEPTETLK", idx4, corpus4)
check("D->E flagged methylation-shaped", bool(h) and h[0]["is_methylation_shaped"] is True)

# CONTROL 4 -- PTM-isobaric. Casanovo's residue vocabulary is fixed, so an unmodelled PTM has to be
# absorbed as a residue call. A->S and F->Y are +15.995, exactly oxidation -- and hair is
# chronically oxidised (sun, bleach, age), which is where this bites hardest.
corpus_ox = {"PEPTAELK": ["PEPTAELK"], "PEPTFELK": ["PEPTFELK"]}
idx_ox = build_index(corpus_ox)
h = find_gvp("PEPTSELK", idx_ox, corpus_ox)
check("A->S flagged PTM-isobaric", bool(h) and h[0]["is_ptm_isobaric"] is True)
check("A->S names oxidation", bool(h) and h[0]["ptm_name"] == "oxidation",
      h[0]["ptm_name"] if h else "no hit")
# NB: PEPTYELK is 1 substitution from BOTH corpus peptides (A->Y and F->Y), and hits come back
# sorted by corpus sequence, so h[0] is the A->Y hit. Select the substitution under test rather
# than assuming it is first -- the same mistake as the S->E case above.
h = find_gvp("PEPTYELK", idx_ox, corpus_ox)
fy = [x for x in h if (x["aa_from"], x["aa_to"]) == ("F", "Y")]
check("F->Y found among the hits", len(fy) == 1, f"got {len(fy)} of {len(h)}")
check("F->Y flagged PTM-isobaric", bool(fy) and fy[0]["is_ptm_isobaric"] is True)
check("F->Y names oxidation", bool(fy) and fy[0]["ptm_name"] == "oxidation")

# N->Q is +14.016 like the pairs already listed; it was missing from the first exclusion table.
corpus_nq = {"PEPTNELK": ["PEPTNELK"]}
h = find_gvp("PEPTQELK", build_index(corpus_nq), corpus_nq)
check("N->Q flagged methylation-shaped", bool(h) and h[0]["is_methylation_shaped"] is True)

# A substitution with no PTM at that mass must NOT be flagged.
corpus_w = {"PEPTAELK": ["PEPTAELK"]}
h = find_gvp("PEPTWELK", build_index(corpus_w), corpus_w)
check("A->W is not PTM-isobaric", bool(h) and h[0]["is_ptm_isobaric"] is False)

# is_IL is returned so an uncallable site is visibly uncallable, never silently absent.
h = find_gvp("LNDLEDALQQSK", idx, corpus)
check("is_IL field returned", bool(h) and "is_IL" in h[0])

# Determinism: neighbours sorted, or counts drift with the hash seed.
c5 = {"AAAAAAAA": ["AAAAAAAA"], "AAAAAAAB": ["AAAAAAAB"]}
r1 = [h["corpus_stripped_seq"] for h in find_gvp("AAAAAAAC", build_index(c5), c5)]
r2 = [h["corpus_stripped_seq"] for h in find_gvp("AAAAAAAC", build_index(c5), c5)]
check("deterministic order", r1 == r2 == sorted(r1))

# Terminal substitutions are REPORTED, not dropped -- de novo confidence is lowest at the ends,
# which is a reason to flag them, not to hide them.
h = find_gvp("SNDLEDALQQAK", idx, corpus)
check("N-terminal substitution reported and flagged",
      bool(h) and h[0]["position"] == 0 and h[0]["is_terminal"] is True)

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
