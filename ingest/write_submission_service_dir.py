#!/usr/bin/env python3
"""Write the AI disk-match results (CoreOmics submission -> service-directory folder) into a new
FRAN table `delimp_submission_service_dir`, so customer pages can show EVERY submission's data
location + whether it's in FRAN yet (analyzed) or only on the share (un-ingested).

One row per submission. Additive, idempotent (upsert by submission_id). Default DRY-RUN.
Usage: python3 write_submission_service_dir.py [--commit]
"""
import os, sys, re, argparse
HERE=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,HERE)
import refresh_leaderboards as _r
import psycopg2, psycopg2.extras
S="/private/tmp/claude-501/-Users-brettphinney-Documents-claude/f2dfbf3c-291a-4516-9718-bdb98f3fcd70/scratchpad"
ap=argparse.ArgumentParser(); ap.add_argument("--commit",action="store_true"); a=ap.parse_args()

# disk matches
disk={}
for i in range(1,9):
    p=f"{S}/disk_match_{i:02d}.tsv"
    if not os.path.exists(p): continue
    for ln in open(p).read().splitlines():
        c=ln.split("\t"); sid=c[0].strip()
        if sid in("submission_id","") or len(sid)<6: continue
        if (c[1].strip().lower() if len(c)>1 else "")!="found": continue
        folder=c[3].strip() if len(c)>3 else ""
        if folder: disk[sid]={"folder":folder,"conf":(c[2].strip().lower() if len(c)>2 else ""),
                              "clue":c[4].strip() if len(c)>4 else ""}

# FRAN coverage (exact project-folder match) + matched submissions
fran_folders=set(); fran_subs=set()
for ln in open(f"{S}/fran_searches.tsv").read().splitlines()[1:]:
    c=ln.split("\t")
    if len(c)<5: continue
    fran_folders.add(f"{c[1]}/{c[2]}/{c[3]}/{c[4]}".rstrip("/").lower())
    fran_folders.add(f"{c[1]}/{c[2]}/{c[3]}".rstrip("/").lower())
if os.path.exists(f"{S}/matches_FINAL.tsv"):
    for ln in open(f"{S}/matches_FINAL.tsv").read().splitlines()[1:]:
        c=ln.split("\t")
        if len(c)>1: fran_subs.add(c[1].strip())
def in_fran(sid,folder): return (sid in fran_subs) or (folder.lower() in fran_folders)

# run counts (.d dirs + raw/sne files under each matched folder)
from collections import Counter
runcount=Counter(); pre="/Volumes/proteomics/Data/lab/service/"
for fn in (f"{S}/service_tree.txt", f"{S}/service_files.txt"):
    if not os.path.exists(fn): continue
    for ln in open(fn):
        ln=ln.rstrip("\n")
        if not ln.startswith(pre): continue
        rel=ln[len(pre):]; low=rel.lower()
        if not (low.endswith(".d") or low.endswith(".sne") or low.endswith(".raw") or low.endswith(".wiff")): continue
        for sid,info in disk.items():
            if rel.startswith(info["folder"]+"/") or rel==info["folder"]:
                runcount[info["folder"]]+=1; break

def Rpath(f): return "R:\\Data\\lab\\service\\"+f.replace("/","\\")
rows=[]
for sid,info in disk.items():
    rows.append((sid, info["folder"], Rpath(info["folder"]), info["folder"].split("/")[0],
                 in_fran(sid,info["folder"]), runcount.get(info["folder"],0), info["conf"], info["clue"]))
print(f"disk-match rows: {len(rows)}  (in_fran={sum(1 for r in rows if r[4])}, backlog={sum(1 for r in rows if not r[4])})")

DDL="""CREATE TABLE IF NOT EXISTS delimp_submission_service_dir (
  submission_id   TEXT PRIMARY KEY,
  service_folder  TEXT,          -- relative: campus/customer[/pi]/project
  service_folder_win TEXT,       -- R:\\Data\\lab\\service\\... (Windows form for ingest)
  campus          TEXT,
  in_fran         BOOLEAN,       -- TRUE = analyzed in FRAN; FALSE = on share, un-ingested
  run_count       INTEGER,       -- .d/.sne/.raw files under the folder
  match_confidence TEXT,
  clue            TEXT,
  matched_by      TEXT DEFAULT 'ai-disk-match',
  matched_at      TIMESTAMP DEFAULT NOW()
);"""

if not a.commit:
    print("\n[dry-run] would CREATE TABLE delimp_submission_service_dir + upsert the rows above.")
    print("sample:")
    for r in rows[:6]:
        print(f"   {r[0]} in_fran={r[4]} runs={r[5]} {r[6]:6} {r[1][:55]}")
    print("\nre-run with --commit to write.")
else:
    con=psycopg2.connect(host="pgfarm.library.ucdavis.edu",port=5432,
     dbname="uc-davis-genome-center-proteomics-core/delimp",
     user="genome-proteomics-service-account",password=_r._token(),sslmode="require",
     connect_timeout=30,options="-c statement_timeout=60000")
    cur=con.cursor(); cur.execute(DDL)
    psycopg2.extras.execute_batch(cur,
        """INSERT INTO delimp_submission_service_dir
             (submission_id,service_folder,service_folder_win,campus,in_fran,run_count,match_confidence,clue,matched_by,matched_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'ai-disk-match',NOW())
           ON CONFLICT (submission_id) DO UPDATE SET
             service_folder=EXCLUDED.service_folder, service_folder_win=EXCLUDED.service_folder_win,
             campus=EXCLUDED.campus, in_fran=EXCLUDED.in_fran, run_count=EXCLUDED.run_count,
             match_confidence=EXCLUDED.match_confidence, clue=EXCLUDED.clue, matched_at=NOW()""",
        rows, page_size=200)
    con.commit()
    cur.execute("SELECT count(*), count(*) FILTER (WHERE in_fran), count(*) FILTER (WHERE NOT in_fran) FROM delimp_submission_service_dir")
    t,inf,bk=cur.fetchone()
    print(f"\n[committed] delimp_submission_service_dir: {t} rows (in_fran={inf}, backlog={bk}).")
    con.close()
