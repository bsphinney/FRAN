"""Aggregate PSMs to peptides, carrying a length-normalised confidence.

WHY conf_geomean EXISTS. Casanovo's peptide score is the PRODUCT of per-residue scores
(model.py _peptide_score), so it is ~p^L and collapses with length whatever the quality. Measured
across 10.76M PSMs, per-residue confidence PEAKS at length 20 while the peptide score ranks those
peptides ~1,400x worse than 7-mers. A raw-score threshold is therefore a LENGTH filter that
discards the best-sequenced material first -- fatal for variant work, where short peptides are the
least informative (a 7-mer maps ambiguously and rarely spans a substitution with flanking context).

conf_geomean = peptide_score ** (1/length) is roughly length-stable and is the only per-peptide
number that compares across lengths. Store it ALONGSIDE length, never instead of it: peptide_score
on its own cannot distinguish a confident long peptide from an unconfident short one.
"""
from __future__ import annotations

import statistics


def _prob(score: float | None) -> float:
    """Casanovo emits scores in [-1, 1]; map to [0, 1] for the geometric mean."""
    if score is None:
        return 0.0
    return max(0.0, (score + 1) / 2 if score < 0 else score)


def aggregate_peptides(psms: list[dict]) -> list[dict]:
    best: dict[str, dict] = {}
    for p in psms:
        seq = p["stripped_seq"]
        if not seq:
            continue
        score = _prob(p.get("peptide_score"))
        row = best.get(seq)
        if row is None:
            best[seq] = {"stripped_seq": seq, "seq_il": p["seq_il"], "length": len(seq),
                         "n_psms": 1, "peptide_score": score}
        else:
            row["n_psms"] += 1
            row["peptide_score"] = max(row["peptide_score"], score)
    for row in best.values():
        row["conf_geomean"] = row["peptide_score"] ** (1.0 / row["length"]) if row["length"] else 0.0
    return sorted(best.values(), key=lambda r: r["stripped_seq"])


def run_stats(peptides: list[dict]) -> dict:
    if not peptides:
        return {"n_psms": 0, "n_peptides": 0, "len_median": None, "conf_geomean_median": None}
    return {
        "n_psms": sum(p["n_psms"] for p in peptides),
        "n_peptides": len(peptides),
        "len_median": int(statistics.median(p["length"] for p in peptides)),
        "conf_geomean_median": float(statistics.median(p["conf_geomean"] for p in peptides)),
    }
