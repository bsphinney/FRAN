"""Theoretical b/y fragment ions for a peptide — pure calculation, no measured data.

Monoisotopic singly/doubly charged b and y ion m/z, computed from the bare sequence.
This is the honest "predicted spectrum" layer: it shows which fragments CAN form and
their exact m/z. Observed relative intensities and which-fragments-are-quantified come
from the DIA-NN spectral library (see queries.peptide_xic) and overlay onto these ions.

Masses are standard monoisotopic residue masses; verified against known PEPTIDE b/y ions
(b2=227.10263, y1=148.06043). Cys defaults to carbamidomethylated (+57.02146), the near-
universal fixed mod — toggleable, and labeled in the response.
"""
from __future__ import annotations

# monoisotopic residue masses (Da)
_RES = {
    "G": 57.02146, "A": 71.03711, "S": 87.03203, "P": 97.05276, "V": 99.06841,
    "T": 101.04768, "C": 103.00919, "L": 113.08406, "I": 113.08406, "N": 114.04293,
    "D": 115.02694, "Q": 128.05858, "K": 128.09496, "E": 129.04259, "M": 131.04049,
    "H": 137.05891, "F": 147.06841, "R": 156.10111, "Y": 163.06333, "W": 186.07931,
}
_WATER = 18.0105646
_PROTON = 1.0072765
_CARBAMIDOMETHYL = 57.02146  # UniMod:4 on C


def fragments(seq: str, carbamidomethyl: bool = True, max_charge: int = 2) -> dict:
    """Return theoretical b and y ions (charges 1..max_charge) for a stripped sequence."""
    s = "".join(c for c in (seq or "").upper() if c in _RES)
    if len(s) < 2:
        return {"sequence": s, "carbamidomethyl": carbamidomethyl, "ions": []}
    res = [_RES[c] + (_CARBAMIDOMETHYL if (carbamidomethyl and c == "C") else 0.0) for c in s]
    n = len(s)
    ions = []
    # b ions: N-terminal, b_i = sum(res[:i]) + proton ; y ions: C-terminal
    b_cum = 0.0
    for i in range(1, n):
        b_cum += res[i - 1]
        for z in range(1, max_charge + 1):
            ions.append({"ion": f"b{i}", "type": "b", "series": i, "charge": z,
                         "mz": round((b_cum + z * _PROTON) / z, 5)})
    y_cum = 0.0
    for i in range(1, n):
        y_cum += res[n - i]
        for z in range(1, max_charge + 1):
            ions.append({"ion": f"y{i}", "type": "y", "series": i, "charge": z,
                         "mz": round((y_cum + _WATER + z * _PROTON) / z, 5)})
    ions.sort(key=lambda x: x["mz"])
    return {"sequence": s, "length": n, "carbamidomethyl": carbamidomethyl,
            "max_charge": max_charge, "ions": ions}
