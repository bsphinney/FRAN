import os, glob, zipfile, re, sys
sys.path.insert(0, "/quobyte/proteomics-grp/brett/glendon/fran_ingest")
from engine_version import _SN, _SN_ANALYSIS

ROOT = "/nfs/lssc0/flinders/proteomics/Data/FRAN_reports"
SNE = "/nfs/lssc0/flinders/proteomics/Data/FRAN_SNE_export"

dirs = [d for d in glob.glob(os.path.join(ROOT, "*")) if os.path.isdir(d)]
zips = glob.glob(os.path.join(ROOT, "*.zip"))
print(f"FRAN_reports: {len(dirs)} dirs, {len(zips)} zips")

def ver_from_dir(d):
    for pat in ("*setup.txt", "*[Aa]nalysis*og*.txt", os.path.join("RunSummaries", "*RunOverview.tsv")):
        for p in glob.glob(os.path.join(d, "*", pat)) + glob.glob(os.path.join(d, pat)):
            try:
                head = open(p, errors="replace").read(20000)
            except OSError:
                continue
            m = _SN.search(head) or _SN_ANALYSIS.search(head)
            if m:
                return m.group(1)
    return None

hit = miss = 0
examples = []
for d in dirs:
    v = ver_from_dir(d)
    if v:
        hit += 1
        if len(examples) < 5:
            examples.append((os.path.basename(d)[:38], v))
    else:
        miss += 1
print(f"  dirs WITH a version source: {hit}   without: {miss}")
print("  examples:", examples)

zh = 0
zex = []
for z in zips[:40]:
    try:
        with zipfile.ZipFile(z) as zf:
            names = zf.namelist()
            cand = [n for n in names if re.search(r"setup\.txt$|[Aa]nalysis.*og.*\.txt$|RunOverview\.tsv$", n)]
            found = None
            for n in cand[:6]:
                head = zf.read(n)[:20000].decode("utf-8", "replace")
                m = _SN.search(head) or _SN_ANALYSIS.search(head)
                if m:
                    found = m.group(1); break
            if found:
                zh += 1
                if len(zex) < 5: zex.append((os.path.basename(z)[:34], found))
    except Exception as e:
        pass
print(f"  zips sampled: {min(40,len(zips))}  with a version inside: {zh}")
print("  zip examples:", zex)

sne_dirs = [d for d in glob.glob(os.path.join(SNE, "*")) if os.path.isdir(d)]
print(f"FRAN_SNE_export: {len(sne_dirs)} dirs")
