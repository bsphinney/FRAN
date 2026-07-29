import os, sys, glob
sys.path.insert(0, "/quobyte/proteomics-grp/brett/glendon/fran_ingest")
import spectrum_lance as sln, xic_lance as xln
import lance

print("=== 1. CHECKSUM: is content_md5 sensitive to Arrow CHUNKING? ===")
for label, path, mod in (
    ("Ver_15 (7,525 rows)", "/quobyte/proteomics-grp/brett/glendon/spectra_lance/Ver_15.lance", sln),
    ("Entrap spectra (18,287)", "/quobyte/proteomics-grp/brett/glendon/spectra_lance/Dog_yeast_entrapment_SN21.lance", sln),
    ("Entrap XIC (18,287)", "/quobyte/proteomics-grp/brett/glendon/xic_lance/Dog_yeast_entrapment_SN21.xic.lance", xln),
):
    t = lance.dataset(path).to_table().cast(mod.SCHEMA)
    n_chunks = t.column(0).num_chunks
    md5_asis = mod.content_md5(t)
    md5_comb = mod.content_md5(t.combine_chunks())
    print(f"  {label:26s} chunks={n_chunks:3d}  as-is={md5_asis[:12]}  combined={md5_comb[:12]}  differ={md5_asis!=md5_comb}")

print("\n=== 2. Is the ENGINE VERSION inside the report parquet itself? ===")
import pyarrow.parquet as pq
cands = [
    "/quobyte/proteomics-grp/brett/sn21/20260727_102317_Dog yeast entrapment SN21/20260720_101232_Dog yeast entrapment SN21_Report.parquet",
]
cands += glob.glob("/nfs/lssc0/flinders/proteomics/Data/FRAN_reports/*/*/*Report_FRAN (Normal).parquet")[:4]
for p in cands:
    try:
        f = pq.ParquetFile(p)
        md = f.metadata
        kv = f.schema_arrow.metadata or {}
        kv_s = {k.decode(): v.decode()[:60] for k, v in list(kv.items())[:6]}
        print(f"  {os.path.basename(p)[:44]:44s}")
        print(f"     created_by = {md.created_by}")
        print(f"     schema kv  = {kv_s if kv_s else '(none)'}")
    except Exception as e:
        print("  ERR", os.path.basename(p)[:40], str(e)[:60])
