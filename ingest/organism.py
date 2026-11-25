"""Single source of truth for organism-name canonicalization at ingest.

Two failure modes this guards against, both seen live in the `delimp` corpus:

1. Junk sentinels stored as a STRING ("Unknown", "nan", "none", "") instead of
   NULL. The dashboard counts any non-empty organism_name as a real species
   (queries.py: `WHERE organism_name IS NOT NULL AND organism_name <> ''`), so a
   literal "Unknown" shows up as a bogus species slice. Unresolved organism MUST
   be NULL -- then it is correctly bucketed into the "Unknown" pile AND excluded
   from the distinct-species count, and it stays queued for the species predictor
   (predicted_organism_name IS NULL) without pretending to be identified.

2. Spectronaut "(Common name)" variants that fragment one species into two
   dashboard slices: "Homo sapiens (Human)" vs "Homo sapiens". We strip the
   trailing parenthetical so both collapse to "Homo sapiens".

Keep this the ONLY place either rule is implemented (CLAUDE.md architectural
rule #3: concepts have one definition). Both the DIA-NN ingest (corpus_ingest.py)
and the Spectronaut lane should route organism strings through canonical_organism()
before writing delimp_sample_metadata.organism_name.
"""
from __future__ import annotations

import re

# Lower-cased strings that mean "we don't actually know" -> store NULL, never the string.
_BANNED = {"", "unknown", "unknwon", "none", "nan", "null", "n/a", "na", "?", "-", "undetermined"}

# Trailing parenthetical Spectronaut adds. We ONLY strip true *common-name* tags
# like "(Human)", "(Mouse)" -- NEVER strain/taxonomic qualifiers, which are real
# biological distinctions: "(strain K12)", "(strain BL21-DE3)", "(strain GS115 / ATCC 20864)".
# Heuristic: a common-name tag is short, alphabetic, and has none of the strain
# markers (digits, "/", "strain", "isolate", "subsp", "serovar", "var.", "ATCC").
_PAREN = re.compile(r"\s*\(([^)]*)\)\s*$")
_STRAIN_MARK = re.compile(r"\d|/|\b(strain|isolate|subsp|serovar|var|pv|ATCC|str)\b", re.I)


def _is_common_name(inner: str) -> bool:
    inner = inner.strip()
    return bool(inner) and len(inner) <= 24 and not _STRAIN_MARK.search(inner)


def canonical_organism(name) -> str | None:
    """Return a clean organism_name, or None if the value is a junk sentinel.

    >>> canonical_organism("Unknown")            # -> None  (NOT the string)
    >>> canonical_organism("  ")                 # -> None
    >>> canonical_organism(None)                 # -> None
    >>> canonical_organism("Homo sapiens (Human)") -> "Homo sapiens"
    >>> canonical_organism("  Mus musculus ")    -> "Mus musculus"
    """
    if name is None:
        return None
    s = str(name).strip()
    if s.lower() in _BANNED:
        return None
    # collapse Spectronaut "(Common name)" variant so it merges with the bare name,
    # but PRESERVE strain/taxonomic qualifiers (real biological distinctions).
    m = _PAREN.search(s)
    if m and _is_common_name(m.group(1)):
        s = _PAREN.sub("", s).strip()
    if s.lower() in _BANNED:  # e.g. "(unknown)" -> ""
        return None
    return s or None


# ── Which organism a SEARCH is, from its identifications ──────────────────────────────────────
#
# Spectronaut's PEP.AllOccurringOrganisms names every organism whose FASTA entries contain the
# peptide -- INCLUDING the contaminant library searched alongside the proteome. corpus_ingest used
# to take the most common value across all precursors, so a search dominated by contaminants was
# recorded as the contaminants' species. Measured 2026-09-16 on a Spectronaut search of human cell
# samples: 15,371 of 15,503 precursors (99%) were Universal Contaminant Protein FASTA hits -- 11,763
# of them bovine serum proteins -- and the search's own database (a custom ORF FASTA with no OS=
# tags) contributed 132, all "Unknown". The corpus recorded Bos taurus for all 7 runs, and for two
# sibling searches of the same samples (224 more runs).
#
# The contaminant library says nothing about the sample, so its identifications do not vote.

# Accession prefixes of contaminant libraries: Spectronaut "Universal Contaminant Protein FASTA"
# (Cont_P02769), MaxQuant contaminants.fasta (CON__P02769), cRAP (cRAP-001 / cRAP001).
_CONTAM_ACC = re.compile(r"^\s*(?:Cont_|CON__|cRAP[-_]?\d)", re.I | re.ASCII)


# The flag the corpus carried before the 2026-11 audit (corpus_ingest.ingest, the ONE writer for both
# the DIA-NN and the Spectronaut path, origin/main corpus_ingest.py:1026): a case-insensitive search
# of str(protein_group) + str(gene) -- the two joined with nothing between, None rendered as "None".
# It matched keratins and the like by NAME, whatever database they came from. Kept only to name the
# 'suspected' class; it never overrides an accession-prefix verdict.
_OLD_NAME_RULE = re.compile(r"KRT|keratin|cont_|contaminant", re.I | re.ASCII)


def contaminant_class(protein_group, gene=None) -> str | None:
    """Membership of a ';'-joined protein group in the contaminant library, four states.

    'contaminant' every accession has a contaminant prefix (Cont_ / CON__ / cRAP)
    'mixed'       some do, some do not: the search database also explains the group, so it is
                  counted as a SAMPLE protein by default, but the fact is kept
    'suspected'   none do, but the OLD name rule matches protein_group + gene (e.g. KRT1 under a
                  plain UniProt accession). Kept flagged as before: is_contaminant stays TRUE
    'sample'      none do, and the old name rule does not match
    None          no accession at all (None, "", ";", whitespace) and no name match
    `gene` is the stored gene string (None when absent); it only matters for 'suspected'.
    Accessions are split on ';' and stripped; empty parts are ignored.
    """
    accs = [] if protein_group is None else [a for a in str(protein_group).split(";") if a.strip()]
    n = sum(1 for a in accs if _CONTAM_ACC.match(a))
    if accs and n == len(accs):
        return "contaminant"
    if n:
        return "mixed"
    if _OLD_NAME_RULE.search(str(protein_group) + str(gene)):
        return "suspected"
    return "sample" if accs else None


def is_contaminant_flag(protein_group, gene=None) -> bool:
    """The stored delimp_proteins.is_contaminant: class 'contaminant' or 'suspected'."""
    return contaminant_class(protein_group, gene) in ("contaminant", "suspected")


def is_contaminant_group(protein_group) -> bool:
    """True when EVERY accession of a ';'-joined protein group comes from a contaminant library.

    A group that mixes a contaminant with a real entry is NOT a contaminant: the peptide is also
    explained by the search database, so it still says something about the sample.
    """
    if protein_group is None:
        return False
    return contaminant_class(protein_group) == "contaminant"   # prefix-only: gene never consulted


def vote_organism(pairs) -> dict:
    """Search organism from (organism_value, protein_group) pairs, one per precursor.

    Rules, in order:
      * precursors whose protein group is contaminant-library-only are skipped;
      * organism values go through canonical_organism(), so "Unknown"/"nan" never vote;
      * organism-UNIQUE precursors vote (a ';'-joined value is shared between organisms and says
        nothing about which one the sample is -- same method as backfill_organism_from_lance.py);
      * only if there are no unique votes at all do shared values vote for each of their parts.

    Returns {"organism": name | None, "votes": Counter, "n": precursors seen,
             "n_contaminant": skipped as contaminant, "n_unlabelled": no usable organism,
             "n_shared": multi-organism}. "organism" is None when nothing but contaminants or
    unlabelled entries was identified -- never a contaminant species.
    """
    from collections import Counter

    unique, shared = Counter(), Counter()
    n = n_contaminant = n_unlabelled = n_shared = 0
    for org, group in pairs:
        n += 1
        if is_contaminant_group(group):
            n_contaminant += 1
            continue
        parts = [canonical_organism(p) for p in str(org).split(";")] if org is not None else []
        parts = [p for p in parts if p]
        if not parts:
            n_unlabelled += 1
        elif len(set(parts)) == 1:
            unique[parts[0]] += 1
        else:
            n_shared += 1
            for p in set(parts):
                shared[p] += 1
    votes = unique or shared
    return {"organism": votes.most_common(1)[0][0] if votes else None, "votes": votes,
            "n": n, "n_contaminant": n_contaminant, "n_unlabelled": n_unlabelled,
            "n_shared": n_shared}
