"""xic_trace_lance.py — registry for the XIC-TRACE lane (fixed-shape extracted chromatogram tensors).

Three lanes, three registries, easily confused — so, plainly:

  delimp_spectrum_lane    observed MS2 fragments per precursor       (spectrum_lance.py)
  delimp_xic_lane         Spectronaut's OWN exported chromatograms   (xic_lance.py)
  delimp_xic_trace_lane   chromatograms WE extract from the .d       (this file)

Only this lane's contents depend on our extraction code, which is why it is the one that must record
an extractor version and the extraction parameters. `CHANGELOG_xic_extractor.md` records a live
defect: `cycle_rt[c] = rt[c * frames_per_cycle]` stamps every event in a cycle with the first (MS1)
frame's time, so MS2 apexes land ~0.263 s early. Shape is unaffected; absolute fragment RT is not.
"Any lane built before this fix carries the shift" is only actionable if each dataset says which
extractor built it — otherwise the only remedy is rebuilding everything.

The tolerances matter for the same reason. IM_TOL went 0.05 -> 0.030 in v1.3.0 and the channel layout
went [9,32] -> [11,32] in v1.2.0, so two datasets can hold different-shaped tensors extracted under
different windows and look identical in the registry. Record the parameters next to the data.
"""
import hashlib
import os

import pyarrow as pa

REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS delimp_xic_trace_lane (
    id             BIGSERIAL PRIMARY KEY,
    lance_path     TEXT UNIQUE NOT NULL,
    run            TEXT,
    n_precursors   INTEGER,
    content_md5    TEXT,
    lance_version  BIGINT,
    ingested_at    TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_xic_trace_lane_run ON delimp_xic_trace_lane (run);
-- Provenance of the numbers, not just of the rows. NULL = written before this was tracked, i.e.
-- the pilot, i.e. assume the pre-fix MS2 time shift.
ALTER TABLE delimp_xic_trace_lane ADD COLUMN IF NOT EXISTS writer_version    TEXT;
ALTER TABLE delimp_xic_trace_lane ADD COLUMN IF NOT EXISTS extractor_version TEXT;
ALTER TABLE delimp_xic_trace_lane ADD COLUMN IF NOT EXISTS n_channels        INTEGER;
ALTER TABLE delimp_xic_trace_lane ADD COLUMN IF NOT EXISTS n_cycles          INTEGER;
ALTER TABLE delimp_xic_trace_lane ADD COLUMN IF NOT EXISTS extract_params    JSONB;
ALTER TABLE delimp_xic_trace_lane ADD COLUMN IF NOT EXISTS updated_at        TIMESTAMPTZ DEFAULT now();
"""


def content_md5(table: pa.Table) -> str:
    """See spectrum_lance.content_md5 — combine_chunks() is required, or a dataset large enough to
    read back multi-chunk reports a false mismatch."""
    table = table.combine_chunks()
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as w:
        w.write_table(table)
    return hashlib.md5(sink.getvalue().to_pybytes()).hexdigest()


def ensure_registry(conn):
    cur = conn.cursor()
    for stmt in filter(str.strip, REGISTRY_DDL.split(";")):
        cur.execute(stmt)
    conn.commit()


def extract_params():
    """The extractor tunables actually in force, read from the extractor rather than restated here —
    a copy would drift, and a drifted provenance record is worse than none."""
    try:
        import xic_extractor as X
        return {
            "ppm": X.PPM, "im_tol": X.IM_TOL, "rt_half_s": X.RT_HALF,
            "n_points": X.N_POINTS, "n_ms1_channels": X.N_MS1_CHANNELS,
            "n_frag_channels": X.N_FRAG_CHANNELS,
        }
    except Exception:
        return None


def register(conn, run, lance_path, n_prec, md5, version,
             n_channels=None, n_cycles=None, extractor_version=None, params=None):
    import json

    from versions import XIC_TRACE_LANE_WRITER_VERSION, xic_extractor_version
    ev = extractor_version or xic_extractor_version()
    pj = json.dumps(params if params is not None else (extract_params() or {}))
    cur = conn.cursor()
    cur.execute("""INSERT INTO delimp_xic_trace_lane
                     (lance_path, run, n_precursors, content_md5, lance_version,
                      writer_version, extractor_version, n_channels, n_cycles, extract_params, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb, now())
                   ON CONFLICT (lance_path) DO UPDATE SET
                     run=EXCLUDED.run, n_precursors=EXCLUDED.n_precursors,
                     content_md5=EXCLUDED.content_md5, lance_version=EXCLUDED.lance_version,
                     writer_version=EXCLUDED.writer_version,
                     extractor_version=EXCLUDED.extractor_version,
                     n_channels=EXCLUDED.n_channels, n_cycles=EXCLUDED.n_cycles,
                     extract_params=EXCLUDED.extract_params, updated_at=now()""",
                (lance_path, run, int(n_prec), md5, int(version),
                 XIC_TRACE_LANE_WRITER_VERSION, ev, n_channels, n_cycles, pj))
    conn.commit()


def verify(lance_path, expected_md5) -> bool:
    """True iff the dataset on disk still hashes to what the registry recorded."""
    import lance
    if not expected_md5 or not os.path.exists(lance_path):
        return False
    return content_md5(lance.dataset(lance_path).to_table()) == expected_md5
