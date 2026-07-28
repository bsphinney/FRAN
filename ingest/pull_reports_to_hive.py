"""pull_reports_to_hive.py — copy every Spectronaut FRAN report off the Windows export box onto
Hive/Flinders, so no report is trapped on a `C:\\` drive.

Context: ~1,871 of FRAN's 1,890 Spectronaut searches have their `..._Report_FRAN (Normal).parquet`
ONLY under `C:\\fran_sne_export\\` on the export workstation — never copied to Flinders. The report
already exists (no need to re-export from the `.sne`); it just needs to be pulled to Hive so
`backfill_fragments.py` can build the observed-spectrum Lance lane from it. This is the CHEAP path.

**Run this ON A WINDOWS INGESTOR** (win-1/win-2/…) that has both `C:\\fran_sne_export\\` and the
Flinders share mounted. It's pure-Python + shutil, idempotent (skips files already on Hive with a
matching size), and preserves the `<name>/<ts>_<name>/<file>` layout the backfill scanner expects.

    python pull_reports_to_hive.py \
        --src "C:\\fran_sne_export" \
        --dest "\\\\flinders\\proteomics\\Data\\FRAN_reports"     # or the mounted equivalent
    # optional extra sources:  --src "B:\\Automatic_SNE_storage"
    # also copy the .params sidecar:  --params
    # mark the coordination queue done as it copies:  --update-queue

METADATA SIDECARS ARE NOW COPIED BY DEFAULT (2026-07-27). The first pull took only the parquet, so
nothing carrying the SEARCH-ENGINE VERSION reached Flinders and `search_engine_version` could be
recovered for only 50 of 1,959 searches. Re-running this brings the sidecars over; they are KB-sized
text files and the reports are skipped as already-present, so the re-run is cheap. Verified on
win-1: +12,908 files, 0 failed. Afterwards, fill the column from Hive with:

    python backfill_engine_versions.py            # re-run; it only touches searches still NULL

The file that actually resolves the version is **`RunSummaries/*RunOverview.tsv`** ("Analysis
Version"), NOT `setup.txt` — the CLI `manageSNE` exports don't emit a setup.txt at all (that's a GUI
artifact), which is why the win-1 pull produced 8,633 RunOverview files and zero setup.txt. That pull
took the column from 50 to 1,142 of 1,963. Report dirs with no RunSummaries folder (615 of 1,737)
still can't be resolved — pull the export dir they originated from.

Verify afterwards from Hive:
    python backfill_fragments.py --scan <dest> --out-dir <lance_dir> --register --workers 8
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys

REPORT_RE = re.compile(r"_Report_FRAN.*\.(parquet|tsv)$", re.I)

# METADATA SIDECARS (added 2026-07-27). The original pull copied ONLY the report parquet, so the
# files that carry the ENGINE VERSION and the INSTRUMENT never reached Flinders: a scan found 0
# setup.txt across all 1,737 archived report dirs, which is why search_engine_version could be
# recovered for just 50 of 1,959 searches. These are small text files (KB, not MB) — copying them
# is nearly free and unlocks:
#   *_Report.setup.txt          -> FIRST LINE "Spectronaut 21.0.260604.94842" (the engine version)
#   RunSummaries/*RunOverview.tsv -> instrument model + serial, gradient, vendor, run date
#   *AnalysisLog / *AnalyisOverview (Spectronaut's own spelling) / *ConditionSetup -> run metadata
SIDECAR_RE = re.compile(
    r"(setup\.txt|RunOverview\.tsv|[Aa]nalysis[Ll]og\.txt|Analyis?Overview\.txt|"
    r"ConditionSetup\.tsv|\.params)$", re.I)


def _rel_tail(src_root, path):
    """Keep the last two path components (…/<ts>_<name>/<file>) so the Flinders layout matches
    what backfill_fragments.py --scan walks. RunSummaries/ files keep one extra level so they land
    in <name>/<ts>_<name>/RunSummaries/ — engine_version.detect() looks for them exactly there."""
    parts = os.path.normpath(path).replace("\\", "/").split("/")
    keep = 4 if len(parts) >= 4 and parts[-2].lower() == "runsummaries" else 3
    return "/".join(parts[-keep:]) if len(parts) >= keep else os.path.basename(path)


def _md5(path, buf=1 << 20):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(buf), b""):
            h.update(chunk)
    return h.hexdigest()


def find_reports(src, params=False, sidecars=True):
    for dp, _, fns in os.walk(src):
        for fn in fns:
            if (REPORT_RE.search(fn)
                    or (params and fn.lower().endswith(".params") and "report_fran" in fn.lower())
                    or (sidecars and SIDECAR_RE.search(fn))):
                yield os.path.join(dp, fn)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", action="append", required=True, help="Windows export dir(s) (repeatable)")
    ap.add_argument("--dest", required=True, help="Flinders/Hive FRAN_reports root")
    ap.add_argument("--params", action="store_true", help="also copy the .params sidecars")
    ap.add_argument("--no-sidecars", action="store_true",
                    help="do NOT copy the setup.txt / RunSummaries / AnalysisLog metadata sidecars "
                         "(they are copied by default — they carry the engine version + instrument)")
    ap.add_argument("--verify-md5", action="store_true", help="md5-verify each copy (slower, safest)")
    ap.add_argument("--update-queue", action="store_true", help="mark delimp_spectrum_regen_queue rows done")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    copied = skipped = failed = 0
    done_paths = []
    for src in a.src:
        if not os.path.isdir(src):
            print(f"[warn] source not found: {src}"); continue
        print(f"scanning {src} ...")
        for path in find_reports(src, a.params, sidecars=not a.no_sidecars):
            dst = os.path.join(a.dest, _rel_tail(src, path).replace("/", os.sep))
            try:
                if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(path):
                    skipped += 1; continue
                if a.dry_run:
                    print(f"  would copy: {path} -> {dst}"); copied += 1; continue
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(path, dst)
                if a.verify_md5 and _md5(path) != _md5(dst):
                    failed += 1; print(f"  [MD5 MISMATCH] {dst}"); continue
                copied += 1; done_paths.append(dst)
                if copied % 50 == 0:
                    print(f"  copied {copied} (skipped {skipped}) ...")
            except Exception as e:  # noqa: BLE001 - one bad file never stops the sweep
                failed += 1; print(f"  [warn] {os.path.basename(path)}: {str(e)[:100]}")

    print(f"\nDONE: copied {copied}, skipped(already on Hive) {skipped}, failed {failed} -> {a.dest}")

    if a.update_queue and done_paths and not a.dry_run:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from refresh_leaderboards import _token
            import psycopg2
            cn = psycopg2.connect(host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
                                  dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
                                  user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
                                  password=_token(), sslmode="require", connect_timeout=30)
            cur = cn.cursor()
            n = 0
            for dst in done_paths:
                nm = re.sub(r"_Report_FRAN.*", "", os.path.basename(dst))
                cur.execute("""UPDATE delimp_spectrum_regen_queue SET status='copied_to_hive', node=%s, note=%s
                               WHERE search_name=%s OR search_name=%s""",
                            (os.environ.get("COMPUTERNAME", "win"), dst, nm, re.sub(r"^\d{8}_\d{6}_", "", nm)))
                n += cur.rowcount
            cn.commit(); cn.close()
            print(f"  marked {n} regen-queue row(s) copied_to_hive")
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] queue update skipped: {str(e)[:100]}")


if __name__ == "__main__":
    main()
