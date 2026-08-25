"""radiant_to_corpus.py — Radiant/Fulcrum -> the DIA-NN-shaped frame corpus_ingest already reads.

Fulcrum output is close to DIA-NN's but not close enough to feed straight through. FRAN_INGEST_
RADIANT_FRAGPIPE.md §4.5 suggests running the upstream `radiant_to_delimp.py` converter and using
the DIA-NN path unchanged; measured against the real poplar result, that would put bad data in the
corpus, so this module normalises instead. What it fixes, all verified on
/quobyte/.../poplar_test/radiant/out (220,523 rows, 18 runs):

1. DECOYS. The raw Fulcrum output has an explicit Decoy column (309 True / 220,214 False) and the
   CONVERTED parquet still carries those 309 rows -- with `>DECOY_` protein groups and, critically,
   EVERY ONE at q <= 0.01. Passed through, they enter the corpus as genuine identifications at 1%
   FDR: exactly the false positives FDR exists to exclude. (Same bug class as the Spectronaut decoy
   hazard, bus msg #49.) Filtered here by BOTH the boolean column and the `>DECOY_` prefix, because
   the converted file has only the latter.

2. PROTEIN.GROUP IS A FASTA HEADER, not an accession:
       >sp|Cont_P08779|K1C16_HUMAN Keratin, type I cytoskeletal 16 OS=Homo sapiens OX=9606 GN=KRT16
   Storing that whole line breaks protein grouping, the protein pages and contaminant detection.
   Reduced here to the accession(s), keeping the `Cont_` tag that marks contaminants.

3. NO Stripped.Sequence -- corpus_ingest REQUIRES it and raises without it. Derived by stripping
   modifications from Modified.Sequence.

4. GENES. §4.4 says to join them from `fulcrum-mbr-library.tsv`. That file, in this result, is a
   FRAGMENT-level spectral library with eight columns (ModifiedPeptide, PrecursorCharge,
   PrecursorMz, Tr_recalibrated, decoy, ProductMz, LibraryIntensity, QValue) and carries no protein
   or gene column at all, so that join cannot work. The genes are instead in the FASTA header that
   Fulcrum puts in Protein.Group -- `GN=KRT16` -- present on 100.0% of rows. Preference order:
   an already-populated Genes column (the upstream converter emits the FULL mapped set, e.g.
   'KRT2;KRT4;KRT6A'), then GN= from the header (the representative protein's gene), then the MBR
   library if a future version does carry the columns.

5. Run IS A CONTAINER URI:
       file:///mnt/results/radiant-results/<name>.mzML.radiantDIA
   which exists on no host. Reduced to <name>. Radiant reads mzML/Parquet only, so these are always
   Thermo .raw acquisitions -- never Bruker .d.

Reads EITHER the Spark partition directory (fulcrum-results/, 32 part-*.parquet files) or an
already-converted delimp_report.parquet. Reading one partition gets you a fraction of the data, so
the directory is always read as a dataset.
"""
from __future__ import annotations

import os
import re

# Fulcrum drops Protein.Names/Genes; the MBR library carries them. Without the join every Radiant
# protein lands with no gene and FRAN's gene views, species aggregation and word-hunt skip the rows.
MBR_LIBRARY = "fulcrum-mbr-library.tsv"

_MOD = re.compile(r"\[[^\]]*\]|\([^)]*\)")          # DIA-NN/ProForma bracket styles
_ACC = re.compile(r"\b(?:sp|tr)\|([A-Za-z0-9_.\-]+)\|")
_GN = re.compile(r"\bGN=([^\s]+)")                    # UniProt gene name inside the FASTA header


def genes_from_header(raw: str) -> str | None:
    """Gene name(s) out of the FASTA header Fulcrum stores in Protein.Group.

    Must run BEFORE clean_protein_group(), which discards the description the gene lives in.
    """
    hits = list(dict.fromkeys(_GN.findall(str(raw or ""))))
    return ";".join(hits) if hits else None


def strip_mods(modseq: str) -> str:
    """Modified.Sequence -> Stripped.Sequence. Underscores are Spectronaut-style terminators."""
    return _MOD.sub("", str(modseq or "")).replace("_", "").upper()


def clean_protein_group(raw: str) -> str:
    """FASTA header line -> the accession(s) only.

    '>sp|Cont_P08779|K1C16_HUMAN Keratin, ... GN=KRT16 PE=1 SV=4'  ->  'Cont_P08779'
    Multi-protein groups keep every accession, ';'-joined, matching how the corpus stores
    Spectronaut/DIA-NN groups. Falls back to the first whitespace-delimited token so an unexpected
    format degrades to something short and greppable rather than a 100-character sentence.
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    accs = _ACC.findall(s)
    if accs:
        return ";".join(dict.fromkeys(accs))
    return s.lstrip(">").split()[0]


def clean_run(raw: str) -> str:
    """Container URI -> raw basename. Both suffixes come off; the acquisition is Thermo .raw."""
    name = str(raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    for suf in (".radiantDIA", ".mzML", ".mzml", ".raw"):
        if name.endswith(suf):
            name = name[: -len(suf)]
    return name


def is_decoy(pg: str, decoy_flag=None) -> bool:
    if decoy_flag is not None and str(decoy_flag).strip().lower() in ("true", "1", "1.0"):
        return True
    return "DECOY" in str(pg or "").upper()


def _read_any(path: str):
    """Fulcrum partition DIRECTORY or a single parquet -> pandas DataFrame."""
    import pandas as pd
    if os.path.isdir(path):
        import pyarrow.dataset as ds
        # A dataset, never a single part-* file: one partition is a fraction of the result
        # (7,322 of 220,523 rows in the poplar example).
        return ds.dataset(path, format="parquet").to_table().to_pandas()
    if str(path).lower().endswith(".parquet"):
        return pd.read_parquet(path)
    return pd.read_csv(path, sep="\t", low_memory=False)


def _gene_map(report_path: str) -> dict:
    """{accession -> (gene, protein_name)} from the MBR library sitting beside the results."""
    import pandas as pd
    root = report_path if os.path.isdir(report_path) else os.path.dirname(report_path)
    for cand in (os.path.join(root, MBR_LIBRARY),
                 os.path.join(root, "..", MBR_LIBRARY),
                 os.path.join(root, "radiant_results", MBR_LIBRARY)):
        if os.path.exists(cand):
            try:
                lib = pd.read_csv(cand, sep="\t", low_memory=False)
            except Exception:                       # noqa: BLE001 - the join is best-effort
                return {}
            cols = {c.lower(): c for c in lib.columns}
            pg = cols.get("protein.group") or cols.get("proteingroup") or cols.get("protein.ids")
            gn = cols.get("genes") or cols.get("gene")
            pn = cols.get("protein.names") or cols.get("proteinname")
            if not (pg and (gn or pn)):
                return {}
            out = {}
            for _, r in lib[[c for c in (pg, gn, pn) if c]].drop_duplicates().iterrows():
                for a in clean_protein_group(r[pg]).split(";"):
                    if a:
                        out.setdefault(a, (r[gn] if gn else None, r[pn] if pn else None))
            return out
    return {}


def to_diann_frame(report_path: str, q_max: float = 0.01):
    """Radiant result -> a DataFrame with DIA-NN column names, ready for corpus_ingest's reader."""
    df = _read_any(report_path)
    n_in = len(df)

    pg_col = "Protein.Group" if "Protein.Group" in df.columns else None
    if pg_col is None:
        raise ValueError(f"Radiant report has no Protein.Group; columns={list(df.columns)[:15]}")
    decoy_col = "Decoy" if "Decoy" in df.columns else None
    mask = df.apply(lambda r: not is_decoy(r[pg_col], r[decoy_col] if decoy_col else None), axis=1)
    df = df[mask]
    n_decoy = n_in - len(df)

    df = df.copy()
    df["Run"] = df["Run"].map(clean_run)
    # Genes BEFORE the protein group is cleaned: cleaning throws away the description they live in.
    header_genes = df[pg_col].map(genes_from_header)
    df["Protein.Group"] = df[pg_col].map(clean_protein_group)
    if "Stripped.Sequence" not in df.columns:
        df["Stripped.Sequence"] = df["Modified.Sequence"].map(strip_mods)
    if "Precursor.Id" not in df.columns:
        df["Precursor.Id"] = (df["Modified.Sequence"].astype(str)
                              + df["Precursor.Charge"].astype(str))
    # Fulcrum names the global q-values differently from DIA-NN; map so the corpus filter applies.
    if "Global.Q.Value" not in df.columns and "Global.Precursor.Q.Value" in df.columns:
        df["Global.Q.Value"] = df["Global.Precursor.Q.Value"]
    if "PG.Q.Value" not in df.columns and "Global.PG.Q.Value" in df.columns:
        df["PG.Q.Value"] = df["Global.PG.Q.Value"]

    # Gene resolution, richest source first. Never leave this empty: without genes, FRAN's gene
    # views, species aggregation and word-hunt silently skip every Radiant row.
    def _blank(col):
        return col.isna() | (col.astype(str).str.strip().isin(("", "nan", "None")))
    if "Genes" not in df.columns:
        df["Genes"] = None
    fill = _blank(df["Genes"])
    df.loc[fill, "Genes"] = header_genes[fill]                       # GN= from the header
    still = _blank(df["Genes"])
    if still.any():
        gmap = _gene_map(report_path)                                # only if a library has them
        if gmap:
            df.loc[still, "Genes"] = df.loc[still, "Protein.Group"].map(
                lambda pg: next((gmap[a][0] for a in str(pg).split(";") if a in gmap), None))
    n_gene = int((~_blank(df["Genes"])).sum())

    if "Q.Value" in df.columns:
        before = len(df)
        df = df[df["Q.Value"] <= q_max]
        n_q = before - len(df)
    else:
        n_q = 0
    print(f"  radiant: {n_in:,} rows -> dropped {n_decoy:,} decoys, {n_q:,} above q<={q_max}"
          f" -> {len(df):,}; runs={df['Run'].nunique()}; "
          f"genes {n_gene:,}/{len(df):,} ({100*n_gene/max(len(df),1):.1f}%)", flush=True)
    return df


if __name__ == "__main__":
    import sys
    d = to_diann_frame(sys.argv[1])
    print(f"\ncolumns: {list(d.columns)}")
    print(f"genes populated: {d['Genes'].notna().sum():,}/{len(d):,}"
          if "Genes" in d.columns else "no Genes column")
    print(d[["Run", "Stripped.Sequence", "Precursor.Charge", "Protein.Group"]].head(3).to_string())
