"""Paralog density per reference peptide: how many DIFFERENT genes sit one substitution away.

WHY THIS EXISTS, AND WHY IT IS NOT A DECOY. A decoy null cannot measure homologous collisions,
because decoys are built by destroying sequence structure and homology IS sequence structure.
Measured on human keratins and KAPs: real/decoy = 25.4x. So the false-positive driver in a
keratin-dominated proteome is invisible to the chance-match null, and has to be characterised
directly instead.

Uniqueness (require the variant peptide absent from the reference) closes the case where BOTH
forms are annotated -- which is every collision found by digesting the reference, by construction.
What it does not close is a de novo peptide ABSENT from the reference sitting one substitution from
an annotated paralog: an unannotated allele, or a variant form of a paralog. That residual scales
with reference completeness, not with homology density.

This module makes paralog density a PER-CANDIDATE feature rather than a cohort statistic. "3
neighbours, all KRT3x" and "3 neighbours across unrelated genes" are different situations, and the
gene identity is what makes them interpretable -- so the gene symbols are returned, not just a
count.

Different genes ONLY. Same-gene isoforms are a different phenomenon (splice variation, not paralogy)
and would inflate the number without carrying the same risk.
"""
from __future__ import annotations

import argparse
import collections
import csv
import re
import sys

_IL = str.maketrans("I", "L")
# UniProt headers: >sp|P04264|K2C1_HUMAN Keratin ... GN=KRT1 PE=1 SV=6
_GENE = re.compile(r"\bGN=(\S+)")
_ACC = re.compile(r"^>\w\w\|([^|]+)\|")


def il(seq: str) -> str:
    return seq.upper().translate(_IL)


def digest(seq: str, missed: int = 1, min_len: int = 7, max_len: int = 40) -> list[str]:
    """Tryptic peptides: cut after K/R, not before P. Missed cleavages included by default,
    because a de novo peptide need not be fully cleaved."""
    sites = [0]
    for i, c in enumerate(seq):
        if c in "KR" and i + 1 < len(seq) and seq[i + 1] != "P":
            sites.append(i + 1)
    sites.append(len(seq))
    out = []
    for a in range(len(sites) - 1):
        for b in range(a + 1, min(a + 2 + missed, len(sites))):
            pep = seq[sites[a]:sites[b]]
            if min_len <= len(pep) <= max_len:
                out.append(pep)
    return out


def load_reference(fasta_path: str, missed: int = 1) -> dict[str, set[str]]:
    """-> {I/L-normalised peptide: {gene symbols}}.

    A peptide shared by several genes carries all of them; that is the point -- a peptide mapping
    to KRT31, KRT33A and KRT35 is already ambiguous before any substitution is considered.
    """
    pep2genes: dict[str, set[str]] = collections.defaultdict(set)
    gene, buf = None, []

    def flush():
        if gene and buf:
            for p in digest("".join(buf), missed):
                pep2genes[il(p)].add(gene)

    with open(fasta_path) as fh:
        for line in fh:
            if line.startswith(">"):
                flush()
                buf = []
                m = _GENE.search(line)
                if m:
                    gene = m.group(1)
                else:                       # no GN= -- fall back to the accession
                    a = _ACC.match(line)
                    gene = a.group(1) if a else line[1:].split()[0]
            else:
                buf.append(line.strip())
        flush()
    return dict(pep2genes)


def build_index(peptides) -> dict:
    """Pigeonhole blocking on halves -- the same structure denovo_gvp uses."""
    idx: dict = collections.defaultdict(list)
    for k in peptides:
        h = len(k) // 2
        idx[(len(k), 0, k[:h])].append(k)
        idx[(len(k), 1, k[h:])].append(k)
    return idx


def paralog_neighbours(peptide: str, index: dict, pep2genes: dict[str, set[str]]) -> dict:
    """One-substitution neighbours of `peptide` belonging to at least one DIFFERENT gene.

    -> {n_paralog_neighbours, paralog_genes (sorted), paralog_peptides (sorted),
        own_genes (sorted)}
    """
    q = il(peptide)
    own = pep2genes.get(q, set())
    h = len(q) // 2
    cands = set(index.get((len(q), 0, q[:h]), ())) | set(index.get((len(q), 1, q[h:]), ()))

    genes: set[str] = set()
    peps: list[str] = []
    for c in sorted(cands):
        if c == q:
            continue
        if sum(1 for a, b in zip(q, c) if a != b) != 1:
            continue
        other = pep2genes.get(c, set()) - own      # DIFFERENT genes only
        if other:
            genes |= other
            peps.append(c)
    return {"n_paralog_neighbours": len(peps),
            "paralog_genes": sorted(genes),
            "paralog_peptides": sorted(peps),
            "own_genes": sorted(own)}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fasta", required=True, help="reference proteome, e.g. UP000005640_9606.fasta")
    ap.add_argument("--peptides", help="one peptide per line; omit to profile the whole reference")
    ap.add_argument("--out", required=True)
    ap.add_argument("--missed", type=int, default=1)
    ap.add_argument("--gene-prefix", help="restrict the summary to genes starting with this "
                                          "(e.g. KRT), for a family-level rate")
    a = ap.parse_args()

    pep2genes = load_reference(a.fasta, a.missed)
    print(f"reference: {len(pep2genes):,} I/L-distinct tryptic peptides, "
          f"{len({g for gs in pep2genes.values() for g in gs}):,} genes", file=sys.stderr)
    index = build_index(pep2genes.keys())

    if a.peptides:
        queries = [l.strip().upper() for l in open(a.peptides) if l.strip()]
    else:
        queries = sorted(pep2genes.keys())
    print(f"profiling {len(queries):,} peptides", file=sys.stderr)

    n_with = 0
    with open(a.out, "w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["peptide", "own_genes", "n_paralog_neighbours", "paralog_genes",
                    "paralog_peptides"])
        for q in queries:
            r = paralog_neighbours(q, index, pep2genes)
            if r["n_paralog_neighbours"]:
                n_with += 1
            w.writerow([q, ";".join(r["own_genes"]), r["n_paralog_neighbours"],
                        ";".join(r["paralog_genes"]), ";".join(r["paralog_peptides"])])

    print(f"{n_with:,} of {len(queries):,} peptides have >=1 cross-gene 1-substitution neighbour "
          f"({100*n_with/len(queries):.2f}%)", file=sys.stderr)
    print(f"wrote {a.out}", file=sys.stderr)
    print("NOTE: a peptide with many cross-gene neighbours is a poor variant substrate -- the "
          "collision is a better explanation than a SNP. Uniqueness already rejects candidates "
          "whose variant form is itself annotated; this bounds the rest.", file=sys.stderr)


if __name__ == "__main__":
    main()
