"""load_run_windows.py - record each raw file's isolation-window scheme in delimp_run_windows.

The window scheme had no column anywhere in FRAN (design doc section 2), yet DIA de novo and
rescoring models (Cascadia, DIANovo, DIA-BERT) need it per run. probe_acquisition.py already
measured it -- and its output went nowhere. This loads it: every raw_files.hive_path with no
delimp_run_windows row is probed (Bruker: DiaFrameMsMsWindows through tdf_safe, immutable read-only;
Thermo: the scan events' reactions, one CoreCLR child per batch) and upserted.

Read-only on the raws. Needs hive_path (resolve_raw_hive_paths) first; runs as a fran_fill step.
A DDA run, a blank or an unreadable file gets a row with NULL windows and a note: that is a real
answer, and recording it stops the next night from probing the same file again. Use --recheck to
re-probe rows whose note says the reader was unavailable or failed.

    python load_run_windows.py [--limit N] [--shard I --shards N] [--recheck] [--apply]
"""
import argparse
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_acquisition as P                                     # noqa: E402

THERMO_BATCH = 200


def scheme_md5(kind, windows):
    """Same method -> same hash. m/z rounded to 0.1 so float noise doesn't split a method."""
    if not windows:
        return None
    if kind == "d":
        key = sorted((w.get("group"), w.get("scan_begin"), w.get("scan_end"),
                      round(w.get("mz_lo") or 0, 1), round(w.get("mz_hi") or 0, 1)) for w in windows)
    else:
        key = sorted((round(w[0], 1), round(w[1], 1)) for w in windows)
    return hashlib.md5(json.dumps(key).encode()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--recheck", action="store_true",
                    help="also re-probe rows whose earlier probe could not read the file")
    a = ap.parse_args()

    from coreomics_import import _conn
    conn = _conn()
    with conn.cursor() as c:
        c.execute("SET statement_timeout = '10min'")
        c.execute("""
            SELECT DISTINCT ON (r.hive_path) r.hive_path, r.raw_basename
              FROM raw_files r
              LEFT JOIN delimp_run_windows w ON w.hive_path = r.hive_path
             WHERE nullif(btrim(r.hive_path), '') IS NOT NULL
               AND (r.hive_path ILIKE '%%.d' OR r.hive_path ILIKE '%%.raw')
               AND (w.hive_path IS NULL
                    OR (%s AND (w.note ILIKE '%%unavailable%%' OR w.note ILIKE '%%failed%%')))
             ORDER BY r.hive_path""", (a.recheck,))
        todo = [(p, b) for i, (p, b) in enumerate(c.fetchall()) if i % a.shards == a.shard]
    if a.limit:
        todo = todo[: a.limit]
    print(f"{len(todo):,} raw file(s) to probe (shard {a.shard}/{a.shards}); apply={a.apply}")
    base = {p: b for p, b in todo}
    t0 = time.time()

    results = []
    for p, _ in todo:
        if p.lower().rstrip("/").endswith(".d"):
            results.append(P.probe_bruker(p))
    thermo = [p for p, _ in todo if p.lower().endswith(".raw")]
    for i in range(0, len(thermo), THERMO_BATCH):
        results.extend(P.probe_thermo_batch(thermo[i:i + THERMO_BATCH]).values())
        print(f"  thermo {min(i + THERMO_BATCH, len(thermo)):,}/{len(thermo):,} "
              f"{time.time() - t0:.0f}s")

    rows, n_env = [], 0
    for d in results:
        p = d.get("path")
        if p not in base:
            continue
        # A reader that could not start says nothing about the FILE. Recording it would stop the
        # next night from probing it (2026-11-13: 5,269 .raw rows written this way because the
        # job had no DOTNET_ROOT). Skip it and fail the step so the status JSON shows the gap.
        if any(s in (d.get("note") or "") for s in ("coreclr unavailable", "reader unavailable")):
            n_env += 1
            continue
        w = d.get("windows")
        rows.append((p, base[p], d.get("kind"), d.get("instrument"), d.get("dia_mz_lo"),
                     d.get("dia_mz_hi"), d.get("dia_n_windows"), d.get("dia_width_med"),
                     json.dumps(w) if w else None,
                     json.dumps(d.get("collision_energies")) if d.get("collision_energies") else None,
                     scheme_md5(d.get("kind"), w), d.get("note"), "probe_acquisition.py"))
    n_dia = sum(1 for r in rows if r[8])
    print(f"probed {len(rows):,} in {time.time() - t0:.0f}s: {n_dia:,} with a DIA window scheme, "
          f"{len(rows) - n_dia:,} without (see note)")
    rc = 0
    if n_env:
        rc = 2
        print(f"ERROR: the Thermo reader could not start for {n_env:,} file(s) (DOTNET_ROOT / "
              f"pythonnet). They were NOT recorded; fix the environment and rerun.", file=sys.stderr)
    if not a.apply:
        for r in rows[:5]:
            print("  sample:", r[0], r[2], r[4], r[5], r[6], (r[11] or "")[:80])
        print("dry run: nothing written (--apply to write)")
        return rc
    from psycopg2.extras import execute_values
    with conn.cursor() as c:
        c.execute("SET lock_timeout = '5s'")
        execute_values(c, """
            INSERT INTO delimp_run_windows (hive_path, raw_basename, kind, instrument, dia_mz_lo,
                   dia_mz_hi, dia_n_windows, dia_width_med, windows, collision_energies,
                   scheme_md5, note, source)
            VALUES %s
            ON CONFLICT (hive_path) DO UPDATE SET
                   kind = EXCLUDED.kind, instrument = EXCLUDED.instrument,
                   dia_mz_lo = EXCLUDED.dia_mz_lo, dia_mz_hi = EXCLUDED.dia_mz_hi,
                   dia_n_windows = EXCLUDED.dia_n_windows, dia_width_med = EXCLUDED.dia_width_med,
                   windows = EXCLUDED.windows, collision_energies = EXCLUDED.collision_energies,
                   scheme_md5 = EXCLUDED.scheme_md5, note = EXCLUDED.note,
                   source = EXCLUDED.source, probed_at = now()""",
                       rows, template="(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s)",
                       page_size=500)
    conn.commit()
    conn.close()
    print(f"upserted {len(rows):,} row(s)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
