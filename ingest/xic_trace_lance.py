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
-- AGREEMENT WITH AN INDEPENDENT ENGINE, per run. Validation across 9 Thermo acquisitions found
-- seven clean (MS2 r 0.93-0.97, area ~1.00) and two that disagree for reasons still unknown --
-- one with MS1 r 0.24 / area 2.79x, one with MS2 area 1.88x. Nothing in the traces themselves
-- reveals which kind a dataset is, so the score is stored beside them: a consumer filters on it
-- rather than assuming the extractor is uniformly good. NULL means never scored, which is NOT the
-- same as scored-and-fine.
ALTER TABLE delimp_xic_trace_lane ADD COLUMN IF NOT EXISTS agree_ms2_r       DOUBLE PRECISION;
ALTER TABLE delimp_xic_trace_lane ADD COLUMN IF NOT EXISTS agree_ms2_area    DOUBLE PRECISION;
ALTER TABLE delimp_xic_trace_lane ADD COLUMN IF NOT EXISTS agree_ms1_r       DOUBLE PRECISION;
ALTER TABLE delimp_xic_trace_lane ADD COLUMN IF NOT EXISTS agree_ms1_area    DOUBLE PRECISION;
ALTER TABLE delimp_xic_trace_lane ADD COLUMN IF NOT EXISTS agree_n_traces    INTEGER;
ALTER TABLE delimp_xic_trace_lane ADD COLUMN IF NOT EXISTS agree_reference   TEXT;
ALTER TABLE delimp_xic_trace_lane ADD COLUMN IF NOT EXISTS agree_verdict     TEXT;
ALTER TABLE delimp_xic_trace_lane ADD COLUMN IF NOT EXISTS agree_scored_at   TIMESTAMPTZ;
-- SECOND-TIER SCORE, available without re-searching anything. Generating DIA-NN chromatograms for
-- every acquisition is not realistic at corpus scale, but 4,111 of 4,199 Orbitrap runs (98%) are
-- already in the spectrum lane, which carries the SEARCH ENGINE's own per-precursor reported RT and
-- per-fragment integrated area. That is a real independent reference for magnitude and timing --
-- weaker than trace-vs-trace because it compares scalars rather than shapes, but available
-- everywhere and free. Both observed failures were magnitude errors (MS1 2.79x, MS2 1.88x), which
-- is exactly what this catches.
ALTER TABLE delimp_xic_trace_lane ADD COLUMN IF NOT EXISTS selfref_area_ratio  DOUBLE PRECISION;
ALTER TABLE delimp_xic_trace_lane ADD COLUMN IF NOT EXISTS selfref_area_cv     DOUBLE PRECISION;
ALTER TABLE delimp_xic_trace_lane ADD COLUMN IF NOT EXISTS selfref_rt_delta_s  DOUBLE PRECISION;
ALTER TABLE delimp_xic_trace_lane ADD COLUMN IF NOT EXISTS selfref_n           INTEGER;
-- MS1 and MS2 are scored SEPARATELY. Two of the three trace-level failures found in validation were
-- MS1-only, with MS2 clean beside them. A single pooled CV would have averaged the broken axis
-- against the healthy one and hidden the very thing this gate exists to catch. MS2 compares against
-- frg_peak_area per fragment, MS1 against the per-isotope ms1_iso_measured intensities.
ALTER TABLE delimp_xic_trace_lane ADD COLUMN IF NOT EXISTS selfref_ms2_cv      DOUBLE PRECISION;
ALTER TABLE delimp_xic_trace_lane ADD COLUMN IF NOT EXISTS selfref_ms1_cv      DOUBLE PRECISION;
-- The MEDIAN RATIO per axis, stored because the CV provably cannot replace it. Calibration run
-- FL010424_He50ng-DiaW22_90m has a trace-level verdict of fail:ms2_area=1.91 -- a systematic 1.91x
-- magnitude error -- yet scores ms2_cv 0.078 and passes the scatter gate, because a CONSTANT scale
-- factor leaves consistency untouched. Scatter and offset are different defects, and only the ratio
-- sees the second. Its absolute scale is arbitrary (our window vs the engine's integration bounds),
-- so the threshold cannot be set a priori -- it has to come from the corpus-wide distribution.
ALTER TABLE delimp_xic_trace_lane ADD COLUMN IF NOT EXISTS selfref_ms2_ratio   DOUBLE PRECISION;
ALTER TABLE delimp_xic_trace_lane ADD COLUMN IF NOT EXISTS selfref_ms1_ratio   DOUBLE PRECISION;
ALTER TABLE delimp_xic_trace_lane ADD COLUMN IF NOT EXISTS selfref_ms2_n       INTEGER;
ALTER TABLE delimp_xic_trace_lane ADD COLUMN IF NOT EXISTS selfref_ms1_n       INTEGER;
ALTER TABLE delimp_xic_trace_lane ADD COLUMN IF NOT EXISTS selfref_verdict     TEXT;
ALTER TABLE delimp_xic_trace_lane ADD COLUMN IF NOT EXISTS selfref_scored_at   TIMESTAMPTZ;
"""

# Self-reference gate. The RATIO may legitimately sit anywhere -- our window and the engine's
# integration bounds differ -- so what is actually diagnostic is CONSISTENCY: within a healthy run
# our area tracks the engine's with modest scatter. A run whose ratio is wildly off, or whose
# scatter is large, is not measuring the same thing.
SELFREF_MAX_CV = 1.5          # coefficient of variation of (our area / engine area)
SELFREF_MAX_RT_DELTA_S = 6.0  # median |our apex - engine reported RT|

# A run passes only if BOTH channel groups agree on BOTH shape and magnitude. The two failures found
# in validation each broke exactly one of these while looking normal on the others, so an AND is the
# point: any single-metric gate would have passed one of them.
AGREE_MIN_R = 0.80
AGREE_AREA_LO, AGREE_AREA_HI = 0.70, 1.40


def selfref_verdict(ms2_cv, ms1_cv, rt_delta_s, n):
    """Verdict from the engine's own reported values, for runs with no DIA-NN chromatograms.

    Reported as a SEPARATE tier, never as 'pass': it compares integrated scalars, not shapes, so a
    run that passes here has not been shown to produce correct chromatograms -- only correct
    magnitudes and timing. Calling that the same thing as a trace-level pass would be the kind of
    quiet overclaim this lane exists to prevent.

    MS1 and MS2 are gated independently and the failing axis is named, because the axes fail
    independently in practice."""
    if not n or n < 50:
        return "unscored"
    bad = []
    for nm, v in (("ms2_cv", ms2_cv), ("ms1_cv", ms1_cv)):
        if v is not None and v > SELFREF_MAX_CV:
            bad.append(f"{nm}={v:.2f}")
    if rt_delta_s is not None and rt_delta_s > SELFREF_MAX_RT_DELTA_S:
        bad.append(f"rt_delta={rt_delta_s:.1f}s")
    return "selfref_ok" if not bad else "fail_selfref:" + ",".join(bad)


def agreement_verdict(ms2_r, ms2_area, ms1_r, ms1_area):
    """'pass' | 'fail:<reason>' | 'unscored'. Reasons are recorded so a failing run can be
    investigated rather than merely dropped."""
    if ms2_r is None and ms1_r is None:
        return "unscored"
    bad = []
    if ms2_r is not None and ms2_r < AGREE_MIN_R:
        bad.append(f"ms2_r={ms2_r:.2f}")
    if ms1_r is not None and ms1_r < AGREE_MIN_R:
        bad.append(f"ms1_r={ms1_r:.2f}")
    for nm, v in (("ms2_area", ms2_area), ("ms1_area", ms1_area)):
        if v is not None and not (AGREE_AREA_LO <= v <= AGREE_AREA_HI):
            bad.append(f"{nm}={v:.2f}")
    return "pass" if not bad else "fail:" + ",".join(bad)


def record_selfref(conn, lance_path, area_ratio=None, ms2_cv=None, ms1_cv=None,
                   rt_delta_s=None, n=None, ms2_n=None, ms1_n=None,
                   ms2_ratio=None, ms1_ratio=None, promote=False):
    """Store the engine-reported-value comparison. selfref_verdict is ALWAYS written to its own
    column, so the scalar result stays inspectable even on runs that already carry a trace-level
    verdict; that overlap is what lets the gate be calibrated against trace-level truth.

    promote=False BY DEFAULT, and it must stay that way until the gate is calibrated. The current
    verdict is scatter-only, and scatter demonstrably misses systematic magnitude errors: calibration
    run FL010424_He50ng-DiaW22_90m is a known trace-level fail (ms2_area=1.91) that scores
    selfref_ok. Writing that into agree_verdict would mark a run 'checked' on the strength of a test
    known to miss its defect, which is worse than leaving it NULL — NULL is honest about ignorance.
    Pass promote=True only once a ratio-based threshold derived from the corpus distribution has been
    shown to separate the known failures."""
    v = selfref_verdict(ms2_cv, ms1_cv, rt_delta_s, n)
    pooled = None
    if ms2_cv is not None or ms1_cv is not None:
        vals = [x for x in (ms2_cv, ms1_cv) if x is not None]
        pooled = max(vals)          # worst axis, never the average -- averaging hides one bad axis
    cur = conn.cursor()
    cur.execute("""UPDATE delimp_xic_trace_lane
                   SET selfref_area_ratio=%s, selfref_area_cv=%s, selfref_rt_delta_s=%s,
                       selfref_n=%s, selfref_ms2_cv=%s, selfref_ms1_cv=%s,
                       selfref_ms2_n=%s, selfref_ms1_n=%s,
                       selfref_ms2_ratio=%s, selfref_ms1_ratio=%s,
                       selfref_verdict=%s, selfref_scored_at=now(),
                       agree_verdict = CASE
                         WHEN %s AND (agree_verdict IS NULL OR agree_verdict = 'unscored')
                         THEN %s ELSE agree_verdict END,
                       agree_scored_at = CASE
                         WHEN %s THEN COALESCE(agree_scored_at, now()) ELSE agree_scored_at END
                   WHERE lance_path=%s""",
                (area_ratio, pooled, rt_delta_s, n, ms2_cv, ms1_cv, ms2_n, ms1_n,
                 ms2_ratio, ms1_ratio, v, bool(promote), v, bool(promote), lance_path))
    conn.commit()
    return v


def record_agreement(conn, lance_path, ms2_r=None, ms2_area=None, ms1_r=None, ms1_area=None,
                     n_traces=None, reference=None):
    """Store how well this dataset agrees with an independent engine. Separate from register()
    because writing traces and judging them are different acts: a dataset can be re-registered
    without being re-scored, and that must stay visible."""
    v = agreement_verdict(ms2_r, ms2_area, ms1_r, ms1_area)
    cur = conn.cursor()
    cur.execute("""UPDATE delimp_xic_trace_lane
                   SET agree_ms2_r=%s, agree_ms2_area=%s, agree_ms1_r=%s, agree_ms1_area=%s,
                       agree_n_traces=%s, agree_reference=%s, agree_verdict=%s, agree_scored_at=now()
                   WHERE lance_path=%s""",
                (ms2_r, ms2_area, ms1_r, ms1_area, n_traces, reference, v, lance_path))
    conn.commit()
    return v


def content_md5(table: pa.Table) -> str:
    """See spectrum_lance.content_md5 — combine_chunks() is required, or a dataset large enough to
    read back multi-chunk reports a false mismatch."""
    table = table.combine_chunks()
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as w:
        w.write_table(table)
    return hashlib.md5(sink.getvalue().to_pybytes()).hexdigest()


def _ddl_statements(ddl):
    """Split DDL into executable statements, ignoring '--' comments.

    Splitting on ';' alone is wrong once the DDL carries prose: a semicolon inside a comment ends the
    chunk early and leaves a fragment that is nothing but comment text. That fragment is truthy in
    Python but an empty query to Postgres, so ensure_registry raised
    'can't execute an empty query' and every writer died AFTER computing its result -- 141 runs
    scored and none recorded.

    ORDER MATTERS: comments are stripped FIRST, then the result is split. Splitting first and
    filtering after does not work -- a comment broken across a semicolon leaves its tail ('only the
    ratio') glued to the next ALTER, which is invalid SQL rather than an empty one.

    Assumes no '--' inside a string literal, which holds for this DDL (it is all ALTER/CREATE).
    """
    code = "\n".join(ln.split("--", 1)[0] for ln in ddl.splitlines())
    return [s for s in code.split(";") if s.strip()]


#: Last column added by REGISTRY_DDL. Its presence means the whole block has already been applied.
#: Keep this pointing at the LAST ALTER in the block -- a sentinel from the middle would let a
#: partially-applied schema look complete.
_SCHEMA_SENTINEL = "selfref_scored_at"


def ensure_registry(conn, force=False):
    """Apply the registry DDL, but only when it is actually needed.

    'ALTER TABLE ... ADD COLUMN IF NOT EXISTS' is a no-op logically and NOT a no-op in the lock
    manager: it still takes an AccessExclusiveLock on the table. With writers running concurrently
    (a 40-wide SLURM array), each one re-applying the schema deadlocks against the others' INSERTs
    -- 'Process A waits for RowExclusiveLock, blocked by B; B waits for AccessExclusiveLock, blocked
    by A'. That cost ~15% of tasks before this guard. Checking information_schema first takes no
    table lock, so the common path is contention-free.
    """
    cur = conn.cursor()
    if not force:
        cur.execute("""SELECT 1 FROM information_schema.columns
                       WHERE table_name='delimp_xic_trace_lane' AND column_name=%s""",
                    (_SCHEMA_SENTINEL,))
        if cur.fetchone():
            return
    for stmt in _ddl_statements(REGISTRY_DDL):
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
