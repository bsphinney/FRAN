import os, sys
sys.path.insert(0, "/quobyte/proteomics-grp/brett/glendon/fran_ingest")
os.environ.setdefault("DELIMP_PG_TOKEN_FILE", "/quobyte/proteomics-grp/brett/.pgfarm_token")
import backfill_fragments as bf, spectrum_lance as sln, xic_lance as xln
import lance

c = bf._pg_conn(); cur = c.cursor()

print("=== engine versions now ===")
cur.execute("""SELECT search_engine, count(*) AS total, count(search_engine_version) AS with_ver
               FROM delimp_searches GROUP BY 1 ORDER BY 2 DESC""")
for r in cur.fetchall(): print("  ", r)
cur.execute("""SELECT search_engine_version, count(*) FROM delimp_searches
               WHERE search_engine_version IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 8""")
print("  top versions:", cur.fetchall())
cur.execute("SELECT search_name, search_engine_version FROM delimp_searches WHERE search_name IN ('Ver_15','Dog_yeast_entrapment_SN21')")
print("  our two:", cur.fetchall())

print("\n=== checksum fix: do EXISTING registry rows verify now? ===")
cur.execute("""SELECT search_name, lance_path, content_md5, n_precursors FROM delimp_spectrum_lane
               WHERE content_md5 IS NOT NULL AND n_precursors > 50000
               ORDER BY n_precursors DESC LIMIT 4""")
big = cur.fetchall()
for nm, p, md5, n in big:
    try:
        t = lance.dataset(p).to_table().cast(sln.SCHEMA)
        nch = t.column(0).num_chunks
        ok = sln.content_md5(t) == md5
        print(f"  {str(nm)[:34]:34s} {n:>9,} prec  chunks={nch:<3d} -> {'MATCH' if ok else 'MISMATCH'}")
    except Exception as e:
        print(f"  {str(nm)[:34]:34s} ERROR {str(e)[:50]}")

print("\n=== the two new datasets ===")
for lane, mod, sid in (("delimp_spectrum_lane", sln, "528e28ec-8634-5f53-aa28-026bbd3ab9d1"),
                       ("delimp_xic_lane", xln, "528e28ec-8634-5f53-aa28-026bbd3ab9d1"),
                       ("delimp_spectrum_lane", sln, "6cc72911-5a9d-51cc-a38d-04a8327fc490")):
    cur.execute(f"SELECT search_name, lance_path, content_md5 FROM {lane} WHERE search_id=%s", (sid,))
    row = cur.fetchone()
    if not row: continue
    nm, p, md5 = row
    t = lance.dataset(p).to_table().cast(mod.SCHEMA)
    print(f"  {lane:22s} {str(nm)[:28]:28s} chunks={t.column(0).num_chunks:<3d} -> {'MATCH' if mod.content_md5(t)==md5 else 'MISMATCH'}")
c.close()
