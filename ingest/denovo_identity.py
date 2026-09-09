"""Donor identity from a de novo run name.

SEARCH the name for a donor token; never parse by position. The Parker hair cohort has at least
four filename grammars, and four independent positional-parsing attempts each produced a different
wrong count (353 / 337 / 334 / 318 ZG runs out of 421). Two traps in particular:

  * `\\b` is NOT a token boundary here. `_` is a word character, so `\\b2G` never matches
    `_10_2G26` and silently drops all 25 of those runs.
  * `2G` is not a separate donor series. It is ZG11-35: contiguous, with ZERO number collisions
    against ZG36-463. That zero-collision result is what licenses folding it into ZG -- if they
    were different donors, folding would merge two people in a validation set, which is worse than
    a broken query.

The raw token is preserved alongside the normalised donor id; nothing overwrites the original.
"""
from __future__ import annotations

import re

# (?:^|_) rather than \b, for the underscore reason above.
_ZG = re.compile(r"(?:^|_)([Zz][Gg]|2[Gg])_?(\d+)")
_OTHER_INITIALS = re.compile(r"(?:^|_)([A-Z]{2})(\d+)(?:_|$)")
_STANDARD = re.compile(r"BSA|Pooled|Femto|HeLa|iRT", re.I)


def parse_identity(run_name: str) -> dict:
    """-> {'donor_id': str|None, 'sample_role': str, 'raw_token': str|None}

    sample_role defaults to 'unknown', never 'donor': folder membership does not imply donor
    status, and a bovine BSA standard with 7,844 PSMs and normal confidence looks like a healthy
    sample to every automated check. In a human variant study every call from it is an artefact.
    """
    if _STANDARD.search(run_name):
        return {"donor_id": None, "sample_role": "standard", "raw_token": None}

    m = _ZG.search(run_name)
    if m:
        # 2G folds to ZG; case is cosmetic.
        return {"donor_id": f"ZG{int(m.group(2))}",
                "sample_role": "donor",
                "raw_token": m.group(0).lstrip("_")}

    m = _OTHER_INITIALS.search(run_name)
    if m:
        # A real token, but not attributable to a known donor series.
        return {"donor_id": None, "sample_role": "unknown",
                "raw_token": f"{m.group(1)}{m.group(2)}"}

    return {"donor_id": None, "sample_role": "unknown", "raw_token": None}
