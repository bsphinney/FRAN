"""FRAN PRIVATE provenance layer — records the FULL, non-sanitized record for every ingested
search: real search name, real raw-file names, every file location, and the parsed customer /
PI / project (from the path). This is the INTERNAL counterpart to FRAN's public/sanitized
browser layer, and the bridge for eventually linking each search to the **sample-submission
system** and **coreomics** (their join keys are customer/PI/project/submission-id).

PRIVATE: `delimp_search_provenance` is NOT in the app's public allowlist — the FRAN browser
never reads it. It exists for internal customer-data tracking + LIMS linkage only.

Core facility path convention (the customer is encoded in the path):
  .../Data/lab/service/off_campus/<Institution>/<PI>/<Project>/...   -> external client
  .../Data/lab/service/on_campus/<PI-lab>/<Project>/...              -> UC Davis internal lab
  .../Data/lab/<anything-else>/...                                   -> core-facility internal
"""
from __future__ import annotations

import json
import os

DDL = """
CREATE TABLE IF NOT EXISTS delimp_search_provenance (
    search_id            UUID PRIMARY KEY,
    real_search_name     TEXT,          -- the un-sanitized name (public layer redacts this)
    output_dir           TEXT,          -- full path / idempotency key
    report_path          TEXT,
    scope                TEXT,          -- customer | internal | unknown
    campus               TEXT,          -- on_campus | off_campus | NULL
    client               TEXT,          -- institution (off) or 'UC Davis' (on) or core
    pi                    TEXT,          -- PI / lab folder
    project              TEXT,          -- project folder
    raw_files_json       JSONB,         -- [{name, path}, ...] EVERY raw file, real names+locations
    n_raw_files          INTEGER,
    -- future LIMS linkage (populated later) ---------------------------------
    coreomics_submission_id   TEXT,
    sample_submission_id      TEXT,
    customer_contact          TEXT,
    linkage_status            TEXT DEFAULT 'unlinked',  -- unlinked | matched | manual | no-match
    notes                TEXT,
    recorded_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_prov_client  ON delimp_search_provenance (client);
CREATE INDEX IF NOT EXISTS idx_prov_pi      ON delimp_search_provenance (pi);
CREATE INDEX IF NOT EXISTS idx_prov_linkage ON delimp_search_provenance (linkage_status);
"""


def provenance_from_path(path: str) -> dict:
    """Parse customer / PI / project from a core-facility file path. Never guesses — pure path
    structure (the core's own service/on_campus/off_campus convention)."""
    out = {"scope": "unknown", "campus": None, "client": None, "pi": None, "project": None}
    if not path:
        return out
    segs = [s for s in path.replace("\\", "/").split("/") if s]
    low = [s.lower() for s in segs]
    if "service" in low:
        rest = segs[low.index("service") + 1:]
        out["scope"] = "customer"
        if rest and rest[0].lower() in ("on_campus", "off_campus"):
            out["campus"] = rest[0].lower()
            rest = rest[1:]
        if out["campus"] == "on_campus":          # UC Davis internal labs: <PI-lab>/<project>
            out["client"] = "UC Davis"
            if rest:
                out["pi"] = rest[0]
            if len(rest) > 1:
                out["project"] = rest[1]
        else:                                      # off-campus: <Institution>/<PI>/<project>
            if rest:
                out["client"] = rest[0]
            if len(rest) > 1:
                out["pi"] = rest[1]
            if len(rest) > 2:
                out["project"] = rest[2]
    elif "lab" in low:                             # core-facility internal (not a customer job)
        rest = segs[low.index("lab") + 1:]
        out["scope"] = "internal"
        out["client"] = "UC Davis Proteomics Core"
        if rest:
            out["project"] = rest[0]
    return out


def search_name_from_raw_files(raw_names: list[str]) -> str | None:
    """Derive a provenance-faithful name from the raw FILE names (the lab/sample), not the staging
    folder. e.g. ['Ex090223_Mucke_fDiaW22_30m_1', ...] -> 'Ex090223_Mucke_fDiaW22_30m'. The folder
    can lie (HUPO_2023 held Mucke/Gladstone rat data); the raw filenames carry the real origin."""
    names = [os.path.splitext(os.path.basename(n.replace("\\", "/")))[0] for n in raw_names if n]
    if not names:
        return None
    # longest common prefix, then trim a trailing run index / separator
    pre = os.path.commonprefix(names)
    pre = pre.rstrip("_-. 0123456789")
    return pre or names[0]


def record_provenance(conn, search_id, real_search_name, output_dir, report_path,
                      raw_files: list[dict], coreomics_submission_id=None):
    """Upsert one private provenance row. raw_files = [{'name':..., 'path':...}, ...] for EVERY
    raw file. Call this from corpus_ingest after a successful commit. Idempotent by search_id."""
    p = provenance_from_path(output_dir or report_path or "")
    cur = conn.cursor()
    cur.execute(DDL)
    cur.execute("""
        INSERT INTO delimp_search_provenance
          (search_id, real_search_name, output_dir, report_path, scope, campus, client, pi,
           project, raw_files_json, n_raw_files, coreomics_submission_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (search_id) DO UPDATE SET
          real_search_name=EXCLUDED.real_search_name, output_dir=EXCLUDED.output_dir,
          report_path=EXCLUDED.report_path, scope=EXCLUDED.scope, campus=EXCLUDED.campus,
          client=EXCLUDED.client, pi=EXCLUDED.pi, project=EXCLUDED.project,
          raw_files_json=EXCLUDED.raw_files_json, n_raw_files=EXCLUDED.n_raw_files,
          coreomics_submission_id=COALESCE(EXCLUDED.coreomics_submission_id, delimp_search_provenance.coreomics_submission_id),
          updated_at=CURRENT_TIMESTAMP
    """, (str(search_id), real_search_name, output_dir, report_path, p["scope"], p["campus"],
          p["client"], p["pi"], p["project"], json.dumps(raw_files), len(raw_files),
          coreomics_submission_id))
    conn.commit()
    return p


def backfill(conn):
    """Record provenance for EVERY existing search (run once after the table is created, or after
    a bulk import). Reads delimp_searches + its raw_files. Idempotent by search_id."""
    cur = conn.cursor()
    cur.execute(DDL); conn.commit()
    cur.execute("SELECT id, search_name, output_dir FROM delimp_searches")
    searches = cur.fetchall()
    done = 0
    for sid, name, odir in searches:
        cur.execute("""SELECT rf.raw_basename, rf.raw_path FROM search_raw_files srf
                       JOIN raw_files rf ON rf.raw_path = srf.raw_path WHERE srf.search_id=%s""", (sid,))
        raws = [{"name": bn, "path": rp} for bn, rp in cur.fetchall()]
        record_provenance(conn, sid, name, odir, odir, raws)
        done += 1
    print(f"backfilled provenance for {done} searches")
    cur.execute("SELECT scope, COUNT(*) FROM delimp_search_provenance GROUP BY 1 ORDER BY 2 DESC")
    print("by scope:", dict(cur.fetchall()))
    cur.execute("SELECT client, COUNT(*) FROM delimp_search_provenance WHERE client IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 12")
    print("by client:", dict(cur.fetchall()))


if __name__ == "__main__":
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    import psycopg2
    from refresh_leaderboards import _token
    con = psycopg2.connect(host=_os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
        dbname=_os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
        user=_os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30)
    if "--backfill" in _sys.argv:
        backfill(con)
    else:
        con.cursor().execute(DDL); con.commit(); print("delimp_search_provenance table ensured")
    con.close()
