"""diann_full_output.py: the parts that decide WHAT gets re-run and HOW a file is classified.

The regeneration itself needs DIA-NN and real .quant files (verified on Hive, FRAN#35). What can
break silently in review is the command surgery: an option left in that makes DIA-NN re-read raw
files or write into the search folder, a path with a space split in two, or a new DIA-NN output
file being dropped instead of registered. Those are pure functions and are tested here.
"""
import json
import os, sys, tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "ingest"))
import diann_full_output as d   # noqa: E402
import versions                 # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

check("version constant mirrors versions.py", d.VERSION == versions.DIANN_FULL_OUTPUT_VERSION)

CHAIN = ("/tools/diann-linux --f /data/UC SantaCruz/a_1.d --f /data/UC SantaCruz/b_2.d --fasta /f/s.fasta "
         "--lib {S}/empirical.parquet --use-quant --quant-ori-names --temp {S}/quant_step4 --matrices "
         "--out {S}/report.parquet --threads 64 --window 15 --qvalue 0.01 --cut K*,R* --unimod4 "
         "--var-mod UniMod:35,15.994915,M --mass-acc 15")

with tempfile.TemporaryDirectory() as t:
    S = os.path.join(t, "search"); os.makedirs(os.path.join(S, "quant_step4"))
    raws = os.path.join(t, "data", "UC SantaCruz"); os.makedirs(raws)
    fa = os.path.join(t, "s.fasta"); open(fa, "w").write(">x\nPEPTIDEK\n")
    cmd = CHAIN.replace("{S}", S).replace("/data/UC SantaCruz", raws).replace("/f/s.fasta", fa)
    open(os.path.join(S, "report.log.txt"), "w").write("DIA-NN 2.7.0\nCompiled\n" + cmd + " \n\nOutput will be filtered at 0.01 FDR\n")
    check("no .quant -> cannot regenerate, with the reason", d.plan_regen(S)["ok"] is False)
    for n in ("a_1", "b_2"):
        open(os.path.join(S, "quant_step4", n + ".quant"), "w").write("q")
    open(os.path.join(S, "empirical.parquet"), "w").write("x")
    open(os.path.join(S, "report.parquet"), "w").write("x")
    p = d.plan_regen(S)
    check("chain search is plannable", p["ok"], str(p))
    check("a path holding a space stays one --f value", p["raws"] == [raws + "/a_1.d", raws + "/b_2.d"], str(p.get("raws")))
    flags = [f for f, _ in p["opts"]]
    for gone in ("--out", "--temp", "--qvalue", "--threads", "--lib", "--matrices", "--use-quant"):
        check(f"{gone} is removed from the search's own options", gone not in flags)
    for kept in ("--fasta", "--window", "--cut", "--unimod4", "--var-mod", "--mass-acc"):
        check(f"{kept} is carried over unchanged", kept in flags)
    check("method is the report step for a chain", p["method"] == "report_step")
    check("log q-value is read", d.log_qvalue(S) == 0.01)
    # single-shot with --reanalyse: first-pass quants only, so the second pass has to be re-run
    os.makedirs(os.path.join(raws, "a_1.d")); os.makedirs(os.path.join(raws, "b_2.d"))
    for n in ("a_1", "b_2"):   # a finished, rollback-mode tdf: safe to re-open
        open(os.path.join(raws, n + ".d", "analysis.tdf"), "wb").write(b"SQLite format 3\x00\x10\x00\x01\x01" + b"\x00" * 80)
    lib = os.path.join(S, "lib.predicted.speclib"); open(lib, "w").write("x")
    cmd2 = (f"/tools/diann-linux --qvalue 0.01 --xic 10 --mobilograms --f {raws}/a_1.d --f {raws}/b_2.d "
            f"--fasta {fa} --lib {lib} --reanalyse --matrices --out {S}/report.parquet --threads 16")
    open(os.path.join(S, "report.log.txt"), "w").write("DIA-NN 2.7.0\n" + cmd2 + " \n")
    for n in ("a_1", "b_2"):
        open(os.path.join(S, "quant_step4", n + ".quant"), "a").write("q")
    open(os.path.join(S, "report.log.txt"), "w").write("DIA-NN 2.7.0\n" + cmd2.replace("--xic 10", "--xic 10 --rt-profiling") + " \n")
    p2 = d.plan_regen(S)
    check("single-shot --reanalyse plans a full re-run", p2["ok"] and p2["method"] == "full_rerun", str(p2))
    check("...against the library the search itself named", p2["lib"] == lib)
    f2 = [f for f, _ in p2["opts"]]
    check("--reanalyse and --rt-profiling are kept, --xic and --mobilograms dropped",
          {"--reanalyse", "--rt-profiling"} <= set(f2) and not {"--xic", "--mobilograms"} & set(f2))
    # a .quant beside the raw that a LATER search rewrote must not be trusted (single pass, no MBR)
    open(os.path.join(S, "report.log.txt"), "w").write("DIA-NN 2.7.0\n" + cmd2.replace(" --reanalyse", "") + " \n")
    import shutil; shutil.rmtree(os.path.join(S, "quant_step4"))
    q = raws + "/a_1.d.quant"; open(q, "w").write("q"); open(raws + "/b_2.d.quant", "w").write("q")
    old = os.path.getmtime(os.path.join(S, "report.parquet"))
    os.utime(q, (old + 3600, old + 3600))
    check("a beside-raw .quant newer than the report is refused", d.plan_regen(S)["ok"] is False)
    os.utime(q, (old - 10, old - 10)); os.utime(raws + "/b_2.d.quant", (old - 10, old - 10))
    check("a beside-raw .quant from this search is used", d.plan_regen(S)["ok"] is True)

KINDS = {"report.parquet": "report", "report-first-pass.parquet": "report_first_pass",
         "report.pg_matrix.tsv": "matrix", "report.stats.tsv": "stats", "report.manifest.txt": "manifest",
         "report.log.txt": "log", "s5_report_123.log": "log", "empirical.parquet": "empirical_library",
         "report-lib.parquet": "empirical_library", "step1.predicted.speclib": "predicted_library",
         "quant_step4/a.quant": "quant", "xic/t0_xic/a.xic.parquet": "xic",
         "report.site_report.parquet": "site_report", "report.UniMod_35_sites_90.tsv": "site_table",
         "params.resolved.cfg": "config", "step5_report.sbatch": "script",
         "some.new.diann.output.bin": "other"}
for rel, k in KINDS.items():
    check(f"{rel} -> {k}", d.kind_of(rel) == k, d.kind_of(rel))
check("an unknown file is still a kind (registered, never dropped)", d.kind_of("anything") == "other")


# ---- the parity check, the protein rows, the raw-file gate, the lock (review findings) ----
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq


def _rep(path, rows):
    cols = ["Run", "Precursor.Id", "Q.Value", "PG.Q.Value", "Global.PG.Q.Value", "Lib.PG.Q.Value",
            "Protein.Group", "Genes", "Decoy", "PG.MaxLFQ"]
    arr = {c: [r.get(c) for r in rows] for c in cols}
    f32 = pa.float32()
    typ = lambda c: f32 if c.endswith("Q.Value") or c == "PG.MaxLFQ" else (pa.int64() if c == "Decoy" else pa.string())  # noqa: E731
    tbl = pa.table({c: pa.array(v, typ(c)) for c, v in arr.items()})
    pq.write_table(tbl, path)


with tempfile.TemporaryDirectory() as t:
    base = [dict({"Run": "a", "Precursor.Id": f"P{i}", "Q.Value": 0.001, "PG.Q.Value": 0.002,
                  "Global.PG.Q.Value": 0.003, "Lib.PG.Q.Value": 0.004, "Protein.Group": "G1",
                  "Genes": "g", "Decoy": 0, "PG.MaxLFQ": 5.0}) for i in range(3)]
    extra = [dict(base[0], **{"Precursor.Id": "HI", "Q.Value": 0.2, "Protein.Group": "G2", "PG.Q.Value": 0.005}),
             dict(base[0], **{"Precursor.Id": "EDGE", "Q.Value": float(np.float32(0.01))}),
             dict(base[0], **{"Precursor.Id": "DEC", "Decoy": 1, "Protein.Group": "D1"})]
    o, f = os.path.join(t, "o.parquet"), os.path.join(t, "f.parquet")
    _rep(o, base); _rep(f, base + extra)
    c = d.compare_reports(o, f)
    check("identical 1% rows plus rows above 1%, a boundary row and a decoy: parity", c["parity"] is True, str(c))
    check("...and the group seen only above 1% is counted", c["pg_at_1pct_PG.Q.Value"] == 2 and c["orig_pg_at_1pct_PG.Q.Value"] == 1)
    _rep(f, [dict(base[0], **{"PG.Q.Value": 0.009})] + base[1:] + extra)
    check("same keys but a different protein q-value is NOT parity", d.compare_reports(o, f)["parity"] is False)
    _rep(f, base + [base[0]] + extra)
    check("a duplicated key is NOT parity", d.compare_reports(o, f)["parity"] is False)
    _rep(o, []); _rep(f, extra)
    check("an empty original is NOT parity", d.compare_reports(o, f)["parity"] is False)

    _rep(f, base + extra + [dict(base[0], **{"Protein.Group": 'A,"B";C', "Precursor.Id": "Q"})])
    cols, n = d.protein_rows(f)
    by = {g: i for i, g in enumerate(cols["protein_group"])}
    check("protein rows: targets only, one per (run, group)", n == 3 and "D1" not in by, str(by))
    i = by["G1"]
    check("...n_precursors counts every target row, n_precursors_q01 only rows strictly under 1%",
          cols["n_precursors"][i] == 4 and cols["n_precursors_q01"][i] == 3, f'{cols["n_precursors"][i]} {cols["n_precursors_q01"][i]}')
    check("...a group seen only above 1% is there with zero 1% precursors", cols["n_precursors_q01"][by["G2"]] == 0)

    D = os.path.join(t, "x.d"); os.makedirs(D)
    open(os.path.join(D, "analysis.tdf"), "wb").write(b"SQLite format 3\x00" + b"\x10\x00\x01\x01" + b"\x00" * 80)
    check("a rollback-mode tdf with no sidecar is safe to open", d.tdf_unsafe(D) is None, str(d.tdf_unsafe(D)))
    open(os.path.join(D, "analysis.tdf-wal"), "wb").write(b"x")
    check("a non-empty -wal beside the tdf is refused", d.tdf_unsafe(D) is not None)
    os.remove(os.path.join(D, "analysis.tdf-wal"))
    open(os.path.join(D, "analysis.tdf"), "wb").write(b"SQLite format 3\x00" + b"\x10\x00\x02\x02" + b"\x00" * 80)
    check("a WAL-mode tdf header is refused", d.tdf_unsafe(D) is not None)

    # the lock: a second job must not touch a search another job holds
    d.WORK_ROOT, d.FULL_ROOT = os.path.join(t, "work"), os.path.join(t, "full")
    S = os.path.join(t, "s"); os.makedirs(os.path.join(S, "quant_step4"))
    open(os.path.join(S, "empirical.parquet"), "w").write("x"); open(os.path.join(S, "a.d"), "w").write("x")
    open(os.path.join(S, "quant_step4", "a.quant"), "w").write("q")
    open(os.path.join(S, "report.log.txt"), "w").write(
        f"DIA-NN 2.7.0\n/tools/diann-linux --f {S}/a.d --lib {S}/empirical.parquet --use-quant --temp {S}/quant_step4 --out {S}/report.parquet \n")
    os.makedirs(os.path.join(d.WORK_ROOT, "sid.lock"))
    open(os.path.join(d.WORK_ROOT, "sid.lock", "owner"), "w").write("someone_else")
    r = d.regen_one("sid", S, 1)
    check("a search another job holds comes back busy and is left alone", r["status"] == "busy", str(r)[:300])
    check("...and its lock is still there", os.path.isdir(os.path.join(d.WORK_ROOT, "sid.lock")))
    os.makedirs(os.path.join(d.FULL_ROOT, "half")); open(os.path.join(d.FULL_ROOT, "half", "report.full.parquet"), "w").write("x")
    arts = list(d.artifacts_for("half", S, hash_files=False))
    check("a regenerated folder with no marker is not registered", not [a for a in arts if a["origin"] == "regenerated"])
    json.dump({"status": "mismatch", "qvalue": 0.5}, open(os.path.join(d.FULL_ROOT, "half", "regen.json"), "w"))
    kinds = {a["kind"] for a in d.artifacts_for("half", S, hash_files=False) if a["origin"] == "regenerated"}
    check("a mismatched report is labelled, never 'full_report'", "full_report_mismatch" in kinds and "full_report" not in kinds, str(kinds))

print("\nFAILED: " + ", ".join(FAILS) if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
