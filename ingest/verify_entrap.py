import os, sys
sys.path.insert(0, "/quobyte/proteomics-grp/brett/glendon/fran_ingest")
os.environ.setdefault("DELIMP_PG_TOKEN_FILE", "/quobyte/proteomics-grp/brett/.pgfarm_token")
import backfill_fragments as bf, spectrum_lance as sln, xic_lance as xln
import lance, pyarrow.compute as pc

SID = "528e28ec-8634-5f53-aa28-026bbd3ab9d1"
c = bf._pg_conn(); cur = c.cursor()
def q(l, s, p=()):
    cur.execute(s, p); print(f"{l}: {cur.fetchall()}")

print("=== PG ===")
q("search", "SELECT search_name,search_engine,search_engine_version,n_precursors_total,n_proteins_total,n_protein_groups_total FROM delimp_searches WHERE id=%s", (SID,))
q("precursors", "SELECT count(*), count(*) FILTER (WHERE q_value<>q_value) FROM delimp_precursors WHERE search_id=%s", (SID,))
q("yeast entrapment hits", """SELECT count(DISTINCT p.protein_group) FROM delimp_proteins p
                              WHERE p.search_id=%s AND p.protein_group ILIKE %s""", (SID, "%YEAST%"))
q("versions across corpus", "SELECT search_engine, count(search_engine_version), count(*) FROM delimp_searches GROUP BY 1")

print("\n=== SPECTRUM LANE ===")
cur.execute("SELECT lance_path,n_precursors,n_fragments,content_md5 FROM delimp_spectrum_lane WHERE search_id=%s", (SID,))
sp, sn_p, sn_f, smd5 = cur.fetchone()
print(f"  {sp}\n  {sn_p:,} prec / {sn_f:,} frag  md5={smd5}")
print("  checksum:", "MATCH" if sln.content_md5(lance.dataset(sp).to_table().cast(sln.SCHEMA)) == smd5 else "MISMATCH")

print("\n=== XIC LANE ===")
cur.execute("SELECT lance_path,n_precursors,n_traces,content_md5 FROM delimp_xic_lane WHERE search_id=%s", (SID,))
xp, xn_p, xn_t, xmd5 = cur.fetchone()
print(f"  {xp}\n  {xn_p:,} prec / {xn_t:,} traces  md5={xmd5}")
t = lance.dataset(xp).to_table()
print("  checksum:", "MATCH" if xln.content_md5(t.cast(xln.SCHEMA)) == xmd5 else "MISMATCH")
print("  rows:", t.num_rows, "cols:", t.num_columns)
nt = [x for x in t["n_traces"].to_pylist() if x is not None]
print(f"  traces/precursor: min={min(nt)} max={max(nt)} mean={sum(nt)/len(nt):.1f}")
print(f"  MS1 traces={sum(t['n_ms1'].to_pylist()):,}  MS2 traces={sum(t['n_ms2'].to_pylist()):,}")

# SCIENTIFIC CHECK: does each XIC apex land at the precursor's reported apex RT?
import statistics
devs, checked = [], 0
for i in range(0, t.num_rows, max(1, t.num_rows // 400)):
    rt = t["rt"][i].as_py()
    lv = t["trace_ms_level"][i].as_py()
    rts = t["trace_rt"][i].as_py()
    ins = t["trace_intensity"][i].as_py()
    if rt is None: continue
    best_v, best_rt = -1.0, None
    for j, lvl in enumerate(lv):
        if lvl != 2: continue
        for a, b in zip(rts[j], ins[j]):
            if b > best_v: best_v, best_rt = b, a
    if best_rt is not None:
        devs.append(abs(best_rt - rt)); checked += 1
devs.sort()
print(f"\n  XIC apex vs reported apex RT ({checked} precursors sampled):")
print(f"    median |dRT| = {statistics.median(devs)*60:.2f} s   p90 = {devs[int(.9*len(devs))]*60:.2f} s   max = {max(devs)*60:.2f} s")
print(f"    within 6 s: {100*sum(1 for d in devs if d<0.1)/len(devs):.1f}%")
r = {k: t[k][0].as_py() for k in ("run","stripped_seq","charge","rt","n_traces","n_ms1","n_ms2")}
print("\n  sample row:", r)
print("  labels:", t["trace_label"][0].as_py()[:9])
print("  first MS2 trace len:", len(t["trace_rt"][0].as_py()[0]), "pts")
c.close()
