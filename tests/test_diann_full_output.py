"""diann_full_output.py: the parts that decide WHAT gets re-run and HOW a file is classified.

The regeneration itself needs DIA-NN and real .quant files (verified on Hive, FRAN#35). What can
break silently in review is the command surgery: an option left in that makes DIA-NN re-read raw
files or write into the search folder, a path with a space split in two, or a new DIA-NN output
file being dropped instead of registered. Those are pure functions and are tested here.
"""
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

print("\nFAILED: " + ", ".join(FAILS) if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
