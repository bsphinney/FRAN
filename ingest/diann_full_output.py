"""diann_full_output.py - keep EVERYTHING a DIA-NN search wrote, and say where it is.

Why (DE-LIMP#106, Brett 2026-11-24). FRAN's corpus tables are a curated view of a DIA-NN search:
`corpus_ingest._diann_rows` keeps rows at precursor Q.Value <= 0.01 and a chosen list of columns.
Every pass that widened that list was itself a list, so each new question found something left out.
This module stops choosing. It does two things per DIA-NN search:

  regen     Re-run DIA-NN's cross-run report step from the search's saved .quant files (no raw file
            is read, nothing is re-searched) at --qvalue 0.5 with --report-decoys, and write the
            result to FULL_ROOT/<search_id>/report.full.parquet. The report the search itself wrote
            was filtered at --qvalue 0.01 by the skill's cfg, so rows above 1% and all decoys exist
            only in the .quant files until this runs. A single-shot MBR search has no
            final-pass .quant files at all, so the search is run again from the raw files
            (plan_regen says which method and why). The regenerated report is checked against
            the original: its target rows at Q.Value <= 0.01 must be the original's rows.

  proteins  (part of register) Load the regenerated report's protein groups, with no precursor
            filter, into `delimp_diann_protein_unfiltered`: the one piece of the unfiltered output
            that is also in SQL, so a protein count from FRAN is no longer biased low.

  register  Record every file under the search's output folder, and the regenerated report, in
            `delimp_search_artifact` (path, size, mtime, sha256 for files up to SHA_MAX_BYTES, and
            for report-like parquet the row count, the full column list and the q-value cut-off it
            was written at). Nothing is filtered: a file DIA-NN adds in a later version is
            registered without a code change, as kind 'other'.

The corpus tables are unchanged. They stay the 1% view the site counts from; the complete output
is the files, and the registry is how a query finds them.

NEVER WRITES INTO A SEARCH FOLDER. DIA-NN drops `<lib>.skyline.speclib` beside its --lib and can
rewrite a .quant it decides not to reuse, so the library is symlinked and the .quant files are
COPIED into a work folder, and DIA-NN is pointed at those. The copies are compared with the
originals afterwards (size + mtime of the originals unchanged).

    python diann_full_output.py plan     --out todo.jsonl            # searches still to do (reads PG)
    python diann_full_output.py regen    --list todo.jsonl --shard 0/4
    python diann_full_output.py register --list todo.jsonl --apply   # one writer; run after regen
    python diann_full_output.py nightly  --apply                     # what fran_fill.py runs
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FULL_ROOT = os.environ.get("FRAN_DIANN_FULL_ROOT",
                           "/nfs/lssc0/flinders/proteomics/Data/FRAN_diann/_full_reports")
WORK_ROOT = os.environ.get("FRAN_DIANN_FULL_WORK",
                           "/quobyte/proteomics-grp/brett/scratch/diann_full_report_work")
FULL_QVALUE = 0.5                    # DIA-NN's own guidance for counting proteins from a report
SHA_MAX_BYTES = 512 * 1024 * 1024    # hash anything up to this; bigger files get size + mtime only
WALK_DEPTH = 4
VERSION = "1.0.0"                    # mirrored in versions.DIANN_FULL_OUTPUT_VERSION

# Options removed from the search's own command line before the report step is re-run. Each either
# names an output/input this module supplies itself, or would make DIA-NN open the raw files.
_DROP = {"--out", "--out-lib", "--temp", "--threads", "--qvalue", "--use-quant", "--quant-ori-names",
         "--export-quant", "--report-decoys", "--matrices", "--xic", "--mobilograms", "--reanalyse",
         "--gen-spec-lib", "--predictor", "--fasta-search", "--rt-profiling", "--lib", "--verbose"}


# --------------------------------------------------------------------------------------------
# reading a search folder
# --------------------------------------------------------------------------------------------
def logged_command(search_dir: str) -> str | None:
    """The command line DIA-NN echoed at the top of report.log.txt (the line naming the binary)."""
    p = os.path.join(search_dir, "report.log.txt")
    if not os.path.exists(p):
        return None
    with open(p, errors="replace") as f:
        for i, ln in enumerate(f):
            if i > 40:
                break
            if re.match(r"^\S*diann\S*\s+--", ln):
                return ln.strip()
    return None


def split_options(cmd: str) -> tuple[str, list[tuple[str, str]]]:
    """-> (binary, [(flag, value)]). Splits before every ` --flag`, so a path holding spaces stays
    one value ('UC SantaCruz' is a real folder). The log echoes the command unquoted, which is why
    this cannot be shlex."""
    parts = re.split(r"\s+(?=--[A-Za-z])", cmd.strip())
    binary, opts = parts[0], []
    for p in parts[1:]:
        m = re.match(r"(--[A-Za-z0-9-]+)\s*(.*)$", p, re.S)
        opts.append((m.group(1), m.group(2).strip()))
    return binary, opts


def raw_stem(path: str) -> str:
    """DIA-NN's --quant-ori-names name for a raw file: the file name without its last extension."""
    b = os.path.basename(path.rstrip("/"))
    return b.rsplit(".", 1)[0] if "." in b else b


def find_quant(search_dir: str, raw: str) -> str | None:
    """The final-pass .quant for one raw file. Order: the 5-step chain's quant_step4/, a
    single-shot quant/ folder, then beside the raw file (where DIA-NN puts it with no --temp, which
    is what the skill's single-shot route did until October 2026)."""
    stem, base = raw_stem(raw), os.path.basename(raw.rstrip("/"))
    cands = [os.path.join(search_dir, "quant_step4", stem + ".quant"),
             os.path.join(search_dir, "quant", stem + ".quant"),
             os.path.join(search_dir, "quant", base + ".quant"),
             raw.rstrip("/") + ".quant"]
    cands += glob.glob(os.path.join(glob.escape(os.path.join(search_dir, "quant")), "*" + glob.escape(stem) + "*.quant"))
    for c in cands:
        if os.path.isfile(c) and os.path.getsize(c) > 0:
            return c
    return None


def dotnet_root(search_dir: str) -> str | None:
    """DIA-NN refuses a Thermo .raw input without a .NET runtime even when it only reads .quant
    files ("cannot read .raw files", 0 files processed, exit 0). Use the runtime the search's own
    job script exported, else the pipeline skill's install."""
    for f in sorted(glob.glob(os.path.join(glob.escape(search_dir), "*.sbatch"))
                    + glob.glob(os.path.join(glob.escape(search_dir), "*.sh"))):
        m = re.search(r"DOTNET_ROOT=([^\s;\"']+)", open(f, errors="replace").read(200000))
        if m and os.path.isdir(m.group(1)):
            return m.group(1)
    d = os.environ.get("DOTNET_ROOT") or os.path.expanduser("~/.proteomics-pipeline/dotnet8")
    return d if os.path.isdir(d) else None


def plan_regen(search_dir: str) -> dict:
    """Everything needed to re-run the report step, or the reason it cannot be."""
    cmd = logged_command(search_dir)
    if not cmd:
        return {"ok": False, "why": "no DIA-NN command line in report.log.txt"}
    binary, opts = split_options(cmd)
    raws = [v for f, v in opts if f == "--f"]
    if not raws:
        return {"ok": False, "why": "command names no --f input"}
    libs = [v for f, v in opts if f == "--lib"]
    reanalyse = any(f == "--reanalyse" for f, _ in opts)
    stems = [raw_stem(r) for r in raws]
    if len(set(stems)) != len(stems):
        return {"ok": False, "why": "two inputs share a run name; their .quant files cannot be told apart"}
    quants = {}
    # Two ways back to the full report, depending on which pass the saved .quant files are from.
    #   report_step  The 5-step chain's step 5 names empirical.parquet and its quant_step4/ files
    #                are final-pass, so the report step alone reproduces the report.
    #   full_rerun   A single-shot --reanalyse (MBR) search saves .quant files in its FIRST pass
    #                only -- DIA-NN says so in its log -- so nothing on disk holds the final pass,
    #                and the search is run again whole from the raw files. Three shortcuts were
    #                measured on 2026-11-24 and none reproduces the original on a multi-run set:
    #                the report step against report-lib.parquet (DIA-NN refuses: "library size
    #                mismatch"); a stand-alone final pass against report-lib.parquet (2-4% of the
    #                1% rows differ); and the original command with --use-quant, which skips the
    #                first pass, so the peak width and RT windows that pass would have calibrated
    #                are re-derived in the second (peak width 3.56 against the original 3.592) and
    #                again 2-4% of rows differ. DIA-NN warns of exactly this in its log.
    method = "full_rerun" if reanalyse else "report_step"
    lib = libs[0] if libs else None
    if lib and not os.path.isfile(lib):
        # A search folder that was moved or renamed after the run: its log names the old place.
        moved = os.path.join(search_dir, os.path.basename(lib))
        if os.path.isfile(moved):
            lib = moved
    if not lib or not os.path.isfile(lib):
        return {"ok": False, "why": f"library not on disk: {lib}"}
    if reanalyse:
        gone = [r for r in raws if not os.path.exists(r)]
        if gone:
            return {"ok": False, "why": f"a full re-run needs the raw files; {len(gone)} of {len(raws)} are gone (e.g. {gone[0]})"}
    else:
        missing = []
        for r in raws:
            q = find_quant(search_dir, r)
            (quants.__setitem__(r, q) if q else missing.append(os.path.basename(r)))
        if missing:
            return {"ok": False, "why": f"{len(missing)} of {len(raws)} runs have no .quant (e.g. {missing[0]})"}
        # A .quant beside the raw file is shared by every search of that raw that set no --temp,
        # so the last one to run owns it. Only trust it when it was written during THIS search.
        report = os.path.join(search_dir, "report.parquet")
        rep_m = os.path.getmtime(report) if os.path.exists(report) else None
        stale = [os.path.basename(q) for q in quants.values()
                 if not q.startswith(search_dir) and rep_m and os.path.getmtime(q) > rep_m + 60]
        if stale:
            return {"ok": False, "why": f".quant beside the raw file was rewritten by a later search ({stale[0]})"}
    fastas = [v for f, v in opts if f == "--fasta"]
    gone = [x for x in fastas if not os.path.isfile(x)]
    if gone:
        return {"ok": False, "why": f"FASTA not on disk: {gone[0]}"}
    keep = [(f, v) for f, v in opts if f not in _DROP]
    if reanalyse:
        keep += [(f, v) for f, v in opts if f in ("--reanalyse", "--rt-profiling")]
    th = [v for f, v in opts if f == "--threads"]
    return {"ok": True, "binary": binary, "opts": keep, "threads": th[0] if th else None, "raws": raws, "quants": quants, "lib": lib,
            "method": method, "original_command": cmd,
            "layout": "chain" if os.path.isdir(os.path.join(search_dir, "quant_step4")) else "single_shot"}


# --------------------------------------------------------------------------------------------
# regen
# --------------------------------------------------------------------------------------------
def compare_reports(orig: str, full: str) -> dict:
    """Is the original report the <=1% target slice of the regenerated one? Keys are (Run, Precursor.Id)."""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    cols = ["Run", "Precursor.Id", "Q.Value", "PG.Q.Value", "Global.PG.Q.Value", "Lib.PG.Q.Value",
            "Global.Q.Value", "Lib.Q.Value", "Protein.Group", "Decoy"]
    fo, ff = pq.ParquetFile(orig), pq.ParquetFile(full)
    to = fo.read(columns=[c for c in cols if c in fo.schema_arrow.names])
    tf = ff.read(columns=[c for c in cols if c in ff.schema_arrow.names])
    out = {"orig_rows": to.num_rows, "full_rows": tf.num_rows,
           "full_columns": len(ff.schema_arrow.names), "orig_columns": len(fo.schema_arrow.names),
           "columns_only_in_orig": sorted(set(fo.schema_arrow.names) - set(ff.schema_arrow.names)),
           "columns_only_in_full": sorted(set(ff.schema_arrow.names) - set(fo.schema_arrow.names))}
    dec = pc.greater(tf["Decoy"], 0) if "Decoy" in tf.schema.names else None
    out["full_decoy_rows"] = pc.sum(pc.cast(dec, "int64")).as_py() if dec is not None else 0
    out["full_max_q"] = pc.max(tf["Q.Value"]).as_py()
    tgt = pc.invert(dec) if dec is not None else None
    # DIA-NN's --qvalue 0.01 filter is strict, on 32-bit values: a row at exactly float32(0.01)
    # is not in the original (seen on 3 searches, one row each), so the slice must be strict too.
    import pyarrow as pa
    m = pc.less(pc.cast(tf["Q.Value"], pa.float32()), pa.scalar(0.01, pa.float32()))
    if tgt is not None:
        m = pc.and_(m, tgt)
    slice_ = tf.filter(m)
    key = lambda t: set(zip(t["Run"].to_pylist(), t["Precursor.Id"].to_pylist()))  # noqa: E731
    ko, kf = key(to), key(slice_)
    out["slice_rows"] = slice_.num_rows
    out["keys_only_in_orig"] = len(ko - kf)
    out["keys_only_in_full_slice"] = len(kf - ko)
    # protein groups: the question the issue asks, answered with no precursor filter
    tt = tf.filter(tgt) if tgt is not None else tf
    for c in ("PG.Q.Value", "Global.PG.Q.Value", "Lib.PG.Q.Value"):
        if c in tt.schema.names:
            out["pg_at_1pct_" + c] = pc.count_distinct(tt.filter(pc.less_equal(tt[c], 0.01))["Protein.Group"]).as_py()
            if c in to.schema.names:
                out["orig_pg_at_1pct_" + c] = pc.count_distinct(to.filter(pc.less_equal(to[c], 0.01))["Protein.Group"]).as_py()
    out["target_rows_above_1pct"] = tt.num_rows - slice_.num_rows
    # q-values of the shared rows must be the same numbers, not merely the same keys
    if not out["keys_only_in_orig"] and not out["keys_only_in_full_slice"] and to.num_rows:
        so = to.sort_by([("Run", "ascending"), ("Precursor.Id", "ascending")])
        sf = slice_.sort_by([("Run", "ascending"), ("Precursor.Id", "ascending")])
        for c in ("Q.Value", "PG.Q.Value", "Global.PG.Q.Value", "Lib.PG.Q.Value"):
            if c in so.schema.names and c in sf.schema.names:
                d = pc.max(pc.abs(pc.subtract(pc.cast(so[c], "float64"), pc.cast(sf[c], "float64")))).as_py()
                out["max_abs_diff_" + c] = d
    out["parity"] = (out["keys_only_in_orig"] == 0 and out["keys_only_in_full_slice"] == 0)
    return out


def regen_one(search_id: str, search_dir: str, threads: int, force: bool = False) -> dict:
    dest = os.path.join(FULL_ROOT, search_id)
    marker = os.path.join(dest, "regen.json")
    if os.path.exists(marker) and not force:
        return json.load(open(marker))
    rec = {"search_id": search_id, "search_dir": search_dir, "writer_version": VERSION,
           "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "host": os.uname().nodename,
           "slurm_job": os.environ.get("SLURM_JOB_ID")}
    plan = plan_regen(search_dir)
    if not plan["ok"]:
        rec.update(status="cannot_regenerate", why=plan["why"])
        return rec                       # not written to FULL_ROOT: a later run may be able to
    work = os.path.join(WORK_ROOT, search_id)
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(os.path.join(work, "quant"))
    os.makedirs(os.path.join(work, "out"))
    try:
        before = {}
        for raw, q in plan["quants"].items():
            st = os.stat(q)
            before[q] = (st.st_size, st.st_mtime)
            shutil.copyfile(q, os.path.join(work, "quant", raw_stem(raw) + ".quant"))
        lib = os.path.join(work, "lib" + os.path.splitext(plan["lib"])[1])
        os.symlink(plan["lib"], lib)     # DIA-NN writes <lib>.skyline.speclib beside this NAME
        copies = {f: os.stat(os.path.join(work, "quant", f)).st_mtime for f in os.listdir(os.path.join(work, "quant"))}
        out = os.path.join(work, "out", "report.full.parquet")
        run_threads = threads
        argv = [plan["binary"]]
        for f, v in plan["opts"]:
            argv.append(f)
            if v:
                argv.append(v)
        if plan["method"] == "report_step":
            argv.append("--use-quant")
        argv += ["--lib", lib, "--quant-ori-names", "--temp", os.path.join(work, "quant"),
                 "--qvalue", str(FULL_QVALUE), "--report-decoys", "--export-quant",
                 "--threads", str(run_threads), "--out", out]
        rec["command"] = argv
        rec["layout"], rec["original_command"] = plan["layout"], plan["original_command"]
        rec["method"] = plan["method"]
        env = dict(os.environ)
        if any(r.lower().endswith(".raw") for r in plan["raws"]):
            dn = dotnet_root(search_dir)
            if not dn:
                rec.update(status="cannot_regenerate", why="Thermo .raw inputs and no .NET runtime found")
                return rec
            env["DOTNET_ROOT"] = dn
            env["PATH"] = dn + os.pathsep + env.get("PATH", "")
        t0 = time.time()
        with open(os.path.join(work, "out", "regen.stdout.log"), "w") as lg:
            rc = subprocess.run(argv, stdout=lg, stderr=subprocess.STDOUT, env=env).returncode
        rec["seconds"], rec["rc"] = round(time.time() - t0), rc
        # DIA-NN exits 0 on fatal errors, so the report file is the test, not the return code.
        if not os.path.exists(out) or os.path.getsize(out) == 0:
            rec.update(status="failed", why="DIA-NN wrote no report")
            # Keep the whole log: DIA-NN's reason is rarely in the last lines.
            faildir = os.path.join(FULL_ROOT, "_failed")
            os.makedirs(faildir, exist_ok=True)
            shutil.copyfile(os.path.join(work, "out", "regen.stdout.log"), os.path.join(faildir, search_id + ".log"))
            rec["log"] = os.path.join(faildir, search_id + ".log")
            return rec
        # Under report_step no .quant should change: one that did was declined by DIA-NN and
        # re-made from the raw file. A full re-run starts with none.
        rec["quant_rewritten"] = sorted(f for f, m in copies.items()
                                        if os.stat(os.path.join(work, "quant", f)).st_mtime != m)
        rec["originals_untouched"] = all((os.stat(q).st_size, os.stat(q).st_mtime) == v for q, v in before.items())
        rec["check"] = compare_reports(os.path.join(search_dir, "report.parquet"), out)
        clean = plan["method"] == "full_rerun" or not rec["quant_rewritten"]
        rec["status"] = "mismatch" if not rec["check"]["parity"] else ("ok" if clean else "ok_requantified")
        rec["qvalue"] = FULL_QVALUE
        os.makedirs(dest, exist_ok=True)
        for f in os.listdir(os.path.join(work, "out")):
            shutil.move(os.path.join(work, "out", f), os.path.join(dest, f))
        rec["report"] = os.path.join(dest, "report.full.parquet")
        rec["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        tmp = marker + ".tmp"
        json.dump(rec, open(tmp, "w"), indent=1)
        os.replace(tmp, marker)
        return rec
    finally:
        shutil.rmtree(work, ignore_errors=True)


def note_failure(rec: dict) -> None:
    d = os.path.join(FULL_ROOT, "_failed")
    os.makedirs(d, exist_ok=True)
    slim = {k: v for k, v in rec.items() if k not in ("command", "original_command")}
    tmp = os.path.join(d, rec["search_id"] + ".json.tmp")
    json.dump(slim, open(tmp, "w"), indent=1)
    os.replace(tmp, os.path.join(d, rec["search_id"] + ".json"))


def failed_recently(search_id: str, days: float = 30) -> bool:
    p = os.path.join(FULL_ROOT, "_failed", search_id + ".json")
    return os.path.exists(p) and time.time() - os.path.getmtime(p) < days * 86400


def cmd_regen(a):
    items = [json.loads(ln) for ln in open(a.list) if ln.strip()]
    if a.shard:
        i, n = (int(x) for x in a.shard.split("/"))
        items = items[i::n]
    threads = a.threads or int(os.environ.get("SLURM_CPUS_PER_TASK", "4"))
    os.makedirs(a.results, exist_ok=True)
    res = open(os.path.join(a.results, f"regen_{os.environ.get('SLURM_JOB_ID', 'x')}_{(a.shard or '0').split('/')[0]}.jsonl"), "a")
    n = {"ok": 0}
    for it in items[: a.limit or None]:
        try:
            r = regen_one(it["id"], it["output_dir"], threads, a.force)
        except Exception as e:  # noqa: BLE001 -- one bad folder must not stop the shard
            r = {"search_id": it["id"], "search_dir": it["output_dir"], "status": "error",
                 "why": f"{type(e).__name__}: {e}"[:600]}
        n[r["status"]] = n.get(r["status"], 0) + 1
        if not r["status"].startswith("ok"):
            note_failure(r)
        slim = {k: v for k, v in r.items() if k not in ("command", "original_command")}
        res.write(json.dumps(slim) + "\n")
        res.flush()
        print(r["status"], it["output_dir"], r.get("why", ""), r.get("seconds", ""), flush=True)
    print("SUMMARY", json.dumps(n))


# --------------------------------------------------------------------------------------------
# register
# --------------------------------------------------------------------------------------------
DDL = [
    """CREATE TABLE IF NOT EXISTS delimp_search_artifact (
        id             bigserial PRIMARY KEY,
        search_id      uuid NOT NULL REFERENCES delimp_searches(id) ON DELETE CASCADE,
        kind           text NOT NULL,
        origin         text NOT NULL CHECK (origin IN ('search', 'regenerated')),
        path           text NOT NULL,
        rel_path       text NOT NULL,
        size_bytes     bigint NOT NULL,
        mtime          timestamptz NOT NULL,
        sha256         text,
        n_rows         bigint,
        columns        jsonb,
        report_qvalue  real,
        has_decoys     boolean,
        detail         jsonb,
        writer_version text NOT NULL,
        registered_at  timestamptz NOT NULL DEFAULT now(),
        verified_at    timestamptz NOT NULL DEFAULT now(),
        UNIQUE (search_id, path))""",
    "CREATE INDEX IF NOT EXISTS delimp_search_artifact_kind ON delimp_search_artifact (kind, search_id)",
    """COMMENT ON TABLE delimp_search_artifact IS
       'Every file a search wrote, on disk on Hive, one row per file (ingest/diann_full_output.py). '
       'The corpus tables hold a filtered view of a search (DIA-NN: precursor Q.Value <= 0.01, chosen '
       'columns); this is where the complete output is. kind = full_report is the report regenerated '
       'from the saved .quant files at --qvalue 0.5 with decoys (origin = regenerated); kind = report '
       'is the one the search wrote. columns lists every column of a report-like parquet as '
       '[name, arrow type]; report_qvalue is the --qvalue the file was written at. sha256 is NULL '
       'for files over 512 MB. ON DELETE CASCADE: corpus_ingest re-ingests by DELETE, and the '
       'nightly fill re-registers.'""",
]

DDL += [
    """CREATE TABLE IF NOT EXISTS delimp_diann_protein_unfiltered (
        search_id          uuid NOT NULL REFERENCES delimp_searches(id) ON DELETE CASCADE,
        run                text NOT NULL,
        protein_group      text NOT NULL,
        raw_path           text,
        gene               text,
        n_precursors       integer NOT NULL,
        n_precursors_q01   integer NOT NULL,
        min_precursor_q    real,
        pg_q_value         real,
        global_pg_q_value  real,
        lib_pg_q_value     real,
        pg_pep             real,
        protein_q_value    real,
        gg_q_value         real,
        pg_maxlfq          real,
        pg_maxlfq_quality  real,
        genes_maxlfq       real,
        genes_maxlfq_unique real,
        report_qvalue      real NOT NULL,
        writer_version     text NOT NULL,
        PRIMARY KEY (search_id, run, protein_group))""",
    "CREATE INDEX IF NOT EXISTS delimp_diann_protein_unfiltered_pg ON delimp_diann_protein_unfiltered (protein_group)",
    """COMMENT ON TABLE delimp_diann_protein_unfiltered IS
       'DIA-NN protein groups per run with NO precursor q-value filter (FRAN#35). One row per '
       '(search, Run, Protein.Group) over the TARGET rows of the report regenerated from saved .quant '
       'files at --qvalue 0.5 (delimp_search_artifact kind = full_report). delimp_proteins is built '
       'from precursors at Q.Value <= 0.01 and so misses protein groups whose precursors are all above '
       '1 percent; count proteins from THIS table, filtering on the protein q-value you mean: '
       'pg_q_value (per run), global_pg_q_value (experiment-wide) or lib_pg_q_value (library). '
       'n_precursors counts every target precursor row up to 50 percent q; n_precursors_q01 only '
       'those at Q.Value <= 0.01. Decoys are not here; they are in the report file. The site does '
       'not read this table.'""",
]

DDL += [
    "ALTER TABLE delimp_search_provenance ADD COLUMN IF NOT EXISTS run_by text",
    "ALTER TABLE delimp_search_provenance ADD COLUMN IF NOT EXISTS run_by_source text",
    """COMMENT ON COLUMN delimp_search_provenance.run_by IS
       'Cluster account that ran the search. Nothing the search wrote names its operator, so this '
       'is the Unix owner of the report file (run_by_source says so). It is an account, not '
       'necessarily a person: gc-prot-core-user is shared, and the corpus re-search drip runs '
       'unattended under one account.'""",
]


def run_by(search_dir: str):
    """(account, how it was found) for whoever ran this search, or (None, None)."""
    import pwd
    for name in ("report.parquet", "report.log.txt", "report.stats.tsv"):
        f = os.path.join(search_dir, name)
        if os.path.exists(f):
            uid = os.stat(f).st_uid
            try:
                user = pwd.getpwuid(uid).pw_name
            except KeyError:
                user = f"uid:{uid}"
            how = f"file owner of {name}"
            if "/FRAN_diann/" in search_dir:
                how += "; corpus re-search drip (unattended)"
            return user, how
    return None, None


_KINDS = [  # first match wins; the patterns are on the path relative to the search folder
    (r"^report\.parquet$", "report"),
    (r"^report-first-pass\.parquet$", "report_first_pass"),
    (r"report.*\.site_report\.parquet$", "site_report"),
    (r"^(report-lib|empirical)\.parquet$", "empirical_library"),
    (r"\.predicted\.speclib$", "predicted_library"),
    (r"\.skyline\.speclib$", "skyline_library"),
    (r"_matrix(_labelled)?\.tsv$", "matrix"),
    (r"\.stats\.tsv$", "stats"),
    (r"\.manifest\.txt$", "manifest"),
    (r"sites_\d+\.tsv$", "site_table"),
    (r"protein_description\.tsv$", "protein_description"),
    (r"\.quant$", "quant"),
    (r"xic\.parquet$|(^|/)[^/]*xic[^/]*/", "xic"),
    (r"mobilogram", "mobilogram"),
    (r"\.log(\.txt)?$", "log"),
    (r"\.(cfg|json)$|file_list\.txt$|input_files\.txt$|jobs\.txt$|window\.txt$", "config"),
    (r"\.(sbatch|sh)$", "script"),
]


def kind_of(rel: str) -> str:
    for pat, k in _KINDS:
        if re.search(pat, rel):
            return k
    return "other"


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(8 << 20), b""):
            h.update(b)
    return h.hexdigest()


def parquet_facts(path: str) -> dict:
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    pf = pq.ParquetFile(path)
    names = pf.schema_arrow.names
    d = {"n_rows": pf.metadata.num_rows, "columns": [[f.name, str(f.type)] for f in pf.schema_arrow]}
    if "Decoy" in names and pf.metadata.num_rows:
        d["has_decoys"] = bool(pc.max(pf.read(columns=["Decoy"])["Decoy"]).as_py())
    return d


def log_qvalue(search_dir: str, stem: str = "report") -> float | None:
    p = os.path.join(search_dir, stem + ".log.txt")
    if os.path.exists(p):
        m = re.search(r"Output will be filtered at ([0-9.]+) FDR", open(p, errors="replace").read(200000))
        if m:
            return float(m.group(1))
    return None


def artifacts_for(search_id: str, search_dir: str, hash_files: bool = True):
    """Yield one dict per file: the search's own output, then the regenerated full report."""
    roots = [("search", search_dir)]
    full = os.path.join(FULL_ROOT, search_id)
    if os.path.isdir(full):
        roots.append(("regenerated", full))
    raws = set()
    cmd = logged_command(search_dir) if os.path.isdir(search_dir) else None
    if cmd:
        raws = {v.rstrip("/") + ".quant" for f, v in split_options(cmd)[1] if f == "--f"}
    for origin, root in roots:
        base = root.rstrip("/").count("/")
        regen = {}
        if origin == "regenerated" and os.path.exists(os.path.join(root, "regen.json")):
            regen = json.load(open(os.path.join(root, "regen.json")))
        for d, dirs, files in os.walk(root):
            if d.count("/") - base >= WALK_DEPTH:
                dirs[:] = []
            for f in files:
                p = os.path.join(d, f)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                rel = os.path.relpath(p, root)
                k = kind_of(rel)
                a = {"origin": origin, "path": p, "rel_path": rel, "kind": k, "size_bytes": st.st_size,
                     "mtime": st.st_mtime, "sha256": None, "n_rows": None, "columns": None,
                     "report_qvalue": None, "has_decoys": None, "detail": None}
                if origin == "regenerated" and rel == "report.full.parquet":
                    a["kind"] = "full_report"
                    a["report_qvalue"] = regen.get("qvalue")
                    a["detail"] = {x: regen.get(x) for x in ("status", "method", "check", "layout", "seconds",
                                                             "quant_rewritten", "slurm_job", "finished")}
                if a["kind"] in ("report", "report_first_pass", "full_report", "site_report",
                                 "empirical_library") and p.endswith(".parquet"):
                    try:
                        a.update(parquet_facts(p))
                    except Exception as e:  # noqa: BLE001 -- a damaged parquet is itself the finding
                        a["detail"] = dict(a["detail"] or {}, parquet_error=f"{type(e).__name__}: {e}"[:300])
                if a["kind"] in ("report", "report_first_pass"):
                    a["report_qvalue"] = log_qvalue(search_dir, rel[: -len(".parquet")])
                if hash_files and st.st_size <= SHA_MAX_BYTES and a["kind"] not in ("quant", "xic", "mobilogram"):
                    a["sha256"] = sha256_of(p)
                yield a
    # .quant files the search left beside its raw files (no --temp): part of its output too
    for q in sorted(raws):
        if os.path.isfile(q):
            st = os.stat(q)
            yield {"origin": "search", "path": q, "rel_path": "(beside raw)/" + os.path.basename(q),
                   "kind": "quant", "size_bytes": st.st_size, "mtime": st.st_mtime, "sha256": None,
                   "n_rows": None, "columns": None, "report_qvalue": None, "has_decoys": None,
                   "detail": {"beside_raw": True}}


_UPSERT = """INSERT INTO delimp_search_artifact
    (search_id, kind, origin, path, rel_path, size_bytes, mtime, sha256, n_rows, columns,
     report_qvalue, has_decoys, detail, writer_version)
  VALUES (%(search_id)s, %(kind)s, %(origin)s, %(path)s, %(rel_path)s, %(size_bytes)s,
          to_timestamp(%(mtime)s), %(sha256)s, %(n_rows)s, %(columns)s, %(report_qvalue)s,
          %(has_decoys)s, %(detail)s, %(wv)s)
  ON CONFLICT (search_id, path) DO UPDATE SET
     kind = EXCLUDED.kind, size_bytes = EXCLUDED.size_bytes, mtime = EXCLUDED.mtime,
     sha256 = COALESCE(EXCLUDED.sha256, delimp_search_artifact.sha256),
     n_rows = COALESCE(EXCLUDED.n_rows, delimp_search_artifact.n_rows),
     columns = COALESCE(EXCLUDED.columns, delimp_search_artifact.columns),
     report_qvalue = COALESCE(EXCLUDED.report_qvalue, delimp_search_artifact.report_qvalue),
     has_decoys = COALESCE(EXCLUDED.has_decoys, delimp_search_artifact.has_decoys),
     detail = COALESCE(EXCLUDED.detail, delimp_search_artifact.detail),
     writer_version = EXCLUDED.writer_version, verified_at = now()"""


def _conn():
    sys.path.insert(0, HERE)
    from coreomics_import import _conn as c
    return c()


_PROT_COLS = ("search_id", "run", "protein_group", "raw_path", "gene", "n_precursors",
              "n_precursors_q01", "min_precursor_q", "pg_q_value", "global_pg_q_value",
              "lib_pg_q_value", "pg_pep", "protein_q_value", "gg_q_value", "pg_maxlfq",
              "pg_maxlfq_quality", "genes_maxlfq", "genes_maxlfq_unique", "report_qvalue",
              "writer_version")
# report column -> (table column, how to collapse it within (Run, Protein.Group)). Every one was
# measured constant within the group (migrations/2026-09-23), so min/max only pick the value.
_PROT_AGG = [("Q.Value", "min_precursor_q", "min"), ("PG.Q.Value", "pg_q_value", "min"),
             ("Global.PG.Q.Value", "global_pg_q_value", "min"), ("Lib.PG.Q.Value", "lib_pg_q_value", "min"),
             ("PG.PEP", "pg_pep", "min"), ("Protein.Q.Value", "protein_q_value", "min"),
             ("GG.Q.Value", "gg_q_value", "min"), ("PG.MaxLFQ", "pg_maxlfq", "max"),
             ("PG.MaxLFQ.Quality", "pg_maxlfq_quality", "max"), ("Genes.MaxLFQ", "genes_maxlfq", "max"),
             ("Genes.MaxLFQ.Unique", "genes_maxlfq_unique", "max")]


def protein_rows(report: str):
    """(Run, Protein.Group) rows from a full report: target rows only, no precursor filter."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    pf = pq.ParquetFile(report)
    names = set(pf.schema_arrow.names)
    have = [(src, dst, how) for src, dst, how in _PROT_AGG if src in names]
    t = pf.read(columns=["Run", "Protein.Group", "Genes", "Decoy"] + [src for src, _, _ in have])
    t = t.filter(pc.equal(t["Decoy"], 0))
    t = t.append_column("_q01", pc.cast(pc.less_equal(t["Q.Value"], 0.01), pa.int32()))
    g = t.group_by(["Run", "Protein.Group"]).aggregate(
        [("Genes", "min"), ("Decoy", "count"), ("_q01", "sum")] + [(src, how) for src, _, how in have])
    out = {"run": g["Run"].to_pylist(), "protein_group": g["Protein.Group"].to_pylist(),
           "gene": g["Genes_min"].to_pylist(), "n_precursors": g["Decoy_count"].to_pylist(),
           "n_precursors_q01": g["_q01_sum"].to_pylist()}
    for src, dst, how in have:
        out[dst] = g[f"{src}_{how}"].to_pylist()
    return out, g.num_rows


def load_proteins(conn, search_id: str, output_dir: str) -> int | None:
    """Replace this search's rows in delimp_diann_protein_unfiltered. None = nothing to load."""
    import csv
    import io
    import math
    full = os.path.join(FULL_ROOT, search_id)
    marker, report = os.path.join(full, "regen.json"), os.path.join(full, "report.full.parquet")
    if not (os.path.exists(marker) and os.path.exists(report)):
        return None
    regen = json.load(open(marker))
    if not str(regen.get("status", "")).startswith("ok"):
        return None                    # a report that does not reproduce the original is not loaded
    cols, n = protein_rows(report)
    cur = conn.cursor()
    cur.execute("SELECT raw_path FROM search_raw_files WHERE search_id = %s", (search_id,))
    by_stem = {raw_stem(r[0]): r[0] for r in cur.fetchall()}
    buf = io.StringIO()
    w = csv.writer(buf)

    def cell(v):
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return ""
        return v

    for i in range(n):
        run, pg = cols["run"][i], cols["protein_group"][i]
        if not pg:
            continue                   # never store an empty or missing group as a name
        gene = cols["gene"][i]
        row = {"search_id": search_id, "run": run, "protein_group": pg,
               "raw_path": by_stem.get(run), "gene": gene if gene else None,
               "report_qvalue": regen.get("qvalue", FULL_QVALUE), "writer_version": VERSION}
        for c in _PROT_COLS:
            if c not in row:
                row[c] = cols[c][i] if c in cols else None
        w.writerow([cell(row[c]) for c in _PROT_COLS])
    buf.seek(0)
    cur.execute("DELETE FROM delimp_diann_protein_unfiltered WHERE search_id = %s", (search_id,))
    cur.copy_expert(f"COPY delimp_diann_protein_unfiltered ({','.join(_PROT_COLS)}) "
                    "FROM STDIN WITH (FORMAT csv, NULL '')", buf)
    conn.commit()
    return n


def pending(cur, engine_version_like: str | None, what: str):
    """DIA-NN searches with no registered artifact of `what` ('report' or 'full_report')."""
    cur.execute("""SELECT s.id::text, s.output_dir, s.n_raw_files FROM delimp_searches s
                   WHERE s.search_engine = 'diann' AND s.output_dir IS NOT NULL
                     AND (%s::text IS NULL OR s.search_engine_version LIKE %s)
                     AND NOT EXISTS (SELECT 1 FROM delimp_search_artifact a
                                     WHERE a.search_id = s.id AND a.kind = %s)
                   ORDER BY s.ingested_at""", (engine_version_like, engine_version_like, what))
    return [{"id": r[0], "output_dir": r[1], "n_raw": r[2]} for r in cur.fetchall()]


def cmd_migrate(a):
    for s in DDL:
        print(s + ";\n")
    if a.apply:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SET lock_timeout = '5s'")
        for s in DDL:
            cur.execute(s)
        conn.commit()
        print("applied")


def cmd_plan(a):
    conn = _conn()
    cur = conn.cursor()
    items = pending(cur, a.version_like, a.missing)
    with open(a.out, "w") as f:
        for it in items:
            f.write(json.dumps(it) + "\n")
    print(f"{len(items)} DIA-NN searches without a registered {a.missing} -> {a.out}")


def cmd_register(a):
    import psycopg2.extras
    items = [json.loads(ln) for ln in open(a.list) if ln.strip()]
    if a.shard:
        i, n = (int(x) for x in a.shard.split("/"))
        items = items[i::n]
    conn = _conn() if a.apply else None
    tot = {"searches": 0, "files": 0, "missing_dir": 0, "bytes": 0}
    for it in items[: a.limit or None]:
        if not os.path.isdir(it["output_dir"]):
            tot["missing_dir"] += 1
            print("DIR MISSING", it["output_dir"], flush=True)
            continue
        rows = []
        for art in artifacts_for(it["id"], it["output_dir"], not a.no_hash):
            art.update(search_id=it["id"], wv=VERSION,
                       columns=json.dumps(art["columns"]) if art["columns"] else None,
                       detail=json.dumps(art["detail"]) if art["detail"] else None)
            rows.append(art)
        tot["searches"] += 1
        tot["files"] += len(rows)
        tot["bytes"] += sum(r["size_bytes"] for r in rows)
        if conn:
            # A token lasts 7 days but an idle connection does not survive a long hash; reconnect.
            for attempt in (1, 2):
                try:
                    cur = conn.cursor()
                    psycopg2.extras.execute_batch(cur, _UPSERT, rows, page_size=200)
                    conn.commit()
                    break
                except (psycopg2.OperationalError, psycopg2.InterfaceError):
                    if attempt == 2:
                        raise
                    conn = _conn()
            who, how = run_by(it["output_dir"])
            if who:
                cur = conn.cursor()
                cur.execute("""UPDATE delimp_search_provenance SET run_by = %s, run_by_source = %s
                               WHERE search_id = %s AND run_by IS DISTINCT FROM %s""",
                            (who, how, it["id"], who))
                tot["run_by_set"] = tot.get("run_by_set", 0) + cur.rowcount
                conn.commit()
            try:
                npr = load_proteins(conn, it["id"], it["output_dir"])
            except Exception as e:  # noqa: BLE001 -- the file registration above is already committed
                conn.rollback()
                npr = f"PROTEIN LOAD FAILED {type(e).__name__}: {e}"[:300]
                tot["protein_load_failed"] = tot.get("protein_load_failed", 0) + 1
            if isinstance(npr, int):
                tot["protein_rows"] = tot.get("protein_rows", 0) + npr
        else:
            npr = "-"
        print(f"{len(rows):5d} files  {npr} protein rows  {it['output_dir']}", flush=True)
    if conn:
        cur = conn.cursor()
        try:
            sys.path.insert(0, HERE)
            import versions
            versions.record_run(cur, "diann_full_output", VERSION,
                                notes=f"{tot['searches']} searches, {tot['files']} files registered")
            conn.commit()
        except Exception as e:  # noqa: BLE001 -- the version log must not undo the registration
            conn.rollback()
            print("WARN: component version not recorded:", e)
    print("SUMMARY", json.dumps(tot), "" if a.apply else "(dry run: nothing written)")


def cmd_nightly(a):
    """Every DIA-NN search with no registered report or no registered full report: regenerate what
    can be regenerated, then register. This is the post-ingest hook (fran_fill.py runs it), so a
    search ingested today is covered tonight without corpus_ingest.py knowing about any of it."""
    import psycopg2.extras
    conn = _conn()
    cur = conn.cursor()
    need_full = {x["id"]: x for x in pending(cur, None, "full_report")}
    need_reg = {x["id"]: x for x in pending(cur, None, "report")}
    conn.close()                       # a regeneration can outlast an idle connection
    threads = int(os.environ.get("SLURM_CPUS_PER_TASK", "2"))
    t0, n = time.time(), {"regenerated": 0, "cannot": 0, "skipped_failed": 0, "registered": 0,
                          "dir_missing": 0, "left": 0}
    todo = list({**need_reg, **need_full}.values())
    for i, it in enumerate(todo):
        if time.time() - t0 > a.budget:
            n["left"] = len(todo) - i
            break
        if not os.path.isdir(it["output_dir"]):
            n["dir_missing"] += 1
            continue
        if it["id"] in need_full:
            if failed_recently(it["id"]) and not a.retry_failed:
                n["skipped_failed"] += 1
            elif a.apply:
                try:
                    r = regen_one(it["id"], it["output_dir"], threads)
                except Exception as e:  # noqa: BLE001
                    r = {"search_id": it["id"], "search_dir": it["output_dir"], "status": "error",
                         "why": f"{type(e).__name__}: {e}"[:600]}
                if r["status"].startswith("ok"):
                    n["regenerated"] += 1
                else:
                    n["cannot"] += 1
                    note_failure(r)
        if a.apply:
            rows = []
            for art in artifacts_for(it["id"], it["output_dir"]):
                art.update(search_id=it["id"], wv=VERSION,
                           columns=json.dumps(art["columns"]) if art["columns"] else None,
                           detail=json.dumps(art["detail"]) if art["detail"] else None)
                rows.append(art)
            c2 = _conn()
            psycopg2.extras.execute_batch(c2.cursor(), _UPSERT, rows, page_size=200)
            c2.commit()
            try:
                if load_proteins(c2, it["id"], it["output_dir"]) is not None:
                    n["protein_tables"] = n.get("protein_tables", 0) + 1
            except Exception as e:  # noqa: BLE001
                c2.rollback()
                print("PROTEIN LOAD FAILED", it["id"], f"{type(e).__name__}: {e}"[:300])
            c2.close()
            n["registered"] += 1
    print(f"{len(need_reg)} DIA-NN searches had no registered report, {len(need_full)} no full report")
    print("SUMMARY", json.dumps(n), "" if a.apply else "(dry run: nothing regenerated or written)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("migrate"); p.add_argument("--apply", action="store_true"); p.set_defaults(fn=cmd_migrate)
    p = sub.add_parser("plan")
    p.add_argument("--out", required=True)
    p.add_argument("--missing", default="full_report", choices=["full_report", "report"])
    p.add_argument("--version-like", default=None, help="e.g. 2.7%% ; default every DIA-NN search")
    p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("regen")
    p.add_argument("--list", required=True); p.add_argument("--shard", default="")
    p.add_argument("--threads", type=int, default=0); p.add_argument("--limit", type=int, default=0)
    p.add_argument("--force", action="store_true")
    p.add_argument("--results", default=os.path.join(FULL_ROOT, "_runs"))
    p.set_defaults(fn=cmd_regen)
    p = sub.add_parser("register")
    p.add_argument("--list", required=True); p.add_argument("--apply", action="store_true")
    p.add_argument("--no-hash", action="store_true"); p.add_argument("--limit", type=int, default=0)
    p.add_argument("--shard", default="")
    p.set_defaults(fn=cmd_register)
    p = sub.add_parser("nightly")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--budget", type=int, default=5 * 3600, help="seconds; the rest waits a night")
    p.add_argument("--retry-failed", action="store_true")
    p.set_defaults(fn=cmd_nightly)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
