"""diann_xic_to_lance.py — DIA-NN `--xic` output -> the XIC lane, in the SAME Lance schema
Spectronaut's exported chromatograms already use (ingest/xic_lance.py:SCHEMA).

WHY THIS EXISTS. `xic_ingest.py` already reads DIA-NN `report_xic/*.xic.parquet`, but it writes
straight into Postgres (`delimp_precursor_xic`). That left the durable trace lane
(`delimp_xic_lane`) Spectronaut-only, so a cross-engine chromatogram view had one engine's traces in
a columnar lane and the other's in a transactional table with a different shape. The lane is the
right home for bulk traces -- Postgres holds only the curated subset a page actually serves
(~6.1 KB/row: the 3.49M-precursor dog lane would be ~22 GB in PG, against 13 GB as Lance).

INPUT FORMAT (verified 2026-08-25 against a real file, 3.86M rows for one run). DIA-NN writes LONG
format, one row per (precursor, feature, retention-time point):

    pr       string   DIA-NN's Precursor.Id = modified sequence + charge, e.g.
                      "AAAEAALAAVLALEAGLSAEQR3", "AAAIGIDLGTTYSC(UniMod:4)VGVFQHGK3"
    feature  string   "ms1", or a fragment label "<type><number>^<charge>" -- y7^1, b3^1, y8^2
    info     int32    0 in every row observed; carried nowhere
    rt       float    retention time of this point (minutes, absolute)
    value    float    intensity at this point

WHAT THE LANE CAN AND CANNOT ANSWER. DIA-NN's `--xic` writes traces ONLY for precursors it
REPORTED -- its identification list, not everything it evaluated. Spectronaut's .xic.db has the same
limitation. So this lane supports "show me both engines' chromatograms for a peptide they BOTH
found", and it can never support "show me what DIA-NN extracted for a peptide it missed": there is
no such trace in the file. A missing trace here means "this engine did not report this precursor",
NEVER "there was no signal". Anything built on top must say which of those it is showing.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

import pyarrow as pa

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import xic_lance  # noqa: E402

# "<modified sequence><charge>": charge is the trailing run of digits. A modification is spelled
# "(UniMod:4)", which also ends in a digit followed by ")", so anchoring on the END is safe while
# splitting on the first digit is not.
_PR = re.compile(r"^(.*?)(\d+)$")
# "y7^1" / "b12^2"; DIA-NN always writes the ^charge suffix for fragments.
_FRG = re.compile(r"^([abcxyz])(\d+)\^(\d+)$", re.I)


def parse_pr(pr: str):
    """'AAAK2' -> ('AAAK', 2). Returns (None, 0) when it does not parse, so a malformed row is
    dropped rather than silently stored under a wrong charge."""
    m = _PR.match(str(pr or ""))
    if not m or not m.group(1):
        return None, 0
    try:
        return m.group(1), int(m.group(2))
    except ValueError:
        return None, 0


def strip_mods(mod_seq: str) -> str:
    """'AAC(UniMod:4)FR' -> 'AACFR'."""
    return re.sub(r"\([^)]*\)|\[[^\]]*\]", "", str(mod_seq or ""))


def _report_meta(report_path: str):
    """(run, precursor_id) -> metadata from the DIA-NN report. The XIC file carries only traces;
    q-value, m/z, ion mobility, protein group and genes live in the report."""
    import pyarrow.parquet as pq
    want = {
        "run": ["Run"], "pid": ["Precursor.Id"], "mz": ["Precursor.Mz"],
        "rt": ["RT"], "im": ["IM", "Ion.Mobility"], "q": ["Q.Value"],
        "pg": ["Protein.Group"], "genes": ["Genes"], "decoy": ["Decoy"],
    }
    if report_path.endswith(".parquet"):
        names = set(pq.ParquetFile(report_path).schema_arrow.names)
    else:
        import csv
        with open(report_path, newline="") as fh:
            names = set(next(csv.reader(fh, delimiter="\t")))
    col = {k: next((c for c in v if c in names), None) for k, v in want.items()}
    if not col["run"] or not col["pid"]:
        raise SystemExit(f"report lacks Run/Precursor.Id: {report_path}")
    use = [c for c in col.values() if c]
    if report_path.endswith(".parquet"):
        tbl = pq.read_table(report_path, columns=use)
    else:
        import pyarrow.csv as pv
        tbl = pv.read_csv(report_path, parse_options=pv.ParseOptions(delimiter="\t"),
                          convert_options=pv.ConvertOptions(include_columns=use))
    meta, by_pr = {}, {}
    d = tbl.to_pydict()
    n = tbl.num_rows
    g = lambda k, i: (d[col[k]][i] if col[k] else None)  # noqa: E731
    for i in range(n):
        rec = {
            "mz": g("mz", i), "rt": g("rt", i), "im": g("im", i), "q": g("q", i),
            "pg": g("pg", i), "genes": g("genes", i), "decoy": bool(g("decoy", i) or 0),
        }
        meta[(g("run", i), g("pid", i))] = rec
        # Precursor-level fallback, for traces DIA-NN wrote in a run where it did not REPORT the
        # precursor -- see build_rows(). Only identity fields are read from it.
        by_pr.setdefault(g("pid", i), rec)
    return meta, by_pr


def build_rows(xic_path: str, run: str, meta: dict, by_pr: dict, search_id, search_name,
               raw_path=""):
    """One run's *.xic.parquet -> rows in xic_lance.SCHEMA order."""
    import pyarrow.parquet as pq
    tbl = pq.read_table(xic_path, columns=["pr", "feature", "rt", "value"])
    d = tbl.to_pydict()
    prs, feats, rts, vals = d["pr"], d["feature"], d["rt"], d["value"]
    # fold long -> {precursor: {feature: ([rt], [intensity])}}, preserving file order (DIA-NN
    # writes each trace's points in ascending rt already; we do not re-sort and silently repair).
    acc: dict[str, dict[str, tuple[list, list]]] = {}
    for i in range(len(prs)):
        f = acc.setdefault(prs[i], {}).setdefault(feats[i], ([], []))
        f[0].append(float(rts[i])); f[1].append(float(vals[i]))

    rows = []
    for pr, traces in acc.items():
        mseq, ch = parse_pr(pr)
        if not mseq or not ch:
            continue
        m = meta.get((run, pr))
        reported_here = m is not None
        if not reported_here:
            # DIA-NN writes traces in run X for precursors it identified in ANOTHER run (its
            # match-between-runs pass) but did not report in X -- 3,006 of 34,867 (8.6%) in the
            # file this was verified against. The trace is real and worth keeping, but the
            # identification is not this run's.
            #
            # m/z, protein group and genes are properties of the PRECURSOR, so taking them from
            # another run is correct. q_value, rt and im are properties of THIS ACQUISITION and are
            # left NULL rather than borrowed -- a q-value copied from the run that did report it
            # would read as "DIA-NN reported this here at 1% FDR", which is exactly false.
            #
            # So in this lane: q_value IS NULL means "trace extracted, NOT reported in this run".
            src = by_pr.get(pr, {})
            m = {"mz": src.get("mz"), "pg": src.get("pg"), "genes": src.get("genes"),
                 "rt": None, "im": None, "q": None, "decoy": src.get("decoy", False)}
        if m.get("decoy"):
            continue                      # decoys must never leak into a public lane
        labels, levels, ranks, t_rt, t_int = [], [], [], [], []
        scored = []
        for lab, (rr, vv) in traces.items():
            if not rr:
                continue
            is_ms1 = str(lab).lower().startswith("ms1")
            scored.append((0 if is_ms1 else 1, -max(vv), lab, rr, vv, 1 if is_ms1 else 2))
        # MS1 first, then fragments strongest-first; trace_rank is that order made explicit so a
        # consumer never has to re-derive "which fragment is the base peak".
        scored.sort(key=lambda x: (x[0], x[1]))
        for rank, (_, _, lab, rr, vv, lvl) in enumerate(scored):
            labels.append(str(lab)); levels.append(lvl); ranks.append(rank)
            t_rt.append(rr); t_int.append(vv)
        if not labels:
            continue
        rows.append({
            "search_id": str(search_id or ""), "search_name": str(search_name or ""),
            "raw_path": raw_path, "run": run, "xicdbid": -1,
            "stripped_seq": strip_mods(mseq), "modified_seq": mseq, "charge": ch,
            "precursor_mz": m.get("mz"), "rt": m.get("rt"), "im": m.get("im"),
            "q_value": m.get("q"), "is_decoy": False,
            "protein_group": m.get("pg"), "genes": m.get("genes"),
            "n_traces": len(labels),
            "n_ms1": sum(1 for x in levels if x == 1), "n_ms2": sum(1 for x in levels if x == 2),
            "trace_label": labels, "trace_ms_level": levels, "trace_rank": ranks,
            "trace_rt": t_rt, "trace_intensity": t_int,
        })
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", required=True, help="DIA-NN result dir (holds report.parquet + report_xic/)")
    # DIA-NN names its outputs after --out, so "report.parquet"/"report_xic" is the common case,
    # not a guarantee: a search run with --out AD-Inifi_search.parquet writes AD-Inifi_search_xic/.
    ap.add_argument("--report", default=None, help="explicit report path (default: <dir>/report.parquet)")
    ap.add_argument("--xic-dir", default=None, help="explicit XIC dir (default: <dir>/report_xic)")
    ap.add_argument("--out", required=True, help="output <name>.xic.lance path")
    ap.add_argument("--search-id", default=None)
    ap.add_argument("--search-name", default=None)
    ap.add_argument("--runs", default="", help="comma-separated run names to keep (default: all)")
    ap.add_argument("--apply", action="store_true", help="write Lance + register (else dry run)")
    a = ap.parse_args()

    xd = a.xic_dir or os.path.join(a.dir, "report_xic")
    if not os.path.isdir(xd):
        xd = a.dir
    xics = sorted(f for f in os.listdir(xd) if f.endswith(".xic.parquet"))
    if not xics:
        raise SystemExit(f"no *.xic.parquet under {xd}")
    rep = a.report or next((os.path.join(a.dir, n) for n in ("report.parquet", "report.tsv")
                            if os.path.exists(os.path.join(a.dir, n))), None)
    if not rep:
        raise SystemExit(f"no report.parquet/report.tsv in {a.dir}")
    keep = {r for r in a.runs.split(",") if r} or None

    print(f"xic files: {len(xics)}  report: {os.path.basename(rep)}", flush=True)
    meta, by_pr = _report_meta(rep)
    print(f"report metadata rows: {len(meta):,}  distinct precursors: {len(by_pr):,}", flush=True)

    total, ntr, first = 0, 0, True
    for fn in xics:
        run = fn[:-len(".xic.parquet")]
        if keep and run not in keep:
            continue
        rows = build_rows(os.path.join(xd, fn), run, meta, by_pr, a.search_id, a.search_name)
        rep_here = sum(1 for r in rows if r["q_value"] is not None)
        mbr = len(rows) - rep_here
        print(f"  {run}: {len(rows):,} precursors, {sum(r['n_traces'] for r in rows):,} traces, "
              f"{rep_here:,} reported in this run, {mbr:,} trace-only (q_value NULL)", flush=True)
        total += len(rows); ntr += sum(r["n_traces"] for r in rows)
        if a.apply and rows:
            tbl = pa.Table.from_pylist(rows, schema=xic_lance.SCHEMA)
            xic_lance.write_lance(tbl, a.out, mode="overwrite" if first else "append")
            first = False
    print(f"\nTOTAL {total:,} precursors / {ntr:,} traces")
    if not a.apply:
        print("DRY RUN — re-run with --apply to write and register.")
        return
    import lance
    ds = lance.dataset(a.out)
    md5 = xic_lance.content_md5(ds.to_table())
    print(f"wrote {a.out}  rows={ds.count_rows():,}  version={ds.version}  md5={md5}")
    if a.search_id:
        from ingest_perrun_xic import _conn
        conn = _conn()
        xic_lance.ensure_registry(conn)
        xic_lance.register(conn, a.search_id, a.search_name, a.out, total, ntr, md5, ds.version)
        conn.commit(); conn.close()
        print("registered in delimp_xic_lane")
    else:
        print("[skip] no --search-id, not registering")


if __name__ == "__main__":
    main()
