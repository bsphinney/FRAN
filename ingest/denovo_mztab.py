"""Read a Casanovo mzTab, across both column generations.

Casanovo 5.2.0 renamed the columns to the spec:
    sequence                -> opt_global_cv_MS:1003169_proforma_peptidoform_sequence
    opt_ms_run[1]_aa_scores -> opt_global_aa_scores
A reader that knows only the old names returns zero peptides from a 5.2 file WITHOUT erroring,
which is indistinguishable from an empty sample. So accept both spellings, and RAISE when neither
is present rather than returning an empty list.

The raw `sequence` is kept WITH its modifications. `N+0.984` versus a plain `D` is a distinction no
mass can recover later -- they are the same mass, and casanovo carries both tokens -- so stripping
it here would silently destroy the evidence phase 2 needs to tell deamidation from a variant.
"""
from __future__ import annotations

import re

from denovo_corpus_match import il

_MOD = re.compile(r"[\[(][^\])]*[\])]|[+-]\d+\.\d+")
_SCAN = re.compile(r"scan=(\d+)")
_VER = re.compile(r"Casanovo,\s*([0-9][^\]\s]*)")

SEQ_COLS = ("sequence", "opt_global_cv_MS:1003169_proforma_peptidoform_sequence")
AA_COLS = ("opt_ms_run[1]_aa_scores", "opt_global_aa_scores")


def _pick(header: list[str], names: tuple[str, ...]) -> int | None:
    for n in names:
        if n in header:
            return header.index(n)
    return None


def read_mztab(path: str) -> dict:
    """-> {'engine_version': str|None, 'psms': [ {...}, ... ]}"""
    psms: list[dict] = []
    version = None
    seq_i = aa_i = sc_i = ch_i = mz_i = ref_i = None

    with open(path) as fh:
        for line in fh:
            if line.startswith("MTD"):
                m = _VER.search(line)
                if m:
                    version = m.group(1)
            elif line.startswith("PSH"):
                h = line.rstrip("\n").split("\t")
                seq_i = _pick(h, SEQ_COLS)
                if seq_i is None:
                    raise ValueError(
                        f"{path}: no recognised sequence column. Looked for {SEQ_COLS}; found "
                        f"{h[:12]}. A casanovo >=5.2 file needs the opt_global_cv name.")
                aa_i = _pick(h, AA_COLS)
                sc_i = h.index("search_engine_score[1]") if "search_engine_score[1]" in h else None
                ch_i = h.index("charge") if "charge" in h else None
                mz_i = h.index("exp_mass_to_charge") if "exp_mass_to_charge" in h else None
                ref_i = h.index("spectra_ref") if "spectra_ref" in h else None
            elif line.startswith("PSM") and seq_i is not None:
                f = line.rstrip("\n").split("\t")
                if len(f) <= seq_i:
                    continue

                def _f(i):
                    try:
                        return float(f[i])
                    except (TypeError, ValueError, IndexError):
                        return None

                scan = None
                if ref_i is not None and len(f) > ref_i:
                    m = _SCAN.search(f[ref_i])
                    scan = int(m.group(1)) if m else None

                aa: list[float] = []
                if aa_i is not None and len(f) > aa_i and f[aa_i] not in ("null", ""):
                    try:
                        aa = [float(x) for x in f[aa_i].split(",") if x]
                    except ValueError:
                        aa = []

                raw = f[seq_i]
                stripped = _MOD.sub("", raw).upper()
                ch = _f(ch_i) if ch_i is not None else None
                psms.append({
                    "scan": scan,
                    "sequence": raw,
                    "stripped_seq": stripped,
                    "seq_il": il(stripped),
                    "charge": int(ch) if ch else None,
                    "exp_mz": _f(mz_i) if mz_i is not None else None,
                    "peptide_score": _f(sc_i) if sc_i is not None else None,
                    "aa_scores": aa,
                })

    if seq_i is None:
        raise ValueError(f"{path}: no PSH header line found")
    return {"engine_version": version, "psms": psms}
