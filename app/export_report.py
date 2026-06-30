"""Export ONE FRAN search as a DIA-NN-style report.parquet that limpa::readDIANN() / the DE-LIMP DPC
pipeline loads directly. The user downloads it from FRAN and uploads it to DE-LIMP (the HF Space or a
local install) to run the LIMPA differential-expression analysis on their own.

READ-ONLY: goes through the app's governed db.query (public-layer allowlist). Mirrors the verified
scripts/db_to_diann_report.py logic, but returns parquet BYTES (no temp file) for streaming.

Only searches that were re-ingested with per-precursor intensity can be exported (you can't quantify
without it); we surface a clear message otherwise.
"""
from __future__ import annotations

import io
import os

from .db import query

# Hard cap so a giant search can't OOM the small App Service instance. ~3M precursor-rows is already a
# very large DIA search; beyond that we tell the user to ask the core for a direct export.
MAX_ROWS = 3_000_000


class ExportError(RuntimeError):
    pass


def _run_basename(raw_path: str) -> str:
    if not raw_path:
        return ""
    base = os.path.basename(str(raw_path).rstrip("/\\"))
    low = base.lower()
    for ext in (".d", ".raw", ".mzml", ".wiff", ".dia"):
        if low.endswith(ext):
            return base[: -len(ext)]
    return base


def _safe_name(name: str | None, search_id) -> str:
    base = name or f"search_{search_id}"
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in base)


def _md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join(("" if c is None else str(c)).replace("|", "\\|") for c in r) + " |")
    return "\n".join(out)


def build_research_brief(search_id: str) -> tuple[bytes, str, dict]:
    """Emit a markdown 'research brief' for ONE search — a pre-filled input packet to hand to a
    HIVE-connected Claude running the proteomics-pipeline skill ("analyze my proteomics data"): raw-file
    locations, organism + the FASTA to download, the search engine (DIA→DIA-NN), and the CoreOmics
    conditions for the LIMPA differential-expression design. READ-ONLY via the governed db layer."""
    meta_rows = query(
        "SELECT id, search_name, search_engine, search_engine_version, n_raw_files FROM delimp_searches WHERE id = %s",
        (search_id,), tables=["delimp_searches"],
    )
    if not meta_rows:
        raise ExportError("Search not found.")
    m = meta_rows[0]

    runs = query(
        """SELECT srf.raw_path, rf.raw_basename, rf.platform, rf.acquisition_method,
                  rf.instrument_model, rf.gradient_minutes,
                  COALESCE(sm.organism_name, sm.predicted_organism_name) AS organism_name,
                  sm.organism_taxon_id
           FROM search_raw_files srf
           JOIN raw_files rf ON rf.raw_path = srf.raw_path
           LEFT JOIN delimp_sample_metadata sm ON sm.raw_path = srf.raw_path
           WHERE srf.search_id = %s ORDER BY rf.raw_basename""",
        (search_id,), tables=["search_raw_files", "raw_files", "delimp_sample_metadata"],
    )
    if not runs:
        raise ExportError("No raw files recorded for this search.")

    # distinct organism(s) + taxid(s) across the runs
    orgs = sorted({(r["organism_name"], r["organism_taxon_id"]) for r in runs if r.get("organism_name")})
    # acquisition: DIA vs DDA (from the per-run acquisition_method / platform)
    acqs = sorted({(r.get("acquisition_method") or "").upper() for r in runs if r.get("acquisition_method")})
    is_dia = any("DIA" in a for a in acqs) or not acqs
    platforms = sorted({r.get("platform") for r in runs if r.get("platform")})
    instruments = sorted({r.get("instrument_model") for r in runs if r.get("instrument_model")})

    # conditions for the LIMPA design: CoreOmics samples for the linked submission (best-effort)
    conds = []
    prov = query("SELECT coreomics_submission_id FROM delimp_search_provenance WHERE search_id = %s LIMIT 1",
                 (search_id,), tables=["delimp_search_provenance"])
    sub_id = prov[0]["coreomics_submission_id"] if prov else None
    if sub_id:
        conds = query(
            "SELECT sample_name, condition_name FROM coreomics_samples_cache WHERE submission_id = %s ORDER BY sample_name",
            (str(sub_id),), tables=["coreomics_samples_cache"],
        )

    name = m.get("search_name") or f"search_{search_id}"
    engine_line = "**DIA-NN** (DIA acquisition)" if is_dia else "**Sage** (DDA) — or FragPipe if preferred"
    L = []
    L.append(f"# Reprocess & analyze: {name}\n")
    L.append("> Hand this to a Claude **with HIVE access** running the **proteomics-pipeline** skill "
             "(trigger: *“analyze my proteomics data”*). Everything the skill normally asks for is "
             "pre-filled below: raw-file locations, organism + FASTA to download, the search engine, "
             "and the experimental design for LIMPA differential expression.\n")
    L.append("## Source search (already in the FRAN corpus)")
    L.append(f"- **FRAN search_id:** `{search_id}`")
    L.append(f"- **Name:** {name}")
    L.append(f"- **Original engine:** {m.get('search_engine') or '?'} {m.get('search_engine_version') or ''}".rstrip())
    L.append(f"- **Platform:** {', '.join(platforms) or 'unknown'}  ·  **Acquisition:** {', '.join(acqs) or '(assume DIA)'}")
    L.append(f"- **Instrument:** {', '.join(instruments) or '⚠ not captured — read from the .d/.raw metadata'}")
    L.append(f"- **Runs:** {len(runs)}\n")

    L.append("## 1. Raw MS files to search")
    L.append(_md_table(["Run", "Path (as stored — Windows/share form)", "Platform", "Acq", "Gradient (min)", "Organism"],
                       [[r.get("raw_basename") or "", f"`{r.get('raw_path')}`", r.get("platform") or "",
                         r.get("acquisition_method") or "", r.get("gradient_minutes") or "", r.get("organism_name") or "—"]
                        for r in runs]))
    L.append("\n> **Locating these on HIVE:** paths beginning `R:\\` are the proteomics/Flinders share — "
             "map to its NFS mount on HIVE (`/nfs/...` or `/quobyte/proteomics-grp/...`). Confirm the mount, "
             "then glob the `.d`/`.raw` by basename if the literal path differs.\n")

    L.append("## 2. Organism & FASTA to download")
    if orgs:
        for org, tax in orgs:
            L.append(f"- **{org}**" + (f" — NCBI taxid **{tax}**" if tax else " — ⚠ taxid not resolved"))
            if tax:
                L.append(f"  - UniProt reference proteome (preferred): search proteomes for organism_id `{tax}`, "
                         f"download its FASTA; or UniProtKB: "
                         f"`https://rest.uniprot.org/uniprotkb/stream?query=organism_id:{tax}&format=fasta`")
        L.append("- Add the workflow's **contaminants** (cRAP) library.")
    else:
        L.append("- ⚠ **Organism not recorded for this search.** Determine it before searching: read the Spectronaut "
                 "`ExperimentSetupOverview` (\"Protein Databases Used\") from the original `.sne`, or run a DIAMOND-nr "
                 "species scan on the identified peptides. Then download that organism's UniProt reference proteome.")
    L.append("")

    L.append("## 3. Search engine")
    L.append(f"- {engine_line}. Use the **pinned** engine version from the matched workflow in "
             "`github.com/bsphinney/DE-LIMP` `workflows/` (match on acquisition + instrument + organism).\n")

    L.append("## 4. Experimental design (conditions) for LIMPA DE")
    if conds:
        L.append(_md_table(["Sample", "Condition"], [[c.get("sample_name") or "", c.get("condition_name") or "—"] for c in conds]))
        L.append("\n> Map each **Run** above to its **Sample/Condition** (match the run basename to the sample name; "
                 "the original submission's sample sheet is authoritative). Conditions define the LIMPA contrast.\n")
    else:
        L.append("- ⚠ No CoreOmics conditions linked. Ask the user for the group/condition of each run before LIMPA DE.\n")

    L.append("## 5. Steps (proteomics-pipeline skill)")
    L.append("1. Locate the raw files (§1) on HIVE.")
    L.append("2. Fetch the matched validated workflow from `bsphinney/DE-LIMP` `workflows/` (acquisition + instrument + organism).")
    L.append("3. Download the pinned search engine for HIVE + the FASTA (§2) + contaminants.")
    L.append("4. Run the engine → `report.parquet` (DIA-NN main report).")
    L.append("5. Run LIMPA differential expression (`run_de.R --method dpc`) using the conditions (§4).")
    L.append("6. Report results + `methods.txt` + a link back to the validated workflow + this FRAN search_id.\n")
    L.append(f"_Generated by FRAN for search `{search_id}`. Quantitative re-analysis of corpus data; "
             "verify the FASTA/organism against the original submission before publishing._")

    md = "\n".join(L).encode("utf-8")
    fname = f"{_safe_name(name, search_id)}_research_brief.md"
    return md, fname, {"runs": len(runs), "organisms": [o[0] for o in orgs], "has_conditions": bool(conds),
                       "search_name": name}


def build_resubmit_brief(submission_id: str) -> tuple[bytes, str, dict]:
    """'Re-search this data' packet for an UN-INGESTED CoreOmics submission — its raw data is on the
    service directory but NOT yet in FRAN. Hands a HIVE/Flinders-connected Claude (proteomics-pipeline
    skill) everything to re-search it: the service-directory folder (Windows + HIVE/Flinders paths), the
    full submission info, organism + FASTA, and the conditions for LIMPA. READ-ONLY via the db layer."""
    sub = query(
        """SELECT submission_id, pi_first_name, pi_last_name, submitter_first_name, submitter_last_name,
                  submitter_email, pi_email, institute, submitted_at::date AS submitted_at, num_samples,
                  organism, species, prot_or_pep, proteomics_type, mass_spec_wanted, sample_prep,
                  gradient_length, dia, tmt, description, other_info
           FROM coreomics_submissions_cache WHERE submission_id = %s""",
        (submission_id,), tables=["coreomics_submissions_cache"])
    if not sub:
        raise ExportError("Submission not found.")
    s = sub[0]
    loc = query(
        """SELECT service_folder, service_folder_win, run_count, in_fran
           FROM delimp_submission_service_dir WHERE submission_id = %s""",
        (submission_id,), tables=["delimp_submission_service_dir"])
    loc = loc[0] if loc else None
    samples = query(
        "SELECT sample_name, condition_name FROM coreomics_samples_cache WHERE submission_id = %s ORDER BY sample_name",
        (submission_id,), tables=["coreomics_samples_cache"])
    pi = " ".join(x for x in (s.get("pi_first_name"), s.get("pi_last_name")) if x) or "?"
    submitter = " ".join(x for x in (s.get("submitter_first_name"), s.get("submitter_last_name")) if x)
    import re as _re
    from collections import Counter as _Counter
    org = s.get("organism") or s.get("species")
    ptype = (s.get("proteomics_type") or "").strip()
    desc = s.get("description") or ""
    win, rel = (loc.get("service_folder_win"), loc.get("service_folder")) if loc else (None, None)
    # — smarter, safer interpretation (these fixes came from a real re-analysis of submission cab61a9f9996) —
    # narrative organism? (a sentence describing a construct, not a resolvable taxon)
    org_narrative = bool(org) and (len(str(org).split()) > 4 or any(
        w in str(org).lower() for w in ("clone", "express", "recombinant", "tagged", " from ",
                                        "construct", " host", "cell line", "transfect")))
    # Only a clean binomial ("Genus species", optional strain in parens) is safe to drop into a
    # UniProt organism_name: URL. Short non-species junk ('CHO', 'E coli', 'Human/Toxoplasma', 'what?')
    # must NOT be interpolated — it returns nothing or two organisms in one query. (bug-logic #2)
    org_is_binomial = bool(_re.match(r"^[A-Z][a-z]+ [a-z]+(?:\s*\([^)]*\))?$", str(org or "").strip()))
    inline_seqs = _re.findall(r"[ACDEFGHIKLMNPQRSTVWY]{25,}", desc.replace("\n", " "))  # explicit target sequences
    cond_counts = _Counter((c.get("condition_name") or "").strip() for c in samples if (c.get("condition_name") or "").strip())
    singleton_design = bool(cond_counts) and all(v < 2 for v in cond_counts.values())     # no replicates → not DE
    acq_known = bool(ptype) or str(s.get("dia")).strip() not in ("", "None", "—")
    is_dia = "dia" in ptype.lower() or str(s.get("dia")).lower() in ("true", "yes", "1")

    L = [f"# Re-search this data: {pi} — CoreOmics submission `{submission_id}`\n",
         "> Hand to a Claude **with HIVE / Flinders access** running the **proteomics-pipeline** skill "
         "(*“analyze my proteomics data”*), or run a search engine directly. This submission's raw data is on the "
         "service directory but **not yet ingested into FRAN**. Re-search it — **or re-use a prior search if one is "
         "already in the folder** (see §1).\n",
         "## Submission (CoreOmics)",
         f"- **PI:** {pi}" + (f"  ·  {s['pi_email']}" if s.get("pi_email") else "")]
    if submitter:
        L.append(f"- **Submitter:** {submitter}" + (f"  ·  {s['submitter_email']}" if s.get("submitter_email") else ""))
    L.append(f"- **Institute:** {s.get('institute') or '—'}")
    L.append(f"- **Submitted:** {s.get('submitted_at') or '—'}  ·  **Samples:** "
             f"{s.get('num_samples') if s.get('num_samples') is not None else '—'}")
    L.append(f"- **Organism (as submitted):** {org or '⚠ not given'}")
    L.append(f"- **Type:** {ptype or '⚠ not recorded'}  ·  **Prot/Pep:** {s.get('prot_or_pep') or '—'}  ·  "
             f"**DIA:** {s.get('dia') or '—'}  ·  **TMT:** {s.get('tmt') or '—'}")
    for label, key in (("MS requested", "mass_spec_wanted"), ("Gradient", "gradient_length"),
                       ("Sample prep", "sample_prep"), ("Description", "description"), ("Other info", "other_info")):
        if s.get(key):
            L.append(f"- **{label}:** {s[key]}")

    L.append("\n## 1. Raw MS files to search — service directory")
    if win or rel:
        L.append("The raw data **lives on the Flinders storage** (`/nfs/lssc0/flinders/proteomics`), which Windows "
                 "boxes see over SMB as the `R:` drive. Use whichever path matches the machine you're on — they all "
                 "point at the same folder:")
        if win:
            L.append(f"- **Windows (SMB):** `{win}`")
        if rel:
            L.append(f"- **Linux / HIVE (Flinders NFS):** `/nfs/lssc0/flinders/proteomics/Data/lab/service/{rel}`")
            L.append(f"- **macOS (SMB):** `/Volumes/proteomics/Data/lab/service/{rel}`")
        L.append("  - If your HIVE node mounts the Flinders share at a different root (e.g. `/quobyte/proteomics-grp/…`), "
                 "confirm the mount; the path under `…/Data/lab/service/` is identical. If the literal folder differs, "
                 "glob by the PI/submitter + date tokens in the folder name.")
        L.append(f"- **List the files actually in the folder** (typically `.d` for timsTOF or `.raw` for Thermo — about "
                 f"{loc.get('run_count') if loc else '?'} runs) and search all of them.")
        L.append("- **Check the folder for an existing search FIRST** — a prior `SpN_*` (Spectronaut), `report.parquet` / "
                 "`report.tsv` (DIA-NN), `*.pdResult`, `combined_protein.tsv`, or a target `*.fasta`. If one is present, "
                 "prefer re-using or comparing it over re-searching from scratch.")
    else:
        L.append("- ⚠ No service-directory folder is recorded for this submission. Find the raw data from the "
                 "submitter/PI name + submission date on the Flinders share (Windows `R:`, Linux "
                 "`/nfs/lssc0/flinders/proteomics`, macOS `/Volumes/proteomics`).")

    L.append("\n## 2. Search database (FASTA) — choose the RIGHT one")
    if inline_seqs:
        L.append(f"- ⚠ **This submission gives explicit target protein/peptide sequence(s) in its description "
                 f"({len(inline_seqs)} found).** The sample is a defined/recombinant construct — **search against THOSE "
                 "sequences** as the primary target DB (materialize them into a FASTA, or use an existing target FASTA "
                 "already in the data folder, e.g. `*Peps*.fasta` / `*MycTag*.fasta`), **not** a whole reference proteome.")
    if org_narrative:
        L.append(f"- ⚠ **The Organism field is free text, not a single organism** — *“{org}”*. Do **not** feed it into a "
                 "UniProt `organism_name:` query (it returns nothing). Parse the roles instead:")
        L.append("  - **source organism** (where the sequence is from), **expression host** (what the protein was made "
                 "in — the real host-cell-protein background for a purified/recombinant sample), **cloning host** (vector "
                 "propagation only — not in the tube). Resolve each to an NCBI taxid; use the **expression host's** "
                 "proteome (+ cRAP) as the background DB.")
        L.append("  - **Diagnostic:** also include the **source-organism** proteome as a **mis-assignment control** — if "
                 "peptides map there but that organism isn't physically in the tube, it flags an artifact (exactly what "
                 "surfaced the spurious *L. amylovorus* hits in this submission's first pass).")
    elif org_is_binomial:
        L.append(f"- **Organism:** {org}. For a **whole lysate**, use its UniProt reference proteome — resolve the taxid "
                 f"and **verify the URL returns entries**: `https://rest.uniprot.org/uniprotkb/stream?query="
                 f"organism_name:\"{org}\"&format=fasta`. For a **purified/recombinant** sample, use the expression-host "
                 "proteome as background instead.")
    elif org:
        # org is present but NOT a clean species name (e.g. 'CHO', 'E coli', 'Human/Toxoplasma') — do
        # NOT build a UniProt URL from it; it would return nothing or mix organisms.
        L.append(f"- ⚠ **Organism field is `{org}` — not a clean species name.** Resolve it to the actual species "
                 "(it may name a cell line, an abbreviation, or several organisms). If multiple organisms are involved, "
                 "build a **combined** FASTA (all of them + cRAP). Verify each UniProt query returns entries before searching.")
    else:
        L.append("- ⚠ Organism not given — determine the source organism / expression host (sample sheet, submitter) "
                 "before choosing a database.")
    L.append("- Always add the **cRAP contaminants** library, and validate any UniProt URL resolves to real entries "
             "before using it.")

    L.append("\n## 3. Search engine — gated on acquisition")
    if acq_known and is_dia:
        L.append("- Acquisition is **DIA** → use **DIA-NN**.")
    elif acq_known and not is_dia:
        L.append("- Acquisition is **DDA** → use **Sage** (or FragPipe).")
    else:
        L.append("- ⚠ **Acquisition not recorded for this submission.** DETECT it from the raw-file metadata first "
                 "(`detect_acquisition.py`), then choose — **DIA → DIA-NN**, **DDA → Sage/FragPipe**. Do **not** default "
                 "to one engine while acquisition is unknown.")
    L.append("- Use the pinned engine version from the matched validated workflow in `github.com/bsphinney/DE-LIMP` "
             "`workflows/` (match on acquisition + instrument + organism).\n")

    L.append("## 4. Experimental design")
    if samples:
        L.append(_md_table(["Sample", "Condition"],
                           [[c.get("sample_name") or "", c.get("condition_name") or "—"] for c in samples]))
        if singleton_design:
            L.append("\n> ⚠ **Every condition has a single replicate** — there is no within-group variance, so a "
                     "LIMPA/limma **differential-expression** contrast is statistically invalid here. Treat this as a "
                     "**detection / dose-response** study: per-target identification (detected yes/no per run), sequence "
                     "coverage, and intensity vs. the condition variable. Only run LIMPA DE if real replicates exist.\n")
        else:
            L.append("\n> Map each raw run to its sample/condition (run basename ↔ sample name). These conditions define "
                     "the LIMPA contrast (needs ≥2 replicates per group).\n")
    else:
        L.append("- ⚠ No CoreOmics sample/condition sheet cached. Ask the user for the group of each run before any DE.\n")

    L.append("## 5. Steps (proteomics-pipeline skill)")
    L.append("1. Locate the folder (§1); **list its files** and check for a prior search result or target FASTA already there.")
    L.append("2. Determine acquisition (§3); fetch the matched validated workflow from `bsphinney/DE-LIMP` `workflows/`.")
    L.append("3. Build the search DB (§2 — target sequences / expression-host background / reference proteome, as appropriate) + cRAP.")
    L.append("4. Run the engine → its main report (`report.parquet` for DIA-NN).")
    if singleton_design:
        L.append("5. **Detection / dose-response readout** (no replicates → skip LIMPA DE): per-target identification + "
                 "coverage + intensity vs. the condition variable.")
    else:
        L.append("5. Run LIMPA differential expression with the conditions (§4) — only with ≥2 replicates per group.")
    L.append("6. **Ingest the result back into FRAN** (`corpus_ingest`) so this submission is no longer un-ingested; "
             "report results + `methods.txt` + a link back to this submission.\n")
    L.append(f"_Generated by FRAN for CoreOmics submission `{submission_id}` (un-ingested; raw data on the service "
             "directory). Verify the FASTA/organism/acquisition against the original submission before publishing._")

    md = "\n".join(L).encode("utf-8")
    fname = f"{_safe_name(pi.replace(' ', '_'), submission_id)}_re-search_this_data.md"
    return md, fname, {"pi": pi, "folder": rel, "has_conditions": bool(samples), "organism": org,
                       "org_narrative": org_narrative, "inline_seqs": len(inline_seqs),
                       "singleton_design": singleton_design, "acq_known": acq_known}


def build_report_parquet(search_id: str) -> tuple[bytes, str, dict]:
    """Return (parquet_bytes, filename, meta) for one search. Raises ExportError with a user-readable
    message when the search has no quantifiable precursors or is too large."""
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq

    meta_rows = query(
        "SELECT id, search_name, search_engine FROM delimp_searches WHERE id = %s",
        (search_id,), tables=["delimp_searches"],
    )
    if not meta_rows:
        raise ExportError("Search not found.")
    meta = meta_rows[0]

    # row-count guard first (cheap COUNT, bounded) so we fail fast on something too big to build in RAM
    cnt = query(
        "SELECT COUNT(*) AS n FROM delimp_precursors WHERE search_id = %s AND intensity IS NOT NULL",
        (search_id,), tables=["delimp_precursors"], fetch="val",
    ) or 0
    if cnt == 0:
        raise ExportError("This search has no per-precursor intensity stored, so a quantitative "
                          "report can't be exported. (It predates the protein-group/intensity "
                          "re-ingest — ask the Proteomics Core to re-ingest it.)")
    if cnt > MAX_ROWS:
        raise ExportError(f"This search is very large ({cnt:,} precursor rows) — too big to export "
                          f"through the browser. Ask the Proteomics Core for a direct export.")

    gene_rows = query(
        "SELECT protein_group, MAX(gene) AS gene FROM delimp_proteins WHERE search_id = %s GROUP BY 1",
        (search_id,), tables=["delimp_proteins"],
    )
    gene_map = {r["protein_group"]: (r["gene"] or "") for r in gene_rows if r["protein_group"] is not None}

    prec = query(
        """SELECT raw_path, stripped_seq, modified_seq_diann, charge, precursor_mz, rt, im,
                  q_value, global_q_value, pg_q_value, intensity, normalized_intensity, protein_group
           FROM delimp_precursors WHERE search_id = %s AND intensity IS NOT NULL""",
        (search_id,), tables=["delimp_precursors"],
    )
    df = pd.DataFrame(prec)
    if df.empty:
        raise ExportError("No quantifiable precursors for this search.")

    # Proteotypic = peptide maps to exactly one protein_group across this search.
    pg_per_pep = df.groupby("stripped_seq")["protein_group"].nunique()
    proteotypic = df["stripped_seq"].map(lambda s: 1 if pg_per_pep.get(s, 0) == 1 else 0)

    mod_seq = df["modified_seq_diann"].where(
        df["modified_seq_diann"].notna() & (df["modified_seq_diann"] != ""), df["stripped_seq"])
    charge = df["charge"]
    precursor_id = mod_seq.astype(str) + charge.fillna(0).astype("Int64").astype(str)
    pg = df["protein_group"].fillna("").astype(str)
    genes = pg.map(lambda g: gene_map.get(g, ""))
    norm = df["normalized_intensity"].where(df["normalized_intensity"].notna(), df["intensity"])

    out = pd.DataFrame({
        "Run": df["raw_path"].map(_run_basename),
        "File.Name": df["raw_path"].astype(str),
        "Protein.Group": pg, "Protein.Ids": pg, "Genes": genes, "Protein.Names": pg,
        "Proteotypic": proteotypic.astype("int64"),
        "Stripped.Sequence": df["stripped_seq"].fillna("").astype(str),
        "Modified.Sequence": mod_seq.astype(str),
        "Precursor.Id": precursor_id.astype(str),
        "Precursor.Charge": charge.astype("float64"),
        "Precursor.Quantity": df["intensity"].astype("float64"),
        "Precursor.Normalised": norm.astype("float64"),
        "Q.Value": df["q_value"].astype("float64"),
        "Global.Q.Value": df["global_q_value"].astype("float64"),
        "PG.Q.Value": df["pg_q_value"].astype("float64"),
        # limpa::readDIANN() defaults request Lib.Q.Value / Lib.PG.Q.Value; emit them (aliased to the
        # global / PG q-values) so a bare readDIANN("...parquet") succeeds with no extra arguments.
        "Lib.Q.Value": df["global_q_value"].astype("float64"),
        "Lib.PG.Q.Value": df["pg_q_value"].astype("float64"),
        "RT": df["rt"].astype("float64"),
        "IM": df["im"].astype("float64"),
        "Precursor.Mz": df["precursor_mz"].astype("float64"),
    })

    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pandas(out, preserve_index=False), buf)
    fname = f"{_safe_name(meta.get('search_name'), search_id)}_report.parquet"
    return buf.getvalue(), fname, {"rows": len(out), "runs": int(out["Run"].nunique()),
                                   "protein_groups": int(out["Protein.Group"].nunique()),
                                   "search_name": meta.get("search_name"), "engine": meta.get("search_engine")}
