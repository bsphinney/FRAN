"""
All SQL for the corpus browser. Every function:
  - names its `tables` so db.query() can enforce the public-layer allowlist,
  - uses %s / %(name)s placeholders only (no string interpolation of input),
  - leans on the schema's indexes (idx_prec_stripped_charge, idx_prec_search,
    idx_proteins_group, idx_proteins_gene, idx_consensus_stripped, etc.).
"""

from __future__ import annotations

from typing import Any

from .db import (
    CACHE,
    SLOW_CACHE,
    estimate_distinct,
    estimate_non_null,
    estimate_rows,
    estimate_value_distribution,
    query,
)


def _exact_or_none(sql: str, table: str):
    """Exact COUNT for a small table; on any error (e.g. timeout) return None so
    the dashboard card shows "—" instead of 503-ing the whole overview."""
    try:
        return query(sql, tables=[table], fetch="val")
    except Exception:  # noqa: BLE001
        return None

MAX_PAGE = 200  # never let the browser pull more than this many rows at once


def _page(limit: int | None, offset: int | None) -> tuple[int, int]:
    lim = min(int(limit or 50), MAX_PAGE)
    off = max(int(offset or 0), 0)
    return lim, off


# ---------------------------------------------------------------------------
# Overview dashboard (cached, cheap aggregates) — "watch it populate"
# ---------------------------------------------------------------------------
def overview_counts() -> dict[str, Any]:
    def _producer() -> dict[str, Any]:
        # Snapshot dashboard. Small tables get exact COUNT(*); the big
        # delimp_precursors / delimp_proteins counts (millions of rows) use the
        # planner's estimates (pg_class.reltuples / pg_stats.n_distinct) so a
        # page load is instant and never trips the 30s statement timeout — the
        # bug that left every card blank. Each value degrades to None ("—")
        # independently; the dashboard never 503s because one count is slow.
        searches = _exact_or_none(
            "SELECT COUNT(*) FROM delimp_searches", "delimp_searches"
        )
        raw_files = _exact_or_none("SELECT COUNT(*) FROM raw_files", "raw_files")
        organisms = _exact_or_none(
            "SELECT COUNT(DISTINCT organism_taxon_id) FROM delimp_sample_metadata "
            "WHERE organism_taxon_id IS NOT NULL",
            "delimp_sample_metadata",
        )
        return {
            "searches": searches,
            "raw_files": raw_files,
            "organisms": organisms,
            "proteins": estimate_rows("delimp_proteins"),
            "precursors": estimate_rows("delimp_precursors"),
            "distinct_peptides": estimate_distinct("delimp_precursors", "stripped_seq"),
            "distinct_protein_groups": estimate_distinct(
                "delimp_proteins", "protein_group"
            ),
            "im_bearing_precursors": estimate_non_null("delimp_precursors", "im"),
            "estimated": True,  # cards may show "~" — counts are planner estimates
        }

    return CACHE.get_or_set("overview_counts", _producer)


def species_distribution(limit: int = 15) -> list[dict[str, Any]]:
    def _producer() -> list[dict[str, Any]]:
        rows = query(
            """
            SELECT COALESCE(organism_name, 'Unknown') AS organism,
                   organism_taxon_id,
                   COUNT(*) AS n_runs
            FROM delimp_sample_metadata
            GROUP BY organism_name, organism_taxon_id
            ORDER BY n_runs DESC
            LIMIT %s
            """,
            (limit,),
            tables=["delimp_sample_metadata"],
        )
        return rows

    return CACHE.get_or_set(f"species_dist_{limit}", _producer)


def platform_distribution() -> list[dict[str, Any]]:
    def _producer() -> list[dict[str, Any]]:
        return query(
            """
            SELECT COALESCE(platform, 'other') AS platform,
                   COALESCE(acquisition_method, 'unknown') AS acquisition_method,
                   COALESCE(instrument_model, 'unknown') AS instrument_model,
                   COUNT(*) AS n_runs
            FROM raw_files
            GROUP BY platform, acquisition_method, instrument_model
            ORDER BY n_runs DESC
            """,
            tables=["raw_files"],
        )

    return CACHE.get_or_set("platform_dist", _producer)


def engine_distribution() -> list[dict[str, Any]]:
    def _producer() -> list[dict[str, Any]]:
        return query(
            """
            SELECT search_engine, COUNT(*) AS n_searches,
                   COALESCE(SUM(n_precursors_total), 0) AS n_precursors
            FROM delimp_searches
            GROUP BY search_engine
            ORDER BY n_searches DESC
            """,
            tables=["delimp_searches"],
        )

    return CACHE.get_or_set("engine_dist", _producer)


def recent_searches(limit: int = 10) -> list[dict[str, Any]]:
    # NOT cached (or very short) so freshly-ingested searches appear fast.
    return query(
        """
        SELECT id, search_name, search_engine, search_engine_version,
               pipeline_id, status, sharing_status,
               n_raw_files, n_precursors_total, n_proteins_total,
               completed_at, submitted_at, ingested_at, delimp_version
        FROM delimp_searches
        ORDER BY COALESCE(ingested_at, submitted_at) DESC
        LIMIT %s
        """,
        (limit,),
        tables=["delimp_searches"],
    )


def im_rt_density_sample(search_id: str | None = None, sample_n: int = 6000) -> dict[str, Any]:
    """ONE point per distinct precursor for the RT/iRT × 1/K0 scatter (DISTINCT ON
    dedups — avoids the old clump-per-peptide artifact from a bare LIMIT). PREFERS
    iRT (cross-run-comparable) when that column exists AND is populated, else raw RT;
    returns {points, x_axis} so the axis auto-switches to true iRT once iRT is ingested.
    Cached snapshot. Falls back to [] on timeout."""
    where = "AND search_id = %s" if search_id else ""
    extra = (search_id,) if search_id else ()

    def _q(with_irt):
        # Global view (no search_id): read the precomputed sample matview (the live
        # DISTINCT ON over millions of rows times out on PG Farm). Per-search view stays
        # live (small). The mv carries rt+irt so the axis logic below still applies.
        cols = "rt, im, charge, precursor_mz, intensity_log2" + (", irt" if with_irt else "")
        if not search_id:
            try:  # precomputed sample (best)
                return query(f"SELECT {cols} FROM delimp_mv_im_scatter LIMIT %s",
                             (sample_n,), tables=["delimp_mv_im_scatter"])
            except Exception:  # noqa: BLE001 - mv not built -> fast random TABLESAMPLE (no full scan/sort)
                return query(
                    f"SELECT {cols} FROM delimp_precursors TABLESAMPLE SYSTEM (2) "
                    f"WHERE im > 0.3 AND rt IS NOT NULL LIMIT %s",
                    (sample_n,), tables=["delimp_precursors"], timeout_ms=20000)
        sel = "stripped_seq, charge, rt, im, precursor_mz, intensity_log2" + (", irt" if with_irt else "")
        out = "rt, im, charge, precursor_mz, intensity_log2" + (", irt" if with_irt else "")
        return query(
            f"""SELECT {out} FROM (
                  SELECT DISTINCT ON (stripped_seq, charge) {sel}
                  FROM delimp_precursors
                  WHERE im > 0.3 AND rt IS NOT NULL {where}
                  ORDER BY stripped_seq, charge
                ) q LIMIT %s""",
            (*extra, sample_n), tables=["delimp_precursors"], timeout_ms=28000)

    def _producer():
        rows = None
        for with_irt in (True, False):     # try iRT-aware; fall back if the column doesn't exist yet
            try:
                rows = _q(with_irt)
                break
            except Exception:  # noqa: BLE001
                continue
        if not rows:
            return {"points": [], "x_axis": "Retention time (min)"}

        def _pt(r, x):
            return {"rt": x, "im": r["im"], "charge": r["charge"],
                    "precursor_mz": r.get("precursor_mz"), "intensity_log2": r.get("intensity_log2")}

        # NEVER mix scales on one axis. Use iRT only when it covers the MAJORITY of the
        # sample, and then plot ONLY iRT-bearing precursors. Otherwise plot raw RT for all.
        # (Mixing iRT minutes-scale clump + raw RT was the two-population artifact.)
        n_irt = sum(1 for r in rows if r.get("irt") is not None)
        if n_irt and n_irt >= 0.5 * len(rows):
            return {"points": [_pt(r, r["irt"]) for r in rows if r.get("irt") is not None],
                    "x_axis": "Indexed retention time (iRT)"}
        return {"points": [_pt(r, r["rt"]) for r in rows],
                "x_axis": "Retention time (min)"}

    if search_id:
        return _producer()
    return SLOW_CACHE.get_or_set(f"im_scatter_{sample_n}", _producer)


def charge_distribution() -> list[dict[str, Any]]:
    def _producer() -> list[dict[str, Any]]:
        # Estimate from pg_stats (instant) — an exact GROUP BY over the 2.7M-row
        # precursors table hits the statement timeout and blanked the dashboard.
        est = estimate_value_distribution("delimp_precursors", "charge")
        if est:
            out = []
            for r in est:
                try:
                    ch = int(float(r["value"]))
                except (TypeError, ValueError):
                    continue
                out.append({"charge": ch, "n": r["n"]})
            return sorted(out, key=lambda x: x["charge"])
        return []

    return CACHE.get_or_set("charge_dist", _producer)


# ---------------------------------------------------------------------------
# Search / browse
# ---------------------------------------------------------------------------
def search_peptides(
    seq: str, exact: bool = False, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    lim, off = _page(limit, offset)
    seq = (seq or "").strip().upper()
    if not seq:
        return {"rows": [], "total": 0}

    if exact:
        where = "stripped_seq = %s"
        param: Any = seq
    else:
        # trigram-indexed substring (idx_prec_stripped_trgm)
        where = "stripped_seq ILIKE %s"
        param = f"%{seq}%"

    rows = query(
        f"""
        SELECT stripped_seq,
               COUNT(*)                         AS n_precursors,
               COUNT(DISTINCT modified_seq_proforma) AS n_modforms,
               COUNT(DISTINCT charge)           AS n_charges,
               COUNT(DISTINCT raw_path)         AS n_runs,
               COUNT(DISTINCT search_id)        AS n_searches,
               MIN(q_value)                     AS best_q_value,
               bool_or(im IS NOT NULL)          AS has_im,
               MAX(n_engines_confirming)        AS max_engines
        FROM delimp_precursors
        WHERE {where}
        GROUP BY stripped_seq
        ORDER BY n_precursors DESC
        LIMIT %s OFFSET %s
        """,
        (param, lim, off),
        tables=["delimp_precursors"],
    )
    total = query(
        f"SELECT COUNT(DISTINCT stripped_seq) FROM delimp_precursors WHERE {where}",
        (param,),
        tables=["delimp_precursors"],
        fetch="val",
    )
    return {"rows": rows, "total": total or 0, "limit": lim, "offset": off}


def search_proteins(
    term: str, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    lim, off = _page(limit, offset)
    term = (term or "").strip()
    if not term:
        return {"rows": [], "total": 0}
    like = f"%{term}%"
    rows = query(
        """
        SELECT protein_group,
               MAX(gene)                  AS gene,
               COUNT(DISTINCT search_id)  AS n_searches,
               COUNT(DISTINCT raw_path)   AS n_runs,
               SUM(n_unique_peptides)     AS sum_unique_peptides,
               SUM(n_precursors)          AS sum_precursors,
               bool_or(is_contaminant)    AS any_contaminant
        FROM delimp_proteins
        WHERE protein_group ILIKE %s OR gene ILIKE %s
        GROUP BY protein_group
        ORDER BY sum_precursors DESC NULLS LAST
        LIMIT %s OFFSET %s
        """,
        (like, like, lim, off),
        tables=["delimp_proteins"],
    )
    total = query(
        "SELECT COUNT(DISTINCT protein_group) FROM delimp_proteins "
        "WHERE protein_group ILIKE %s OR gene ILIKE %s",
        (like, like),
        tables=["delimp_proteins"],
        fetch="val",
    )
    return {"rows": rows, "total": total or 0, "limit": lim, "offset": off}


# ---------------------------------------------------------------------------
# Detail views
# ---------------------------------------------------------------------------
def protein_detail(protein_group: str) -> dict[str, Any]:
    pg = (protein_group or "").strip()
    summary = query(
        """
        SELECT protein_group,
               MAX(gene) AS gene,
               COUNT(DISTINCT search_id) AS n_searches,
               COUNT(DISTINCT raw_path)  AS n_runs,
               SUM(n_unique_peptides)    AS sum_unique_peptides,
               SUM(n_precursors)         AS sum_precursors,
               AVG(NULLIF(intensity, 0)) AS avg_intensity,
               MIN(pg_q_value)           AS best_pg_q,
               bool_or(is_contaminant)   AS any_contaminant
        FROM delimp_proteins
        WHERE protein_group = %s
        GROUP BY protein_group
        """,
        (pg,),
        tables=["delimp_proteins"],
        fetch="one",
    )
    per_search = query(
        """
        SELECT p.search_id, s.search_name, s.search_engine,
               p.raw_path, p.gene, p.n_unique_peptides, p.n_precursors,
               p.intensity, p.normalized_intensity, p.pg_q_value
        FROM delimp_proteins p
        JOIN delimp_searches s ON s.id = p.search_id
        WHERE p.protein_group = %s
        ORDER BY p.intensity DESC NULLS LAST
        LIMIT %s
        """,
        (pg, MAX_PAGE),
        tables=["delimp_proteins", "delimp_searches"],
    )
    # Observed peptides for this protein group: join precursors to the
    # search/run set where this protein group was reported. We bound via the
    # (search_id, raw_path) pairs from delimp_proteins to stay index-friendly.
    peptides = query(
        """
        WITH pg_runs AS (
            SELECT DISTINCT search_id, raw_path
            FROM delimp_proteins
            WHERE protein_group = %s
        )
        SELECT pr.stripped_seq,
               COUNT(*)                       AS n_precursors,
               COUNT(DISTINCT pr.charge)      AS n_charges,
               COUNT(DISTINCT pr.raw_path)    AS n_runs,
               MIN(pr.q_value)                AS best_q_value,
               bool_or(pr.im IS NOT NULL)     AS has_im
        FROM delimp_precursors pr
        JOIN pg_runs r ON r.search_id = pr.search_id AND r.raw_path = pr.raw_path
        GROUP BY pr.stripped_seq
        ORDER BY n_precursors DESC
        LIMIT %s
        """,
        (pg, MAX_PAGE),
        tables=["delimp_proteins", "delimp_precursors"],
    )
    return {"summary": summary, "per_search": per_search, "peptides": peptides}


# ---------------------------------------------------------------------------
# Corpus leaderboards ("most common ...") — heavy GROUP BYs, long-cached.
# "Common" = most reproducibly OBSERVED (across runs/searches), which is the
# meaningful signal and gets richer as the corpus diversifies. Failures raise
# (so they are NOT cached); the endpoint wraps each in a safe fallback.
# ---------------------------------------------------------------------------
def top_peptides(limit: int = 40) -> list[dict[str, Any]]:
    def _p():
        try:  # fast path: precomputed materialized view (refreshed offline)
            return query(
                "SELECT stripped_seq, n_obs, n_runs, n_searches, n_charges, has_im "
                "FROM delimp_mv_top_peptides ORDER BY n_runs DESC, n_obs DESC LIMIT %s",
                (int(limit),), tables=["delimp_mv_top_peptides"],
            )
        except Exception:  # noqa: BLE001 - mv not built -> instant pg_stats approximation
            mcv = estimate_value_distribution("delimp_precursors", "stripped_seq") or []
            mcv.sort(key=lambda m: -m["n"])
            return [{"stripped_seq": m["value"], "n_obs": m["n"], "n_runs": None,
                     "n_searches": None, "n_charges": None, "has_im": None, "approximate": True}
                    for m in mcv[:int(limit)]]
    return SLOW_CACHE.get_or_set(f"top_pep_{limit}", _p)


def peptide_flyability(stripped_seq: str) -> dict[str, Any] | None:
    """Precomputed Koina PFly flyability for one peptide (0 = poor flyer, 1 = strong flyer)
    plus the 4 class probabilities. Returns None if not yet scored."""
    try:
        rows = query(
            "SELECT stripped_seq, flyability, c1, c2, c3, c4, n_obs, mean_log2_intensity, model "
            "FROM delimp_peptide_flyability WHERE stripped_seq = %s",
            (stripped_seq.strip().upper(),), tables=["delimp_peptide_flyability"])
    except Exception:  # noqa: BLE001 - table not built yet
        return None
    return rows[0] if rows else None


def flyability_scatter(sample_n: int = 8000) -> list[dict[str, Any]]:
    """Predicted flyability vs observed mean intensity, one point per peptide — the
    Highlights scatter. The table is small (~tens of thousands of rows) so a direct read
    is fast; cached. Empty list until flyability_ingest.py has run."""
    def _p():
        try:
            return query(
                "SELECT stripped_seq, flyability, mean_log2_intensity, n_obs "
                "FROM delimp_peptide_flyability "
                "WHERE flyability IS NOT NULL AND mean_log2_intensity IS NOT NULL "
                "ORDER BY n_obs DESC LIMIT %s",
                (int(sample_n),), tables=["delimp_peptide_flyability"])
        except Exception:  # noqa: BLE001
            return []
    return SLOW_CACHE.get_or_set(f"fly_scatter_{sample_n}", _p)


def top_proteins(limit: int = 40) -> list[dict[str, Any]]:
    def _p():
        try:
            return query(
                "SELECT protein_group, gene, n_searches, n_runs, sum_precursors, any_contaminant "
                "FROM delimp_mv_top_proteins ORDER BY n_runs DESC, sum_precursors DESC NULLS LAST LIMIT %s",
                (int(limit),), tables=["delimp_mv_top_proteins"],
            )
        except Exception:  # noqa: BLE001 - mv not built -> instant pg_stats approximation
            mcv = estimate_value_distribution("delimp_proteins", "protein_group") or []
            mcv.sort(key=lambda m: -m["n"])
            return [{"protein_group": m["value"], "gene": None, "n_searches": None,
                     "n_runs": None, "sum_precursors": m["n"], "any_contaminant": None,
                     "approximate": True} for m in mcv[:int(limit)]]
    return SLOW_CACHE.get_or_set(f"top_prot_{limit}", _p)


def top_genes(limit: int = 40) -> list[dict[str, Any]]:
    def _p():
        try:
            return query(
                "SELECT gene, n_groups, n_searches, n_runs FROM delimp_mv_top_genes "
                "ORDER BY n_runs DESC, n_searches DESC LIMIT %s",
                (int(limit),), tables=["delimp_mv_top_genes"],
            )
        except Exception:  # noqa: BLE001 - mv not built -> instant pg_stats approximation
            mcv = estimate_value_distribution("delimp_proteins", "gene") or []
            mcv = [m for m in mcv if m["value"]]
            mcv.sort(key=lambda m: -m["n"])
            return [{"gene": m["value"], "n_groups": None, "n_searches": None,
                     "n_runs": m["n"], "approximate": True} for m in mcv[:int(limit)]]
    return SLOW_CACHE.get_or_set(f"top_gene_{limit}", _p)


def word_leaderboard(scan_n: int = 30000) -> list[dict[str, Any]]:
    """Fun: English words / names / spicy words hidden in the corpus peptides.
    Scans the most-observed `scan_n` distinct peptides for AA-spellable words."""
    def _p():
        from . import wordhunt
        try:  # scan the precomputed top peptides (mv) instead of a live full GROUP BY
            peps = query(
                "SELECT stripped_seq, n_obs FROM delimp_mv_top_peptides ORDER BY n_obs DESC LIMIT %s",
                (int(scan_n),), tables=["delimp_mv_top_peptides"],
            )
        except Exception:  # noqa: BLE001 - mv not built -> scan the pg_stats MCV peptides (instant)
            mcv = estimate_value_distribution("delimp_precursors", "stripped_seq") or []
            peps = [{"stripped_seq": m["value"], "n_obs": m["n"]} for m in mcv]
        return wordhunt.scan(peps)
    return SLOW_CACHE.get_or_set(f"wordhunt_{scan_n}", _p)


def protein_coverage_peptides(protein_group: str, limit: int = 4000) -> dict[str, Any]:
    """Candidate observed peptides for a protein group (for the sequence coverage
    map): distinct stripped sequences seen in the runs where this PG was reported.
    Substring-mapping onto the fetched sequence (in coverage.py) keeps only the
    ones that actually belong to the protein."""
    pg = (protein_group or "").strip()
    gene = query(
        "SELECT MAX(gene) AS gene FROM delimp_proteins WHERE protein_group = %s",
        (pg,), tables=["delimp_proteins"], fetch="val",
    )
    peps = query(
        """
        WITH pg_runs AS (
            SELECT DISTINCT search_id, raw_path
            FROM delimp_proteins WHERE protein_group = %s
        )
        SELECT pr.stripped_seq,
               COUNT(*)                    AS n_precursors,
               COUNT(DISTINCT pr.charge)   AS n_charges,
               COUNT(DISTINCT pr.raw_path) AS n_runs,
               MIN(pr.q_value)             AS best_q_value,
               bool_or(pr.im IS NOT NULL)  AS has_im
        FROM delimp_precursors pr
        JOIN pg_runs r ON r.search_id = pr.search_id AND r.raw_path = pr.raw_path
        GROUP BY pr.stripped_seq
        ORDER BY n_precursors DESC
        LIMIT %s
        """,
        (pg, min(int(limit), 8000)),
        tables=["delimp_proteins", "delimp_precursors"],
    )
    return {"gene": gene, "peptides": peps}


def peptide_detail(stripped_seq: str) -> dict[str, Any]:
    seq = (stripped_seq or "").strip().upper()
    summary = query(
        """
        SELECT stripped_seq,
               COUNT(*) AS n_precursors,
               COUNT(DISTINCT modified_seq_proforma) AS n_modforms,
               COUNT(DISTINCT charge) AS n_charges,
               COUNT(DISTINCT raw_path) AS n_runs,
               COUNT(DISTINCT search_id) AS n_searches,
               MIN(q_value) AS best_q_value,
               AVG(rt) AS avg_rt,
               AVG(im) AS avg_im,
               bool_or(im IS NOT NULL) AS has_im,
               MAX(n_engines_confirming) AS max_engines
        FROM delimp_precursors
        WHERE stripped_seq = %s
        GROUP BY stripped_seq
        """,
        (seq,),
        tables=["delimp_precursors"],
        fetch="one",
    )
    # One row per (modified form, charge) with aggregate coordinates.
    forms = query(
        """
        SELECT modified_seq_proforma, charge,
               COUNT(*) AS n_obs,
               AVG(precursor_mz) AS avg_mz,
               AVG(rt) AS avg_rt,
               AVG(im) AS avg_im,
               MIN(q_value) AS best_q_value,
               AVG(intensity_log2) AS avg_log2_int,
               MAX(n_engines_confirming) AS max_engines
        FROM delimp_precursors
        WHERE stripped_seq = %s
        GROUP BY modified_seq_proforma, charge
        ORDER BY n_obs DESC
        LIMIT %s
        """,
        (seq, MAX_PAGE),
        tables=["delimp_precursors"],
    )
    # Individual observations across runs (bounded).
    observations = query(
        """
        SELECT pr.search_id, s.search_name, s.search_engine,
               pr.raw_path, pr.modified_seq_proforma, pr.charge,
               pr.precursor_mz, pr.rt, pr.im, pr.q_value,
               pr.intensity, pr.n_engines_confirming
        FROM delimp_precursors pr
        JOIN delimp_searches s ON s.id = pr.search_id
        WHERE pr.stripped_seq = %s
        ORDER BY pr.intensity DESC NULLS LAST
        LIMIT %s
        """,
        (seq, MAX_PAGE),
        tables=["delimp_precursors", "delimp_searches"],
    )
    # Cross-engine consensus, if recorded.
    consensus = query(
        """
        SELECT modified_seq_proforma, charge, engines, n_engines, best_q_value, raw_path
        FROM delimp_consensus_ids
        WHERE stripped_seq = %s
        ORDER BY n_engines DESC
        LIMIT %s
        """,
        (seq, MAX_PAGE),
        tables=["delimp_consensus_ids"],
    )
    return {
        "summary": summary,
        "forms": forms,
        "observations": observations,
        "consensus": consensus,
    }


def peptide_search_library(stripped_seq: str, charge: int | None = None) -> dict[str, Any] | None:
    """The search's OWN predicted/library fragment intensities (from the ingested DIA-NN
    report-lib) for the predicted-spectrum panel — labeled with the engine + version that
    produced them. Returns None until XIC/library is ingested."""
    import json as _json
    seq = (stripped_seq or "").strip().upper()
    try:
        rows = query(
            "SELECT charge, engine, engine_version, ms1_apex, fragments FROM delimp_precursor_xic WHERE stripped_seq = %s",
            (seq,), tables=["delimp_precursor_xic"],
        )
    except Exception:  # noqa: BLE001 - not ingested yet
        return None
    if not rows:
        return None

    def _j(v):
        return _json.loads(v) if isinstance(v, str) else v

    cand = [r for r in rows if charge and r["charge"] == charge] or rows
    row = max(cand, key=lambda r: r.get("ms1_apex") or 0)
    peaks = [{"ion": (f.get("type", "") + str(f.get("series", ""))), "label": f.get("label"),
              "mz": f.get("mz"), "rel_intensity": f.get("rel_intensity")}
             for f in (_j(row["fragments"]) or []) if f.get("mz") and f.get("rel_intensity")]
    peaks.sort(key=lambda p: -(p["rel_intensity"] or 0))
    return {"engine": row.get("engine"), "version": row.get("engine_version"),
            "charge": row["charge"], "peaks": peaks}


def peptide_interference(stripped_seq: str, mz_tol: float = 0.01,
                         rt_window: float = 0.5, max_partners: int = 40) -> dict[str, Any]:
    """Shared-transition / interference signal: for this peptide's quant fragments, find
    OTHER peptides in the corpus whose fragments share the same m/z (±tol). Ones that also
    co-elute (|ΔRT(apex)| ≤ window) are potential interference; RT-resolved ones share the
    transition but are separated in time. A novel DIA specificity/QC datapoint."""
    import json as _json

    seq = (stripped_seq or "").strip().upper()
    try:
        meas = query(
            "SELECT precursor_id, charge, rt_apex, fragments FROM delimp_precursor_xic WHERE stripped_seq = %s",
            (seq,), tables=["delimp_precursor_xic"],
        )
    except Exception:  # noqa: BLE001 - no XIC ingested yet
        return {"available": False}
    if not meas:
        return {"available": False}

    def _j(v):
        return _json.loads(v) if isinstance(v, str) else v

    # this peptide's quant transitions (label, mz, apex RT, charge)
    targets = []
    for m in meas:
        rt0 = m["rt_apex"]
        for f in _j(m["fragments"]) or []:
            if (f.get("exclude_from_quant", 0) == 0) and f.get("mz"):
                targets.append((f["label"], float(f["mz"]), rt0, m["charge"]))
    if not targets:
        return {"available": True, "stripped_seq": seq, "transitions": [], "partners": []}

    def _producer():
        transitions, partners = [], {}
        for label, mz, rt0, ch in targets[:12]:
            rows = query(
                """SELECT x.stripped_seq, x.charge, x.rt_apex
                   FROM delimp_precursor_xic x, jsonb_array_elements(x.fragments) f
                   WHERE x.stripped_seq <> %s AND (f->>'mz')::float BETWEEN %s AND %s
                   LIMIT 3000""",
                (seq, mz - mz_tol, mz + mz_tol), tables=["delimp_precursor_xic"],
            )
            seen, co = set(), 0
            for r in rows:
                p = r["stripped_seq"]
                if p in seen:
                    continue
                seen.add(p)
                dRT = (abs((r["rt_apex"] or 0) - (rt0 or 0))
                       if (rt0 is not None and r["rt_apex"] is not None) else None)
                coel = dRT is not None and dRT <= rt_window
                if coel:
                    co += 1
                pe = partners.setdefault(p, {"peptide": p, "shared": 0, "co_eluting": 0, "min_dRT": None})
                pe["shared"] += 1
                if coel:
                    pe["co_eluting"] += 1
                if dRT is not None:
                    pe["min_dRT"] = dRT if pe["min_dRT"] is None else min(pe["min_dRT"], dRT)
            transitions.append({"label": label, "mz": round(mz, 4), "charge": ch,
                                "n_sharing": len(seen), "n_co_eluting": co})
        for pe in partners.values():
            if pe["min_dRT"] is not None:
                pe["min_dRT"] = round(pe["min_dRT"], 3)
        plist = sorted(partners.values(), key=lambda x: (-x["co_eluting"], -x["shared"]))[:max_partners]
        return {"available": True, "stripped_seq": seq, "rt_window": rt_window, "mz_tol": mz_tol,
                "transitions": transitions, "partners": plist}

    return SLOW_CACHE.get_or_set(f"interf_{seq}", _producer)


def gene_detail(gene: str) -> dict[str, Any]:
    """Everything the corpus knows about a GENE: all its protein groups (each links to
    the protein page), how widely it's seen, which organisms/sample types, and which
    search pipelines found it (flagging proteogenomics / custom-FASTA searches)."""
    g = (gene or "").strip()
    proteins = query(
        """
        SELECT protein_group,
               COUNT(DISTINCT search_id) AS n_searches,
               COUNT(DISTINCT raw_path)  AS n_runs,
               SUM(n_precursors)         AS sum_precursors,
               SUM(n_unique_peptides)    AS sum_unique_peptides,
               bool_or(is_contaminant)   AS any_contaminant
        FROM delimp_proteins
        WHERE gene = %s
        GROUP BY protein_group
        ORDER BY sum_precursors DESC NULLS LAST
        LIMIT %s
        """,
        (g, MAX_PAGE), tables=["delimp_proteins"],
    )
    totals = query(
        """SELECT COUNT(DISTINCT protein_group) AS n_groups,
                  COUNT(DISTINCT search_id) AS n_searches,
                  COUNT(DISTINCT raw_path)  AS n_runs
           FROM delimp_proteins WHERE gene = %s""",
        (g,), tables=["delimp_proteins"], fetch="one",
    )
    organisms = query(
        """
        SELECT COALESCE(sm.organism_name, 'Unknown') AS organism, COUNT(DISTINCT p.raw_path) AS n_runs
        FROM delimp_proteins p
        JOIN delimp_sample_metadata sm ON sm.raw_path = p.raw_path
        WHERE p.gene = %s
        GROUP BY sm.organism_name ORDER BY n_runs DESC LIMIT 12
        """,
        (g,), tables=["delimp_proteins", "delimp_sample_metadata"],
    )
    # which search pipelines detected this gene — proteogenomics/custom-FASTA flagged
    pipelines = query(
        """
        SELECT s.pipeline_id, s.search_engine, s.fasta_path,
               COUNT(DISTINCT s.id) AS n_searches
        FROM delimp_proteins p
        JOIN delimp_searches s ON s.id = p.search_id
        WHERE p.gene = %s
        GROUP BY s.pipeline_id, s.search_engine, s.fasta_path
        ORDER BY n_searches DESC LIMIT 20
        """,
        (g,), tables=["delimp_proteins", "delimp_searches"],
    )
    for p in pipelines:
        fp = (p.get("fasta_path") or "").lower()
        pid = (p.get("pipeline_id") or "").lower()
        p["proteogenomics"] = any(k in fp or k in pid for k in
                                  ("proteogenom", "rnaseq", "rna-seq", "transcript", "custom", "novel", "denovo", "de-novo"))
    return {"gene": g, "proteins": proteins, "totals": totals,
            "organisms": organisms, "pipelines": pipelines}


def peptide_xic(stripped_seq: str, top_n: int = 6) -> dict[str, Any]:
    """Dual-pane XIC for a peptide — PER PRECURSOR (each charge state is its own
    precursor with its own chromatogram). Returns one entry per precursor (MS1 +
    top-N quant fragments with usage %), plus the corpus charge-state distribution
    ('do we see the +2 or +3 more?'). Traces are the apex-aligned average of real
    DIA-NN --xic runs. {available: False} until XIC is ingested."""
    import json as _json

    seq = (stripped_seq or "").strip().upper()
    try:
        meas = query(
            """SELECT precursor_id, charge, raw_path, rt_apex, ms1_apex, ms1, fragments
               FROM delimp_precursor_xic WHERE stripped_seq = %s ORDER BY charge""",
            (seq,), tables=["delimp_precursor_xic"],
        )
    except Exception:  # noqa: BLE001 - table not created until first XIC ingest
        return {"available": False}
    if not meas:
        return {"available": False}
    try:
        qrows = query(
            """SELECT search_id, precursor_id, quant_labels
               FROM delimp_xic_quant WHERE stripped_seq = %s""",
            (seq,), tables=["delimp_xic_quant"],
        )
    except Exception:  # noqa: BLE001
        qrows = []

    def _j(v):
        return _json.loads(v) if isinstance(v, str) else v

    # per-precursor quant usage: precursor_id -> {label: set(search_id)}
    usage_by_pid: dict[str, dict[str, set]] = {}
    searches_by_pid: dict[str, set] = {}
    for r in qrows:
        pid = r["precursor_id"]
        searches_by_pid.setdefault(pid, set()).add(r["search_id"])
        d = usage_by_pid.setdefault(pid, {})
        for lab in _j(r["quant_labels"]) or []:
            d.setdefault(lab, set()).add(r["search_id"])

    # corpus charge-state distribution (how often each charge is observed) — honest,
    # from all precursor observations, not just XIC-bearing ones.
    charge_dist = []
    try:
        cd = query(
            "SELECT charge, COUNT(*) AS n FROM delimp_precursors WHERE stripped_seq = %s GROUP BY charge",
            (seq,), tables=["delimp_precursors"],
        )
        tot = sum(c["n"] for c in cd) or 1
        charge_dist = sorted(({"charge": c["charge"], "n_obs": c["n"],
                               "pct": round(100 * c["n"] / tot)} for c in cd),
                             key=lambda x: -x["n_obs"])
    except Exception:  # noqa: BLE001
        pass

    precursors = []
    for m in meas:
        pid = m["precursor_id"]
        frags = _j(m["fragments"]) or []
        rep_frags = {f["label"]: f for f in frags}
        rel = {lab: (f.get("rel_intensity") or 0) for lab, f in rep_frags.items()}
        u = usage_by_pid.get(pid, {})
        ns = max(len(searches_by_pid.get(pid, set())), 1)
        usage_list = sorted(
            ({"label": lab, "n_searches": len(sids), "pct": round(100 * len(sids) / ns),
              "rel_intensity": round(rel.get(lab, 0), 4)} for lab, sids in u.items()),
            key=lambda x: (-x["n_searches"], -x["rel_intensity"], x["label"]),
        )
        if not usage_list:  # no quant overlay -> rank by library intensity
            usage_list = sorted(
                ({"label": lab, "n_searches": 0, "pct": 0, "rel_intensity": round(ri, 4)}
                 for lab, ri in rel.items()), key=lambda x: -x["rel_intensity"])
        top = [x["label"] for x in usage_list[:top_n]]
        bottom = [{"label": lab, "ion": rep_frags[lab].get("type", "") + str(rep_frags[lab].get("series", "")),
                   "mz": rep_frags[lab].get("mz"), "charge": rep_frags[lab].get("charge"),
                   "rel_intensity": rep_frags[lab].get("rel_intensity"),
                   "trace": rep_frags[lab].get("trace") or []}
                  for lab in top if lab in rep_frags]
        has_real = any(b["trace"] for b in bottom)
        precursors.append({
            "precursor_id": pid, "charge": m["charge"], "rt_apex": m["rt_apex"],
            "representative_run": m["raw_path"], "n_searches": len(searches_by_pid.get(pid, set())),
            "ms1": _j(m["ms1"]) or [], "fragments": bottom, "fragment_usage": usage_list,
            "has_real_trace": has_real})
    precursors.sort(key=lambda p: -(p.get("rt_apex") is not None), )  # stable; keep charge order from SQL
    return {"available": True, "stripped_seq": seq,
            "rt_axis": "RT − apex (min)",  # traces are apex-aligned averages
            "charge_distribution": charge_dist, "precursors": precursors}


def list_searches(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    lim, off = _page(limit, offset)
    rows = query(
        """
        SELECT id, search_name, search_engine, search_engine_version,
               pipeline_id, pipeline_version, status, sharing_status,
               n_raw_files, n_precursors_total, n_proteins_total,
               fasta_n_proteins, completed_at, submitted_at, ingested_at,
               delimp_version, doi, pride_accession
        FROM delimp_searches
        ORDER BY COALESCE(ingested_at, submitted_at) DESC
        LIMIT %s OFFSET %s
        """,
        (lim, off),
        tables=["delimp_searches"],
    )
    total = query(
        "SELECT COUNT(*) FROM delimp_searches",
        tables=["delimp_searches"],
        fetch="val",
    )
    return {"rows": rows, "total": total or 0, "limit": lim, "offset": off}


def search_detail(search_id: str) -> dict[str, Any]:
    sid = (search_id or "").strip()
    summary = query(
        """
        SELECT id, search_name, search_engine, search_engine_version,
               pipeline_id, pipeline_version, status, sharing_status,
               n_raw_files, n_precursors_total, n_proteins_total,
               fasta_path, fasta_n_proteins, contaminant_lib,
               completed_at, submitted_at, ingested_at, delimp_version,
               doi, pride_accession, citation
        FROM delimp_searches
        WHERE id = %s
        """,
        (sid,),
        tables=["delimp_searches"],
        fetch="one",
    )
    runs = query(
        """
        SELECT srf.raw_path, srf.n_precursors, srf.n_proteins,
               rf.raw_basename, rf.platform, rf.acquisition_method,
               rf.instrument_model, rf.gradient_minutes,
               sm.organism_name, sm.sample_type, sm.organism_taxon_id
        FROM search_raw_files srf
        JOIN raw_files rf ON rf.raw_path = srf.raw_path
        LEFT JOIN delimp_sample_metadata sm ON sm.raw_path = srf.raw_path
        WHERE srf.search_id = %s
        ORDER BY srf.n_precursors DESC NULLS LAST
        LIMIT %s
        """,
        (sid, MAX_PAGE),
        tables=["search_raw_files", "raw_files", "delimp_sample_metadata"],
    )
    return {"summary": summary, "runs": runs}
