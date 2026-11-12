"""Probe MS1/MS2 resolving power of every Orbitrap run in CORPUS_PROBE.tsv (one array task's share).

Writes ONE part file per task (one writer per file on Quobyte/NFS). Merge with --merge.
"""
import csv, json, os, subprocess, sys
ROOT = "/nfs/lssc0/flinders/proteomics/Data/FRAN_diann"
PROBE_TSV = ROOT + "/CORPUS_PROBE.tsv"
PARTS = ROOT + "/_resolution_probe"
OUT = ROOT + "/CORPUS_RESOLUTION.tsv"
PY = "/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python"
PROBE = "/quobyte/proteomics-grp/fran/engines/bin/thermo_acq_probe.py"
COLS = ["run", "path", "instrument", "ms1_resolution", "ms2_resolution", "acquisition", "error"]
ORBI = ("orbitrap", "exploris", "lumos", "fusion", "exactive", "eclipse")

def orbitrap_rows():
    with open(PROBE_TSV) as fh:
        return [r for r in csv.DictReader(fh, delimiter="\t")
                if any(k in (r["instrument"] or "").lower() for k in ORBI)]

def probe(task, ntasks):
    os.makedirs(PARTS, exist_ok=True)
    rows = orbitrap_rows()[task::ntasks]
    part = os.path.join(PARTS, "part_%03d.tsv" % task)
    with open(part, "w", newline="") as fh:
        w = csv.DictWriter(fh, COLS, delimiter="\t")
        w.writeheader()
        for r in rows:
            out = {"run": r["run"], "path": r["path"], "instrument": r["instrument"]}
            try:
                p = subprocess.run([PY, PROBE, r["path"], "--max-scans", "400", "--method-lines", "0"],
                                   capture_output=True, text=True, timeout=300)
                d = json.loads(p.stdout) if p.stdout.strip() else {"error": (p.stderr or "")[-200:]}
            except Exception as e:                                     # noqa: BLE001
                d = {"error": "%s: %s" % (type(e).__name__, str(e)[:150])}
            out.update(ms1_resolution=d.get("ms1_resolution") or "", ms2_resolution=d.get("ms2_resolution") or "",
                       acquisition=d.get("acquisition") or "", error=(d.get("error") or "").replace("\t", " ").replace("\n", " "))
            w.writerow(out)
            fh.flush()
    print("task %d: %d runs -> %s" % (task, len(rows), part))

def merge():
    import glob
    rows = []
    for f in sorted(glob.glob(PARTS + "/part_*.tsv")):
        with open(f) as fh:
            rows += list(csv.DictReader(fh, delimiter="\t"))
    tmp = OUT + ".tmp"
    with open(tmp, "w", newline="") as fh:
        w = csv.DictWriter(fh, COLS, delimiter="\t")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, OUT)
    ok = sum(1 for r in rows if r["ms1_resolution"] and r["ms2_resolution"])
    print("merged %d runs (%d with both resolutions, expected %d) -> %s" % (len(rows), ok, len(orbitrap_rows()), OUT))

if __name__ == "__main__":
    if sys.argv[1] == "--merge":
        merge()
    else:
        probe(int(sys.argv[1]), int(sys.argv[2]))
