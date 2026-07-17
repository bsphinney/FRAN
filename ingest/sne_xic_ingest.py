"""sne_xic_ingest.py — ingest Spectronaut "All XICs" SQLite databases into FRAN so the
peptide-page dual-pane XIC viewer works for Spectronaut data too (the counterpart to the
DIA-NN xic_ingest.py).

Spectronaut stores the chromatograms separately from the report (Spectronaut 21 manual,
Appendix 9 "All XIC database export"): one SQLite db PER RAW FILE, with three tables —
  IonTraces(FGID, IonRank, MSLevel, <Intensity bytes>, RTAxis_XOffset, RTAxis_Length, RTAxis_ID, Run_ID)
  RTAxis(ID, <RT bytes>)            Run(ID, RawFileName)
Intensity/RT arrays are base64-encoded little-endian float32. Fragment identities come from
the db's IonLabel column (e.g. 'y4+'); the report supplies each precursor's peptide identity
(via FGID == FG.XICDBID) and the exact fragment m/z. So this needs BOTH:
  --report  the FRAN Spectronaut report TSV (must include FG.XICDBID + the F.Frg* columns), and
  --xic-dir the folder of *.xic.db / *.sqlite XIC dbs (from "Export All XIC" / --setXICExportDirectory).

Like the DIA-NN path, the stored trace per precursor is the APEX-ALIGNED AVERAGE across all
runs that saw it (a denoised 'typical acquired XIC'). Writes the SAME delimp_precursor_xic /
delimp_xic_quant / delimp_search_sources tables, so the peptide page reads it unchanged.

Usage:
  python sne_xic_ingest.py --report "Chase-set1_Report_FRAN (Normal).tsv" \
      --xic-dir ./xics --search-id chase_set1 --pg
  add --dry-run to parse + summarize without writing.
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import re
import sqlite3
import struct
import sys

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from xic_ingest import AVG_W, AVG_K, DDL, QUANT_DDL, SOURCES_DDL, _avg_on_grid, _apex_rt  # noqa: E402
import spectronaut_to_corpus as s2c  # noqa: E402


# ---- report column resolution (reuse s2c, add the XIC-link columns) ----
# Fragment labels come straight from the XIC db's IonLabel column, so we no longer need
# F.Rank. The report is still needed to map FGID -> peptide identity and exact fragment m/z.
_EXTRA = {
    "xicdbid":  [r"^FG\.XICDBID$", r"XICDBID$"],
    "frg_excl": [r"^F\.ExcludedFromQuantification$", r"ExcludedFromQuant"],
    "frg_rel":  [r"^F\.MeasuredRelativeIntensity$", r"^F\.NormalizedPeakArea$", r"MeasuredRelativeIntensity$"],
}

# XIC-db IonLabel -> normalized FRAN label. MS2: 'y4+' / 'b5++' (trailing +'s = charge).
_LBL = re.compile(r"^([abcxyz])(\d+)(\++)$", re.I)


def _norm_label(ionlabel):
    """('y4+') -> ('y4^1','y',4,1); returns None for MS1/non-b-y labels."""
    if not ionlabel:
        return None
    m = _LBL.match(str(ionlabel).strip())
    if not m:
        return None
    z = len(m.group(3)) or 1
    return f"{m.group(1).lower()}{m.group(2)}^{z}", m.group(1).lower(), int(m.group(2)), z


def _resolve(header):
    cols = s2c.resolve_columns(header)
    for field, pats in _EXTRA.items():
        hit = s2c.match_column(header, pats)   # matches TSV dotted OR parquet underscored
        if hit:
            cols[field] = hit
    return cols


def _f(row, cols, field):
    if field not in cols:
        return None
    v = row.get(cols[field])
    try:
        return float(v) if pd.notna(v) else None
    except (TypeError, ValueError):
        return None


def parse_report(path, q_max=0.01):
    """Build xicdbid -> {preckey, meta, frags{label: {mz,exclude,rel,type,series,charge}}}.
    preckey groups the same precursor across runs (modified-seq + charge) for cross-run
    averaging. Fragments are keyed by normalized label (e.g. 'y4^1') to match the db IonLabel;
    the report supplies exact m/z + quant-exclusion per fragment."""
    header = s2c.report_columns(path)
    cols = _resolve(header)
    need = ["run", "stripped_seq", "charge", "xicdbid", "frg_type", "frg_num"]
    missing = [n for n in need if n not in cols]
    if missing:
        raise ValueError(f"report missing columns for {missing} (need FG.XICDBID + F.Frg*); resolved={cols}")
    usecols = list(dict.fromkeys(cols[k] for k in cols))
    idx = {}
    for chunk in s2c.iter_chunks(path, usecols, 200_000):
        for _, r in chunk.iterrows():
            qv = r.get(cols["q_value"]) if "q_value" in cols else None
            if qv is not None and pd.notna(qv) and float(qv) > q_max:
                continue
            xid = r.get(cols["xicdbid"])
            if pd.isna(xid):
                continue
            xid = int(xid)
            ftype = str(r.get(cols["frg_type"])).lower()
            fnum = int(r[cols["frg_num"]]) if pd.notna(r.get(cols["frg_num"])) else 0
            fz = int(r[cols["frg_charge"]]) if ("frg_charge" in cols and pd.notna(r.get(cols["frg_charge"]))) else 1
            label = f"{ftype}{fnum}^{fz}"
            e = idx.get(xid)
            if e is None:
                modseq = r.get(cols["modified_seq"]) if "modified_seq" in cols else None
                stripped = s2c._strip_seq(modseq, r.get(cols.get("stripped_seq", "")))
                charge = int(r[cols["charge"]]) if pd.notna(r.get(cols["charge"])) else 0
                proforma = s2c._to_proforma(modseq) or stripped
                e = idx[xid] = {
                    "preckey": f"{proforma}{charge}",
                    "meta": {"stripped": stripped, "modseq": str(modseq) if modseq is not None else stripped,
                             "charge": charge, "precursor_mz": _f(r, cols, "precursor_mz")},
                    "frags": {}}
            e["frags"][label] = {
                "label": label, "type": ftype, "series": fnum, "charge": fz,
                "mz": _f(r, cols, "frg_mz"), "rel_intensity": _f(r, cols, "frg_rel"),
                "exclude_from_quant": int(r[cols["frg_excl"]]) if ("frg_excl" in cols and pd.notna(r.get(cols["frg_excl"]))) else 0,
            }
    return idx


# ---- SQLite XIC decoding ----
def _pick(cols, *subs):
    for c in cols:
        cl = c.lower()
        if all(x in cl for x in subs):
            return c
    return None


def _decode(b64):
    if not b64:
        return []
    if isinstance(b64, bytes):
        b64 = b64.decode("ascii", "ignore")
    out = []
    for piece in str(b64).split(","):
        piece = piece.strip()
        if not piece:
            continue
        raw = base64.b64decode(piece)
        n = len(raw) // 4
        if n:
            out.extend(struct.unpack(f"<{n}f", raw))
    return out


def read_xic_db(db_path, keep_fgids):
    """Yield (fgid, ms1_traces[list], frag_traces{label:[{rt,i}]}) for each KEPT precursor in
    one run db. Each db holds millions of IonTraces rows; we skip (without decoding) any FGID
    that isn't a q<=0.01 precursor in the report. Fragment labels come from IonLabel."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    try:
        it_cols = [r[1] for r in con.execute("PRAGMA table_info(IonTraces)")]
        rt_cols = [r[1] for r in con.execute("PRAGMA table_info(RTAxis)")]
        if not it_cols or not rt_cols:
            return
        c_int = _pick(it_cols, "inten", "byte") or "IntensityDataBytes"
        c_fg = _pick(it_cols, "fgid") or "FGID"
        c_lbl = _pick(it_cols, "ionlabel") or _pick(it_cols, "label") or "IonLabel"
        c_lvl = _pick(it_cols, "mslevel") or "MSLevel"
        c_xoff = _pick(it_cols, "xoffset") or "RTAxis_XOffset"
        c_len = _pick(it_cols, "length") or "RTAxis_Length"
        c_rtid = _pick(it_cols, "rtaxis", "id") or "RTAxis_ID"
        rt_id = _pick(rt_cols, "id") or "ID"
        rt_bytes = _pick(rt_cols, "rt", "byte") or _pick(rt_cols, "byte") or "RTDataBytes"
        rtaxis = {row[0]: _decode(row[1]) for row in con.execute(f"SELECT {rt_id},{rt_bytes} FROM RTAxis")}
        cur = con.execute(
            f"SELECT {c_fg},{c_lvl},{c_lbl},{c_int},{c_xoff},{c_len},{c_rtid} FROM IonTraces ORDER BY {c_fg}")
        cur_fg, ms1, frags = None, [], {}
        for fg, lvl, lbl, ib, xoff, length, rtid in cur:
            if fg not in keep_fgids:        # skip BEFORE the expensive base64 decode
                continue
            if fg != cur_fg:
                if cur_fg is not None:
                    yield cur_fg, ms1, frags
                cur_fg, ms1, frags = fg, [], {}
            inten = _decode(ib)
            if not inten:
                continue
            rtfull = rtaxis.get(rtid, [])
            xoff = int(xoff or 0); length = int(length or len(inten))
            rt = rtfull[xoff:xoff + length]
            trace = [{"rt": round(float(t), 5), "i": float(v)} for t, v in zip(rt, inten) if v is not None]
            if int(lvl) == 1:
                ms1.append(trace)              # MS1 isotopes (mono-isotopic, M+1, ...)
            else:
                nl = _norm_label(lbl)
                if nl:
                    frags[nl[0]] = trace        # key by normalized label e.g. 'y4^1'
        if cur_fg is not None:
            yield cur_fg, ms1, frags
    finally:
        con.close()


def _best_ms1(traces):
    """pick the highest-apex MS1 trace for a run (DIA-NN stores one MS1 chromatogram)."""
    best, bv = None, -1.0
    for tr in traces:
        a = max((p["i"] for p in tr), default=0.0)
        if a > bv:
            best, bv = tr, a
    return best or []


def build_records(report_idx, xic_dbs, search_id):
    keep = set(report_idx.keys())
    acc = {}  # preckey -> {ms1:[traces], labels:{label:[traces]}, apex, rt_apex, n, meta, fmeta:{label:fd}}
    for db in xic_dbs:
        for fg, ms1_traces, frag_traces in read_xic_db(db, keep):
            rep = report_idx.get(int(fg))
            if not rep:
                continue
            pk = rep["preckey"]
            a = acc.setdefault(pk, {"ms1": [], "labels": {}, "apex": 0.0, "rt_apex": None,
                                    "n": 0, "meta": rep["meta"], "fmeta": {}})
            a["n"] += 1
            ms1 = _best_ms1(ms1_traces)
            if ms1:
                a["ms1"].append(ms1)
                ap = max((p["i"] for p in ms1), default=0.0)
                if ap > a["apex"]:
                    a["apex"] = ap; a["rt_apex"] = _apex_rt(ms1)
            for label, trace in frag_traces.items():
                if not trace:
                    continue
                fd = rep["frags"].get(label)   # report supplies exact m/z + quant-exclusion
                if not fd:                     # label in db but not report -> minimal meta
                    parts = label.split("^")
                    fd = {"label": label, "type": parts[0][0], "series": int(parts[0][1:] or 0),
                          "charge": int(parts[1]) if len(parts) > 1 else 1, "mz": None,
                          "rel_intensity": None, "exclude_from_quant": 0}
                a["labels"].setdefault(label, []).append(trace)
                a["fmeta"][label] = fd
    grid = np.linspace(-AVG_W, AVG_W, AVG_K)
    for pk, a in acc.items():
        if not a["ms1"]:
            continue
        ms1_avg = _avg_on_grid(a["ms1"], grid) or []
        frags, quant = [], []
        for label, fd in a["fmeta"].items():
            avg = _avg_on_grid(a["labels"].get(label, []), grid) or []
            if fd["exclude_from_quant"] == 0:
                quant.append(label)
            frags.append({"label": label, "type": fd["type"], "series": fd["series"],
                          "charge": fd["charge"], "mz": fd["mz"], "rel_intensity": fd["rel_intensity"],
                          "exclude_from_quant": fd["exclude_from_quant"], "score": None,
                          "apex": max((p["i"] for p in avg), default=0.0), "trace": avg})
        frags.sort(key=lambda f: (f["rel_intensity"] if f["rel_intensity"] is not None else f["apex"]), reverse=True)
        m = a["meta"]
        yield {"search_id": search_id, "precursor_id": pk, "stripped_seq": m["stripped"],
               "charge": m["charge"], "precursor_mz": m["precursor_mz"], "raw_path": f"avg of {a['n']} runs",
               "rt_apex": a["rt_apex"], "ms1_apex": a["apex"], "ms1": ms1_avg, "fragments": frags,
               "quant_labels": quant, "n_fragments_total": len(frags)}


def _sn_version(report_path):
    for name in ("Chase-set1_AnalysisLog.txt",):  # best-effort; scan any *AnalysisLog*.txt nearby
        pass
    for log in glob.glob(os.path.join(os.path.dirname(report_path), "*[Aa]nalysis*og*.txt")):
        try:
            m = re.search(r"Spectronaut\s+v?([0-9]+\.[0-9]+(?:\.[0-9]+)?)", open(log, errors="replace").read(8000))
            if m:
                return m.group(1)
        except OSError:
            continue
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True, help="FRAN Spectronaut report TSV (needs FG.XICDBID + F.Rank)")
    ap.add_argument("--xic-dir", required=True, help="folder with the *.sqlite XIC dbs (one per raw file)")
    ap.add_argument("--search-id", required=True)
    ap.add_argument("--pg", action="store_true", help="write to PG Farm")
    ap.add_argument("--duckdb", help="path to a DuckDB sink instead")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    dbs = sorted(glob.glob(os.path.join(a.xic_dir, "*.sqlite")) + glob.glob(os.path.join(a.xic_dir, "*.db")))
    if not dbs:
        sys.exit(f"no *.sqlite XIC dbs in {a.xic_dir}")
    print(f"report: {os.path.basename(a.report)} · {len(dbs)} XIC db(s)")
    report_idx = parse_report(a.report)
    print(f"{len(report_idx):,} precursor×run XIC links in report")
    ver = _sn_version(a.report)
    print(f"engine: Spectronaut {ver or '(version not found)'}")
    recs = list(build_records(report_idx, dbs, a.search_id))
    print(f"{len(recs):,} precursors with averaged XIC")
    if recs:
        e = recs[0]
        print(f"  e.g. {e['stripped_seq']}{e['charge']}+ : {e['n_fragments_total']} frags, "
              f"{len(e['quant_labels'])} quant, {len(e['ms1'])} MS1 pts")
    if a.dry_run:
        print("dry-run: nothing written"); return
    if not recs:
        sys.exit("no records (do report FG.XICDBID and the SQLite FGIDs match?)")

    meas = [(r["precursor_id"], r["stripped_seq"], r["charge"], r["precursor_mz"], r["raw_path"],
             r["search_id"], "spectronaut", ver, r["rt_apex"], r["ms1_apex"], json.dumps(r["ms1"]),
             json.dumps(r["fragments"]), r["n_fragments_total"]) for r in recs]
    quant = [(r["search_id"], r["precursor_id"], r["stripped_seq"], r["charge"],
              json.dumps(r["quant_labels"])) for r in recs]
    src = [(a.search_id, "xic_sqlite", os.path.abspath(db), "local", os.path.getsize(db),
            json.dumps(["ms1_xic", "fragment_xic"]), json.dumps([])) for db in dbs]

    if a.duckdb:
        import duckdb
        con = duckdb.connect(a.duckdb)
        for ddl in (DDL, QUANT_DDL, SOURCES_DDL):
            con.execute(ddl)
        con.executemany(
            """INSERT INTO delimp_precursor_xic VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (precursor_id) DO UPDATE SET
                 stripped_seq=excluded.stripped_seq, charge=excluded.charge, precursor_mz=excluded.precursor_mz,
                 raw_path=excluded.raw_path, search_id=excluded.search_id, engine=excluded.engine,
                 engine_version=excluded.engine_version, rt_apex=excluded.rt_apex, ms1_apex=excluded.ms1_apex,
                 ms1=excluded.ms1, fragments=excluded.fragments, n_fragments_total=excluded.n_fragments_total
               WHERE excluded.ms1_apex > delimp_precursor_xic.ms1_apex""", meas)
        con.execute("DELETE FROM delimp_xic_quant WHERE search_id = ?", [a.search_id])
        con.executemany("INSERT INTO delimp_xic_quant VALUES (?,?,?,?,?)", quant)
        con.execute("DELETE FROM delimp_search_sources WHERE search_id = ?", [a.search_id])
        con.executemany("INSERT INTO delimp_search_sources (search_id,file_role,path,host,bytes,extracted,available_features) VALUES (?,?,?,?,?,?,?)", src)
        con.close()
        print(f"upserted {len(meas)} measurements -> {a.duckdb}")
    elif a.pg:
        import psycopg2, psycopg2.extras
        from refresh_leaderboards import _token
        con = psycopg2.connect(host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
            dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
            user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
            password=_token(), sslmode="require", connect_timeout=30)
        cur = con.cursor()
        for ddl in (DDL, QUANT_DDL, SOURCES_DDL):
            cur.execute(ddl.replace("JSON", "JSONB").replace("DOUBLE", "DOUBLE PRECISION"))
        psycopg2.extras.execute_values(cur,
            """INSERT INTO delimp_precursor_xic VALUES %s
               ON CONFLICT (precursor_id) DO UPDATE SET
                 stripped_seq=excluded.stripped_seq, charge=excluded.charge, precursor_mz=excluded.precursor_mz,
                 raw_path=excluded.raw_path, search_id=excluded.search_id, engine=excluded.engine,
                 engine_version=excluded.engine_version, rt_apex=excluded.rt_apex, ms1_apex=excluded.ms1_apex,
                 ms1=excluded.ms1, fragments=excluded.fragments, n_fragments_total=excluded.n_fragments_total
               WHERE excluded.ms1_apex > delimp_precursor_xic.ms1_apex""", meas, page_size=500)
        cur.execute("DELETE FROM delimp_xic_quant WHERE search_id = %s", (a.search_id,))
        psycopg2.extras.execute_values(cur, "INSERT INTO delimp_xic_quant VALUES %s", quant, page_size=500)
        cur.execute("DELETE FROM delimp_search_sources WHERE search_id = %s", (a.search_id,))
        psycopg2.extras.execute_values(cur,
            "INSERT INTO delimp_search_sources (search_id,file_role,path,host,bytes,extracted,available_features) VALUES %s",
            src, page_size=200)
        con.commit(); con.close()
        print(f"upserted {len(meas)} measurements + {len(quant)} quant rows -> PG Farm")
    else:
        sys.exit("choose a sink: --pg or --duckdb PATH")


if __name__ == "__main__":
    main()
