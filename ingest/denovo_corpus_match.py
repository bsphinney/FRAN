"""Match de novo peptides against the FRAN corpus on an I/L-normalised key.

De novo sequencing cannot distinguish isoleucine from leucine, so the join must be I/L-normalised:
exact matching finds 16.3% of the crane peptides, I/L-normalised finds 32.8%. Half the signal is in
the normalisation.

The result is one row per (de novo peptide x matched corpus spelling), NOT one row per peptide,
because the collapse is one-to-many in reverse: ALLEVLGR matches the real corpus peptides AIIEVLGR,
ALIEVLGR and ALLEVLGR. 8.2% of crane matches were ambiguous like this. Showing one and hiding the
rest would misrepresent what de novo can actually distinguish.
"""
from __future__ import annotations

_IL = str.maketrans("I", "L")


def il(seq: str) -> str:
    return seq.upper().translate(_IL)


def match_peptides(peptides: list[str], corpus: dict[str, list[str]]) -> list[dict]:
    """corpus maps an I/L-normalised key -> the real corpus spellings under it.

    Candidates are SORTED before emission: unsorted set iteration makes the output depend on
    Python's per-run string hash seed, which drifted a downstream candidate count by 3 between
    otherwise identical runs.
    """
    rows = []
    for pep in peptides:
        hits = corpus.get(il(pep))
        if not hits:
            continue
        for spelling in sorted(hits):
            rows.append({"stripped_seq": pep,
                         "corpus_stripped_seq": spelling,
                         "match_kind": "exact" if spelling == pep else "il",
                         "n_candidates": len(hits)})
    return rows
