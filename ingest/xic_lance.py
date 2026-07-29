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
    depend on how Lance happens to chunk the data on read."""
    table = table.combine_chunks()
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as w:
        w.write_table(table)
    return hashlib.md5(sink.getvalue().to_pybytes()).hexdigest()


def write_lance(table: pa.Table, path: str, mode: str = "overwrite"):
    import lance
    table = table.cast(SCHEMA) if table.schema != SCHEMA else table
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    ds = lance.write_dataset(table, path, mode=mode)
    return table.num_rows, content_md5(table), ds.version


def ensure_registry(conn):
    cur = conn.cursor(); cur.execute(REGISTRY_DDL); conn.commit()


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


def verify(lance_path, expected_md5) -> bool:
    import lance
    return content_md5(lance.dataset(lance_path).to_table().cast(SCHEMA)) == expected_md5


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


def report_index(report_path, q_max=0.01, keep_decoys=False):
    """{xicdbid -> identity dict} for the precursors we want traces for (targets at q<=q_max).
    Decoys are excluded by default: the XIC lane mirrors the public corpus population."""
    import pyarrow.parquet as pq
    names = set(pq.read_schema(report_path).names)
    use, col = {}, {}
    for field, cands in _COLS.items():
        hit = next((c for c in cands if c in names), None)
        if hit:
            col[field] = hit
            use[hit] = field
    if "xicdbid" not in col:
        return {}, "report has no FG.XICDBID column — cannot link traces to peptides"
    t = pq.read_table(report_path, columns=list(use)).to_pydict()
    n = len(t[col["xicdbid"]])
    out = {}
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
        if fg in out:
            continue                       # fragment-level report: one row per fragment
        rec = {f: (t[c][i] if c in t else None) for c, f in use.items()}
        rec["xicdbid"] = fg
        out[fg] = rec
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
    if not dbs:
        raise ValueError(f"no *.xic.db / *.sqlite under {xic_dir}")
    keep = set(idx)
    rows, n_traces = [], 0
    for db in dbs:
        for fg, traces in read_xic_db(db, keep):
            r = idx.get(fg)
            if not r:
                continue
            ms1 = sum(1 for t in traces if t["ms_level"] == 1)
            rows.append({
                "search_id": str(search_id) if search_id else None,
                "search_name": search_name,
                "raw_path": db,
                "run": str(r.get("run") or ""),
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
    _, md5, version = write_lance(tbl, lance_path, mode="overwrite")
    return (lance_path, tbl.num_rows, n_traces, md5, version)
