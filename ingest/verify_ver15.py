import os, sys
sys.path.insert(0, "/quobyte/proteomics-grp/brett/glendon/fran_ingest")
os.environ.setdefault("DELIMP_PG_TOKEN_FILE", "/quobyte/proteomics-grp/brett/.pgfarm_token")
import backfill_fragments as bf, spectrum_lance as sln
import lance, pyarrow.compute as pc

SID = "6cc72911-5a9d-51cc-a38d-04a8327fc490"
c = bf._pg_conn(); cur = c.cursor()

def q(label, sql, params=()):
    cur.execute(sql, params); print(f"{label}: {cur.fetchall()}")

print("=== PG ===")
q("search", "SELECT search_name,search_engine,n_raw_files,n_precursors_total,n_proteins_total,status,output_dir FROM delimp_searches WHERE id=%s", (SID,))
q("sample meta", "SELECT raw_path,organism_name,organism_taxon_id FROM delimp_sample_metadata WHERE raw_path IN (SELECT DISTINCT raw_path FROM delimp_proteins WHERE search_id=%s)", (SID,))
q("precursor rows", "SELECT count(*) FROM delimp_precursors WHERE search_id=%s", (SID,))
q("q_value sanity (null/NaN/max/min)", """SELECT count(*) FILTER (WHERE q_value IS NULL),
                              count(*) FILTER (WHERE q_value <> q_value),
                              max(q_value), min(q_value)
                       FROM delimp_precursors WHERE search_id=%s""", (SID,))
q("distinct seqs / charges", "SELECT count(DISTINCT stripped_seq), count(DISTINCT charge) FROM delimp_precursors WHERE search_id=%s", (SID,))
q("im/rt populated", "SELECT count(*) FILTER (WHERE im IS NOT NULL), count(*) FILTER (WHERE rt IS NOT NULL), round(avg(im)::numeric,3) FROM delimp_precursors WHERE search_id=%s", (SID,))
q("proteins", "SELECT count(*), count(*) FILTER (WHERE pg_q_value IS NULL) FROM delimp_proteins WHERE search_id=%s", (SID,))
q("provenance", "SELECT real_search_name,output_dir,n_raw_files,report_path FROM delimp_search_provenance WHERE search_id=%s", (SID,))
q("lane registry", "SELECT search_name,lance_path,n_precursors,n_fragments,lance_version FROM delimp_spectrum_lane WHERE search_id=%s", (SID,))
cur.execute("SELECT content_md5,lance_path FROM delimp_spectrum_lane WHERE search_id=%s", (SID,))
reg_md5, lpath = cur.fetchone()

print("\n=== LANCE ===")
ds = lance.dataset(lpath)
t = ds.to_table()
print("rows:", t.num_rows, " cols:", t.num_columns)
dec = t["is_decoy"].to_pylist()
print("is_decoy -> True:", sum(1 for x in dec if x), " False:", sum(1 for x in dec if x is False), " null:", sum(1 for x in dec if x is None))
fl = [x for x in pc.list_value_length(t["frg_mz"]).to_pylist() if x is not None]
print(f"fragments: total={sum(fl):,}  per-precursor min={min(fl)} max={max(fl)} mean={sum(fl)/len(fl):.1f}")
for col in ("ms1_iso_measured", "prec_window", "signal_to_noise", "rt", "irt_empirical", "frg_predicted_relint", "frg_mass_acc_ppm"):
    if col in t.schema.names:
        print(f"  {col}: nulls={t[col].null_count}/{t.num_rows}")
r0 = {k: t[k][0].as_py() for k in ("search_name","run","stripped_seq","charge","rt","im","q_value","prec_window","signal_to_noise","is_decoy") if k in t.schema.names}
print("sample row:", r0)
print("frg_ion[0]:", t["frg_ion"][0].as_py()[:8] if "frg_ion" in t.schema.names else "n/a")
print("frg_mz[0]:", [round(x,3) for x in t["frg_mz"][0].as_py()[:8]])

print("\n=== CHECKSUM ===")
fn = getattr(sln, "content_md5", None) or getattr(sln, "_content_md5", None) or getattr(sln, "dataset_md5", None)
new = fn(lpath) if fn else None
print("registry:", reg_md5, "\nrecomputed:", new, "->", "MATCH" if new == reg_md5 else ("no helper found" if new is None else "MISMATCH"))
c.close()
