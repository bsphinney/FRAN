"""Find genetically variant peptide candidates: de novo peptides one substitution from the corpus.

A LIBRARY FIRST, with a thin CLI that writes TSV. Deliberately no database writes: exploratory
candidate generation must not become corpus state, or every threshold someone tries turns into a
result that another reader may take at face value. Hand the reviewed output to the ingest path
separately.

THE SEARCH. Comparing 15k peptides against 2.9M naively is 43 billion character comparisons.
Pigeonhole instead: two equal-length strings differing in <=1 position must agree EXACTLY on at
least one half, so index the corpus by (len, left half) and (len, right half), probe both, verify
with a real Hamming distance. Exact -- no false negatives. Measured: 6.6 s to build the index,
0.1 s to classify 15,512 peptides against 2.9M.

EVERY VERDICT IS RETURNED; NOTHING IS FILTERED. A candidate carries all its control results so a
reader can see which one did the work, thresholds can move without re-running, and the downstream
gates (entrapment FDR, carrier prevalence) operate on a complete set rather than a pre-empted one.

THE CONTROLS, and what each rules out:
  one_nt_reachable      A real GVP comes from a single nucleotide polymorphism, so the
                        substitution must be reachable by one nucleotide change in the codon.
                        S->E is not, and 31% of raw candidates fail this.
  is_isobaric           Near-isobaric swaps are what de novo cannot resolve from the spectrum.
  is_deamidation_shaped N->D and Q->E are +0.98402, EXACTLY the deamidation delta. Casanovo
                        carries N[Deamidated] and D at the same mass, so writing D is a learned
                        prior from modern training data -- systematically wrong on archaeological
                        samples. The REVERSE direction (D->N, E->Q) cannot be produced by
                        deamidation and stays valid.
  is_methylation_shaped +14.016: D->E, S->T, G->A, V->I. (V->I never reaches here, being
                        I/L-invisible.) From the 2026-07 Parker VM isobaric list.
  is_terminal           De novo confidence is lowest at the termini -- Casanovo's own feature set
                        weights N- and C-terminal confidence separately. Reported, never dropped.

WHAT THESE CONTROLS ARE NOT. They are per-candidate plausibility tests, not an error rate. A decoy
peptide passes them as happily as a real variant: measured on the crane cohort, 437 of 1,133
"credible" candidates are chance matches, a 39% FDR. Run ingest/denovo_null.py alongside this, and
never report a candidate count without it.
"""
from __future__ import annotations

import argparse
import collections
import csv
import itertools
import os
import sys

_BASES = "TCAG"
_AAS = "FFLLSSSSYY**CC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG"
_CODON: dict[str, list[str]] = {}
for _i, _c in enumerate(itertools.product(_BASES, repeat=3)):
    _CODON.setdefault(_AAS[_i], []).append("".join(_c))

MASS = {"G": 57.02146, "A": 71.03711, "S": 87.03203, "P": 97.05276, "V": 99.06841,
        "T": 101.04768, "C": 103.00919, "L": 113.08406, "I": 113.08406, "N": 114.04293,
        "D": 115.02694, "Q": 128.05858, "K": 128.09496, "E": 129.04259, "M": 131.04049,
        "H": 137.05891, "F": 147.06841, "R": 156.10111, "Y": 163.06333, "W": 186.07931}

# corpus residue -> what de novo reads, when the chemistry can produce it
DEAMIDATION = {("N", "D"), ("Q", "E")}                       # +0.98402
METHYLATION = {("D", "E"), ("S", "T"), ("G", "A"), ("V", "I")}   # +14.016
ISOBARIC_TOL = 0.06

FIELDS = ["stripped_seq", "corpus_stripped_seq", "position", "aa_from", "aa_to", "mass_delta",
          "one_nt_reachable", "is_isobaric", "is_deamidation_shaped", "is_methylation_shaped",
          "is_terminal", "n_neighbours"]


def one_nt_apart(x: str, y: str) -> bool:
    """Is residue y reachable from x by exactly one nucleotide substitution?"""
    return any(sum(a != b for a, b in zip(cx, cy)) == 1
               for cx in _CODON.get(x, ()) for cy in _CODON.get(y, ()))


def build_index(corpus: dict[str, list[str]]) -> dict:
    """(length, half, text) -> [corpus I/L keys]. Pigeonhole blocking on halves."""
    idx: dict = collections.defaultdict(list)
    for key in corpus:
        h = len(key) // 2
        idx[(len(key), 0, key[:h])].append(key)
        idx[(len(key), 1, key[h:])].append(key)
    return idx


def find_gvp(peptide: str, index: dict, corpus: dict[str, list[str]]) -> list[dict]:
    """One-substitution neighbours of `peptide` in the corpus, with every control verdict.

    An exact (or I/L-equivalent) corpus peptide is CONSERVED, not a variant, and returns [].
    """
    q = peptide.upper()
    if q in corpus:
        return []
    h = len(q) // 2
    cands = set(index.get((len(q), 0, q[:h]), ())) | set(index.get((len(q), 1, q[h:]), ()))

    out: list[dict] = []
    for cand in sorted(cands):        # sorted: set order otherwise depends on the hash seed
        diff = [i for i, (a, b) in enumerate(zip(q, cand)) if a != b]
        if len(diff) != 1:
            continue
        i = diff[0]
        frm, to = cand[i], q[i]
        dm = abs(MASS.get(frm, 0.0) - MASS.get(to, 0.0))
        for spelling in sorted(corpus[cand]):
            out.append({
                "stripped_seq": q,
                "corpus_stripped_seq": spelling,
                "position": i,
                "aa_from": frm,
                "aa_to": to,
                "mass_delta": round(dm, 5),
                "one_nt_reachable": one_nt_apart(frm, to),
                "is_isobaric": dm < ISOBARIC_TOL,
                "is_deamidation_shaped": (frm, to) in DEAMIDATION,
                "is_methylation_shaped": (frm, to) in METHYLATION,
                "is_terminal": i == 0 or i == len(q) - 1,
                "n_neighbours": len(cands),
            })
    return out


def scan(peptides, index, corpus) -> list[dict]:
    """find_gvp over many peptides. Returns every candidate; filtering is the caller's business."""
    rows = []
    for p in peptides:
        rows.extend(find_gvp(p, index, corpus))
    return rows


# ---------------------------------------------------------------------------------------------
# thin CLI


def _load_corpus(path: str) -> dict[str, list[str]]:
    """One peptide per line -> {I/L key: [real spellings]}."""
    il = str.maketrans("I", "L")
    corpus: dict[str, list[str]] = {}
    with open(path) as fh:
        for line in fh:
            s = line.strip().upper()
            if s:
                corpus.setdefault(s.translate(il), []).append(s)
    return corpus


def _load_queries(path: str) -> list[tuple[str, str]]:
    """-> [(group, peptide)]. A plain peptide list, or a 2-column TSV of group<TAB>peptide.

    The group column is what makes per-DONOR analysis possible: a cohort of 378 runs can be 372
    donors, and a tool that assumes one group per run bakes in the wrong unit.
    """
    il = str.maketrans("I", "L")
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                out.append((parts[0].strip(), parts[1].strip().upper().translate(il)))
            else:
                out.append(("", parts[0].strip().upper().translate(il)))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", required=True,
                    help="one peptide per line (dump delimp_peptide_consensus.stripped_seq)")
    ap.add_argument("--peptides", required=True,
                    help="one peptide per line, OR group<TAB>peptide for per-donor grouping")
    ap.add_argument("--out", required=True, help="TSV output")
    ap.add_argument("--summary", action="store_true",
                    help="also print per-group counts to stderr")
    a = ap.parse_args()

    corpus = _load_corpus(a.corpus)
    index = build_index(corpus)
    queries = _load_queries(a.peptides)
    print(f"corpus {len(corpus):,} I/L keys · {len(queries):,} query peptides", file=sys.stderr)

    groups = collections.Counter()
    with open(a.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["group"] + FIELDS, delimiter="\t")
        w.writeheader()
        for group, pep in queries:
            for row in find_gvp(pep, index, corpus):
                w.writerow({"group": group, **row})
                groups[group] += 1

    total = sum(groups.values())
    print(f"wrote {total:,} candidate rows -> {a.out}", file=sys.stderr)
    print("NOTE: these are candidates, not results. Run ingest/denovo_null.py for the "
          "chance-match rate; ~39% of 'credible' candidates were chance on the crane cohort.",
          file=sys.stderr)
    if a.summary:
        for g, n in groups.most_common():
            print(f"  {g or '(ungrouped)':<24}{n:>8}", file=sys.stderr)


if __name__ == "__main__":
    main()
