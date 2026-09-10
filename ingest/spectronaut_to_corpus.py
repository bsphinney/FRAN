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
    "q_value":      [r"^EG\.Qvalue$", r"EG\.(?!Global)\w*Qvalue"],
    # Experiment-wide (global) precursor q-value. Spectronaut DOES export one -- verified present
    # and populated in every FRAN (Normal) report checked, and genuinely independent of EG.Qvalue
    # (not a copy). The loose fallback above is anchored away from "Global" so that a report
    # lacking EG.Qvalue cannot silently land the GLOBAL value in q_value.
    "global_q_value":[r"^EG\.GlobalPrecursorQvalue$", r"GlobalPrecursorQvalue$"],
    # Decoys: absent from the target-only FRAN.rs schema, but present in the "everything + decoys"
    # ML export. They must never reach the public corpus — see the skip in iter_records().
    "is_decoy":     [r"^EG\.IsDecoy$", r"IsDecoy$"],
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
    # PTM site localization. UNPARAMETERIZED columns only -- the [Mod-Name]-suffixed columns
    # (EG.PTMPositions [GlyGly (K)], EG.PTMProbabilities [GlyGly (K)], etc.) vary per search
    # (one column per modification actually searched for), so a fixed regex can't match them
    # reliably across reports. The unparameterized localization string below (e.g.
    # "_...K[GlyGly (K): 100%]..._") carries the same per-site probabilities inline and is
    # present in every report regardless of which mods were searched.
    "ptm_localization":      [r"^EG\.PTMLocalizationProbabilities$", r"PTMLocalizationProbabilities$"],
    "ptm_assay_probability": [r"^EG\.PTMAssayProbability$", r"PTMAssayProbability$"],
    "has_localization":      [r"^EG\.HasLocalizationInformation$", r"HasLocalizationInformation$"],
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
# GlyGly (ubiquitin/NEDD8/ISG15 remnant, UniMod 121) was missing here -- with no mapping,
# _to_proforma's repl() below silently returned the ORIGINAL "[GlyGly (K)]" text instead of
# "[UNIMOD:121]" for every GG precursor ever ingested (~750,000 rows). Fixed 2026-09-10.
_MOD_UNIMOD = {"Carbamidomethyl": 4, "Oxidation": 35, "Acetyl": 1,
               "Phospho": 21, "Deamidation": 7, "Gln->pyro-Glu": 28, "Glu->pyro-Glu": 27,
               "GlyGly": 121}

# Per-site localization probability, e.g. "[GlyGly (K): 100%]" or, for an ambiguous site,
# two brackets like "[GlyGly (K): 95.4%]" and "[GlyGly (K): 4.6%]" on the SAME precursor.
_LOC_PROB_RE = re.compile(r":\s*([\d.]+)\s*%\]")


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


def _to_proforma(modseq: str, unmapped: dict | None = None) -> str | None:
    """Spectronaut EG.ModifiedSequence (e.g. "_...K[GlyGly (K)]..._") -> ProForma.

    `unmapped`, if given, is a dict this function increments (name -> count) whenever a
    bracketed mod name has no entry in _MOD_UNIMOD. Without that counter the old behavior --
    silently returning the original bracketed text for any unmapped name -- is exactly how
    ~750,000 GlyGly (ubiquitin remnant) precursors went unrecognized for years: the ProForma
    string looked plausible (it still had brackets) so nothing downstream ever complained.
    """
    if not isinstance(modseq, str) or not modseq:
        return None
    def repl(m):
        name = m.group(1).split(" ")[0].split("(")[0].strip()
        uid = _MOD_UNIMOD.get(name)
        if uid is None and unmapped is not None:
            unmapped[name] = unmapped.get(name, 0) + 1
        return f"[UNIMOD:{uid}]" if uid else m.group(0)
    s = re.sub(r"\[([^\]]*)\]", repl, modseq.strip("_"))
    return s


def _parse_localization_min(loc_str) -> float | None:
    """EG.PTMLocalizationProbabilities -> the MINIMUM per-site probability on the precursor
    (as a 0-1 fraction), or None if the string carries no probability annotation.

    Minimum, not mean or max: delimp_precursors.site_localization_probability gates a
    downstream training-set filter (fran_schema.sql: `n_mods = 0 OR
    site_localization_probability > 0.75`) that reads the column as "how confident are we in
    EVERY modification position reported on this precursor" -- if even one site is
    ambiguously placed, the precursor's reported mod positions are only as trustworthy as the
    worst-localized site.

    Deliberately NOT scoped to one modification name (e.g. only GlyGly sites): verified on
    Hive that the SAME string can carry an ambiguous, unrelated site for a different
    modification -- e.g. "_AAM[Oxidation (M): 100%]...QVSK[GlyGly (K): 100%]...M[Oxidation
    (M): 0%]..._", a confidently localized GlyGly alongside an oxidation the search could not
    place between two candidate methionines. A GlyGly-only minimum would report 100% for that
    precursor even though its mod positions overall are not fully resolved; the whole-precursor
    gate above is exactly the consumer that needs the wider view. Also verified the
    single-modification ambiguous case this format supports: "_K[GlyGly (K): 95.4%]...K[GlyGly
    (K): 4.6%]K_" -- one GG mark, uncertain which of two K's carries it -- where the minimum
    (4.6%) is the only value that reflects how unresolved the placement really is.
    """
    if not isinstance(loc_str, str) or not loc_str:
        return None
    probs = [float(p) / 100.0 for p in _LOC_PROB_RE.findall(loc_str)]
    return min(probs) if probs else None


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
    """Yield normalized per-precursor records (dict) from a Spectronaut report (TSV/Parquet).

    Prints a summary of rows dropped for a non-numeric q-value, and a summary of any
    modification name seen in EG.ModifiedSequence with no UniMod mapping; silence in either
    means none."""
    _skipped = {"non_numeric_q": 0, "values": set()}
    _unmapped_mods: dict[str, int] = {}
    cols = resolve_columns(report_columns(report_path))
    need = ["run", "stripped_seq", "charge"]
    missing = [n for n in need if n not in cols]
    if missing:
        raise ValueError(f"Spectronaut report missing required columns for {missing}; resolved={cols}")
    usecols = list(dict.fromkeys(cols.values()))
    yielded = 0
    for chunk in iter_chunks(report_path, usecols, chunksize):
        for _, r in chunk.iterrows():
            # Decoys are reversed/scrambled sequences, not identifications: drop them before the
            # q-filter. Their EG.Qvalue is the string "NaN", and NaN > q_max is False, so the
            # filter below would otherwise WAVE THEM THROUGH into delimp_precursors as real IDs.
            dv = r.get(cols["is_decoy"]) if "is_decoy" in cols else None
            if dv is not None and pd.notna(dv) and str(dv).strip().lower() in ("true", "1", "1.0"):
                continue
            qv = r.get(cols["q_value"]) if "q_value" in cols else None
            if qv is not None and pd.notna(qv):
                # EG.Qvalue is a STRING column and is not always a number. Spectronaut writes the
                # literal "Profiled" for precursors it quantified by cross-run profiling rather than
                # identified in this run -- 71,021 of 600,000 sampled values (12%) in
                # 20191111_112559_diaPASEF_run_2. float() raised ValueError on those, which is why
                # reports of this shape never ingested at all.
                #
                # They are DROPPED, not admitted with a NULL q-value, for two reasons. First, the
                # same hazard the decoy comment above describes: a None q-value passes `> q_max`
                # and would be waved through as a 1%-FDR identification on no evidence. Second,
                # consistency of meaning -- every search already in the corpus was ingested by code
                # that crashed on this value, so no existing row is a profiled one, and admitting
                # them here would make this search's "identifications" mean something different
                # from the other 434M.
                try:
                    if float(qv) > q_max:
                        continue
                except (TypeError, ValueError):
                    _skipped["non_numeric_q"] += 1
                    _skipped["values"].add(str(qv)[:24])
                    continue
            modseq = r.get(cols["modified_seq"]) if "modified_seq" in cols else None
            # run NAME (matches the DIA-NN convention corpus_ingest expects); strip a trailing
            # vendor extension so corpus_ingest can build a clean raw_path.
            run = re.sub(r"\.(d|raw|mzml|wiff|htrms)$", "", str(r.get(cols["run"])), flags=re.I)
            nmods = len(re.findall(r"\[[^\]]*\]|\([^)]*\)", str(modseq))) if isinstance(modseq, str) else 0
            loc_str = r.get(cols["ptm_localization"]) if "ptm_localization" in cols else None
            site_loc_prob = _parse_localization_min(loc_str)
            # Defensive: if Spectronaut itself flags this precursor as having no localization
            # information, don't report a probability parsed off of it either.
            has_loc = r.get(cols["has_localization"]) if "has_localization" in cols else None
            if has_loc is not None and pd.notna(has_loc) and str(has_loc).strip().lower() in ("false", "0", "0.0"):
                site_loc_prob = None
            rec = {
                "run": run,                                # corpus_ingest keys on "run"
                "stripped_seq": _strip_seq(modseq, r.get(cols.get("stripped_seq", ""))),
                "modified_seq_diann": str(modseq) if modseq is not None else None,
                "modified_seq_proforma": _to_proforma(modseq, _unmapped_mods),
                "mods": None, "n_mods": nmods,             # full mod JSON TODO; proforma carries detail
                # Precursor-wide minimum per-site localization probability -- see
                # _parse_localization_min() for why minimum and why not GlyGly-only. No
                # delimp_precursors column exists yet for the raw annotation string itself
                # (see spectronaut_to_corpus.py's module docstring / the ingest report), so only
                # the derived numeric value is threaded through to a real column;
                # ptm_assay_probability/has_localization_info are carried on the record for a
                # future column or other consumers but are NOT written to SQL today.
                "site_localization_probability": site_loc_prob,
                "ptm_assay_probability": _f(r, cols, "ptm_assay_probability"),
                "has_localization_info": (bool(has_loc) if (has_loc is not None and pd.notna(has_loc)) else None),
                "charge": int(r[cols["charge"]]) if pd.notna(r.get(cols["charge"])) else None,
                "precursor_mz": _f(r, cols, "precursor_mz"),
                "rt": _f(r, cols, "rt"),
                "irt": _f(r, cols, "irt"),                 # cross-run RT (AI-training)
                "im": _f(r, cols, "im"),                   # 1/K0 (AI-training)
                "iim": None,                               # Spectronaut has no indexed IM column
                "ccs": _f(r, cols, "ccs"),
                "q_value": _f(r, cols, "q_value"),
                "global_q_value": _f(r, cols, "global_q_value"),
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
            yielded += 1
            yield rec

    if _skipped["non_numeric_q"]:
        print(f"  [spectronaut] dropped {_skipped['non_numeric_q']:,} row(s) with a "
              f"non-numeric q-value {sorted(_skipped['values'])} "
              f"(kept {yielded:,}) -- see iter_records()", flush=True)
    if _unmapped_mods:
        # NOT a hard failure -- an ingest run must never crash mid-stream over one unrecognized
        # mod name -- but this must be LOUD: an unmapped name means _to_proforma passed the raw
        # bracketed text through untouched, exactly how GlyGly went unrecognized for years (see
        # _MOD_UNIMOD's comment). Extend _MOD_UNIMOD when this prints.
        detail = ", ".join(f"{name!r}x{n:,}" for name, n in sorted(_unmapped_mods.items(), key=lambda kv: -kv[1]))
        print(f"  [spectronaut] {sum(_unmapped_mods.values()):,} modification occurrence(s) had "
              f"no UniMod mapping and were left as literal text in modified_seq_proforma: "
              f"{detail} -- add to _MOD_UNIMOD in spectronaut_to_corpus.py", flush=True)

def _f(row, cols, field):
    if field not in cols:
        return None
    v = row.get(cols[field])
    try:
        f = float(v) if pd.notna(v) else None
    except (TypeError, ValueError):
        return None
    return None if (f is not None and f != f) else f   # coerce NaN (incl. the string "NaN") -> None


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
