"""Read-side queries for the de novo lane.

FRAN never writes a de novo row -- the DE-LIMP ingestor owns the write path. Every function names
its `tables` so db.query() can enforce the public allowlist.

Two things here are load-bearing and easy to undo by accident:

  * `peptide_denovo` joins on `seq_il`, not `stripped_seq`. De novo cannot distinguish isoleucine
    from leucine, so matching the raw spelling finds only half of what is there (16.3% vs 32.8%
    measured on the crane cohort).
  * `length` is selected wherever `peptide_score` is. The Casanovo peptide score is the product of
    per-residue scores, so on its own it cannot distinguish a confident long peptide from an
    unconfident short one. `conf_geomean` is the length-normalised number to compare on.
"""
from __future__ import annotations

from typing import Any

from .db import query


def _page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(int(limit or 50), 500)), max(0, int(offset or 0))


def list_runs(cohort: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """The de novo run index."""
    lim, off = _page(limit, offset)
    params: dict[str, Any] = {"lim": lim, "off": off}
    where = ""
    if cohort:
        where = "WHERE cohort = %(cohort)s"
        params["cohort"] = cohort
    rows = query(
        f"""SELECT run_id, run_name, cohort, denovo_engine, engine_version, gpu_arch, donor_id,
                   sample_role, n_psms, n_peptides, len_median, conf_geomean_median, match_fdr,
                   ingest_flag
              FROM delimp_denovo_run {where}
             ORDER BY ingested_at DESC, run_name
             LIMIT %(lim)s OFFSET %(off)s""",
        params, tables=["delimp_denovo_run"])
    total = query(f"SELECT count(*) FROM delimp_denovo_run {where}", params,
                  tables=["delimp_denovo_run"], fetch="val")
    return {"rows": rows, "total": total}


def run_detail(run_id: str) -> dict[str, Any]:
    """One run's provenance and class split, for the run page header."""
    rows = query(
        """SELECT run_id, run_name, cohort, denovo_engine, engine_version, weights, gpu_arch,
                  donor_id, sample_role, replicate_of, replicate_kind, n_psms, n_peptides,
                  len_median, conf_geomean_median, match_fdr, ingest_flag, ingested_at
             FROM delimp_denovo_run WHERE run_id = %(run_id)s""",
        {"run_id": run_id}, tables=["delimp_denovo_run"])
    if not rows:
        return {"run": None, "classes": {}}
    classes = query(
        """SELECT corpus_class, count(*) AS n
             FROM delimp_denovo_peptide WHERE run_id = %(run_id)s GROUP BY 1""",
        {"run_id": run_id}, tables=["delimp_denovo_peptide"])
    return {"run": rows[0], "classes": {r["corpus_class"]: r["n"] for r in classes}}


def run_peptides(run_id: str, corpus_class: str | None = None,
                 limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """A run's peptides, each with the corpus spellings it links to.

    corpus_stripped_seq is aggregated into an array because the I/L match is one-to-many: one de
    novo peptide can point at several real corpus peptides (8.2% of crane matches did), and the UI
    shows every candidate rather than silently choosing one.
    """
    lim, off = _page(limit, offset)
    params: dict[str, Any] = {"run_id": run_id, "lim": lim, "off": off}
    cls = ""
    if corpus_class in ("conserved", "gvp", "novel"):
        cls = "AND p.corpus_class = %(cls)s"
        params["cls"] = corpus_class
    rows = query(
        f"""SELECT p.stripped_seq, p.length, p.n_psms, p.peptide_score, p.conf_geomean,
                   p.corpus_class,
                   coalesce(array_agg(m.corpus_stripped_seq ORDER BY m.corpus_stripped_seq)
                            FILTER (WHERE m.corpus_stripped_seq IS NOT NULL),
                            ARRAY[]::text[]) AS corpus_hits,
                   max(m.n_candidates) AS n_candidates
              FROM delimp_denovo_peptide p
              LEFT JOIN delimp_denovo_corpus_match m
                     ON m.run_id = p.run_id AND m.stripped_seq = p.stripped_seq
             WHERE p.run_id = %(run_id)s {cls}
             GROUP BY p.stripped_seq, p.length, p.n_psms, p.peptide_score, p.conf_geomean,
                      p.corpus_class
             ORDER BY p.n_psms DESC, p.stripped_seq
             LIMIT %(lim)s OFFSET %(off)s""",
        params, tables=["delimp_denovo_peptide", "delimp_denovo_corpus_match"])
    total = query(
        f"SELECT count(*) FROM delimp_denovo_peptide p WHERE p.run_id = %(run_id)s {cls}",
        params, tables=["delimp_denovo_peptide"], fetch="val")
    return {"rows": rows, "total": total}


def peptide_denovo(stripped_seq: str) -> dict[str, Any]:
    """Direction B: which de novo runs called this peptide.

    Joins on seq_il so a DIA peptide spelled with I finds the de novo peptide spelled with L.
    """
    seq = (stripped_seq or "").strip().upper()
    if not seq:
        return {"rows": []}
    rows = query(
        """SELECT r.run_id, r.run_name, r.cohort, r.donor_id, r.sample_role,
                  p.stripped_seq, p.length, p.n_psms, p.conf_geomean, p.corpus_class
             FROM delimp_denovo_peptide p
             JOIN delimp_denovo_run r ON r.run_id = p.run_id
            WHERE p.seq_il = replace(%(seq)s, 'I', 'L')
            ORDER BY p.n_psms DESC
            LIMIT 100""",
        {"seq": seq}, tables=["delimp_denovo_peptide", "delimp_denovo_run"])
    return {"rows": rows}
