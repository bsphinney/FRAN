"""xic_lance.py — the observed-CHROMATOGRAM (XIC) lane: Lance + DB registry.

Sibling of `spectrum_lance.py`. That lane stores each precursor's observed MS2 spectrum (one
intensity per fragment, at the apex). This one stores the full **extracted ion chromatograms** —
the intensity of every fragment and MS1 isotope ACROSS RETENTION TIME, i.e. the actual elution
profiles the search engine integrated.

Source: Spectronaut's "All XIC" SQLite export (one .xic.db per raw file, produced by
`--setXICExportDirectory` / GUI Export All XIC). Structure (SN21 manual, Appendix 9):
    Run(ID, RawFileName)
    RTAxis(ID, RTDataBytes)                      base64 little-endian float32 RT axis
    IonTraces(Run_ID, FGID, IonRank, MSLevel, IonLabel, IntensityDataBytes,
              RTAxis_XOffset, RTAxis_Length, RTAxis_ID)
`FGID` == the report's `FG.XICDBID`, which is how a trace is tied back to a peptide identity.

Why a SEPARATE dataset rather than columns on the spectrum lane: the 1,552 existing spectrum
datasets would all have to be rewritten to gain the columns, and XIC availability is a different
population anyway (only exports run WITH ion traces have it). Separate dataset + separate registry
keeps both lanes independently derivable and independently verifiable.

Layout: ONE ROW PER PRECURSOR. Traces are parallel LIST columns; because MS1 and MS2 traces sit on
different RT axes (verified: only 2 of 2,000 precursors share one axis across all their traces),
the RT vector is stored PER TRACE as list<list<float32>> rather than once per precursor.
"""
from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
import struct

import pyarrow as pa

_f32 = pa.float32()
_i16 = pa.int16()
_str = pa.string()
_lf = pa.list_(_f32)
_li = pa.list_(_i16)
_ls = pa.list_(_str)
_llf = pa.list_(pa.list_(_f32))          # one float vector PER TRACE

SCHEMA = pa.schema([
    ("search_id", _str), ("search_name", _str), ("raw_path", _str), ("run", _str),
    ("xicdbid", pa.int64()),
    ("stripped_seq", _str), ("modified_seq", _str), ("charge", _i16),
    # im is REQUIRED, not optional: these are diaPASEF runs and any re-extraction from the .d gates
    # on |mobility - im| (our engine uses +/-0.05 1/K0). A chromatogram stored without the ion
    # mobility it belongs to cannot be reproduced or compared — omitting it made the first
    # extractor-verification run meaningless (65% of precursors returned empty).
    ("precursor_mz", _f32), ("rt", _f32), ("im", _f32), ("q_value", _f32), ("is_decoy", pa.bool_()),
    ("protein_group", _str), ("genes", _str),
    ("n_traces", _i16), ("n_ms1", _i16), ("n_ms2", _i16),
    # one element per trace
    ("trace_label", _ls), ("trace_ms_level", _li), ("trace_rank", _li),
    ("trace_rt", _llf), ("trace_intensity", _llf),
])

_REG_TABLE = "delimp_xic_lane"

REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS delimp_xic_lane (
    id             BIGSERIAL PRIMARY KEY,
    lance_path     TEXT UNIQUE NOT NULL,
    search_id      UUID,
    search_name    TEXT,
    n_precursors   INTEGER,
    n_traces       BIGINT,
    content_md5    TEXT,
    lance_version  BIGINT,
    ingested_at    TIMESTAMPTZ DEFAULT now(),
    updated_at     TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_xic_lane_name ON delimp_xic_lane (search_name);
CREATE INDEX IF NOT EXISTS idx_xic_lane_sid  ON delimp_xic_lane (search_id);
ALTER TABLE delimp_xic_lane ADD COLUMN IF NOT EXISTS writer_version TEXT;
"""


def content_md5(table: pa.Table) -> str:
    """See spectrum_lance.content_md5 — combine_chunks() is required so the checksum doesn't
    depend on how Lance happens to chunk the data on read.

    Only for tables that are ALREADY in memory. To checksum a Lance dataset use dataset_md5():
    this function needs the whole table resident and then copies it three more times, which is
    what OOM-killed the 224-run PROT_0793 mouse lane."""
    table = table.combine_chunks()
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as w:
        w.write_table(table)
    return hashlib.md5(sink.getvalue().to_pybytes()).hexdigest()


# In ROWS, and a row here is a whole precursor's chromatograms, so rows are fat and vary wildly:
# 2.2 KB/row on the 967,427-row 1020_S6-H2 lane, 37 KB/row on the 198,522-row diann251_fragexport16
# lane. 65,536 rows is a 2.4 GB batch on the latter, which defeats the point; 8,192 keeps the batch
# at ~300 MB even there. Changing this constant changes the digest, so it is pinned, not tuned.
MD5_BATCH_ROWS = 8_192


class LegacyDigestTooLarge(RuntimeError):
    """verify()'s pre-1.2.0 whole-table fallback would not fit in memory.

    NOT a checksum mismatch. A caller that renders this as MISMATCH is reporting corruption where
    there is none, which is why verify() raises instead of returning False."""


# Peak RSS of the whole-table content_md5 over the dataset's on-disk Lance bytes. Measured on three
# lanes spanning 2-10 GB and 0.2-4.9M rows, from a ~160 MB baseline:
#     2.02 GB /   967,427 rows ->  8,370 MB   4.14x
#     7.42 GB /   198,522 rows -> 18,074 MB   2.44x
#     9.71 GB / 4,924,411 rows -> 48,435 MB   4.99x
# 5.0x is the worst of those rounded up. It over-estimates a densely packed lane (the 7.42 GB one
# would be refused at an estimated 37 GB when it really needs 18), and that is the direction to err:
# the cost of over-estimating is a message telling you to raise max_bytes, the cost of
# under-estimating is a SIGKILL with no diagnostic.
_LEGACY_MD5_RSS_PER_DISK_BYTE = 5.0

# The smallest allocation OBSERVED to lose to this: verifying the 7.42 GB diann251_fragexport16 row
# was OOM-killed at 16 GB. A caller with a bigger allocation passes max_bytes.
LEGACY_VERIFY_MAX_BYTES = 16 * 1024 ** 3


def _fixed_tables(batches, n: int):
    """Re-cut an arbitrary stream of record batches into single-chunk tables of exactly n rows
    (the last one short).

    The cut has to be imposed HERE rather than asked of Lance. Measured on a 967,427-row dataset
    built by 319 appends: `ds.to_batches(batch_size=65536)` returned 319 batches of 35, 129, 201,
    222 … rows — one per fragment, ignoring the requested size. A digest over Lance's own batches
    would therefore change with the number of runs appended. Cutting to a fixed n restores the
    layout-independence that content_md5 bought by holding the whole table at once (verified: the
    same digest for source batch sizes 1,024 / 8,192 / 65,536)."""
    buf, have = [], 0
    for b in batches:
        buf.append(b)
        have += b.num_rows
        while have >= n:
            t = pa.Table.from_batches(buf).combine_chunks()
            yield t.slice(0, n)
            t = t.slice(n)
            buf, have = t.to_batches(), t.num_rows
    if have:
        yield pa.Table.from_batches(buf).combine_chunks()


def dataset_md5(ds, batch_rows: int = MD5_BATCH_ROWS) -> str:
    """content_md5 of a whole Lance dataset, without ever holding the whole dataset.

    content_md5(ds.to_table()) was the only place the finished lane was materialised, and it built
    four full-size buffers to do it — to_table(), combine_chunks(), the IPC stream, then
    to_pybytes() — three of them alive at once. Measured from a ~150 MB baseline:

        2.02 GB / 967,427 rows   whole-table  8,370 MB, 32s     streamed  2,498 MB,  9s
        7.42 GB / 198,522 rows   whole-table 18,074 MB, 53s     streamed  2,233 MB, 21s

    i.e. the old cost tracked the dataset and the streamed one does not. That call, not the
    per-run loop, is what OOM-killed the 224-run PROT_0793 mouse lane at 48 GB — it was reached
    AFTER every run had been written, which is why the failure left a complete orphan directory.
    Re-run at 160 GB the same code finished with MaxRSS 64.9 GB; on the 9.71 GB lane it produced,
    this function is 61s and 1,582 MB peak, and the whole build now fits in 16 GB (MaxRSS
    13.9 GB, byte-identical output).

    DIFFERENT DIGEST, ON PURPOSE. The IPC stream frames each record batch separately, so hashing
    n-row batches cannot produce the byte stream a single whole-table batch produces. md5s
    recorded by writer <= 1.1.0 do not verify against this function — verify() falls back to the
    old definition for them, and writer_version says which one a registry row carries."""
    h = hashlib.md5()
    for t in _fixed_tables(ds.to_batches(batch_size=batch_rows), batch_rows):
        # Lance can hand back a schema carrying its own metadata; the digest must not depend on it.
        if t.schema != SCHEMA:
            t = t.cast(SCHEMA)
        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, t.schema) as w:
            w.write_table(t)
        # memoryview, not to_pybytes(): hashing must not copy the buffer it is hashing.
        h.update(memoryview(sink.getvalue()))
    return h.hexdigest()


def write_lance(table: pa.Table, path: str, mode: str = "overwrite", checksum: bool = True):
    """Write/append one table. Returns (n_rows, content_md5 or None, version).

    checksum=False is for callers appending one run of many: the per-run digest costs three full
    copies of the table (combine_chunks + IPC buffer + to_pybytes) and answers nothing, because
    the registry stores a digest of the WHOLE dataset. diann_xic_to_lance paid it 224 times for
    one search and discarded the result every time."""
    import lance
    table = table.cast(SCHEMA) if table.schema != SCHEMA else table
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    ds = lance.write_dataset(table, path, mode=mode)
    return table.num_rows, (content_md5(table) if checksum else None), ds.version


def ensure_registry(conn):
    """Create the registry only when it is actually missing.

    This used to run REGISTRY_DDL on EVERY call. `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` still
    takes an AccessExclusiveLock even when the column already exists, so under concurrent ingest it
    queues behind whatever else is writing and dies on statement_timeout. That is not hypothetical:
    8 of 13 searches in the 2026-08-25 DIA-NN XIC batch failed here, every one of them with
    "canceling statement due to statement timeout" from this line, and not one for a data reason.
    corpus_ingest documents the same hazard for its own ALTERs.

    The catalog check is a cheap read, so the common path -- registry already correct -- now takes
    no lock at all."""
    cur = conn.cursor()
    cur.execute("""SELECT to_regclass('public.%s') IS NOT NULL,
                          EXISTS (SELECT 1 FROM information_schema.columns
                                   WHERE table_name='%s' AND column_name='writer_version')"""
                % (_REG_TABLE, _REG_TABLE))
    have_table, have_col = cur.fetchone()
    if have_table and have_col:
        return
    cur.execute(REGISTRY_DDL)
    conn.commit()


def register(conn, search_id, search_name, lance_path, n_prec, n_traces, md5, version):
    from versions import XIC_LANE_WRITER_VERSION
    cur = conn.cursor()
    cur.execute("""INSERT INTO delimp_xic_lane
                     (lance_path, search_id, search_name, n_precursors, n_traces, content_md5,
                      lance_version, writer_version, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
                   ON CONFLICT (lance_path) DO UPDATE SET
                     search_id=EXCLUDED.search_id, search_name=EXCLUDED.search_name,
                     n_precursors=EXCLUDED.n_precursors, n_traces=EXCLUDED.n_traces,
                     content_md5=EXCLUDED.content_md5, lance_version=EXCLUDED.lance_version,
                     writer_version=EXCLUDED.writer_version,
                     updated_at=now()""",
                (lance_path, str(search_id) if search_id else None, search_name,
                 int(n_prec), int(n_traces), md5, int(version), XIC_LANE_WRITER_VERSION))
    conn.commit()


def verify(lance_path, expected_md5, max_bytes: int = LEGACY_VERIFY_MAX_BYTES) -> bool:
    """Streaming first, whole-table only if that misses.

    Rows registered by writer <= 1.1.0 carry the whole-table content_md5, so the fallback is what
    keeps them verifiable, and trying it AFTER the cheap check rather than dispatching on
    writer_version is deliberate: a NULL or pilot writer_version cannot then produce a false OK.
    Verified against live registry rows — Dog_yeast_entrapment_SN21 (writer NULL) and the
    202106022_TIMS03 lane (1.1.0) both MATCH here and MISMATCH on the streamed digest alone.

    BUDGET FOR THE FALLBACK. It is the 4x-RSS path dataset_md5 exists to avoid, and on a wide lane
    it is not survivable in a small allocation: verifying the 7.42 GB / 198,522-row
    diann251_fragexport16 row was OOM-killed at 16 GB (it needs ~18 GB, measured). That is inherent
    to the old digest — you cannot check a whole-table checksum without the whole table — and it is
    unchanged from before 1.2.0, when this was the ONLY path. Rows written at 1.2.0 never reach it.
    Rather than be SIGKILLed there, which leaves the caller a vanished process and no diagnostic,
    the fallback is estimated first and refused with LegacyDigestTooLarge if it will not fit."""
    import lance
    ds = lance.dataset(lance_path)
    if dataset_md5(ds) == expected_md5:
        return True
    # Only pre-1.2.0 rows get this far. Size the whole-table read before attempting it. On-disk
    # bytes are the cheap proxy; they over-count a dataset that still holds superseded versions,
    # which again errs toward refusing rather than dying.
    on_disk = sum(os.path.getsize(os.path.join(dp, f))
                  for dp, _dn, fns in os.walk(lance_path) for f in fns)
    need = int(on_disk * _LEGACY_MD5_RSS_PER_DISK_BYTE)
    if need > max_bytes:
        raise LegacyDigestTooLarge(
            f"{lance_path}: the streamed digest does not match the stored one. That means EITHER "
            f"the row predates xic writer 1.2.0 (so it needs the whole-table checksum) OR the data "
            f"has changed — and telling those apart requires the whole-table read, which is an "
            f"estimated {need / 1024 ** 3:.0f} GB of RSS for {on_disk / 1024 ** 3:.1f} GB on disk "
            f"({ds.count_rows():,} rows), above the {max_bytes / 1024 ** 3:.0f} GB this check will "
            f"attempt. Re-run with a larger allocation and a raised max_bytes to settle it, or "
            f"rebuild the lane at writer 1.2.0, which verifies in ~1.5 GB. This is a memory limit, "
            f"NOT a verdict on the data.")
    return content_md5(ds.to_table().cast(SCHEMA)) == expected_md5


# ── decoding ────────────────────────────────────────────────────────────────────────────────
def _decode(b64) -> list[float]:
    """base64 little-endian float32 -> list. Spectronaut occasionally emits comma-joined chunks."""
    if not b64:
        return []
    if isinstance(b64, bytes):
        b64 = b64.decode("ascii", "ignore")
    out: list[float] = []
    for piece in str(b64).split(","):
        piece = piece.strip()
        if not piece:
            continue
        raw = base64.b64decode(piece)
        n = len(raw) // 4
        if n:
            out.extend(struct.unpack(f"<{n}f", raw))
    return out


def read_xic_db(db_path, keep_fgids=None):
    """Yield (fgid, [trace, ...]) per precursor, where trace = dict(label, ms_level, rank, rt[], i[]).
    Traces are grouped by FGID; rows whose FGID isn't wanted are skipped BEFORE the base64 decode."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    try:
        rtaxis = {rid: _decode(b) for rid, b in con.execute("SELECT ID, RTDataBytes FROM RTAxis")}
        cur = con.execute("""SELECT FGID, MSLevel, IonLabel, IntensityDataBytes,
                                    RTAxis_XOffset, RTAxis_Length, RTAxis_ID, IonRank
                             FROM IonTraces ORDER BY FGID""")
        cur_fg, traces = None, []
        for fg, lvl, lbl, ib, xoff, length, rtid, rank in cur:
            if keep_fgids is not None and fg not in keep_fgids:
                continue
            if fg != cur_fg:
                if cur_fg is not None and traces:
                    yield cur_fg, traces
                cur_fg, traces = fg, []
            inten = _decode(ib)
            if not inten:
                continue
            full = rtaxis.get(rtid, [])
            xoff = int(xoff or 0)
            length = int(length or len(inten))
            rt = full[xoff:xoff + length]
            n = min(len(rt), len(inten))
            if not n:
                continue
            traces.append({"label": str(lbl or ""), "ms_level": int(lvl or 0), "rank": int(rank or 0),
                           "rt": [float(x) for x in rt[:n]], "i": [float(x) for x in inten[:n]]})
        if cur_fg is not None and traces:
            yield cur_fg, traces
    finally:
        con.close()


# ── report join + dataset build ─────────────────────────────────────────────────────────────
_COLS = {  # field -> candidate report column names (parquet underscores or TSV dots)
    "xicdbid": ["FG_XICDBID", "FG.XICDBID"],
    "run": ["R_FileName", "R.FileName"],
    "stripped_seq": ["PEP_StrippedSequence", "PEP.StrippedSequence"],
    "modified_seq": ["EG_ModifiedSequence", "EG.ModifiedSequence"],
    "charge": ["FG_Charge", "FG.Charge"],
    "precursor_mz": ["FG_PrecMz", "FG.PrecMz"],
    "rt": ["EG_ApexRT", "EG.ApexRT"],
    "im": ["EG_IonMobility", "EG.IonMobility", "FG_ApexIonMobility", "FG.ApexIonMobility"],
    "q_value": ["EG_Qvalue", "EG.Qvalue"],
    "is_decoy": ["EG_IsDecoy", "EG.IsDecoy"],
    "protein_group": ["PG_ProteinGroups", "PG.ProteinGroups"],
    "genes": ["PG_Genes", "PG.Genes"],
}


def _num(x):
    try:
        v = float(x)
        return None if v != v else v
    except (TypeError, ValueError):
        return None


def _truthy(x):
    return str(x).strip().lower() in ("true", "1", "1.0")


def _report_columns(path):
    """Header names of a Spectronaut report — parquet OR tsv."""
    if str(path).lower().endswith(".parquet"):
        import pyarrow.parquet as pq
        return list(pq.read_schema(path).names)
    import pandas as pd
    return list(pd.read_csv(path, sep="\t", nrows=0).columns)


def _iter_report_batches(path, cols, chunksize=500_000):
    """Yield {column: [values]} batches from a Spectronaut report, parquet OR tsv.

    TSV is not a fallback curiosity: manageSNE exports TSV whenever the operator avoids the parquet
    writer (win-2 hit parquet corruption on very large .sne, so the Taha allDog 128-column report was
    deliberately exported as a 34 GB TSV). report_index used to call pq.read_schema() unconditionally
    and died with "Parquet magic bytes not found in footer" on exactly those reports.

    Batched on purpose: a fragment-level report is tens of millions of rows and the index only ever
    keeps ONE entry per XICDBID, so streaming keeps peak memory at a chunk rather than the file.
    """
    if str(path).lower().endswith(".parquet"):
        import pyarrow.parquet as pq
        for b in pq.ParquetFile(path).iter_batches(batch_size=chunksize, columns=list(cols)):
            yield b.to_pydict()
    else:
        import pandas as pd
        for ch in pd.read_csv(path, sep="\t", usecols=list(cols), chunksize=chunksize,
                              low_memory=False):
            # NaN -> None. pandas represents an EMPTY TSV cell as float('nan'), while the parquet
            # reader yields None, so without this the two formats disagree on what "missing" is and
            # a blank PG.Genes arrives at an Arrow string column as a float:
            # "ArrowTypeError: Expected bytes, got a 'float' object", 50 minutes into the build.
            # Normalise at the source so every consumer sees one missing-value convention; _num()
            # and _truthy() already treat None correctly.
            ch = ch.astype(object).where(pd.notna(ch), None)
            yield {c: ch[c].tolist() for c in ch.columns}


def report_index(report_path, q_max=0.01, keep_decoys=False):
    """{run -> {xicdbid -> identity dict}} for the (precursor, run) pairs we want traces for.

    KEYED BY RUN, and that is load-bearing (fixed 2026-08-21). FG.XICDBID is GLOBAL across the
    experiment, not per-file: every run's .xic.db holds a trace for EVERY library precursor -- on the
    Taha allDog set all 220 dbs carry the identical 43,952 FGIDs (measured, jaccard 1.000). So a
    flat {fgid -> record} index, deduped by fgid, kept ONE arbitrary run's record and handed it to
    all 220 dbs. Two things broke:

      * `run`, `rt`, `im` and `q_value` on a trace came from whichever run happened to appear first
        in the report -- wrong for 219 dbs out of 220.
      * per-run FDR was lost. Every db contributed all 43,952 FGIDs, so the lane would emit
        220 x 43,952 = 9.67M rows when only 3,493,169 (precursor, run) pairs actually pass q<=0.01;
        roughly two thirds of the lane would have been traces for precursors NOT identified in that
        run, wearing another run's identity.

    Keying by (run, fgid) makes each trace carry its own run's measurements and restores the FDR
    filter, so the lane population matches the corpus. Decoys are excluded by default for the same
    reason: the XIC lane mirrors the public corpus population."""
    names = set(_report_columns(report_path))
    use, col = {}, {}
    for field, cands in _COLS.items():
        hit = next((c for c in cands if c in names), None)
        if hit:
            col[field] = hit
            use[hit] = field
    if "xicdbid" not in col:
        return {}, "report has no FG.XICDBID column — cannot link traces to peptides"
    if "run" not in col:
        return {}, "report has no R.FileName column — cannot key traces per run"
    out = {}
    for t in _iter_report_batches(report_path, list(use)):
        n = len(t[col["xicdbid"]])
        for i in range(n):
            if not keep_decoys and "is_decoy" in col and _truthy(t[col["is_decoy"]][i]):
                continue
            q = _num(t[col["q_value"]][i]) if "q_value" in col else None
            if q is None or q > q_max:
                continue
            fg = t[col["xicdbid"]][i]
            if fg is None:
                continue
            try:
                fg = int(fg)
            except (TypeError, ValueError):
                continue
            run = str(t[col["run"]][i] or "")
            if not run:
                continue
            per_run = out.setdefault(run, {})
            if fg in per_run:
                continue                   # fragment-level report: one row per fragment
            rec = {f: (t[c][i] if c in t else None) for c, f in use.items()}
            rec["xicdbid"] = fg
            per_run[fg] = rec
    return out, None


def process_one(report_path, xic_dir, out_dir, search_id=None, search_name=None, dry=False):
    """Report + XIC dbs -> one Lance dataset. Returns (lance_path, n_prec, n_traces, md5, version)."""
    import glob as _glob
    idx, err = report_index(report_path)
    if err:
        raise ValueError(err)
    if not idx:
        return (None, 0, 0, None, None)
    dbs = sorted(_glob.glob(os.path.join(xic_dir, "*.xic.db")) + _glob.glob(os.path.join(xic_dir, "*.sqlite")))
    # Drop macOS AppleDouble stubs BY NAME before trying to open anything. These shares are mounted
    # by Macs, so every real file can have a 4 KB "._<name>" sibling that matches the glob exactly.
    # One sits next to the Taha allDog XIC dbs (._…_VER_1_….xic.db). It is a resource fork, not a
    # database, and it is not a corrupt db either -- so skipping it by name is correct, not a
    # tolerance for bad data.
    dbs = [d for d in dbs if not os.path.basename(d).startswith("._")]
    if not dbs:
        raise ValueError(f"no *.xic.db / *.sqlite under {xic_dir}")
    rows, n_traces = [], 0
    bad, unmatched = [], []
    for db in dbs:
        # Which run is this db? Spectronaut names each All-XIC db "<R.FileName>.xic.db", so the
        # basename IS the run key. Taking the run from the FILE rather than from the report record
        # is the whole point of the (run, fgid) index: the db is ground truth for which run these
        # traces were measured in.
        run = os.path.basename(db)
        for _suf in (".xic.db", ".sqlite"):
            if run.endswith(_suf):
                run = run[: -len(_suf)]
                break
        idx_run = idx.get(run)
        if not idx_run:
            # No FDR-passing precursors for this run -- or the db basename does not match any
            # R.FileName. Either way say so; a silently skipped run is missing data.
            unmatched.append(os.path.basename(db))
            continue
        keep = set(idx_run)
        # One unreadable db must not cost the whole search its chromatogram lane. read_xic_db opens
        # SQLite and streams, so a truncated/locked/non-db file raises HERE, mid-iteration, and used
        # to propagate out of process_one -- corpus_ingest catches that best-effort, so the lane
        # silently produced NOTHING for the search. Skip the file, keep the rest, and say so loudly:
        # a skipped db is real missing data and must never look like a clean run.
        try:
            db_rows = list(read_xic_db(db, keep))
        except (sqlite3.Error, OSError) as e:
            bad.append((db, str(e)[:80]))
            print(f"  [xic] SKIPPED unreadable db {os.path.basename(db)}: {str(e)[:80]}", flush=True)
            continue
        for fg, traces in db_rows:
            r = idx_run.get(fg)
            if not r:
                continue
            ms1 = sum(1 for t in traces if t["ms_level"] == 1)
            rows.append({
                "search_id": str(search_id) if search_id else None,
                "search_name": search_name,
                "raw_path": db,
                "run": run,
                "xicdbid": fg,
                "stripped_seq": r.get("stripped_seq"),
                "modified_seq": r.get("modified_seq"),
                "charge": int(_num(r.get("charge")) or 0),
                "precursor_mz": _num(r.get("precursor_mz")),
                "rt": _num(r.get("rt")),
                "im": _num(r.get("im")),
                "q_value": _num(r.get("q_value")),
                "is_decoy": _truthy(r.get("is_decoy")),
                "protein_group": r.get("protein_group"),
                "genes": r.get("genes"),
                "n_traces": len(traces),
                "n_ms1": ms1,
                "n_ms2": len(traces) - ms1,
                "trace_label": [t["label"] for t in traces],
                "trace_ms_level": [t["ms_level"] for t in traces],
                "trace_rank": [t["rank"] for t in traces],
                "trace_rt": [t["rt"] for t in traces],
                "trace_intensity": [t["i"] for t in traces],
            })
            n_traces += len(traces)
    if unmatched:
        print(f"  [xic] {len(unmatched)} of {len(dbs)} dbs had no FDR-passing precursors in the "
              f"report (or the basename matched no R.FileName): "
              f"{', '.join(unmatched[:5])}{' …' if len(unmatched) > 5 else ''}", flush=True)
    if bad:
        print(f"  [xic] WARNING: {len(bad)} of {len(dbs)} XIC dbs were unreadable and are NOT in the "
              f"lane: {', '.join(os.path.basename(b) for b, _ in bad[:5])}"
              f"{' …' if len(bad) > 5 else ''}", flush=True)
    if not rows:
        return (None, 0, 0, None, None)
    tbl = pa.Table.from_pylist(rows, schema=SCHEMA)
    if dry:
        return (None, tbl.num_rows, n_traces, None, None)
    base = os.path.basename(str(report_path)).replace("\\", "/")
    import re as _re
    base = _re.sub(r"_Report(_FRAN \(Normal\))?\.(parquet|tsv)$", "", base, flags=_re.I)
    base = _re.sub(r"^\d{8}_\d{6}_", "", base)
    safe = _re.sub(r"[^A-Za-z0-9._-]+", "_", search_name or base).strip("_")
    lance_path = os.path.join(out_dir, f"{safe}.xic.lance")
    n_rows, _, version = write_lance(tbl, lance_path, mode="overwrite", checksum=False)
    # Read the digest back off the DATASET rather than off the table in hand, so a Spectronaut lane
    # and a DIA-NN lane registered by the same writer_version carry the same KIND of digest. The
    # extra pass is a streaming read; holding tbl through it is not, hence the del.
    del tbl, rows
    # THE DATASET IS ON DISK BY NOW, and the caller registers it from what this returns. Reading it
    # back re-opens a Quobyte path, so a transient I/O failure here would propagate into
    # corpus_ingest's blanket "XIC lane best-effort, never fail the ingest" except and skip
    # register() -- leaving a complete, correct, UNREGISTERED lane that nothing in the database can
    # see. That is the same shape as the OOM this writer was just fixed for: a failure AFTER the
    # write, invisible from the registry. The registry row is the thing that must survive; the
    # digest is not, so a checksum failure downgrades to content_md5 NULL and says so.
    md5 = None
    try:
        import lance
        md5 = dataset_md5(lance.dataset(lance_path))
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] XIC lane WRITTEN BUT NOT CHECKSUMMED: {lance_path} — registering with "
              f"content_md5 NULL; this lane cannot be verified until it is rebuilt "
              f"({type(e).__name__}: {str(e)[:120]})", flush=True)
    return (lance_path, n_rows, n_traces, md5, version)
