"""diann_xic_to_lance must refuse XIC files whose run names do not match the report's Run column.

The lane joins each <run>.xic.parquet to report rows by (Run, Precursor.Id). If the file stems do not
equal the report's Run values -- a different DIA-NN layout, a renamed file, a report of a different
run set -- every trace is written with q_value NULL ("extracted, not reported in this run"), the
script exits 0, and the queue records the lane as done. That is a silently wrong lane, so a mismatch
must stop the write (unless --runs names an explicit subset).

Run:  python tests/test_diann_xic_run_match.py     (needs pyarrow; no lance, no database)
"""
import os, subprocess, sys, tempfile
import pyarrow as pa
import pyarrow.parquet as pq

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "ingest", "diann_xic_to_lance.py")

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def write_report(d, runs):
    rows = {"Run": [], "Precursor.Id": [], "Precursor.Mz": [], "RT": [], "Q.Value": [],
            "Protein.Group": [], "Genes": []}
    for r in runs:
        rows["Run"].append(r); rows["Precursor.Id"].append("PEPTIDEK2")
        rows["Precursor.Mz"].append(466.7); rows["RT"].append(12.3); rows["Q.Value"].append(0.001)
        rows["Protein.Group"].append("P00001"); rows["Genes"].append("GENE1")
    pq.write_table(pa.table(rows), os.path.join(d, "report.parquet"))


def write_xic(xdir, run):
    tbl = pa.table({"pr": ["PEPTIDEK2", "PEPTIDEK2"], "feature": ["ms1", "y4^1"],
                    "info": pa.array([0, 0], pa.int32()), "rt": [12.2, 12.3],
                    "value": [100.0, 50.0]})
    os.makedirs(xdir, exist_ok=True)
    pq.write_table(tbl, os.path.join(xdir, f"{run}.xic.parquet"))


def run_dry(d, *extra):
    return subprocess.run([sys.executable, SCRIPT, "--dir", d, "--xic-dir", os.path.join(d, "xic"),
                           "--out", os.path.join(d, "out.xic.lance"), *extra],
                          capture_output=True, text=True)


with tempfile.TemporaryDirectory() as tmp:
    # matching: parallel-chain layout, one folder per array task
    ok = os.path.join(tmp, "ok"); os.makedirs(ok)
    write_report(ok, ["runA", "runB"])
    write_xic(os.path.join(ok, "xic", "t0_xic"), "runA")
    write_xic(os.path.join(ok, "xic", "t1_xic"), "runB")
    r = run_dry(ok)
    check("matching run names pass (dry run exits 0)", r.returncode == 0, r.stdout[-400:] + r.stderr[-400:])
    check("matching run names report traces as reported in-run",
          "1 reported in this run" in r.stdout, r.stdout[-400:])

    # an XIC file for a run the report does not contain
    extra = os.path.join(tmp, "extra"); os.makedirs(extra)
    write_report(extra, ["runA", "runB"])
    write_xic(os.path.join(extra, "xic"), "runA")
    write_xic(os.path.join(extra, "xic"), "runB")
    write_xic(os.path.join(extra, "xic"), "runA.d")          # a stem DIA-NN never reports
    r = run_dry(extra)
    check("an XIC run absent from the report is refused", r.returncode != 0, r.stdout[-400:])
    out = r.stdout + r.stderr
    check("the refusal names the unmatched XIC run",
          "do not match the report" in out and "runA.d" in out.split("do not match the report", 1)[-1],
          r.stderr[-400:])

    # a report run with no XIC file
    missing = os.path.join(tmp, "missing"); os.makedirs(missing)
    write_report(missing, ["runA", "runB"])
    write_xic(os.path.join(missing, "xic"), "runA")
    r = run_dry(missing)
    check("a report run with no XIC file is refused", r.returncode != 0, r.stdout[-400:])
    out = r.stdout + r.stderr
    check("the refusal names the run without XICs",
          "do not match the report" in out and "runB" in out.split("do not match the report", 1)[-1],
          r.stderr[-400:])

    # --runs names an explicit subset: only those must match
    r = run_dry(missing, "--runs", "runA")
    check("--runs restricts the check to the named runs", r.returncode == 0, r.stdout[-400:] + r.stderr[-400:])

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
