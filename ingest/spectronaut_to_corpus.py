"""spectronaut_to_corpus.py — adapter so the DE-LIMP corpus can ingest Spectronaut
precursor reports, not just DIA-NN report.parquet.

Spectronaut's "Normal"/BGS Factory report is long-format (one row per precursor x run)
with prefixed columns (R. run, PG. protein group, PEP. peptide, EG. elution group /
precursor, FG. fragment group / charge). Column names drift across Spectronaut
versions, so columns are resolved by FUZZY regex (first match wins) — mirroring the
validated patterns in R/server_comparator.R.

This module ONLY normalizes Spectronaut rows into the same per-precursor record shape
that ingest_search.py already writes for DIA-NN. Wiring: in ingest_search.py, detect
input type (parquet+DIA-NN columns -> existing path; .tsv/.xls with EG./FG. columns ->
this adapter) and feed these records to the SAME writer/bulk-insert. No schema change.

STATUS: ready; UNTESTED until HIVE maintenance ends (the dog Spectronaut report lives at
/nfs/lssc0/.../Ameer_Taha_spectronaut/.../*BGS Factory Report (Normal).tsv and PG Farm
is the write target). Validate first with:  python spectronaut_to_corpus.py REPORT.tsv --dry-run
"""
from __future__ import annotations

import re
import sys

import pandas as pd

# Ordered regex per normalized field — first matching column header wins.
# AI-TRAINING COMPLETENESS: to feed future model training the export MUST carry,
# beyond IDs: iRT (cross-run RT), IM/1-K0, and the MS2 FRAGMENT SPECTRUM
# (F.FrgMz + F.PeakArea/F.NormalizedPeakArea + F.FrgType/FrgNum/FrgZ/FrgLossType).
# Configure the Spectronaut report schema to include the F.* fragment columns
# (a precursor-only report has no spectra -> no spectrum-prediction training data).
COLMAP = {
    "run":          [r"^R\.FileName$", r"^R\.Raw ?File ?Name$", r"^R\.Replicate$", r"^R\."],
    "protein_group":[r"^PG\.ProteinGroups$", r"^PG\.ProteinAccessions$", r"ProteinGroups?$", r"^Group$"],
    "gene":         [r"^PG\.Genes$", r"Genes?$"],
    "organism":     [r"^PEP\.AllOccurringOrganisms$", r"AllOccurringOrganisms$", r"^PG\.Organisms$", r"Organisms?$"],
    "pg_q_value":   [r"^PG\.Qvalue$", r"PG.*Qvalue"],
    "stripped_seq": [r"^PEP\.StrippedSequence$", r"StrippedSequence$"],
    "modified_seq": [r"^EG\.ModifiedSequence$", r"^EG\.ModifiedPeptide$", r"ModifiedSequence$", r"^EG\.PrecursorId$"],
    "charge":       [r"^FG\.Charge$", r"Charge$"],
    "q_value":      [r"^EG\.Qvalue$", r"EG.*Qvalue"],
    "precursor_mz": [r"^FG\.PrecMz$", r"^EG\.PrecursorMz$", r"PrecMz$", r"PrecursorMz$"],
    "rt":           [r"^EG\.ApexRT$", r"^EG\.MeanApexRT$", r"^EG\.RTEmpirical$", r"ApexRT$"],
    "irt":          [r"^EG\.iRT$", r"^EG\.IRTEmpirical$", r"^EG\.RTPredicted$", r"\biRT\b"],
    "im":           [r"^EG\.IonMobility$", r"^EG\.ApexIonMobility$", r"^FG\.ApexIonMobility$", r"IonMobility$"],
    "ccs":          [r"^EG\.CCS$", r"\bCCS\b"],
    "intensity":    [r"^FG\.Quantity$", r"^FG\.MS2Quantity$", r"^EG\.TotalQuantity.*$", r"Quantity$"],
    # PRECURSOR-level normalized quantity only (FG./EG.); never a per-fragment area
    # (F.NormalizedPeakArea) — that would put one fragment's value on the precursor row — and
    # never a score (EG.NormalizedCscore). Null if the schema has no precursor-level one.
    "norm_intensity":[r"^FG\.Normalized.*(MS2|PeakArea|Quantity)", r"^EG\.Normalized.*(Quantity|Intensity)"],
    "pep":          [r"^EG\.PEP$", r"PosteriorErrorProbability"],
    # acquisition metadata (for CE/instrument-conditioned training; often absent in the report)
    "instrument":   [r"^R\.Instrument", r"Instrument"],
    "ce":           [r"CollisionEnergy", r"\bNCE\b", r"^FG\.CollisionEnergy"],
    # MS2 fragment spectrum (the spectrum-prediction training signal)
    "frg_mz":       [r"^F\.FrgMz$", r"FrgMz$"],
    "frg_type":     [r"^F\.FrgType$", r"FrgType$"],
    "frg_num":      [r"^F\.FrgNum$", r"FrgNum$"],
    "frg_charge":   [r"^F\.FrgZ$", r"^F\.FrgCharge$", r"^F\.Charge$", r"FrgZ$"],
    "frg_loss":     [r"^F\.FrgLossType$", r"FrgLossType$"],
    "frg_intensity":[r"^F\.PeakArea$", r"^F\.NormalizedPeakArea$", r"^F\.MeasuredRelativeIntensity$", r"PeakArea$"],
    "frg_ion":      [r"^F\.FrgIon$", r"FrgIon$"],                       # e.g. "y4" (label)
    "frg_measured_relint":  [r"^F\.MeasuredRelativeIntensity$", r"MeasuredRelativeIntensity$"],
    "frg_predicted_relint": [r"^F\.PredictedRelativeIntensity$", r"PredictedRelativeIntensity$"],  # library ref
}
FRAG_FIELDS = ("frg_mz", "frg_type", "frg_num", "frg_charge", "frg_loss", "frg_intensity")

# Common Spectronaut mod names -> UniMod (best-effort ProForma; extend as needed).
_MOD_UNIMOD = {"Carbamidomethyl": 4, "Oxidation": 35, "Acetyl": 1,
               "Phospho": 21, "Deamidation": 7, "Gln->pyro-Glu": 28, "Glu->pyro-Glu": 27}


def _norm(col: str) -> str:
    """Spectronaut's PARQUET export renames the dotted prefix with an underscore
    (F.FrgMz -> F_FrgMz, FG.XICDBID -> FG_XICDBID). Turn the FIRST underscore after the
    leading letter-prefix back into a dot so the dotted COLMAP regexes match either format."""
    return re.sub(r"^([A-Za-z]+)_", r"\1.", col)


def match_column(header: list[str], pats: list[str]):
    """First header column (TSV dotted OR parquet underscored) matching any pattern."""
    for pat in pats:
        hit = next((c for c in header if re.search(pat, c, re.I) or re.search(pat, _norm(c), re.I)), None)
        if hit:
            return hit
    return None


def resolve_columns(header: list[str]) -> dict:
    out = {}
    for field, pats in COLMAP.items():
        hit = match_column(header, pats)
        if hit:
            out[field] = hit
    return out


def _strip_seq(modseq: str, stripped: str | None) -> str:
    if isinstance(stripped, str) and stripped:
        return stripped.upper()
    # derive from Spectronaut modified seq: drop _, [..], (..)
    s = re.sub(r"\[[^\]]*\]|\([^)]*\)|_", "", str(modseq or ""))
    return s.upper()


def _to_proforma(modseq: str) -> str | None:
    if not isinstance(modseq, str) or not modseq:
        return None
    def repl(m):
        name = m.group(1).split(" ")[0].split("(")[0].strip()
        uid = _MOD_UNIMOD.get(name)
        return f"[UNIMOD:{uid}]" if uid else m.group(0)
    s = re.sub(r"\[([^\]]*)\]", repl, modseq.strip("_"))
    return s


def report_columns(path: str) -> list[str]:
    """Header column names of a Spectronaut report — TSV or Parquet."""
    if str(path).lower().endswith(".parquet"):
        import pyarrow.parquet as pq
        return list(pq.read_schema(path).names)
    return list(pd.read_csv(path, sep="\t", nrows=0).columns)


def iter_chunks(path: str, usecols: list, chunksize: int = 200_000):
    """Yield DataFrames of a Spectronaut report (TSV or Parquet), restricted to usecols.
    Parquet is columnar+compressed (much smaller on disk, faster to read); both stream in
    chunks so a multi-GB fragment-level report never has to fit in memory at once."""
    if str(path).lower().endswith(".parquet"):
        import pyarrow.parquet as pq
        for batch in pq.ParquetFile(path).iter_batches(batch_size=chunksize, columns=usecols):
            yield batch.to_pandas()
    else:
        for chunk in pd.read_csv(path, sep="\t", usecols=usecols, chunksize=chunksize, low_memory=False):
            yield chunk


def iter_records(report_path: str, q_max: float = 0.01, chunksize: int = 200_000):
    """Yield normalized per-precursor records (dict) from a Spectronaut report (TSV/Parquet)."""
    cols = resolve_columns(report_columns(report_path))
    need = ["run", "stripped_seq", "charge"]
    missing = [n for n in need if n not in cols]
    if missing:
        raise ValueError(f"Spectronaut report missing required columns for {missing}; resolved={cols}")
    usecols = list(dict.fromkeys(cols.values()))
    for chunk in iter_chunks(report_path, usecols, chunksize):
        for _, r in chunk.iterrows():
            qv = r.get(cols["q_value"]) if "q_value" in cols else None
            if qv is not None and pd.notna(qv) and float(qv) > q_max:
                continue
            modseq = r.get(cols["modified_seq"]) if "modified_seq" in cols else None
            # run NAME (matches the DIA-NN convention corpus_ingest expects); strip a trailing
            # vendor extension so corpus_ingest can build a clean raw_path.
            run = re.sub(r"\.(d|raw|mzml|wiff|htrms)$", "", str(r.get(cols["run"])), flags=re.I)
            nmods = len(re.findall(r"\[[^\]]*\]|\([^)]*\)", str(modseq))) if isinstance(modseq, str) else 0
            rec = {
                "run": run,                                # corpus_ingest keys on "run"
                "stripped_seq": _strip_seq(modseq, r.get(cols.get("stripped_seq", ""))),
                "modified_seq_diann": str(modseq) if modseq is not None else None,
                "modified_seq_proforma": _to_proforma(modseq),
                "mods": None, "n_mods": nmods,             # full mod JSON TODO; proforma carries detail
                "charge": int(r[cols["charge"]]) if pd.notna(r.get(cols["charge"])) else None,
                "precursor_mz": _f(r, cols, "precursor_mz"),
                "rt": _f(r, cols, "rt"),
                "irt": _f(r, cols, "irt"),                 # cross-run RT (AI-training)
                "im": _f(r, cols, "im"),                   # 1/K0 (AI-training)
                "iim": None,                               # Spectronaut has no indexed IM column
                "ccs": _f(r, cols, "ccs"),
                "q_value": _f(r, cols, "q_value"),
                "global_q_value": None,                    # Spectronaut: EG.Qvalue only (no separate global)
                "pg_q_value": _f(r, cols, "pg_q_value"),
                "pep": _f(r, cols, "pep"),
                "intensity": _f(r, cols, "intensity"),
                "normalized_intensity": _f(r, cols, "norm_intensity"),
                "instrument": str(r.get(cols["instrument"])) if "instrument" in cols else None,
                "ce": _f(r, cols, "ce"),
                "organism": str(r.get(cols["organism"])) if "organism" in cols else None,
                "protein_group": str(r.get(cols["protein_group"])) if "protein_group" in cols else None,
                "gene": str(r.get(cols["gene"])) if "gene" in cols else None,
                "engine": "spectronaut",
            }
            # MS2 fragment (spectrum-prediction training signal): present only if the
            # export carries F.* columns. Fragment-level reports emit one fragment per
            # row -> the ingest groups by (raw_path, modified_seq_diann, charge) to
            # assemble the observed spectrum.
            if "frg_mz" in cols:
                rec["fragment"] = {
                    "mz": _f(r, cols, "frg_mz"),
                    "type": str(r.get(cols["frg_type"])) if "frg_type" in cols else None,
                    "num": _f(r, cols, "frg_num"),
                    "charge": _f(r, cols, "frg_charge"),
                    "loss": str(r.get(cols["frg_loss"])) if "frg_loss" in cols else None,
                    "intensity": _f(r, cols, "frg_intensity"),
                    "ion": str(r.get(cols["frg_ion"])) if "frg_ion" in cols else None,
                    "measured_relint": _f(r, cols, "frg_measured_relint"),
                    "predicted_relint": _f(r, cols, "frg_predicted_relint"),
                }
            yield rec


def _f(row, cols, field):
    if field not in cols:
        return None
    v = row.get(cols[field])
    try:
        return float(v) if pd.notna(v) else None
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    path = sys.argv[1]
    dry = "--dry-run" in sys.argv
    hdr = list(pd.read_csv(path, sep="\t", nrows=0).columns)
    resolved = resolve_columns(hdr)
    print(f"resolved {len(resolved)}/{len(COLMAP)} fields:")
    for k in COLMAP:
        print(f"  {k:16s} -> {resolved.get(k, '*** NOT FOUND ***')}")
    print("\nAI-TRAINING DATA AVAILABILITY in this export:")
    print(f"  MS2 fragment spectrum : {'YES' if 'frg_mz' in resolved and 'frg_intensity' in resolved else 'NO  -> re-export with F.FrgMz + F.PeakArea/F.NormalizedPeakArea + F.FrgType/FrgNum/FrgZ'}")
    print(f"  iRT (cross-run RT)    : {'YES' if 'irt' in resolved else 'no (RT only)'}")
    print(f"  ion mobility / CCS    : {'YES' if ('im' in resolved or 'ccs' in resolved) else 'no'}")
    print(f"  collision energy/instr: {'YES' if ('ce' in resolved or 'instrument' in resolved) else 'no (often absent in the report; pull from run metadata)'}")
    if dry:
        n = 0; runs = set(); pgs = set()
        for rec in iter_records(path):
            n += 1; runs.add(rec["raw_path"]); pgs.add(rec["protein_group"])
            if n <= 3:
                print("  sample:", {k: rec[k] for k in ("raw_path", "stripped_seq", "charge", "rt", "im", "q_value", "protein_group", "gene")})
            if n >= 500_000:
                break
        print(f"DRY RUN: {n} precursor rows @ q<=0.01, {len(runs)} runs, {len(pgs)} protein groups")
    else:
        print("(no --dry-run) — wire iter_records() into ingest_search.py's writer to load into PG Farm.")
