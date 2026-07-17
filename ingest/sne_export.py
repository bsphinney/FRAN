"""sne_export.py — find every Spectronaut *.sne experiment under one or more roots and
export a FRAN-ready precursor/fragment report from each, using the Spectronaut command
line. The reports can then be ingested with:  corpus_ingest.py REPORT.tsv --engine spectronaut

Verified against the Spectronaut 21 manual (§3.11 Command Line Mode, "Loading SNE files",
Table 12 — manageSNE):

    spectronaut manageSNE -sne <file.sne> -o <out_dir> -rs <report_schema> [--writeParquet]

  * the executable is `spectronaut` on Linux and `dotnet Spectronaut.dll` on Windows
  * -o (output directory) is REQUIRED
  * -rs is the custom report schema (path to a *.rs file, or a schema name already in the
    GUI's report-schema repository). The schema MUST contain the columns FRAN needs
    (see fran_required_columns() / the README this script prints with --columns).
  * default output is TSV (what the FRAN Spectronaut adapter reads); pass --parquet only if
    you also teach spectronaut_to_corpus.py to read parquet.

Spectronaut needs an activated license on the machine that runs this (this script only
drives the CLI; run it where Spectronaut is installed). Nothing here touches the .sne
files except to read their paths.

Usage:
  python sne_export.py /path/to/projects --schema FRAN_ingest --out /path/to/reports
  python sne_export.py ROOT1 ROOT2 --schema /path/FRAN_ingest.rs --out ./reports --dry-run
  python sne_export.py --columns                 # just print the report-schema columns FRAN needs
"""
from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys


# The Spectronaut report columns FRAN ingests (mirrors scripts/spectronaut_to_corpus.py
# COLMAP — the first/primary identifier for each field). Group order = priority.
FRAN_COLUMNS = {
    "required (the report is useless to FRAN without these)": [
        "R.FileName",            # run / raw file
        "PEP.StrippedSequence",  # bare peptide sequence
        "FG.Charge",             # precursor charge
    ],
    "core precursor record (strongly recommended)": [
        "PG.ProteinGroups",      # protein group accessions
        "PG.Genes",              # gene symbols
        "PG.Qvalue",             # protein-group q-value
        "EG.ModifiedSequence",   # modified sequence (-> ProForma + mods)
        "EG.Qvalue",             # precursor q-value (FRAN filters q<=0.01)
        "EG.PEP",                # posterior error probability
        "FG.PrecMz",             # precursor m/z
        "EG.ApexRT",             # retention time (apex)
        "EG.iRT",                # indexed/cross-run RT  <-- drives FRAN's iRT axis
        "EG.IonMobility",        # 1/K0 ion mobility (timsTOF)  <-- FRAN's differentiator
        "EG.CCS",                # collision cross section (timsTOF)
        "FG.Quantity",           # precursor quantity (intensity)
        "FG.NormalizedMS2PeakArea",  # normalized intensity (any FG.Normalized* works)
    ],
    "MS2 fragment spectrum (makes a FRAGMENT report — large, but the AI-training payload)": [
        "F.FrgMz",               # fragment m/z
        "F.FrgType",             # b / y / etc.
        "F.FrgNum",              # series number
        "F.FrgZ",                # fragment charge
        "F.FrgLossType",         # neutral loss
        "F.PeakArea",            # fragment intensity (or F.NormalizedPeakArea)
    ],
    "acquisition metadata (optional; for CE/instrument-conditioned training)": [
        "R.Instrument",          # instrument model (often absent in the report)
        "FG.CollisionEnergy",    # NCE (often absent; pull from run metadata otherwise)
    ],
}


def print_columns():
    print("FRAN Spectronaut report-schema columns\n" + "=" * 38)
    for group, cols in FRAN_COLUMNS.items():
        print(f"\n## {group}")
        for c in cols:
            print(f"  {c}")
    print("\nNotes:")
    print(" - Include the F.* columns to get a FRAGMENT-level report (one row per fragment).")
    print("   That captures the observed MS2 spectrum FRAN needs for spectra display + AI")
    print("   training. It is much larger; a precursor-only report (no F.*) is fine if you")
    print("   only want IDs/quant. The adapter handles both.")
    print(" - Export as TSV (default). FRAN's adapter reads tab-separated text.")
    print(" - Filter is applied at ingest (q<=0.01); no need to pre-filter in the schema.")


def _spectronaut_cmd(explicit: str | None) -> list[str]:
    """Resolve how to invoke Spectronaut. Precedence: --spectronaut, $SPECTRONAUT_EXE,
    $SPECTRONAUT_DLL (via dotnet), `spectronaut` on PATH.
    A single executable PATH (even with spaces, e.g. C:\\Program Files\\...) is run directly
    and never split. A '.dll' is run via dotnet. An explicit 'dotnet <dll>' string is honored."""
    def resolve(s):
        s = s.strip().strip('"')
        low = s.lower()
        if low.startswith("dotnet ") or low.startswith('dotnet"'):
            return ["dotnet", s.split(None, 1)[1].strip().strip('"')]
        if low.endswith(".dll"):
            return ["dotnet", s]
        return [s]                      # single exe path — DO NOT split on spaces
    if explicit:
        return resolve(explicit)
    exe = os.environ.get("SPECTRONAUT_EXE")
    if exe:
        return [exe.strip().strip('"')]
    dll = os.environ.get("SPECTRONAUT_DLL")
    if dll and shutil.which("dotnet"):
        return ["dotnet", dll.strip().strip('"')]
    if shutil.which("spectronaut"):
        return ["spectronaut"]
    return ["spectronaut"]


def find_sne(roots: list[str]) -> list[str]:
    found = []
    for root in roots:
        if os.path.isfile(root) and root.lower().endswith(".sne"):
            found.append(root); continue
        found += glob.glob(os.path.join(root, "**", "*.sne"), recursive=True)
    # de-dup, stable order
    seen, out = set(), []
    for p in sorted(found):
        rp = os.path.realpath(p)
        if rp not in seen:
            seen.add(rp); out.append(p)
    return out


def _safe_name(sne_path: str) -> str:
    base = os.path.splitext(os.path.basename(sne_path))[0]
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in base)


def export_one(sne: str, name: str, out_dir: str, schema: str, snexec: list[str],
               parquet: bool, dry: bool, xic_dir: str | None) -> str | None:
    # --setXICExportDirectory is a GLOBAL option (manual Table 13) and must come BEFORE the
    # command: it switches on automatic export of all XICs as per-raw-file SQLite dbs into
    # the given dir. The .sne must have been saved WITH ion traces (FULL) for there to be
    # XICs to dump. We point it at out_dir/xics so the dbs get zipped with the report.
    pre = ["--setXICExportDirectory", xic_dir] if xic_dir else []
    cmd = [*snexec, *pre, "manageSNE", "-sne", sne, "-n", name, "-o", out_dir, "-rs", schema]
    if parquet:
        cmd.append("--writeParquet")
    if xic_dir and not dry:
        os.makedirs(xic_dir, exist_ok=True)
    print(f"\n• {os.path.basename(sne)}\n  -> {out_dir}\n  $ {' '.join(_q(c) for c in cmd)}")
    if dry:
        return None
    os.makedirs(out_dir, exist_ok=True)
    try:
        subprocess.run(cmd, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"  !! export failed: {e}")
        return None
    # locate the report Spectronaut wrote. Spectronaut nests it in a timestamped subfolder
    # of -o, so search RECURSIVELY; prefer the *Report* file over side reports (RunOverview,
    # iRTCalibration, ConditionSetup, ...).
    ext = "parquet" if parquet else "tsv"
    allf = glob.glob(os.path.join(out_dir, "**", f"*.{ext}"), recursive=True)
    rep = [f for f in allf if "report" in os.path.basename(f).lower()]
    hits = sorted(rep or allf, key=os.path.getmtime, reverse=True)
    if not hits:
        print(f"  !! no .{ext} report produced under {out_dir}")
        return None
    print(f"  ok report: {hits[0]}")
    return hits[0]


def zip_experiment(out_dir: str, zip_path: str, keep_loose: bool) -> bool:
    """Zip everything Spectronaut wrote for one experiment into a single archive, then
    drop the loose folder (unless --keep-loose) so you end up with one .zip per experiment
    instead of a sprawl of files."""
    import zipfile
    if not os.path.isdir(out_dir):
        return False
    files = [os.path.join(r, f) for r, _, fs in os.walk(out_dir) for f in fs]
    if not files:
        return False
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, os.path.relpath(f, out_dir))
    size_mb = os.path.getsize(zip_path) / 1e6
    print(f"  zipped {len(files)} file(s) -> {zip_path} ({size_mb:.1f} MB)")
    if not keep_loose:
        shutil.rmtree(out_dir, ignore_errors=True)
    return True


def _q(s: str) -> str:
    return f'"{s}"' if " " in s else s


def main():
    ap = argparse.ArgumentParser(description="Find *.sne files and export FRAN-ready reports.")
    ap.add_argument("roots", nargs="*", help="directories (or .sne files) to search")
    ap.add_argument("--schema", help="report schema: path to a *.rs file OR a schema name in the Spectronaut repo")
    ap.add_argument("--out", default="./sne_reports", help="base output directory for reports")
    ap.add_argument("--spectronaut", help="how to invoke Spectronaut (e.g. 'dotnet C:/Spectronaut/Spectronaut.dll'); auto-detected otherwise")
    ap.add_argument("--parquet", action="store_true", help="write reports as Parquet instead of TSV (smaller/faster; FRAN ingests either)")
    ap.add_argument("--ingest", action="store_true", help="after each export, ingest report (+XICs) into FRAN")
    ap.add_argument("--xics", action="store_true", help="also try to dump XIC dbs via --setXICExportDirectory. NOTE: manageSNE generally does NOT export XICs from a loaded SNE — use Spectronaut's GUI 'Export all XIC' for chromatograms. Off by default.")
    ap.add_argument("--keep-loose", action="store_true", help="keep the loose report folders too (default: keep only the per-experiment .zip)")
    ap.add_argument("--flinders", metavar="DIR", help="also copy each experiment .zip to this folder (e.g. the mounted Flinders archive)")
    ap.add_argument("--dry-run", action="store_true", help="list .sne files + the exact commands; run nothing")
    ap.add_argument("--columns", action="store_true", help="print the report-schema columns FRAN needs and exit")
    a = ap.parse_args()

    if a.columns:
        print_columns(); return
    if not a.roots:
        ap.error("give at least one root directory to search (or use --columns)")
    if not a.schema and not a.dry_run:
        ap.error("--schema is required to export (the custom FRAN report schema). See --columns.")

    snes = find_sne(a.roots)
    print(f"found {len(snes)} .sne file(s) under {', '.join(a.roots)}")
    if not snes:
        return
    snexec = _spectronaut_cmd(a.spectronaut)
    print(f"spectronaut: {' '.join(snexec)}{'  (NOT on PATH — set --spectronaut or $SPECTRONAUT_DLL)' if snexec == ['spectronaut'] and not shutil.which('spectronaut') else ''}")

    here = os.path.dirname(os.path.abspath(__file__))
    n_ok, zips = 0, []
    for sne in snes:
        name = _safe_name(sne)
        out_dir = os.path.join(a.out, name)
        xic_dir = os.path.join(out_dir, "xics") if a.xics else None
        rep = export_one(sne, name, out_dir, a.schema or "(none)", snexec, a.parquet, a.dry_run, xic_dir)
        if a.dry_run or not rep:
            continue
        n_ok += 1
        # ingest the LOOSE report first (corpus_ingest reads the plain file), then the XICs,
        # then zip. (XIC ingest needs both the report — for fragment labels via F.Rank — and
        # the SQLite dbs.)
        if a.ingest:
            print(f"  >> ingesting report as '{name}'")
            subprocess.run([sys.executable, os.path.join(here, "corpus_ingest.py"),
                            rep, "--engine", "spectronaut", "--name", name])
            if xic_dir and glob.glob(os.path.join(xic_dir, "*.sqlite")) + glob.glob(os.path.join(xic_dir, "*.db")):
                print(f"  >> ingesting XICs from {xic_dir}")
                subprocess.run([sys.executable, os.path.join(here, "sne_xic_ingest.py"),
                                "--report", rep, "--xic-dir", xic_dir, "--search-id", name, "--pg"])
            elif xic_dir:
                print(f"  (no .sqlite XICs in {xic_dir} — was the .sne saved WITH ion traces?)")
        # one .zip per experiment, loose folder removed -> no sprawl of files
        zip_path = os.path.join(a.out, name + ".zip")
        if zip_experiment(out_dir, zip_path, a.keep_loose):
            zips.append(zip_path)
            # copy to Flinders right away (incremental: an interrupted run still leaves
            # finished experiments archived). Mounted-drive copy can fail -> warn, continue.
            if a.flinders:
                try:
                    os.makedirs(a.flinders, exist_ok=True)
                    dest = shutil.copy2(zip_path, os.path.join(a.flinders, os.path.basename(zip_path)))
                    print(f"  copied -> {dest}")
                except OSError as e:
                    print(f"  !! Flinders copy failed (mount dropped?): {e}")

    if not a.dry_run:
        print(f"\ndone: {n_ok}/{len(snes)} experiments exported · {len(zips)} zip(s) -> {a.out}"
              + (f" · copied to {a.flinders}" if a.flinders else ""))
        if not a.ingest:
            print("ingest a zip's report with:  unzip -p <name>.zip '*.tsv' > r.tsv && "
                  "python corpus_ingest.py r.tsv --engine spectronaut --name <name>")


if __name__ == "__main__":
    main()
