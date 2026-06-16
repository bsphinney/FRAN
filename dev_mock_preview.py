"""DEV-ONLY screenshot harness. Monkeypatches the DB layer with schema-accurate
mock rows so we can preview the populated UI without the live PG Farm credential.
NOT part of the deployed app. Run: python dev_mock_preview.py  (serves :7862)
Mock magnitudes mirror the documented live corpus (~2.7M precursors)."""
import random, math, sys
sys.path.insert(0, ".")
from app import db, queries  # noqa

random.seed(7)

def _norm(s):
    s = " ".join(str(s).lower().split())
    return s

def mock_query(sql, params=None, *, tables, fetch="all"):
    s = _norm(sql)
    # ---- overview counts ----
    if "count(*) from delimp_searches" in s and "raw" not in s:
        return 3
    if "count(*) from delimp_proteins" in s:
        return 253456
    if "count(*) from raw_files" in s:
        return 263
    if "count(*) from delimp_precursors where im is not null" in s:
        return 2724314
    if "count(*) from delimp_precursors" in s and "distinct" not in s:
        return 2724314
    if "count(distinct stripped_seq) from delimp_precursors" in s and "where" not in s:
        return 214882
    if "count(distinct protein_group) from delimp_proteins" in s:
        return 14913
    if "count(distinct organism_taxon_id)" in s:
        return 4
    # ---- distributions ----
    if "from delimp_sample_metadata" in s and "group by organism_name" in s:
        return [
            {"organism": "Canis lupus familiaris", "organism_taxon_id": 9615, "n_runs": 16},
            {"organism": "Mus musculus", "organism_taxon_id": 10090, "n_runs": 120},
            {"organism": "Homo sapiens", "organism_taxon_id": 9606, "n_runs": 95},
            {"organism": "Bos taurus", "organism_taxon_id": 9913, "n_runs": 16},
        ]
    if "from raw_files" in s and "group by platform" in s:
        return [
            {"platform": "timstof", "acquisition_method": "diaPASEF", "instrument_model": "timsTOF HT", "n_runs": 215},
            {"platform": "timstof", "acquisition_method": "ddaPASEF", "instrument_model": "timsTOF Pro 2", "n_runs": 32},
            {"platform": "orbitrap", "acquisition_method": "DIA", "instrument_model": "Orbitrap Exploris 480", "n_runs": 16},
        ]
    if "group by search_engine" in s:
        return [
            {"search_engine": "diann", "n_searches": 3, "n_precursors": 2724314},
        ]
    if "group by charge" in s:
        return [{"charge": z, "n": int(2724314 * w)} for z, w in [(1,0.04),(2,0.61),(3,0.27),(4,0.07),(5,0.01)]]
    # ---- recent / list searches ----
    if "from delimp_searches" in s and "order by" in s:
        rows = [
            {"id": "11111111-1111-1111-1111-111111111111", "search_name": "fp215_diann25_requant", "search_engine": "diann", "search_engine_version": "2.5", "pipeline_id": "dpc_quant_limpa", "pipeline_version": "1.0", "status": "completed", "sharing_status": "private", "n_raw_files": 215, "n_precursors_total": 2392626, "n_proteins_total": 226000, "fasta_n_proteins": 21000, "completed_at": "2026-06-12T20:11:00Z", "submitted_at": "2026-06-12T09:00:00Z", "ingested_at": "2026-06-13T01:22:00Z", "delimp_version": "3.11.66", "doi": None, "pride_accession": None},
            {"id": "22222222-2222-2222-2222-222222222222", "search_name": "diann251_clean16", "search_engine": "diann", "search_engine_version": "2.5.1", "pipeline_id": "dpc_quant_limpa", "pipeline_version": "1.0", "status": "completed", "sharing_status": "private", "n_raw_files": 16, "n_precursors_total": 165844, "n_proteins_total": 13607, "fasta_n_proteins": 45000, "completed_at": "2026-06-11T18:30:00Z", "submitted_at": "2026-06-11T08:00:00Z", "ingested_at": "2026-06-12T22:05:00Z", "delimp_version": "3.11.66", "doi": None, "pride_accession": None},
            {"id": "33333333-3333-3333-3333-333333333333", "search_name": "bovine_plasma_DIA", "search_engine": "diann", "search_engine_version": "2.3", "pipeline_id": "maxlfq_limma", "pipeline_version": "1.0", "status": "completed", "sharing_status": "private", "n_raw_files": 16, "n_precursors_total": 165844, "n_proteins_total": 13849, "fasta_n_proteins": 38000, "completed_at": "2026-06-10T12:00:00Z", "submitted_at": "2026-06-10T07:00:00Z", "ingested_at": "2026-06-10T14:00:00Z", "delimp_version": "3.11.45", "doi": None, "pride_accession": None},
        ]
        return rows
    # ---- im scatter ----
    if "im is not null and rt is not null" in s:
        n = (params[-1] if params else 4000)
        pts = []
        for _ in range(min(n, 4000)):
            z = random.choices([2,3,4],[0.6,0.3,0.1])[0]
            rt = random.uniform(2, 60)
            base = {2:0.85,3:1.05,4:1.25}[z]
            im = base + random.uniform(-0.12,0.18) + rt*0.002
            pts.append({"rt": round(rt,2),"im": round(im,3),"charge": z,"precursor_mz": round(random.uniform(350,1200),3),"intensity_log2": round(random.uniform(12,26),1)})
        return pts
    # ---- peptide search ----
    if "group by stripped_seq" in s and "order by n_precursors" in s:
        peps = ["LLPGFMCQGGDFTR","VLDALQAIK","SAMPLEPEPTIDER","HVFGQAAK","DLGEEHFK","YICDNQDTISSK"]
        return [{"stripped_seq": p,"n_precursors": random.randint(40,900),"n_modforms": random.randint(1,4),"n_charges": random.randint(1,3),"n_runs": random.randint(2,210),"n_searches": random.randint(1,3),"best_q_value": round(random.uniform(1e-6,9e-4),7),"has_im": True,"max_engines": random.choice([1,1,2])} for p in peps]
    if "count(distinct stripped_seq) from delimp_precursors where" in s:
        return 6
    # ---- protein search ----
    if "group by protein_group" in s and "order by sum_precursors" in s:
        return [{"protein_group": pg,"gene": g,"n_searches": random.randint(1,3),"n_runs": random.randint(2,210),"sum_unique_peptides": random.randint(5,120),"sum_precursors": random.randint(50,5000),"any_contaminant": False} for pg,g in [("P02769","ALB"),("P00761","TRY1"),("Q29443","TF"),("P81644","APOA2")]]
    if "count(distinct protein_group) from delimp_proteins where" in s:
        return 4
    return [] if fetch == "all" else (None if fetch in ("one","val") else None)

db.query = mock_query
queries.query = mock_query
db.CACHE.clear()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=7862)
