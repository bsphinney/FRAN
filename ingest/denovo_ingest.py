"""Ingest one de novo mzTab into the FRAN corpus. DRY RUN unless --apply.

The corpus index is a one-time COPY of delimp_peptide_consensus (2.93M peptides, 44 MB, 5.2 s)
folded to an I/L key in Python. Matching in Python rather than SQL keeps the join off a 262 GB
production database; the whole pass takes ~7 s.

Idempotent per run: run_id = uuid5(NS, run_name), and --apply deletes that run's rows before
inserting. Re-ingest is delete+insert scoped to one run, never a corpus-wide rewrite.

MATCH_FDR IS COMPUTED ON EVERY RUN, not offered as an option. Decoy peptides -- interior-reversed,
so matched to the real set on length, composition and both termini -- are run through the identical
matching code and their hit rate is the chance-match rate. Measured 38.7% on the crane cohort and
corroborated at 38.0% by an independent reversed-corpus null, so a candidate count reported without
it overstates the result by about 2x.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from denovo_aggregate import aggregate_peptides, run_stats     # noqa: E402
from denovo_corpus_match import il, match_peptides             # noqa: E402
from denovo_identity import parse_identity                     # noqa: E402
from denovo_mztab import read_mztab                            # noqa: E402

NS = uuid.UUID("6f5c9b3a-1d2e-4f8a-9c7b-3e5d1a2b4c6d")   # de novo lane namespace


def interior_reverse(p: str) -> str:
    """Reverse the interior, keep both termini. Preserves length, composition and both ends,
    so the decoy differs from the real peptide in nothing except being a real sequence."""
    return p if len(p) < 4 else p[0] + p[-2:0:-1] + p[-1]


def load_corpus_index(conn) -> dict[str, list[str]]:
    """I/L key -> the real corpus spellings under it, from delimp_peptide_consensus."""
    idx: dict[str, list[str]] = {}
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT stripped_seq FROM delimp_peptide_consensus")
    for (seq,) in cur:
        idx.setdefault(il(seq), []).append(seq)
    return idx


# Below this many real matches the decoy ratio is dominated by counting noise and would be read as
# a precise small number. Measured motivation: an ancient-enamel run produced 51 matches from 22,084
# peptides and a match_fdr of 0.0196, which looks like a 2% error rate and is actually nothing --
# FRAN's corpus is tryptic DIA of tissue and plasma and holds no enamel proteome, so that cohort
# simply does not overlap it (0.23%, against 32.8% for tryptic hair).
MIN_MATCHES_FOR_FDR = 200


def match_fdr(peptides: list[dict], corpus: dict[str, list[str]], n_real_hits: int) -> float | None:
    """Chance-match rate: decoy peptides through the identical matching code.

    Returns None rather than a number when there are too few real matches to divide by.
    """
    if not peptides or n_real_hits < MIN_MATCHES_FOR_FDR:
        return None
    decoys = sorted({interior_reverse(p["stripped_seq"]) for p in peptides})
    decoys = [d for d in decoys if il(d) not in corpus or d not in corpus.get(il(d), [])]
    if not decoys:
        return None
    d_hits = len({m["stripped_seq"] for m in match_peptides(decoys, corpus)})
    real_rate = n_real_hits / len(peptides)
    return round((d_hits / len(decoys)) / real_rate, 4) if real_rate else None


def build_rows(mztab_path: str, cohort: str, corpus: dict[str, list[str]],
               gpu_arch: str | None = None, weights: str | None = None) -> dict:
    run_name = os.path.basename(mztab_path).replace(".mztab", "")
    ident = parse_identity(run_name)
    d = read_mztab(mztab_path)
    peptides = aggregate_peptides(d["psms"])
    matches = match_peptides([p["stripped_seq"] for p in peptides], corpus)

    matched = {m["stripped_seq"] for m in matches}
    for p in peptides:
        p["corpus_class"] = "conserved" if p["stripped_seq"] in matched else "novel"

    return {
        "run": {
            "run_id": str(uuid.uuid5(NS, run_name)), "run_name": run_name, "cohort": cohort,
            "denovo_engine": "casanovo", "engine_version": d["engine_version"],
            "weights": weights, "gpu_arch": gpu_arch, "bundle_format": "mztab",
            "donor_id": ident["donor_id"], "sample_role": ident["sample_role"],
            "match_fdr": match_fdr(peptides, corpus, len(matched)),
            "ingest_flag": None, **run_stats(peptides),
        },
        "peptides": peptides, "psms": d["psms"], "matches": matches,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mztab", required=True)
    ap.add_argument("--cohort", required=True)
    ap.add_argument("--gpu-arch")
    ap.add_argument("--weights")
    ap.add_argument("--apply", action="store_true", help="write; otherwise dry run")
    a = ap.parse_args()

    import psycopg2
    import psycopg2.extras
    from refresh_leaderboards import _token

    con = psycopg2.connect(
        host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
        dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30)

    corpus = load_corpus_index(con)
    out = build_rows(a.mztab, a.cohort, corpus, a.gpu_arch, a.weights)
    print(json.dumps({"run": out["run"], "n_matches": len(out["matches"])}, indent=2, default=str))

    if not a.apply:
        print("\nDRY RUN -- nothing written. Re-run with --apply.")
        return

    cur = con.cursor()
    rid = out["run"]["run_id"]
    for t in ("delimp_denovo_corpus_match", "delimp_denovo_psm", "delimp_denovo_peptide"):
        cur.execute(f"DELETE FROM {t} WHERE run_id = %s", (rid,))
    cur.execute("DELETE FROM delimp_denovo_run WHERE run_id = %s", (rid,))
    cur.execute(
        "INSERT INTO delimp_denovo_run (run_id,run_name,cohort,denovo_engine,engine_version,"
        "weights,gpu_arch,bundle_format,donor_id,sample_role,n_psms,n_peptides,len_median,"
        "conf_geomean_median,match_fdr,ingest_flag) VALUES (%(run_id)s,%(run_name)s,%(cohort)s,"
        "%(denovo_engine)s,%(engine_version)s,%(weights)s,%(gpu_arch)s,%(bundle_format)s,"
        "%(donor_id)s,%(sample_role)s,%(n_psms)s,%(n_peptides)s,%(len_median)s,"
        "%(conf_geomean_median)s,%(match_fdr)s,%(ingest_flag)s)", out["run"])
    psycopg2.extras.execute_values(
        cur, "INSERT INTO delimp_denovo_peptide (run_id,stripped_seq,seq_il,length,n_psms,"
             "peptide_score,conf_geomean,corpus_class) VALUES %s",
        [(rid, p["stripped_seq"], p["seq_il"], p["length"], p["n_psms"], p["peptide_score"],
          p["conf_geomean"], p["corpus_class"]) for p in out["peptides"]])
    psycopg2.extras.execute_values(
        cur, "INSERT INTO delimp_denovo_psm (run_id,scan,stripped_seq,seq_il,sequence,charge,"
             "exp_mz,peptide_score,aa_scores) VALUES %s",
        [(rid, p["scan"], p["stripped_seq"], p["seq_il"], p["sequence"], p["charge"], p["exp_mz"],
          p["peptide_score"], p["aa_scores"]) for p in out["psms"] if p["scan"] is not None])
    psycopg2.extras.execute_values(
        cur, "INSERT INTO delimp_denovo_corpus_match (run_id,stripped_seq,corpus_stripped_seq,"
             "match_kind,n_candidates) VALUES %s",
        [(rid, m["stripped_seq"], m["corpus_stripped_seq"], m["match_kind"], m["n_candidates"])
         for m in out["matches"]])
    con.commit()
    print(f"\nwrote run {rid}: {len(out['peptides'])} peptides, {len(out['matches'])} matches")


if __name__ == "__main__":
    main()
