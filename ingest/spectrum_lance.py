"""spectrum_lance.py — the observed-spectrum store for DIA-CLIP training: Lance + DB registry.

Decision (2026-07-17): store the real acquired data recovered from the Spectronaut FRAN reports
in the **Lance** columnar format (Arrow-based, fast random access, versioned) — the same format
`depthcharge`/Casanovo moved to for MS training data — NOT inside the transactional Postgres
corpus. This is how DL people actually store training data (train from columnar files, not a
relational DB). Durability (the real concern behind "files get lost") is handled by the **DB
registry**: every Lance dataset is recorded in `delimp_spectrum_lane` with a CONTENT md5 + row
counts, so a lost/corrupt dataset is detectable and re-derivable from the archived reports. The
data has two independent homes (the Lance lane + the archived reports), never zero.

Schema: ONE ROW PER PRECURSOR (per run). Scalars are columns; the observed MS2 spectrum + MS1
isotope envelope are Arrow LIST columns (a precursor's whole spectrum in one row — the shape a
training DataLoader fetches by index).
"""
from __future__ import annotations

import hashlib
import os

import pyarrow as pa

_f32 = pa.float32()
_i16 = pa.int16()
_str = pa.string()
_lf = pa.list_(_f32)
_li = pa.list_(_i16)
_ls = pa.list_(_str)

SCHEMA = pa.schema([
    ("search_id", _str), ("search_name", _str), ("raw_path", _str), ("run", _str),
    ("stripped_seq", _str), ("modified_seq", _str), ("charge", _i16),
    ("precursor_mz", _f32), ("prec_mz_calibrated", _f32),
    ("rt", _f32), ("rt_predicted", _f32), ("irt_empirical", _f32), ("irt_predicted", _f32),
    ("im", _f32), ("q_value", _f32), ("global_q_value", _f32), ("pg_q_value", _f32),
    ("signal_to_noise", _f32), ("int_corr_score", _f32),
    ("ms1_iso_measured", _lf), ("ms1_iso_rel_measured", _lf), ("ms1_iso_rel_predicted", _lf),
    ("ms1_quantity", _f32), ("ms2_quantity", _f32),
    ("prec_window", _str), ("prec_window_number", _i16), ("xicdbid", pa.int64()),
    ("fragment_count", _i16), ("interference_ms1", pa.bool_()), ("interference_ms2", pa.bool_()),
    ("is_decoy", pa.bool_()), ("missed_cleavages", _i16), ("is_proteotypic", pa.bool_()),
    ("ptm_localization", _str), ("protein_group", _str), ("genes", _str), ("organism", _str),
    # observed MS2 spectrum — parallel list columns, one element per fragment
    ("frg_mz", _lf), ("frg_type", _ls), ("frg_num", _li), ("frg_ion", _ls), ("frg_charge", _li),
    ("frg_loss", _ls), ("frg_peak_area", _lf), ("frg_norm_area", _lf),
    ("frg_measured_relint", _lf), ("frg_predicted_relint", _lf), ("frg_mass_acc_ppm", _lf),
])

REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS delimp_spectrum_lane (
    search_id      UUID PRIMARY KEY,
    search_name    TEXT,
    lance_path     TEXT NOT NULL,
    n_precursors   INTEGER,
    n_fragments    BIGINT,
    content_md5    TEXT,          -- md5 of the Arrow content (integrity + loss detection)
    lance_version  BIGINT,        -- Lance dataset version at register time
    ingested_at    TIMESTAMPTZ DEFAULT now(),
    updated_at     TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_spectrum_lane_name ON delimp_spectrum_lane (search_name);
"""


def content_md5(table: pa.Table) -> str:
    """Deterministic content checksum of the Arrow table (independent of Lance file layout).
    Re-read a dataset, rebuild the table, recompute this -> matches iff the data is intact."""
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as w:
        w.write_table(table)
    return hashlib.md5(sink.getvalue().to_pybytes()).hexdigest()


def write_lance(table: pa.Table, path: str, mode: str = "overwrite"):
    """Write/append a Lance dataset. Returns (n_rows, content_md5, version). Idempotent per
    search when each search has its own <name>.lance path and mode='overwrite'."""
    import lance
    table = table.cast(SCHEMA) if table.schema != SCHEMA else table
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    ds = lance.write_dataset(table, path, mode=mode)
    return table.num_rows, content_md5(table), ds.version


def register(conn, search_id, search_name, lance_path, n_prec, n_frag, md5, version):
    """Record the dataset in the DB registry (the durable manifest). Upsert by search_id.
    Call ensure_registry(conn) once before the first register()."""
    cur = conn.cursor()
    cur.execute("""INSERT INTO delimp_spectrum_lane
                     (search_id, search_name, lance_path, n_precursors, n_fragments, content_md5, lance_version, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s, now())
                   ON CONFLICT (search_id) DO UPDATE SET
                     search_name=EXCLUDED.search_name, lance_path=EXCLUDED.lance_path,
                     n_precursors=EXCLUDED.n_precursors, n_fragments=EXCLUDED.n_fragments,
                     content_md5=EXCLUDED.content_md5, lance_version=EXCLUDED.lance_version,
                     updated_at=now()""",
                (str(search_id) if search_id else None, search_name, lance_path,
                 int(n_prec), int(n_frag), md5, int(version)))
    conn.commit()


def ensure_registry(conn):
    cur = conn.cursor(); cur.execute(REGISTRY_DDL); conn.commit()


def verify(lance_path, expected_md5) -> bool:
    """Re-read a Lance dataset and confirm its content md5 matches the registry (loss/corruption
    check). Returns True iff intact."""
    import lance
    tbl = lance.dataset(lance_path).to_table().cast(SCHEMA)
    return content_md5(tbl) == expected_md5
