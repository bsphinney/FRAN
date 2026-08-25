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
    # Added 2026-08-10 (writer 1.2.0). APPENDED, never inserted: verify() reconstructs an older
    # dataset's write-time schema by filtering SCHEMA to the fields that dataset actually has, which
    # is only correct while new fields go on the END and existing ones are untouched.
    #
    # frg_excluded is Spectronaut's own per-fragment verdict on whether the fragment was used for
    # quantification (31-48% True). Phase 2's fragment aggregates are WRONG without it — they would
    # average intensities Spectronaut itself discarded.
    ("frg_excluded", pa.list_(pa.bool_())),
    ("frg_chan_interference", pa.list_(pa.bool_())),
])

REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS delimp_spectrum_lane (
    id             BIGSERIAL PRIMARY KEY,
    lance_path     TEXT UNIQUE NOT NULL,   -- the dataset dir; the idempotency key (upsert on it)
    search_id      UUID,                   -- link to delimp_searches when the report matched (nullable)
    search_name    TEXT,
    n_precursors   INTEGER,
    n_fragments    BIGINT,
    content_md5    TEXT,          -- md5 of the Arrow content (integrity + loss detection)
    lance_version  BIGINT,        -- Lance dataset version at register time
    ingested_at    TIMESTAMPTZ DEFAULT now(),
    updated_at     TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_spectrum_lane_name ON delimp_spectrum_lane (search_name);
CREATE INDEX IF NOT EXISTS idx_spectrum_lane_sid  ON delimp_spectrum_lane (search_id);
-- Which code wrote this dataset. Added after the 2026-07-27 content_md5 chunking fix, which left
-- pre- and post-fix datasets indistinguishable on disk; NULL means "written before versions were
-- tracked", which is itself the answer to "could this be an old one?".
ALTER TABLE delimp_spectrum_lane ADD COLUMN IF NOT EXISTS writer_version TEXT;
"""


def content_md5(table: pa.Table) -> str:
    """Deterministic content checksum of the Arrow table (independent of Lance file layout).
    Re-read a dataset, rebuild the table, recompute this -> matches iff the data is intact.

    combine_chunks() is REQUIRED (fixed 2026-07-27): the IPC stream encodes each record batch
    separately, so the same rows split differently produce different bytes. A table is one chunk
    when built in memory at write time, but Lance hands back MULTIPLE chunks when reading a larger
    dataset — so every dataset big enough to read back multi-chunk reported a false MISMATCH, i.e.
    the integrity check silently failed open on exactly the biggest datasets. Canonicalising to a
    single chunk makes the checksum chunking-independent, and matches how the stored md5s were
    computed (single-chunk, at write time), so existing registry rows verify correctly."""
    table = table.combine_chunks()
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
    """Record the dataset in the DB registry (the durable manifest). Upsert by lance_path (so a
    re-run overwrites cleanly and an UNMATCHED report — search_id NULL — still registers). Call
    ensure_registry(conn) once before the first register()."""
    from versions import SPECTRUM_LANE_WRITER_VERSION
    cur = conn.cursor()
    # NOTE, and keep it OUT of the SQL string below: psycopg2 printf-formats the query whenever
    # params are passed, so ANY literal "%" in the statement -- including inside a -- SQL comment --
    # becomes a bogus conversion specifier and execute() dies with
    # "IndexError: tuple index out of range". This is not hypothetical. A comment here once read
    # "link rate fell 92.8% -> 55.1%", which silently broke EVERY spectrum-lane registration from
    # 2026-08-11 to 2026-08-21: Lance datasets kept being written to disk and not one was recorded
    # in the registry, which is the exact failure this registry exists to prevent. Write percent
    # signs as the word, or escape them as "%%", and prefer Python comments for prose.
    #
    # Why the UPDATE below COALESCEs instead of assigning: a re-parse re-resolves search_id by name,
    # and when that lookup fails it passes NULL. Under a bare assignment that OVERWRITES a link
    # established earlier by a richer mechanism. The 2026-08-10 re-parse did exactly that -- the
    # lane's link rate fell from 92.8 to 55.1 percent, and 577 datasets had to be restored from
    # delimp_spectrum_lane_runs. Same bug class as build_lane_run_index.py's raw_path clobber.
    # A fresh NULL means "could not resolve", never "unlink".
    cur.execute("""INSERT INTO delimp_spectrum_lane
                     (lance_path, search_id, search_name, n_precursors, n_fragments, content_md5,
                      lance_version, writer_version, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
                   ON CONFLICT (lance_path) DO UPDATE SET
                     search_id=COALESCE(EXCLUDED.search_id, delimp_spectrum_lane.search_id),
                     search_name=EXCLUDED.search_name,
                     n_precursors=EXCLUDED.n_precursors, n_fragments=EXCLUDED.n_fragments,
                     content_md5=EXCLUDED.content_md5, lance_version=EXCLUDED.lance_version,
                     writer_version=EXCLUDED.writer_version,
                     updated_at=now()""",
                (lance_path, str(search_id) if search_id else None, search_name,
                 int(n_prec), int(n_frag), md5, int(version), SPECTRUM_LANE_WRITER_VERSION))
    conn.commit()


def ensure_registry(conn):
    cur = conn.cursor(); cur.execute(REGISTRY_DDL); conn.commit()


def verify(lance_path, expected_md5) -> bool:
    """Re-read a Lance dataset and confirm its content md5 matches the registry (loss/corruption
    check). Returns True iff intact.

    Casts to the schema THIS dataset was written with, not the current global SCHEMA: a dataset
    written before a field was appended has fewer columns, and casting it to today's SCHEMA raises
    — which would turn every pre-existing dataset from "intact" into "unverifiable" the moment a
    column is added. Filtering SCHEMA down to the fields actually present reproduces the original
    write-time schema exactly, so the stored md5 still matches. Only valid because new fields are
    APPENDED and existing ones never change type or order."""
    import lance
    ds = lance.dataset(lance_path)
    have = set(ds.schema.names)
    sub = pa.schema([f for f in SCHEMA if f.name in have])
    tbl = ds.to_table().cast(sub)
    return content_md5(tbl) == expected_md5
