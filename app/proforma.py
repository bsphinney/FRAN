"""ProForma parsing for FRAN's modified_seq_proforma column.

WHY THIS COLUMN AND NOT `mods`: modified_seq_proforma is 100% populated corpus-wide (measured:
249,978 of 249,978 sampled). `mods` is 1.43% populated because ingest/spectronaut_to_corpus.py:199
writes `"mods": None` with a TODO, and Spectronaut is 2,008 of the 2,086 searches. The GIN index
idx_prec_mods_gin sits on that near-empty column and is dead by construction. Do not reach for it.
"""
from __future__ import annotations

import re
from typing import NamedTuple

# UNIMOD ids seen anywhere in the corpus (a token scan of 18,856 modified precursors found SIX,
# and no others): 4 Carbamidomethyl, 35 Oxidation, 1 Acetyl, 21 Phospho, 7 Deamidated,
# 27 Glu->pyro-Glu.
#
# VARIABLE_MODS deliberately EXCLUDES 4 (Carbamidomethyl). It is a fixed modification -- iodoacetamide,
# a reagent -- and is 60.9% of all modifications corpus-wide. Including it would bury every real
# site under cysteine alkylation.
VARIABLE_MODS: dict[int, str] = {
    21: "Phospho",
    121: "GlyGly",          # the ubiquitin remnant -- see the note below
    1: "Acetyl",
    35: "Oxidation",
    7: "Deamidated",
    27: "Glu->pyro-Glu",
    28: "Gln->pyro-Glu",   # found by test_every_ingest_mapped_modification_can_become_a_site
}
_FIXED_MODS: dict[int, str] = {4: "Carbamidomethyl"}

# UNIMOD 121 (GlyGly) is the ubiquitin remnant -- the direct readout of protein ubiquitination,
# and biology, not sample handling. It is here because of a gap that existed for exactly one
# afternoon and would otherwise have been invisible for much longer:
# ingest/spectronaut_to_corpus.py gained `"GlyGly": 121` so that re-ingested Spectronaut rows
# normalise to [UNIMOD:121] instead of the literal text `[GlyGly (K)]`. Without 121 in
# VARIABLE_MODS, those rows PARSE CLEANLY -- parse_proforma returns Mod(121, 'K', pos) -- and are
# then silently dropped by sites_in_protein's variable_only filter. A ubiquitylation site would
# render as an unmarked residue: absent data looking exactly like clean data, which is the failure
# mode this whole feature exists to avoid. ~750,000 GG precursors sit in the Bennett_Penn_Ubiq
# searches (61-66% of those runs), so the blast radius was the corpus's entire ubiquitin story.
# If a modification is added to _MOD_UNIMOD in the ingest, it must be added here too.

# Modifications that are genuinely biological, versus those that are largely sample-handling
# artifacts. Both are "variable"; only the first group is biology, and the UI should not imply
# otherwise. Oxidation in particular is 34% of all modifications and is mostly handling.
BIOLOGICAL_MODS: frozenset[int] = frozenset({21, 121, 1})

_TOKEN = re.compile(r"\[UNIMOD:(\d+)\]")


class Mod(NamedTuple):
    unimod_id: int
    residue: str | None   # None for an N-terminal modification
    pos: int              # 1-based index into the stripped sequence; 0 = N-terminal


def mod_name(uid: int) -> str:
    return VARIABLE_MODS.get(uid) or _FIXED_MODS.get(uid) or f"UNIMOD:{uid}"


def parse_proforma(pf: str | None) -> list[Mod]:
    """Return every modification in a ProForma string, positioned against the stripped sequence.

    `pos` is the 1-based index of the residue the tag FOLLOWS. A tag appearing before any residue
    is N-terminal and gets pos=0 with residue=None.

    That N-terminal case is the whole reason this is a parser and not a regex. Given
    `[UNIMOD:1]SETAPAETATPAPVEKS[UNIMOD:21]PAK`, a parser that assumes every tag follows the
    residue it modifies assigns the acetyl to a nonexistent residue and then shifts the phospho --
    and every later position in that peptide -- by one. Acetyl is 5.9% of modified precursors, so
    this is the common case, not an exotic one.
    """
    if not pf or not isinstance(pf, str):
        return []
    mods: list[Mod] = []
    n_res = 0            # residues emitted so far == 1-based index of the most recent residue
    last_res: str | None = None
    i, n = 0, len(pf)
    while i < n:
        ch = pf[i]
        if ch == "[":
            m = _TOKEN.match(pf, i)
            if m is not None:
                mods.append(Mod(int(m.group(1)), last_res if n_res else None, n_res))
                i = m.end()
                continue
            # An unrecognised bracket token (an unmapped mod name left as literal text by
            # _to_proforma). Skip the whole token -- its letters are NOT residues.
            close = pf.find("]", i)
            i = n if close < 0 else close + 1
            continue
        if ch.isalpha():
            n_res += 1
            last_res = ch
        # Anything else (the '_' delimiters Spectronaut and DIA-NN wrap sequences in, digits,
        # punctuation) is neither a residue nor a tag. Skipping rather than counting it is what
        # keeps positions aligned with stripped_seq.
        i += 1
    return mods


def sites_in_protein(pf: str | None, peptide_start: int,
                     variable_only: bool = True) -> list[tuple[int, int, str | None]]:
    """Map a peptide's modifications onto PROTEIN coordinates.

    `peptide_start` is the coverage map's 1-based inclusive start (app/static/app.js indexes with
    `p.start-1`, which is what pins the convention). An N-terminal modification maps to the
    peptide's own first residue.

    Returns (unimod_id, position_in_protein, residue).
    """
    out = []
    for mod in parse_proforma(pf):
        if variable_only and mod.unimod_id not in VARIABLE_MODS:
            continue
        pos = peptide_start + mod.pos - 1 if mod.pos else peptide_start
        out.append((mod.unimod_id, pos, mod.residue))
    return out
