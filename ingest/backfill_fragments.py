"""backfill_fragments.py — recover the REAL acquired data from the archived Spectronaut FRAN
reports into the Lance training lane, WITHOUT re-searching, re-exporting, or predicting anything.

The bulk Spectronaut ingest exported fragment-level reports (`..._Report_FRAN (Normal).parquet`,
131 columns) but only kept ~18 precursor fields, DROPPING the fragments, the MS1 isotope pattern,
the DIA window, and predicted-vs-observed RT/intensity. All of that is still in the archived report
parquets on Flinders. This tool re-parses them (VECTORIZED) into ONE Lance dataset per search —
one row per precursor, the observed MS2 spectrum + MS1 envelope as Lance list columns — and records
each dataset in the DB registry (`delimp_spectrum_lane`) with a content md5 + row counts. See
`spectrum_lance.py` for the schema/rationale (Lance + DB registry, not loose files, not the PG
corpus). Everything stored is the search engine's OWN recorded values — no Koina, no y/b guessing.

Archived reports (verified 2026-07-17):
    /nfs/lssc0/flinders/proteomics/Data/FRAN_reports/<name>/<ts>_<name>/<name>_Report_FRAN (Normal).parquet

Usage:
    python backfill_fragments.py "<...>_Report_FRAN (Normal).parquet" --out-dir /path/to/spectra.lance.d
    python backfill_fragments.py --scan /nfs/lssc0/flinders/proteomics/Data/FRAN_reports \
        --out-dir /quobyte/proteomics-grp/brett/glendon/spectra_lance --register --workers 8
    --register     record each dataset in delimp_spectrum_lane (matched via provenance).
    --workers N    parse this many reports in parallel (files/CPU); DB writes stay paced (one
                   registry upsert per search) so the shared PG-Farm DB is never overloaded.
    --limit N / --dry-run.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import spectronaut_to_corpus as s2c   # noqa: E402
import spectrum_lance as sln          # noqa: E402

FRAG_MAP = {
    "run": ["R.FileName", "R.Raw File Name"], "stripped_seq": ["PEP.StrippedSequence"],
    "modified_seq": ["EG.ModifiedSequence", "EG.ModifiedPeptide"], "charge": ["FG.Charge"],
    "q_value": ["EG.Qvalue"], "frg_mz": ["F.FrgMz"], "frg_type": ["F.FrgType"],
    "frg_num": ["F.FrgNum"], "frg_ion": ["F.FrgIon"], "frg_charge": ["F.Charge", "F.FrgZ"],
    "frg_loss": ["F.FrgLossType"], "frg_peak_area": ["F.PeakArea"],
    "frg_norm_area": ["F.NormalizedPeakArea"], "frg_measured_relint": ["F.MeasuredRelativeIntensity"],
    "frg_predicted_relint": ["F.PredictedRelativeIntensity"],
    "frg_mass_acc_ppm": ["F.CalibratedMassAccuracy (PPM)", "F.CalibratedMassAccuracy_(PPM)"],
}
PREC_MAP = {
    "run": ["R.FileName", "R.Raw File Name"], "stripped_seq": ["PEP.StrippedSequence"],
    "modified_seq": ["EG.ModifiedSequence", "EG.ModifiedPeptide"], "charge": ["FG.Charge"],
    "precursor_mz": ["FG.PrecMz"], "prec_mz_calibrated": ["FG.PrecMzCalibrated", "FG.CalibratedMz"],
    "rt": ["EG.ApexRT"], "rt_predicted": ["EG.RTPredicted"], "irt_empirical": ["EG.iRTEmpirical"],
    "irt_predicted": ["EG.iRTPredicted"], "im": ["EG.IonMobility", "EG.ApexIonMobility"],
    "ms1_iso_measured": ["FG.MS1IsotopeIntensities (Measured)", "FG.MS1IsotopeIntensities_(Measured)"],
    "ms1_iso_rel_measured": ["FG.MS1IsotopeRelativeIntensities (Measured)", "FG.MS1IsotopeRelativeIntensities_(Measured)"],
    "ms1_iso_rel_predicted": ["FG.MS1IsotopeRelativeIntensities (Predicted)", "FG.MS1IsotopeRelativeIntensities_(Predicted)"],
    "ms1_quantity": ["FG.MS1Quantity"], "ms2_quantity": ["FG.MS2Quantity"],
    "prec_window": ["FG.PrecWindow"], "prec_window_number": ["FG.PrecWindowNumber"],
    "xicdbid": ["FG.XICDBID"], "fragment_count": ["FG.FragmentCount"], "q_value": ["EG.Qvalue"],
    "global_q_value": ["EG.GlobalPrecursorQvalue"], "pg_q_value": ["PG.Qvalue"],
    "signal_to_noise": ["EG.SignalToNoise"], "int_corr_score": ["EG.IntCorrScore"],
    "interference_ms1": ["FG.HasPossibleInterference (MS1)", "FG.HasPossibleInterference_(MS1)"],
    "interference_ms2": ["FG.HasPossibleInterference (MS2)", "FG.HasPossibleInterference_(MS2)"],
    "is_decoy": ["EG.IsDecoy"], "missed_cleavages": ["PEP.NrOfMissedCleavages"],
    "is_proteotypic": ["PEP.IsProteotypic"], "ptm_localization": ["EG.PTMLocalizationProbabilities"],
    "protein_group": ["PG.ProteinGroups", "PG.ProteinAccessions"], "genes": ["PG.Genes"],
    "organism": ["PEP.AllOccurringOrganisms"],
}
_NUM = {"charge", "q_value", "frg_mz", "frg_num", "frg_charge", "frg_peak_area", "frg_norm_area",
        "frg_measured_relint", "frg_predicted_relint", "frg_mass_acc_ppm", "precursor_mz",
        "prec_mz_calibrated", "rt", "rt_predicted", "irt_empirical", "irt_predicted", "im",
        "ms1_quantity", "ms2_quantity", "prec_window_number", "xicdbid", "fragment_count",
        "global_q_value", "pg_q_value", "signal_to_noise", "int_corr_score", "missed_cleavages"}
_FRAG_LIST = ["frg_mz", "frg_type", "frg_num", "frg_ion", "frg_charge", "frg_loss",
              "frg_peak_area", "frg_norm_area", "frg_measured_relint", "frg_predicted_relint",
              "frg_mass_acc_ppm"]
_KEYS = ["run", "modified_seq", "charge"]


def _safe(n): return re.sub(r"[^A-Za-z0-9._-]+", "_", n or "search")


def _strip_ts(n):
    """drop a leading Spectronaut export timestamp 'YYYYMMDD_HHMMSS_' so re-exports of the same
    experiment (differing only by timestamp) map to ONE dataset path -> no duplicate datasets."""
    return re.sub(r"^\d{8}_\d{6}_", "", n or "")


def report_name(p):
    b = re.sub(r"_Report_FRAN \(Normal\)\.(parquet|tsv)$", "", os.path.basename(str(p).replace("\\", "/")), flags=re.I)
    return b or os.path.basename(os.path.dirname(str(p).replace("\\", "/")))


def _resolve(header, fmap):
    norm = {s2c._norm(c).lower(): c for c in header}
    low = {c.lower(): c for c in header}
    out = {}
    for field, cands in fmap.items():
        for cand in cands:
            if cand.lower() in low:
                out[field] = low[cand.lower()]; break
            if s2c._norm(cand).lower() in norm:
                out[field] = norm[s2c._norm(cand).lower()]; break
    return out


def _extract(report_path):
    import pandas as pd
    import pyarrow.parquet as pq
    is_pq = str(report_path).lower().endswith(".parquet")
    header = list(pq.read_schema(report_path).names) if is_pq else list(pd.read_csv(report_path, sep="\t", nrows=0).columns)
    fr, pr = _resolve(header, FRAG_MAP), _resolve(header, PREC_MAP)
    if "frg_mz" not in fr:
        return None, None
    want = list(dict.fromkeys(list(fr.values()) + list(pr.values())))
    df = pq.read_table(report_path, columns=want).to_pandas() if is_pq else pd.read_csv(report_path, sep="\t", usecols=want, low_memory=False)

    def build(cmap):
        cols = {f: c for f, c in cmap.items() if c in df.columns}
        out = df[list(cols.values())].copy(); out.columns = list(cols.keys())
        for f in out.columns:
            if f in _NUM:
                out[f] = pd.to_numeric(out[f], errors="coerce")
        return out
    fdf = build(fr)
    if "q_value" in fdf.columns:
        fdf = fdf[fdf["q_value"] <= 0.01]
    fdf = fdf.reset_index(drop=True)
    pdf = build(pr)
    if "q_value" in pdf.columns:
        pdf = pdf[pdf["q_value"] <= 0.01]
    pdf = pdf.drop_duplicates(subset=[c for c in _KEYS if c in pdf.columns], keep="first").reset_index(drop=True)
    return fdf, pdf


def _assemble_table(fdf, pdf):
    """Fold per-fragment rows into per-precursor list columns, merge with precursor extras, and
    build a pyarrow Table matching spectrum_lance.SCHEMA."""
    import pandas as pd
    import numpy as np
    import pyarrow as pa
    present = [c for c in _FRAG_LIST if c in fdf.columns]
    agg = fdf.groupby(_KEYS, sort=False)[present].agg(list).reset_index()
    df = pdf.merge(agg, on=_KEYS, how="left")
    n = len(df)

    def isna(v):
        try:
            return v is None or (isinstance(v, float) and np.isnan(v))
        except Exception:  # noqa: BLE001
            return v is None

    def scalar(col, cast):
        if col not in df.columns:
            return pa.array([None] * n)
        return pa.array([None if isna(v) else cast(v) for v in df[col]])

    def semis(col):  # ";"-joined string -> list<float>
        if col not in df.columns:
            return [None] * n
        return [sln_parse(v) for v in df[col]]

    def flist(col, cast):  # list column with per-element coercion
        if col not in df.columns:
            return [None] * n
        out = []
        for lst in df[col]:
            if not isinstance(lst, (list, tuple)):
                out.append(None)
            else:
                out.append([None if isna(e) else cast(e) for e in lst])
        return out

    cols = {
        "search_id": pa.array([None] * n, pa.string()),
        "search_name": pa.array([None] * n, pa.string()),
        "raw_path": pa.array([None] * n, pa.string()),
        "run": scalar("run", str), "stripped_seq": scalar("stripped_seq", str),
        "modified_seq": scalar("modified_seq", str),
        "charge": scalar("charge", lambda v: int(round(float(v)))),
        "precursor_mz": scalar("precursor_mz", float), "prec_mz_calibrated": scalar("prec_mz_calibrated", float),
        "rt": scalar("rt", float), "rt_predicted": scalar("rt_predicted", float),
        "irt_empirical": scalar("irt_empirical", float), "irt_predicted": scalar("irt_predicted", float),
        "im": scalar("im", float), "q_value": scalar("q_value", float),
        "global_q_value": scalar("global_q_value", float), "pg_q_value": scalar("pg_q_value", float),
        "signal_to_noise": scalar("signal_to_noise", float), "int_corr_score": scalar("int_corr_score", float),
        "ms1_iso_measured": pa.array(semis("ms1_iso_measured"), sln.SCHEMA.field("ms1_iso_measured").type),
        "ms1_iso_rel_measured": pa.array(semis("ms1_iso_rel_measured"), sln.SCHEMA.field("ms1_iso_rel_measured").type),
        "ms1_iso_rel_predicted": pa.array(semis("ms1_iso_rel_predicted"), sln.SCHEMA.field("ms1_iso_rel_predicted").type),
        "ms1_quantity": scalar("ms1_quantity", float), "ms2_quantity": scalar("ms2_quantity", float),
        "prec_window": scalar("prec_window", str),
        "prec_window_number": scalar("prec_window_number", lambda v: int(round(float(v)))),
        "xicdbid": scalar("xicdbid", lambda v: int(round(float(v)))),
        "fragment_count": scalar("fragment_count", lambda v: int(round(float(v)))),
        "interference_ms1": scalar("interference_ms1", lambda v: str(v).lower() in ("true", "1", "1.0")),
        "interference_ms2": scalar("interference_ms2", lambda v: str(v).lower() in ("true", "1", "1.0")),
        "is_decoy": scalar("is_decoy", lambda v: str(v).lower() in ("true", "1", "1.0")),
        "missed_cleavages": scalar("missed_cleavages", lambda v: int(round(float(v)))),
        "is_proteotypic": scalar("is_proteotypic", lambda v: str(v).lower() in ("true", "1", "1.0")),
        "ptm_localization": scalar("ptm_localization", str), "protein_group": scalar("protein_group", str),
        "genes": scalar("genes", str), "organism": scalar("organism", str),
        "frg_mz": pa.array(flist("frg_mz", float), sln.SCHEMA.field("frg_mz").type),
        "frg_type": pa.array(flist("frg_type", str), sln.SCHEMA.field("frg_type").type),
        "frg_num": pa.array(flist("frg_num", lambda v: int(round(float(v)))), sln.SCHEMA.field("frg_num").type),
        "frg_ion": pa.array(flist("frg_ion", str), sln.SCHEMA.field("frg_ion").type),
        "frg_charge": pa.array(flist("frg_charge", lambda v: int(round(float(v)))), sln.SCHEMA.field("frg_charge").type),
        "frg_loss": pa.array(flist("frg_loss", str), sln.SCHEMA.field("frg_loss").type),
        "frg_peak_area": pa.array(flist("frg_peak_area", float), sln.SCHEMA.field("frg_peak_area").type),
        "frg_norm_area": pa.array(flist("frg_norm_area", float), sln.SCHEMA.field("frg_norm_area").type),
        "frg_measured_relint": pa.array(flist("frg_measured_relint", float), sln.SCHEMA.field("frg_measured_relint").type),
        "frg_predicted_relint": pa.array(flist("frg_predicted_relint", float), sln.SCHEMA.field("frg_predicted_relint").type),
        "frg_mass_acc_ppm": pa.array(flist("frg_mass_acc_ppm", float), sln.SCHEMA.field("frg_mass_acc_ppm").type),
    }
    tbl = pa.table({k: cols[k] for k in sln.SCHEMA.names}).cast(sln.SCHEMA)
    n_frag = int(sum(len(x) for x in flist("frg_mz", float) if x))
    return tbl, n_frag


def sln_parse(s):
    import numpy as np
    if s is None or (isinstance(s, float) and np.isnan(s)):
        return None
    s = str(s).strip()
    if not s or s.lower() in ("nan", "none"):
        return None
    out = []
    for p in s.split(";"):
        try:
            out.append(float(p.strip()))
        except (TypeError, ValueError):
            out.append(None)
    return out or None


def process_one(report_path, out_dir, dry=False, resume=True):
    """Parse one report -> Lance dataset. Returns (name, lance_path, n_prec, n_frag, md5, version).
    n_prec == -1 means SKIPPED (dataset already exists, resume mode). No DB here (DB writes are
    done by the parent so they stay serialized/paced)."""
    import pyarrow as pa  # noqa: F401
    name = report_name(report_path)
    lance_path = os.path.join(out_dir, f"{_safe(_strip_ts(name))}.lance")
    # RESUME: if this experiment's dataset already exists, skip BEFORE the expensive parse — so a
    # re-run only touches newly-arrived reports (already-done datasets are already registered).
    if resume and not dry and os.path.isdir(lance_path) and os.listdir(lance_path):
        return (name, lance_path, -1, -1, None, None)
    fdf, pdf = _extract(report_path)
    if fdf is None or fdf.empty or pdf is None or pdf.empty:
        return (name, None, 0, 0, None, None)
    tbl, n_frag = _assemble_table(fdf, pdf)
    name_arr = pa.array([name] * tbl.num_rows, pa.string())
    tbl = tbl.set_column(tbl.schema.get_field_index("search_name"), "search_name", name_arr)
    n_prec = tbl.num_rows
    if dry:
        return (name, None, n_prec, n_frag, None, None)
    # dataset named by the timestamp-stripped experiment name so re-exports collapse to one.
    _, md5, version = sln.write_lance(tbl, lance_path, mode="overwrite")
    return (name, lance_path, n_prec, n_frag, md5, version)


def _find_reports(root):
    for dp, _, fns in os.walk(root):
        for fn in fns:
            if fn.lower().endswith((".parquet", ".tsv")) and "report_fran" in fn.lower():
                yield os.path.join(dp, fn)


def _pg_conn():
    from refresh_leaderboards import _token
    import psycopg2
    return psycopg2.connect(host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
                            dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
                            user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
                            password=_token(), sslmode="require", connect_timeout=30)


def _register(conn, name, res):
    """res = (name, lance_path, n_prec, n_frag, md5, version). Resolve search_id via provenance."""
    _, lpath, n_prec, n_frag, md5, ver = res
    cur = conn.cursor()
    cur.execute("""SELECT s.id FROM delimp_searches s
                   LEFT JOIN delimp_search_provenance p ON p.search_id=s.id
                   WHERE p.real_search_name=%s OR s.search_name=%s LIMIT 1""", (name, name))
    row = cur.fetchone()
    sid = row[0] if row else None
    sln.register(conn, sid, name, lpath, n_prec, n_frag, md5, ver)
    return sid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("report", nargs="?")
    ap.add_argument("--scan")
    ap.add_argument("--out-dir", default="./spectra_lance")
    ap.add_argument("--register", action="store_true")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--no-resume", action="store_true", help="reprocess every report even if its Lance dataset already exists")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    resume = not a.no_resume
    reports = ([a.report] if a.report else []) + (list(_find_reports(a.scan)) if a.scan else [])
    if not reports:
        sys.exit("give a report path or --scan <dir>")
    if a.limit:
        reports = reports[:a.limit]
    if not a.dry_run:
        os.makedirs(a.out_dir, exist_ok=True)
    print(f"{len(reports)} report(s) -> {a.out_dir}  (workers={a.workers}, register={a.register})")

    conn = None
    if a.register and not a.dry_run:
        conn = _pg_conn(); sln.ensure_registry(conn)

    tp = tf = done = skipped = 0

    def handle(res):
        nonlocal tp, tf, done, skipped
        name, lpath, n_prec, n_frag, md5, ver = res
        if n_prec == -1:   # resume: dataset already built (already registered by the prior run)
            skipped += 1; return
        if not n_prec:
            print(f"  [skip] {name}: no fragment-level rows"); return
        tp += n_prec; tf += n_frag; done += 1
        note = f"{n_prec:,} precursors / {n_frag:,} fragments"
        if lpath and conn is not None:
            try:
                sid = _register(conn, name, res)
                note += f"  registered{'' if sid else ' (no search match)'}"
            except Exception as e:  # noqa: BLE001
                conn.rollback(); note += f"  [warn] register: {str(e)[:70]}"
        print(f"  {name}: {note}")

    if a.workers > 1 and len(reports) > 1:
        # max_tasks_per_child=1: recycle each worker after one report so the memory a big report
        # builds (agg(list) over millions of fragments) is fully freed before the next -> no
        # accumulation across reports (the OOM cause at higher worker counts).
        with ProcessPoolExecutor(max_workers=a.workers, max_tasks_per_child=1) as ex:
            futs = {ex.submit(process_one, rp, a.out_dir, a.dry_run, resume): rp for rp in reports}
            for i, fut in enumerate(as_completed(futs), 1):
                try:
                    handle(fut.result())
                except Exception as e:  # noqa: BLE001
                    print(f"  [warn] {os.path.basename(futs[fut])}: {str(e)[:100]}")
                if i % 25 == 0:
                    print(f"[{i}/{len(reports)}] {done} new, {skipped} skipped, {tf:,} fragments so far")
    else:
        for i, rp in enumerate(reports, 1):
            try:
                handle(process_one(rp, a.out_dir, a.dry_run, resume))
            except Exception as e:  # noqa: BLE001
                print(f"  [warn] {os.path.basename(rp)}: {str(e)[:100]}")
            if i % 25 == 0:
                print(f"[{i}/{len(reports)}] {done} new, {skipped} skipped, {tf:,} fragments so far")

    print(f"DONE: {done} new Lance datasets ({skipped} already existed, skipped), "
          f"{tp:,} precursors / {tf:,} fragments -> {a.out_dir}")
    if conn is not None:
        conn.close()


if __name__ == "__main__":
    main()
