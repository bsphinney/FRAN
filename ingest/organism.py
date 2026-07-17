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
