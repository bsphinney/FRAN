"""Ingest DIA-NN XIC + spectral-library data into FRAN as COMPACT, queryable records
(PG Farm or a portable DuckDB file) — never leaving the bulky raw *.xic.parquet around.

For each precursor in a DIA-NN search this stores, AI-training-complete:
  - the full annotated fragment SPECTRUM (every fragment: m/z, b/y type, series, charge,
    relative intensity, fragment score, and whether the engine used it for quant), and
  - real XIC TRACES (intensity vs RT) for the MS1 precursor and the quant fragments —
    the dual-pane (MS1 / fragments) chromatogram, straight from DIA-NN --xic output.

Because different searches quantify on different fragments, we keep each search's quant
set per precursor; the FRAN endpoint then reports the top-5 MOST COMMON quant fragments
across searches + each fragment's usage %.

Inputs (a DIA-NN result dir): report-lib.parquet (fragments + Exclude.From.Quant) and
either report_xic/*.xic.parquet or a flat *.xic.parquet (the --xic traces).

Sinks:  --duckdb fran_xic.duckdb   (portable, transferable)   OR   --pg  (PG Farm, live corpus)
Usage:
  python xic_ingest.py --dir /path/to/diann_out --search-id savannah_nov2025 --duckdb fran_xic.duckdb
  python xic_ingest.py --dir /path/to/diann_out --search-id mysearch --pg            # writes to PG Farm
  add --dry-run to parse + summarize without writing.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

import pandas as pd

_FRAG_RE = re.compile(r"^([abcxyz])(\d+)\^(\d+)$", re.I)
MAX_PTS = 40  # cap trace length (DIA-NN windows are ~15-25 pts; guards pathological cases)

# --- theoretical fragment m/z (for the xic-only path, when no report-lib is present) ---
_RES = {"G":57.02146,"A":71.03711,"S":87.03203,"P":97.05276,"V":99.06841,"T":101.04768,
        "C":103.00919,"L":113.08406,"I":113.08406,"N":114.04293,"D":115.02694,"Q":128.05858,
        "K":128.09496,"E":129.04259,"M":131.04049,"H":137.05891,"F":147.06841,"R":156.10111,
        "Y":163.06333,"W":186.07931}
_WATER, _PROTON, _CAM = 18.0105646, 1.0072765, 57.02146  # CAM = carbamidomethyl (standard fixed Cys mod)


def _strip_pr(pr):
    """DIA-NN Precursor.Id = modified-seq + charge, e.g. '(UniMod:1)AAAAAAGAGPEMVR2'.
    Return (stripped_seq, charge)."""
    pr = str(pr)
    m = re.match(r"^(.*?)(\d+)$", pr)
    seq, charge = (m.group(1), int(m.group(2))) if m else (pr, 0)
    stripped = re.sub(r"\([^)]*\)", "", seq)            # drop (UniMod:X) tokens
    stripped = re.sub(r"[^A-Za-z]", "", stripped).upper()
    return stripped, charge


def _frag_mz(stripped, typ, series, z):
    """Theoretical singly/multiply-charged b/y ion m/z (Cys assumed carbamidomethyl)."""
    if not stripped or series < 1 or series >= len(stripped) + 1:
        return None
    res = [_RES.get(c, 0.0) + (_CAM if c == "C" else 0.0) for c in stripped]
    if typ == "b":
        neutral = sum(res[:series])
    else:  # y
        neutral = sum(res[len(stripped) - series:]) + _WATER
    if neutral <= 0:
        return None
    return round((neutral + z * _PROTON) / z, 5)

# Provenance: pointers to the ORIGINAL DIA-NN files per search, with what we've already
# extracted vs what's still available, so we can re-extract features we skipped (e.g. the
# ion-mobility mobilograms) later WITHOUT re-running the search. Keeps "everything for AI
# training" reachable even though we only store compact derived records.
SOURCES_DDL = """
CREATE TABLE IF NOT EXISTS delimp_search_sources (
    search_id           TEXT NOT NULL,
    file_role           TEXT NOT NULL,   -- report | report_lib | xic | ms1_mobilogram | ms2_mobilogram | speclib | raw | fasta
    path                TEXT NOT NULL,   -- absolute path on the storage host
    host                TEXT,            -- where 'path' resolves (hive | flinders | dataarchive | local)
    bytes               BIGINT,
    extracted           JSON,            -- features already pulled into the corpus, e.g. ["fragments","xic_traces"]
    available_features  JSON,            -- features present but NOT yet extracted, e.g. ["ion_mobility_mobilogram"]
    recorded_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (search_id, file_role, path)
)
"""

# what each DIA-NN artifact CAN yield, and what this ingester actually pulls from it
_SOURCE_SPEC = {
    "report":         {"glob": ["report.parquet"],            "avail": ["precursors", "rt", "im", "irt", "quant"], "got": []},
    "report_lib":     {"glob": ["report-lib.parquet", "*report-lib*.parquet"], "avail": ["fragment_spectrum", "fragment_scores"], "got": ["fragment_spectrum", "fragment_scores"]},
    "xic":            {"glob": ["report_xic/*.xic.parquet", "*.xic.parquet"],  "avail": ["ms1_xic", "fragment_xic"], "got": ["ms1_xic", "fragment_xic"]},
    "ms1_mobilogram": {"glob": ["report_xic/*.ms1_mobilogram.parquet", "*.ms1_mobilogram.parquet"], "avail": ["ms1_ion_mobility_mobilogram"], "got": []},
    "ms2_mobilogram": {"glob": ["report_xic/*.ms2_mobilogram.parquet", "*.ms2_mobilogram.parquet"], "avail": ["fragment_ion_mobility_mobilogram"], "got": []},
    "speclib":        {"glob": ["*.predicted.speclib", "*.skyline.speclib"],   "avail": ["library_alt_format"], "got": []},
}


def _scan_sources(d: str, host: str = "dataarchive") -> list[tuple]:
    """Record pointers to every recognized DIA-NN artifact in the result dir."""
    rows = []
    for role, spec in _SOURCE_SPEC.items():
        seen = set()
        for pat in spec["glob"]:
            for path in glob.glob(os.path.join(d, pat)):
                if path in seen:
                    continue
                seen.add(path)
                try:
                    sz = os.path.getsize(path)
                except OSError:
                    sz = None
                # available-but-not-extracted = what the file offers minus what we pulled
                not_extracted = [f for f in spec["avail"] if f not in spec["got"]]
                rows.append((role, path, host, sz, spec["got"], not_extracted))
    return rows


# MEASUREMENT (deduplicated): ONE representative XIC per precursor (the physical
# chromatogram is the same regardless of which FASTA was searched), keyed by precursor_id
# = DIA-NN Precursor.Id (modified-seq + charge). Re-searching the same raw with a different
# database upserts here (keeps the highest-MS1-apex representative) -> NO duplicate traces.
DDL = """
CREATE TABLE IF NOT EXISTS delimp_precursor_xic (
    precursor_id       TEXT NOT NULL,  -- DIA-NN Precursor.Id = the precursor grain
    stripped_seq       TEXT NOT NULL,  -- for lookup/aggregation by bare sequence
    charge             INTEGER NOT NULL,
    precursor_mz       REAL,
    raw_path           TEXT,           -- provenance: representative run the traces came from
    search_id          TEXT,           -- provenance: search the representative came from
    engine             TEXT,           -- search engine (e.g. diann)
    engine_version     TEXT,           -- engine version that produced the library (e.g. 2.3.0)
    rt_apex            REAL,
    ms1_apex           DOUBLE,         -- MS1 apex intensity (used to pick the representative)
    ms1                JSON,           -- [{rt,i}] MS1 precursor chromatogram
    fragments          JSON,           -- ALL fragments: [{label,type,series,charge,mz,rel_intensity,exclude_from_quant,score,apex,trace:[{rt,i}]}]
    n_fragments_total  INTEGER,
    PRIMARY KEY (precursor_id)
)
"""

# INTERPRETATION overlay (one row per search × precursor): which fragments THIS search/DB
# quantified on. Drives the "usage % across searches" side list. Tiny (just labels).
QUANT_DDL = """
CREATE TABLE IF NOT EXISTS delimp_xic_quant (
    search_id     TEXT NOT NULL,
    precursor_id  TEXT NOT NULL,
    stripped_seq  TEXT NOT NULL,
    charge        INTEGER NOT NULL,
    quant_labels  JSON,
    PRIMARY KEY (search_id, precursor_id)
)
"""


# columns _lib_quant uses. _LIB_REQ are REQUIRED for the FULL (real m/z + rel-intensity +
# quant-subset) path; Fragment.Score / Exclude.From.Quant are OPTIONAL (defaulted per-row via
# .get). A refined DIA-NN library (report-lib.parquet OR a *_lib_final.parquet) has all of
# _LIB_REQ + Exclude.From.Quant even if it omits Fragment.Score; a pure predicted speclib won't.
_LIB_REQ = ["Precursor.Id", "Stripped.Sequence", "Precursor.Charge", "Precursor.Mz",
            "Product.Mz", "Relative.Intensity", "Fragment.Type", "Fragment.Series.Number",
            "Fragment.Charge"]
_LIB_OPT = ["Fragment.Score", "Exclude.From.Quant"]


def _lib_cols(lib_path: str) -> set:
    import pyarrow.parquet as pq
    return set(pq.read_schema(lib_path).names)


def _lib_is_usable(lib_path: str) -> bool:
    """True iff the parquet has what _lib_quant needs for the FULL path (all _LIB_REQ +
    Exclude.From.Quant). Lets us auto-fall-back to the XIC-only path for a bare predicted speclib."""
    cols = _lib_cols(lib_path)
    return all(c in cols for c in _LIB_REQ) and "Exclude.From.Quant" in cols


def _lib_quant(lib_path: str) -> dict:
    """precursor_id -> {label -> {mz, rel_intensity, type, series, charge, score, exclude}}"""
    present = _lib_cols(lib_path)
    cols = [c for c in (_LIB_REQ + _LIB_OPT) if c in present]   # read only columns that exist
    lib = pd.read_parquet(lib_path, columns=cols)
    out = {}
    for pid, g in lib.groupby("Precursor.Id"):
        frs = {}
        for _, r in g.iterrows():
            label = f"{r['Fragment.Type']}{int(r['Fragment.Series.Number'])}^{int(r['Fragment.Charge'])}"
            frs[label] = {"mz": float(r["Product.Mz"]), "rel_intensity": float(r["Relative.Intensity"]),
                          "type": str(r["Fragment.Type"]).lower(), "series": int(r["Fragment.Series.Number"]),
                          "charge": int(r["Fragment.Charge"]), "score": float(r.get("Fragment.Score") or 0),
                          "exclude_from_quant": int(r.get("Exclude.From.Quant") or 0)}
        out[str(pid)] = {"stripped": str(g["Stripped.Sequence"].iloc[0]),
                         "charge": int(g["Precursor.Charge"].iloc[0]),
                         "mz": float(g["Precursor.Mz"].iloc[0]), "frags": frs}
    return out


def _xic_traces(xic_path: str) -> dict:
    """precursor_id -> {'ms1':[{rt,i}], labels:{label:[{rt,i}]}, 'apex':ms1_apex}"""
    dx = pd.read_parquet(xic_path, columns=["pr", "feature", "rt", "value"])
    dx["feature"] = dx["feature"].astype(str)
    out = {}
    for pid, g in dx.groupby("pr"):
        ms1_rows = g[g["feature"] == "ms1"].sort_values("rt")
        ms1 = [{"rt": round(float(a), 5), "i": float(b)} for a, b in zip(ms1_rows["rt"], ms1_rows["value"])][:MAX_PTS]
        labels = {}
        for feat in g["feature"].unique():
            if not _FRAG_RE.match(feat):
                continue
            t = g[g["feature"] == feat].sort_values("rt")
            labels[feat] = [{"rt": round(float(a), 5), "i": float(b)} for a, b in zip(t["rt"], t["value"])][:MAX_PTS]
        out[str(pid)] = {"ms1": ms1, "labels": labels,
                         "apex": max((p["i"] for p in ms1), default=0.0)}
    return out


AVG_W = 0.5   # ± minutes window around apex for the averaged ("typical") XIC
AVG_K = 41    # points on the apex-aligned grid


def _apex_rt(trace):
    return max(trace, key=lambda p: p["i"])["rt"] if trace else None


def _avg_on_grid(traces, grid):
    """Apex-align each run's trace (subtract its own apex RT), resample onto the shared
    relative-RT grid, and average across runs. Real data, denoised — no fabrication."""
    import numpy as np
    stack = []
    for tr in traces:
        if not tr:
            continue
        ap = _apex_rt(tr)
        xs = np.array([p["rt"] - ap for p in tr]); ys = np.array([p["i"] for p in tr])
        order = np.argsort(xs)
        stack.append(np.interp(grid, xs[order], ys[order], left=0.0, right=0.0))
    if not stack:
        return None
    mean = np.mean(np.vstack(stack), axis=0)
    return [{"rt": round(float(g), 4), "i": float(v)} for g, v in zip(grid, mean)]


def _records(lib: dict, xic_files: list[str], search_id: str):
    """Merge lib (quant) + per-run xic; the representative trace is the APEX-ALIGNED
    AVERAGE across all runs that saw the precursor (denoised 'typical acquired XIC')."""
    import numpy as np
    runs = {}  # pid -> {'ms1':[traces...], labels:{lab:[traces...]}, 'apex':max, 'rt_apex':best, 'n':int}
    for xf in xic_files:
        tr = _xic_traces(xf)
        for pid, t in tr.items():
            r = runs.setdefault(pid, {"ms1": [], "labels": {}, "apex": 0.0, "rt_apex": None, "n": 0})
            r["n"] += 1
            if t["ms1"]:
                r["ms1"].append(t["ms1"])
                if t["apex"] > r["apex"]:
                    r["apex"] = t["apex"]; r["rt_apex"] = _apex_rt(t["ms1"])
            for lab, tt in t["labels"].items():
                r["labels"].setdefault(lab, []).append(tt)
    grid = np.linspace(-AVG_W, AVG_W, AVG_K)
    for pid, meta in lib.items():
        r = runs.get(pid)
        if not r or not r["ms1"]:
            continue
        ms1_avg = _avg_on_grid(r["ms1"], grid) or []
        frags, quant_labels = [], []
        for label, fd in meta["frags"].items():
            if fd["exclude_from_quant"] == 0:
                quant_labels.append(label)
            avg = _avg_on_grid(r["labels"].get(label, []), grid) or []
            frags.append({"label": label, **fd, "apex": max((p["i"] for p in avg), default=0.0),
                          "trace": avg})
        frags.sort(key=lambda f: f["rel_intensity"], reverse=True)
        yield {"search_id": search_id, "stripped_seq": meta["stripped"], "charge": meta["charge"],
               "precursor_id": pid, "precursor_mz": meta["mz"], "raw_path": f"avg of {r['n']} runs",
               "rt_apex": r["rt_apex"], "ms1_apex": r["apex"], "ms1": ms1_avg, "fragments": frags,
               "quant_labels": quant_labels, "n_fragments_total": len(frags),
               "n_runs_averaged": r["n"]}


def _records_xiconly(xic_files: list[str], search_id: str):
    """XIC-ONLY path: a search with --xic traces but NO report-lib.parquet (e.g. an older
    DIA-NN run that only emitted report_xic + .predicted.speclib). We reconstruct each
    precursor's fragment set from the XIC feature labels alone — m/z is THEORETICAL b/y
    (Cys carbamidomethyl assumed), there is no engine relative-intensity or fragment score,
    and every observed fragment counts as a quant fragment (the engine's quant subset is
    unknown without the library). Traces are the same apex-aligned average of real data."""
    import numpy as np
    runs = {}  # pid -> {'ms1':[traces], labels:{lab:[traces]}, 'apex':max, 'rt_apex':best, 'n':int}
    for xf in xic_files:
        tr = _xic_traces(xf)
        for pid, t in tr.items():
            r = runs.setdefault(pid, {"ms1": [], "labels": {}, "apex": 0.0, "rt_apex": None, "n": 0})
            r["n"] += 1
            if t["ms1"]:
                r["ms1"].append(t["ms1"])
                if t["apex"] > r["apex"]:
                    r["apex"] = t["apex"]; r["rt_apex"] = _apex_rt(t["ms1"])
            for lab, tt in t["labels"].items():
                r["labels"].setdefault(lab, []).append(tt)
    grid = np.linspace(-AVG_W, AVG_W, AVG_K)
    for pid, r in runs.items():
        if not r["ms1"]:
            continue
        stripped, charge = _strip_pr(pid)
        ms1_avg = _avg_on_grid(r["ms1"], grid) or []
        frags, quant_labels = [], []
        for label, traces in r["labels"].items():
            m = _FRAG_RE.match(label)
            if not m:
                continue
            typ, series, fz = m.group(1).lower(), int(m.group(2)), int(m.group(3))
            avg = _avg_on_grid(traces, grid) or []
            quant_labels.append(label)   # no library => treat every observed fragment as quant
            frags.append({"label": label, "type": typ, "series": series, "charge": fz,
                          "mz": _frag_mz(stripped, typ, series, fz), "rel_intensity": None,
                          "exclude_from_quant": 0, "score": None,
                          "apex": max((p["i"] for p in avg), default=0.0), "trace": avg})
        # rank by trace apex since there is no library relative intensity
        frags.sort(key=lambda f: f["apex"], reverse=True)
        yield {"search_id": search_id, "stripped_seq": stripped, "charge": charge,
               "precursor_id": pid, "precursor_mz": None, "raw_path": f"avg of {r['n']} runs",
               "rt_apex": r["rt_apex"], "ms1_apex": r["apex"], "ms1": ms1_avg, "fragments": frags,
               "quant_labels": quant_labels, "n_fragments_total": len(frags),
               "n_runs_averaged": r["n"]}


def _diann_version(d: str) -> str | None:
    """Parse the DIA-NN version (e.g. '2.3.0', '1.8.1') from report.log.txt — it is NOT
    in the parquet, only the log. Recorded so we know which predictor version made the
    library (a 1.8.1 library differs from a 2.5 one)."""
    for name in ("report.log.txt", "report-first-pass.log.txt"):
        p = os.path.join(d, name)
        if os.path.exists(p):
            try:
                txt = open(p, errors="replace").read(4000)
            except OSError:
                continue
            m = re.search(r"DIA-NN\s+([0-9]+\.[0-9]+(?:\.[0-9]+)?)", txt)
            if m:
                return m.group(1)
    hits = glob.glob(os.path.join(d, "*.log.txt"))
    for p in hits:
        try:
            m = re.search(r"DIA-NN\s+([0-9]+\.[0-9]+(?:\.[0-9]+)?)", open(p, errors="replace").read(4000))
            if m:
                return m.group(1)
        except OSError:
            continue
    return None


def _find_inputs(d: str):
    lib = os.path.join(d, "report-lib.parquet")
    if not os.path.exists(lib):
        hits = (glob.glob(os.path.join(d, "*report-lib*.parquet"))
                or glob.glob(os.path.join(d, "*lib_final*.parquet")))  # refined DIA-NN lib
        lib = hits[0] if hits else None
    # XIC traces, in priority order: a consolidated report_xic/ subdir; flat *.xic.parquet in d;
    # OR the per-run DIA-NN temp layout <run>.d_report_xic/<run>.xic.parquet (point --dir at the
    # parent, e.g. DIA_NN_TEMP). The last is what DIA-NN leaves when --xic ran but no consolidated
    # report_xic was written.
    xics = (glob.glob(os.path.join(d, "report_xic", "*.xic.parquet"))
            or glob.glob(os.path.join(d, "*.xic.parquet"))
            or glob.glob(os.path.join(d, "*_report_xic", "*.xic.parquet")))
    return lib, sorted(xics)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="DIA-NN result dir (report-lib.parquet + xic)")
    ap.add_argument("--search-id", required=True)
    ap.add_argument("--duckdb", help="path to a DuckDB file sink")
    ap.add_argument("--pg", action="store_true", help="write to PG Farm (token like corpus_ingest)")
    ap.add_argument("--lib", help="explicit library parquet (report-lib.parquet or a refined "
                    "*_lib_final.parquet) when it isn't alongside --dir; enables the FULL path")
    ap.add_argument("--engine-version", help="DIA-NN version to record when it can't be read from "
                    "a log in --dir (e.g. per-run XIC temp dirs have no report.log.txt)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    lib_path, xics = _find_inputs(a.dir)
    if a.lib:                                    # explicit override wins over auto-discovery
        lib_path = a.lib
    if not xics:
        sys.exit(f"need *.xic.parquet in {a.dir} (none found)")
    # only take the FULL (library) path if the lib actually carries the quant columns; otherwise
    # a bare predicted speclib would crash _lib_quant — fall back to the XIC-only path instead.
    if lib_path and not _lib_is_usable(lib_path):
        print(f"lib {os.path.basename(lib_path)} lacks quant columns (need {_LIB_REQ} + "
              f"Exclude.From.Quant) — falling back to XIC-only")
        lib_path = None
    engine, engine_version = "diann", (_diann_version(a.dir) or a.engine_version)
    print(f"engine: DIA-NN {engine_version or '(version not found in log)'}")
    if lib_path:
        print(f"lib: {os.path.basename(lib_path)} · {len(xics)} xic run(s)")
        lib = _lib_quant(lib_path)
        recs = list(_records(lib, xics, a.search_id))
        print(f"{len(recs)} precursors with XIC+library")
    else:
        print(f"no report-lib.parquet · XIC-ONLY path · {len(xics)} xic run(s) "
              f"(theoretical b/y m/z, no engine rel-intensity/quant subset)")
        recs = list(_records_xiconly(xics, a.search_id))
        print(f"{len(recs)} precursors with XIC (library-free)")
    if recs:
        ex = recs[0]
        print(f"  e.g. {ex['stripped_seq']}{ex['charge']}+ : {ex['n_fragments_total']} frags, "
              f"{len(ex['quant_labels'])} quant, {len(ex['ms1'])} MS1 pts")
    if a.dry_run:
        print("dry-run: nothing written"); return

    # MEASUREMENT rows (deduped by precursor_id). Column order must match DDL:
    # precursor_id, stripped_seq, charge, precursor_mz, raw_path, search_id, rt_apex, ms1_apex, ms1, fragments, n_fragments_total
    meas = [(r["precursor_id"], r["stripped_seq"], r["charge"], r["precursor_mz"], r["raw_path"],
             r["search_id"], engine, engine_version, r["rt_apex"], r["ms1_apex"], json.dumps(r["ms1"]),
             json.dumps(r["fragments"]), r["n_fragments_total"]) for r in recs]
    # QUANT overlay rows: search_id, precursor_id, stripped_seq, charge, quant_labels
    quant = [(r["search_id"], r["precursor_id"], r["stripped_seq"], r["charge"],
              json.dumps(r["quant_labels"])) for r in recs]
    src = _scan_sources(a.dir)
    src_rows = [(a.search_id, role, path, host, sz, json.dumps(got), json.dumps(navail))
                for (role, path, host, sz, got, navail) in src]

    if a.duckdb:
        import duckdb
        con = duckdb.connect(a.duckdb)
        for ddl in (DDL, QUANT_DDL, SOURCES_DDL):
            con.execute(ddl)
        # upsert measurement: keep the higher-MS1-apex representative (dedup across searches/DBs)
        con.executemany(
            """INSERT INTO delimp_precursor_xic VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (precursor_id) DO UPDATE SET
                 stripped_seq=excluded.stripped_seq, charge=excluded.charge, precursor_mz=excluded.precursor_mz,
                 raw_path=excluded.raw_path, search_id=excluded.search_id, engine=excluded.engine,
                 engine_version=excluded.engine_version, rt_apex=excluded.rt_apex,
                 ms1_apex=excluded.ms1_apex, ms1=excluded.ms1, fragments=excluded.fragments,
                 n_fragments_total=excluded.n_fragments_total
               WHERE excluded.ms1_apex > delimp_precursor_xic.ms1_apex""", meas)
        con.execute("DELETE FROM delimp_xic_quant WHERE search_id = ?", [a.search_id])
        con.executemany("INSERT INTO delimp_xic_quant VALUES (?,?,?,?,?)", quant)
        con.execute("DELETE FROM delimp_search_sources WHERE search_id = ?", [a.search_id])
        con.executemany(
            "INSERT INTO delimp_search_sources (search_id,file_role,path,host,bytes,extracted,available_features) "
            "VALUES (?,?,?,?,?,?,?)", src_rows)
        con.close()
        print(f"upserted {len(meas)} measurements + {len(quant)} quant rows + {len(src)} source pointers -> {a.duckdb}")
    elif a.pg:
        import psycopg2, psycopg2.extras
        # PG Farm wants a JWT as the DB password; ~/.pgfarm_token may hold either the long-lived
        # service-account SECRET or a JWT. _token() passes a JWT through unchanged and exchanges a
        # secret for one (POST /auth/service-account/login) — same as verify_setup/refresh_leaderboards.
        # (The old inline raw-file-as-password only worked when the file already held a JWT.)
        from refresh_leaderboards import _token
        pw = _token()
        con = psycopg2.connect(host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"),
            port=5432, dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
            user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
            password=pw, sslmode="require", connect_timeout=30)
        cur = con.cursor()
        for ddl in (DDL, QUANT_DDL, SOURCES_DDL):
            cur.execute(ddl.replace("JSON", "JSONB").replace("DOUBLE", "DOUBLE PRECISION"))
        psycopg2.extras.execute_values(cur,
            """INSERT INTO delimp_precursor_xic VALUES %s
               ON CONFLICT (precursor_id) DO UPDATE SET
                 stripped_seq=excluded.stripped_seq, charge=excluded.charge, precursor_mz=excluded.precursor_mz,
                 raw_path=excluded.raw_path, search_id=excluded.search_id, engine=excluded.engine,
                 engine_version=excluded.engine_version, rt_apex=excluded.rt_apex,
                 ms1_apex=excluded.ms1_apex, ms1=excluded.ms1, fragments=excluded.fragments,
                 n_fragments_total=excluded.n_fragments_total
               WHERE excluded.ms1_apex > delimp_precursor_xic.ms1_apex""", meas, page_size=500)
        cur.execute("DELETE FROM delimp_xic_quant WHERE search_id = %s", (a.search_id,))
        psycopg2.extras.execute_values(cur, "INSERT INTO delimp_xic_quant VALUES %s", quant, page_size=500)
        cur.execute("DELETE FROM delimp_search_sources WHERE search_id = %s", (a.search_id,))
        psycopg2.extras.execute_values(cur,
            "INSERT INTO delimp_search_sources (search_id,file_role,path,host,bytes,extracted,available_features) VALUES %s",
            src_rows, page_size=200)
        con.commit(); con.close()
        print(f"upserted {len(meas)} measurements + {len(quant)} quant rows + {len(src)} source pointers -> PG Farm")
    else:
        sys.exit("choose a sink: --duckdb PATH or --pg")


if __name__ == "__main__":
    main()
