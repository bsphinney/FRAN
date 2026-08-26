"""corpus_ingest.py — self-contained FRAN corpus ingester (NO HIVE needed).

Reads a DIA-NN report.parquet (or a Spectronaut report via spectronaut_to_corpus)
and writes directly to the PG Farm `delimp` corpus DB. Runs on a workstation with
the PG Farm token, so it bypasses HIVE entirely while HIVE is in maintenance.

  python corpus_ingest.py /path/to/searchdir [--engine diann|spectronaut]
        [--organism-name "Canis lupus familiaris" --taxon 9615]
        [--name MySearch] [--dry-run]

Idempotent: deletes any prior rows for this output_dir, then re-inserts (so re-runs
are safe). Transaction-wrapped (rolls back on any error). Schema v1
(scripts/migrate_pg_v1.sql); populates delimp_searches, raw_files (+ raw_name_anonymized),
search_raw_files, delimp_sample_metadata, delimp_proteins, delimp_precursors.

Token: $DELIMP_PG_PASSWORD, or a file at $DELIMP_PG_TOKEN_FILE / ~/.pgfarm_token.
NOTE: iRT/iIM + observed fragment spectra need the schema-v2 columns (not in v1) —
this writer fills what v1 has (rt, im, q-values, intensity, mods); extend after the
v2 migration. VALIDATE with --dry-run first, then ingest a single search before bulk.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from versions import CORPUS_INGEST_VERSION, SCHEMA_VERSION  # noqa: E402


def _versions():
    """versions module, or a stand-in that reports 'unknown' rather than raising.

    A module-scope `from versions import ...` is what broke the shared copy on 2026-08-20 (win-1
    #57): versions.py is not guaranteed to sit beside every deployment of this file. An ingester
    that refuses to start because it cannot name its own version is worse than one that admits it
    does not know."""
    class _Unknown:
        CORPUS_INGEST_VERSION = SCHEMA_VERSION = "unknown"
        DUPLICATE_GUARD_VERSION = "unknown"
        @staticmethod
        def pipeline_stamp():
            return "corpus_ingest/unknown guard/unknown"
    try:
        import versions as _v
    except ImportError:                      # pragma: no cover - depends on deployment layout
        return _Unknown
    # A STALE versions.py is the likelier failure, and it is nastier: the module imports fine and
    # then AttributeErrors deep inside the ingest. That is exactly how the first FragPipe run died --
    # the shared copy had been updated but this deployment's had not. Verify the surface we use.
    if not hasattr(_v, "pipeline_stamp"):
        print("  [warn] versions.py is present but predates pipeline_stamp(); "
              "this deployment is not fully synced — recording versions as 'unknown'", flush=True)
        return _Unknown
    return _v

_UNIMOD = {"UniMod:4": 4, "UniMod:35": 35, "UniMod:1": 1, "UniMod:21": 21, "UniMod:7": 7}


def _acq_for(engine, platform):
    """Acquisition label implied by the SEARCH ENGINE (with platform only choosing the DIA flavour).

    Both engines FRAN ingests are DIA search engines, so the result being in the corpus at all is the
    evidence. `platform` picks the name a proteomicist expects: diaPASEF on timsTOF, plain DIA on an
    Orbitrap. Kept as a named function so the one place this is decided is greppable — the previous
    inline expression keyed on platform alone, which is the wrong basis."""
    return "diaPASEF" if platform == "timstof" else "DIA"


def _resolve_platform(plat, model, mobility_min):
    """Platform, with a model/platform contradiction resolved in the MODEL's favour.

    Only when the run has no ion-mobility range. A genuine timsTOF acquisition always has one, so
    "Orbitrap model + no mobility + platform=timstof" is not a judgement call -- it is the
    file-extension inference having guessed from a Thermo file that happens to be named ".d", which
    is how 29 runs (22 of them an external collaborator's Orbitrap) ended up on the wrong platform.
    If the run DOES carry mobility, the existing platform wins and nothing is touched."""
    try:
        from instrument_labels import platform_for_model
        want = platform_for_model(model)
        if want and plat and want != plat and mobility_min is None:
            return want
    except Exception:  # noqa: BLE001 - metadata tidying must never fail an ingest
        pass
    return plat


def _norm_instrument(model, serial):
    """Canonical (model, serial). One physical instrument was reaching raw_files under several
    labels -- a leading space, a zero-padding variant, a case variant, and a serial carrying three
    different model names -- which silently splits every per-instrument aggregate. Normalising here
    means a re-ingest cannot reintroduce what fix_instrument_labels.py cleaned up. Never raises: a
    metadata problem must not fail an ingest."""
    try:
        from instrument_labels import normalize
        return normalize(model, serial)
    except Exception:  # noqa: BLE001
        return model, serial


def _platform_from_disk(output_dir, runs):
    """Ground-truth platform from the raws actually on disk, for reports that settle it no other way.

    A Spectronaut BGS-schema export carries no EG.IonMobility column, and the adapter strips the
    vendor extension off R.FileName -- so BOTH signals used below go blank and a diaPASEF search
    silently lands as 'orbitrap'. That is not cosmetic: it picks the wrong extension for every
    synthetic raw_path (<dir>/<run>.raw for what is really a .d), and labels the acquisition 'DIA'
    instead of 'diaPASEF'. The files themselves are unambiguous -- Bruker is a .d FOLDER, Thermo a
    .raw file -- and resolve_raw_hive_paths treats that physical format as ground truth, so use it
    here too. Looks beside output_dir (the same place the raw-metadata index below reads) and counts
    only names matching a run in THIS search. Returns None for "no opinion", never a guess.
    """
    import glob as _g
    base = os.path.dirname((output_dir or "").rstrip("/"))
    if not base or not os.path.isdir(base):
        return None
    want, n_d, n_raw = set(runs), 0, 0
    try:
        for pat, is_d in (("*.d", True), ("*/*.d", True), ("*.raw", False), ("*/*.raw", False)):
            for _p in _g.glob(os.path.join(base, pat)):
                if os.path.splitext(os.path.basename(_p))[0] in want:
                    if is_d:
                        n_d += 1
                    else:
                        n_raw += 1
    except OSError:
        return None
    if n_d and n_d >= n_raw:
        return "timstof"
    return "orbitrap" if n_raw else None


def _conn():
    import psycopg2
    # Use _token() so a token FILE holding the service-account SECRET (not a JWT) is exchanged
    # for a JWT — the raw file contents are NOT the DB password. (Was a latent bug: worked only
    # where ~/.pgfarm_token already held a JWT; failed on secret-only boxes. Flagged by win-1.)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from refresh_leaderboards import _token
    return psycopg2.connect(
        host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
        dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30)


def sanitize(name: str) -> str:
    base = (name or "").replace("\\", "/").split("/")[-1]
    return f"run-{hashlib.sha1((name or '').encode()).hexdigest()[:6]}"


def parse_mods(modseq):
    """DIA-NN Modified.Sequence -> (mods_json, n_mods, proforma)."""
    if not isinstance(modseq, str) or not modseq:
        return None, 0, None
    mods = re.findall(r"\(([^)]+)\)|\[([^\]]+)\]", modseq)
    flat = [m[0] or m[1] for m in mods]
    proforma = re.sub(r"\(([^)]+)\)", lambda m: f"[{m.group(1).replace('UniMod:', 'UNIMOD:')}]", modseq)
    js = json.dumps([{"mod": m, "unimod": _UNIMOD.get(m)} for m in flat]) if flat else None
    return js, len(flat), proforma


# Monoisotopic residue masses + common UniMod deltas, to COMPUTE precursor m/z when a report
# omits it (DIA-NN 1.x report.tsv has no Precursor.Mz column, but delimp_precursors.precursor_mz
# is NOT NULL). m/z = (Σresidues + water + Σmod-deltas + z·proton) / z.
_AA = {"G": 57.02146, "A": 71.03711, "S": 87.03203, "P": 97.05276, "V": 99.06841, "T": 101.04768,
       "C": 103.00919, "L": 113.08406, "I": 113.08406, "N": 114.04293, "D": 115.02694, "Q": 128.05858,
       "K": 128.09496, "E": 129.04259, "M": 131.04049, "H": 137.05891, "F": 147.06841, "R": 156.10111,
       "Y": 163.06333, "W": 186.07931}
_H2O, _PROT = 18.0105646, 1.0072765
_UNIMOD_MASS = {1: 42.010565, 4: 57.021464, 5: 43.005814, 7: 0.984016, 21: 79.966331, 26: 39.994915,
                27: -18.010565, 28: -17.026549, 35: 15.994915, 121: 114.042927, 259: 8.014199,
                267: 10.008269, 385: -17.026549, 1301: 128.094963}


def _calc_prec_mz(modseq, charge):
    if not modseq or charge in (None, ""):
        return None
    try:
        z = int(charge)
    except (TypeError, ValueError):
        return None
    if z < 1:
        return None
    s = str(modseq)
    mod = sum(_UNIMOD_MASS.get(int(m), 0.0) for m in re.findall(r"\(UniMod:(\d+)\)", s, re.I))
    stripped = re.sub(r"[^A-Za-z]", "", re.sub(r"\([^)]*\)|\[[^\]]*\]", "", s)).upper()
    if not stripped:
        return None
    neutral = sum(_AA.get(c, 0.0) for c in stripped) + _H2O + mod
    return round((neutral + z * _PROT) / z, 5) if neutral > 0 else None


def _records(report_path, engine):
    """Yield normalized per-precursor dicts from a DIA-NN / FragPipe / Radiant / Spectronaut report.

    FragPipe DIA is DIA-NN-shaped by construction (diaTracer -> MSFragger -> ... -> DIA-NN 1.8.2b8
    writes report.tsv), so it needs no adapter -- only its own report discovery and version string.
    Radiant/Fulcrum is close but not close enough and goes through radiant_to_corpus first.
    """
    if engine == "spectronaut":
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from spectronaut_to_corpus import iter_records
        yield from iter_records(report_path)
        return
    import pandas as pd
    if engine == "radiant":
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from radiant_to_corpus import to_diann_frame
        df = to_diann_frame(report_path)
        yield from _diann_rows(df)
        return
    # DIA-NN 1.8/1.9 (and FragPipe's bundled 1.8.2b8) write report.tsv; 2.0+ writes parquet.
    if str(report_path).lower().endswith((".tsv", ".txt", ".csv")):
        df = pd.read_csv(report_path, sep="\t", low_memory=False)
    else:
        df = pd.read_parquet(report_path)
    yield from _diann_rows(df)


def _diann_rows(df):
    """Walk a DIA-NN-shaped DataFrame -> per-precursor dicts. Shared by diann, fragpipe and radiant."""
    import pandas as pd
    def c(*names):
        for n in names:
            if n in df.columns:
                return n
        return None
    cR, cMod, cStr = c("Run", "File.Name"), c("Modified.Sequence"), c("Stripped.Sequence")
    cCh, cMz = c("Precursor.Charge"), c("Precursor.Mz", "FG.PrecMz", "Precursor.Mz.Calibrated")
    cRT, cIM = c("RT"), c("IM")
    cIRT, cIIM = c("iRT", "RT.Predicted"), c("iIM")   # iRT/iIM = cross-run-comparable (AI-training)
    cQ, cGQ, cPGQ = c("Q.Value"), c("Global.Q.Value"), c("PG.Q.Value")
    cInt, cNorm = c("Precursor.Quantity"), c("Precursor.Normalised")
    cPG, cGene = c("Protein.Group", "Protein.Ids"), c("Genes")
    if not (cR and cStr and cCh):
        raise ValueError(f"DIA-NN report missing Run/Stripped.Sequence/Precursor.Charge; have {list(df.columns)[:20]}")
    if cQ:
        df = df[df[cQ] <= 0.01]
    # rename the resolved dotted columns to simple names, then iterate as dicts
    # (robust — pandas itertuples mangles dotted column names).
    mapping = {"run": cR, "stripped_seq": cStr, "modified_seq_diann": cMod, "charge": cCh,
               "precursor_mz": cMz, "rt": cRT, "irt": cIRT, "im": cIM, "iim": cIIM,
               "q_value": cQ, "global_q_value": cGQ, "pg_q_value": cPGQ,
               "intensity": cInt, "normalized_intensity": cNorm,
               "protein_group": cPG, "gene": cGene}
    present = {k: v for k, v in mapping.items() if v}
    sub = df[list(present.values())].rename(columns={v: k for k, v in present.items()})
    for row in sub.to_dict("records"):
        modseq = row.get("modified_seq_diann")
        mods, nmods, pf = parse_mods(modseq)
        row["mods"], row["n_mods"], row["modified_seq_proforma"] = mods, nmods, pf
        for f in mapping:                       # ensure all keys exist
            row.setdefault(f, None)
        if row.get("precursor_mz") is None:     # DIA-NN 1.x has no Precursor.Mz -> compute it
            row["precursor_mz"] = _calc_prec_mz(modseq or row.get("stripped_seq"), row.get("charge"))
        if row.get("run"):                      # DIA-NN 1.7.x File.Name is a full path -> basename
            row["run"] = re.sub(r"\.(d|raw|mzml|wiff|htrms|dia)$", "",
                                os.path.basename(str(row["run"]).replace("\\", "/")), flags=re.I)
        yield row


def ingest(searchdir, engine, organism_name, taxon, name, dry, output_dir=None):
    report = searchdir
    if os.path.isdir(searchdir):
        if engine == "fragpipe":
            # FragPipe 24 bundles DIA-NN 1.8.2b8, which HAS NO PARQUET WRITER -- globbing for
            # report.parquet finds nothing. The precursor report is report.tsv under
            # dia-quant-output/. combined_protein.tsv is IonQuant over pseudo-spectra and is
            # explicitly NOT the quant of record.
            cand = [os.path.join(searchdir, f) for f in
                    ("dia-quant-output/report.tsv", "report.tsv",
                     "out/dia-quant-output/report.tsv")
                    if os.path.exists(os.path.join(searchdir, f))]
        elif engine == "radiant":
            # Prefer the RAW Spark partition directory over any pre-converted parquet: the
            # converted file still carries decoys and FASTA-header protein groups.
            cand = [os.path.join(searchdir, f) for f in
                    ("radiant_results/fulcrum-results", "fulcrum-results",
                     "out/radiant_results/fulcrum-results", "delimp_report.parquet")
                    if os.path.exists(os.path.join(searchdir, f))]
        else:
            # DIA-NN 2.0+ -> report.parquet; 1.8/1.9 -> report.tsv. Prefer parquet if both exist.
            cand = [os.path.join(searchdir, f) for f in ("report.parquet", "report.tsv")
                    if os.path.exists(os.path.join(searchdir, f))]
        report = cand[0] if cand else searchdir
    # output_dir is the idempotency key (delete-then-insert by it) AND the stored provenance
    # raw_path base. Allow an explicit STABLE value so ingesting from a temp-extracted report
    # doesn't bake a /tmp path in (which would also break idempotency across runs).
    output_dir = output_dir or (os.path.dirname(report) if os.path.isfile(report) else searchdir)
    search_name = name  # if None, derived from raw-FILE names after `runs` is known (the folder
    # can lie — e.g. Mucke/Gladstone rat data staged in a "HUPO_2023" dir; raw names carry origin)
    # Spectronaut FRAGMENT-level reports emit one row PER FRAGMENT, so the same precursor
    # appears many times. delimp_precursors is precursor-level, so we collapse to one record
    # per (run, modified-seq, charge). We STREAM-dedup so a huge report (millions of fragment
    # rows) never has to fully materialize in memory. (DIA-NN report.parquet is already
    # precursor-level.)
    # OBSERVED SPECTRUM (2026-07-17): the report's fragments + MS1 isotope envelope + DIA window +
    # predicted-vs-observed RT used to be DROPPED at this collapse. delimp_precursors stays
    # precursor-level (unchanged); the full observed spectrum is written AFTER commit to the
    # **Lance** training lane (backfill_fragments.process_one re-parses the same report) and
    # recorded in the delimp_spectrum_lane registry — see the post-commit block below. Lance +
    # DB registry is how DL people store training data (depthcharge/Casanovo), durable via the
    # checksummed registry, and re-derivable from the archived report. Never predicted/guessed.
    if engine == "spectronaut":
        seen, recs, n_raw = set(), [], 0
        for x in _records(report, engine):
            n_raw += 1
            k = (str(x.get("run")), str(x.get("modified_seq_diann") or x.get("stripped_seq")), x.get("charge"))
            if k in seen:
                continue
            seen.add(k); recs.append(x)
        if n_raw > len(recs):
            print(f"  collapsed {n_raw:,} fragment-rows -> {len(recs):,} precursors (fragment-level report)")
    else:
        recs = list(_records(report, engine))
    if not recs:
        sys.exit("No precursor records parsed (check the report / --engine).")
    # Organism: if not given on the CLI, derive it from the report — Spectronaut carries
    # PEP.AllOccurringOrganisms per peptide, so the dominant species is the experiment organism.
    if not organism_name:
        from collections import Counter
        _TAXON = {"Homo sapiens": 9606, "Mus musculus": 10090, "Rattus norvegicus": 10116,
                  "Bos taurus": 9913, "Sus scrofa": 9823, "Ovis aries": 9940, "Oryctolagus cuniculus": 9986,
                  "Macaca mulatta": 9544, "Macaca fascicularis": 9541, "Gallus gallus": 9031,
                  "Saccharomyces cerevisiae": 559292, "Escherichia coli": 562, "Canis lupus familiaris": 9615}
        oc = Counter()
        for x in recs:
            o = x.get("organism")
            if o and str(o).strip().lower() not in ("none", "nan", ""):
                for part in str(o).split(";"):
                    part = part.strip()
                    if part:
                        oc[part] += 1
        if oc:
            organism_name = oc.most_common(1)[0][0]
            taxon = taxon or _TAXON.get(organism_name)
            print(f"  organism (from report): {organism_name}" + (f" [taxon {taxon}]" if taxon else ""))
    # Canonicalize: junk sentinels ("Unknown"/""/etc) -> NULL (never a string, which would
    # masquerade as a real species on the dashboard); strip Spectronaut "(Common name)"
    # variants so they merge with the bare species. Single source of truth: organism.py.
    try:
        from organism import canonical_organism
    except ImportError:  # when run as a module
        from .organism import canonical_organism
    organism_name = canonical_organism(organism_name)
    runs = sorted({str(x["run"]) for x in recs if x.get("run")})
    if not search_name:  # name from the raw FILE prefix (faithful to origin), folder as fallback
        try:
            from provenance import search_name_from_raw_files
            search_name = search_name_from_raw_files(runs)
        except Exception:  # noqa: BLE001
            search_name = None
        search_name = search_name or os.path.basename(output_dir.rstrip("/"))
    # Platform detection. DIA-NN writes IM=0.0 (not NULL) for Orbitrap data, so a bare
    # "is the IM column present" test wrongly flags Orbitrap as timsTOF. Require a REAL
    # 1/K0 (0.3-2.5 via _im) on at least some precursors. Corroborating signal: timsTOF raw
    # files are .d folders, Orbitrap are .raw (rarely .mzML) — so if the run names already
    # carry an extension, trust .d => timsTOF / .raw|.mzml => orbitrap as a tiebreak.
    has_real_im = any(_im(x.get("im")) is not None for x in recs)
    ext_hits = [str(x.get("run", "")).lower() for x in recs[:2000]]
    looks_dotd = any(r.endswith(".d") for r in ext_hits)
    looks_raw = any(r.endswith((".raw", ".mzml")) for r in ext_hits)
    if engine == "radiant":
        # Radiant/Fulcrum reads mzML/Parquet only -- feeding it Bruker .d fails outright
        # (FunctionNotImplemented in MsReaderPointerAcc.cpp). Every Radiant row is therefore
        # Thermo, and the corpus raw file is <name>.raw. Do not let the generic sniffing below
        # guess otherwise from an absent IM column.
        platform = "orbitrap"
    elif has_real_im or (looks_dotd and not looks_raw):
        platform = "timstof"
    elif looks_raw:
        platform = "orbitrap"
    else:
        # Neither signal fired: no real IM anywhere AND the run names carry no extension -- which is
        # exactly what a Spectronaut BGS-schema export looks like. Silence is not evidence of an
        # Orbitrap, so ask the filesystem before falling back (see _platform_from_disk).
        platform = _platform_from_disk(output_dir, runs) or "orbitrap"
    # protein aggregation per (run, protein_group). Protein-level abundance = SUM of the protein's
    # precursor intensities (DIA-NN/Spectronaut give no per-PG quant column here, but each precursor
    # carries Precursor.Quantity + protein_group, so summing is a faithful protein-quant proxy).
    # Without this, delimp_proteins.intensity stays NULL and the species page's most/least-abundant
    # are empty.
    prot = {}
    for x in recs:
        pgk = (str(x["run"]), str(x.get("protein_group") or ""))
        a = prot.setdefault(pgk, {"peps": set(), "n": 0, "gene": _clean_gene(x.get("gene")),
                                  "int": 0.0, "nint": 0.0, "has_int": False, "pgq": None})
        # prefer a REAL gene symbol over a junk/None one (reports often have 'NaN'/'' for some rows)
        if a["gene"] is None:
            a["gene"] = _clean_gene(x.get("gene"))
        a["peps"].add(x["stripped_seq"]); a["n"] += 1
        iv = _flt(x.get("intensity"))
        if iv is not None:
            a["int"] += iv; a["has_int"] = True
        nv = _flt(x.get("normalized_intensity"))
        if nv is not None:
            a["nint"] += nv
        # protein-group q-value = best (min) PG q-value across the protein's precursors. The report
        # carries pg_q_value per precursor; without capturing it here delimp_proteins.pg_q_value
        # stays NULL (protein_detail's "Best PG q" + any PG-FDR filtering had nothing to read).
        qv = _flt(x.get("pg_q_value"))
        if qv is not None:
            a["pgq"] = qv if a["pgq"] is None else min(a["pgq"], qv)
    # PROTEIN GROUPS vs PROTEINS (2026-07-27). Spectronaut reports both and they are NOT the same:
    # a protein group's label is the ';'-joined accessions of its members ("E2RE03;J9P669"), so 635
    # groups can expand to 1,350 proteins. FRAN stored only the group count — in a column named
    # n_proteins_total, which the UI rendered as "Proteins" — so every search under-reported proteins
    # by ~2x against the customer's own Spectronaut overview. Record BOTH: n_protein_groups_total is
    # the group count (what n_proteins_total used to hold), n_proteins_total is now the true protein
    # count. Verified: expanding PG.ProteinGroups == PG.ProteinAccessions exactly, on Ver_15 and 6
    # archived FRAN reports — so this is derivable from protein_group alone, no re-export needed.
    pg_labels = {k[1] for k in prot}
    n_protein_groups = len(pg_labels)
    n_proteins = len({a.strip() for lbl in pg_labels for a in str(lbl).split(";")
                      if a.strip() and a.strip().lower() not in ("nan", "none", "null")})
    print(f"[{engine}] {search_name}: {len(recs):,} precursors, {len(runs)} runs, {len(prot):,} protein×run, "
          f"{n_protein_groups:,} protein groups / {n_proteins:,} proteins, platform={platform}")
    if dry:
        print("  DRY RUN — sample precursor:", {k: recs[0][k] for k in ("run", "stripped_seq", "charge", "rt", "im", "q_value", "protein_group")})
        print("  (no DB writes)")
        return

    # Say out loud which code is running. The 2026-08-25 guard failure was invisible partly because
    # nothing in the log distinguished a run WITH a working guard from one without.
    print(f"  code: {_versions().pipeline_stamp()}"
          f"{'' if not ALLOW_DUPLICATE else '  [--allow-duplicate: guard BYPASSED]'}", flush=True)

    import psycopg2.extras
    conn = _conn(); conn.autocommit = False
    try:
        cur = conn.cursor()
        # Stamp this run into delimp_component_version before doing any work, so a run that later
        # dies still leaves a record of which code touched the corpus. Never fatal.
        import versions as _V
        _V.record_run(cur, "corpus_ingest", CORPUS_INGEST_VERSION,
                      notes=f"schema={SCHEMA_VERSION}")
        conn.commit()

        # ENGINE WHITELIST PRE-FLIGHT. delimp_searches.search_engine carries a CHECK constraint
        # listing the permitted engines. Adding a new engine therefore fails with a bare
        # CheckViolation AFTER the whole report has been parsed -- 16 s on the poplar Radiant set,
        # but an hour on a 34 GB Spectronaut report. Fail here instead, with the fix in the message.
        cur.execute("""SELECT pg_get_constraintdef(oid) FROM pg_constraint
                       WHERE conrelid='delimp_searches'::regclass
                         AND conname='delimp_searches_search_engine_check'""")
        _row = cur.fetchone()
        if _row and f"'{engine}'" not in _row[0]:
            allowed = re.findall(r"'([a-z_]+)'", _row[0])
            conn.rollback(); conn.close()
            raise SystemExit(
                f"engine {engine!r} is not permitted by delimp_searches_search_engine_check.\n"
                f"  allowed: {', '.join(allowed)}\n"
                f"  fix:     ALTER TABLE delimp_searches DROP CONSTRAINT "
                f"delimp_searches_search_engine_check;\n"
                f"           ALTER TABLE delimp_searches ADD CONSTRAINT "
                f"delimp_searches_search_engine_check\n"
                f"             CHECK (search_engine = ANY (ARRAY["
                f"{', '.join(repr(a) for a in allowed + [engine])}]));\n"
                f"  (and add it to schema/fran_schema.sql so new installs get it too)")

        # ── DUPLICATE GUARD ───────────────────────────────────────────────────────────────────
        # The idempotency key is output_dir, so THE SAME SEARCH AT TWO PATHS BECOMES TWO SEARCHES.
        # That is not hypothetical: the same .sne routinely sits on a project drive and in
        # S:\sne_storage, and an audit on 2026-08-24 found 184 duplicate groups / 41.8M redundant
        # precursors, most from the original bulk load.
        #
        # Matching on search_name does NOT work -- the name comes from the .sne folder in one place
        # and from the raw-file prefix in another, so the SAME result arrives as "OI07152026" and
        # "20260717_102207_07152026". The stable identity is the SET OF RAW FILES plus the precursor
        # count, which no naming or drive-letter difference can change.
        #
        # Narrow on (n_raw_files, n_precursors_total) in SQL -- both exact and indexed-cheap -- then
        # compare basename SETS in Python rather than trusting a DB-side string_agg ordering to match
        # Python's sorted(). Re-ingesting the SAME output_dir is untouched: that is the intended
        # delete-then-insert. Override with --allow-duplicate for a deliberate second copy.
        if not ALLOW_DUPLICATE:
            cur.execute("""SELECT id, search_name, output_dir, ingested_at FROM delimp_searches
                           WHERE n_raw_files=%s AND n_precursors_total=%s AND output_dir<>%s""",
                        (len(runs), len(recs), output_dir))
            mine = {str(r) for r in runs}
            for _id, _nm, _od, _at in cur.fetchall():
                cur.execute("""SELECT rf.raw_basename FROM search_raw_files f
                               JOIN raw_files rf ON rf.raw_path=f.raw_path
                               WHERE f.search_id=%s""", (_id,))
                if {r[0] for r in cur.fetchall()} == mine:
                    print(f"  SKIPPED — DUPLICATE of an already-ingested search.\n"
                          f"    this : {output_dir}\n"
                          f"    exists: {_od}\n"
                          f"    match : same {len(mine)} raw files AND same {len(recs):,} precursors "
                          f"(search_name {_nm!r}, ingested {_at:%Y-%m-%d})\n"
                          f"    Nothing was written. Record this path against the existing search in\n"
                          f"    delimp_search_sources (file_role 'sne') instead, or re-run with "
                          f"--allow-duplicate if it is genuinely a separate result.", flush=True)
                    conn.rollback(); conn.close()
                    return
        # auto-add the cross-run-comparable columns (not in v1 schema) so the uploader
        # can store iRT/iIM straight from the report — the path to a TRUE iRT axis.
        # IMPORTANT: never run ALTER TABLE unconditionally here. `ADD COLUMN` takes an
        # AccessExclusiveLock on this huge shared table; if it queues behind a long-running
        # statement (e.g. our delete-then-insert), it blocks EVERY query DB-wide (this caused
        # a full PG Farm stall, 2026-06-15). So check the catalog first (a lockless read) and
        # only ALTER on the rare path where the columns are genuinely missing.
        cur.execute("""SELECT column_name FROM information_schema.columns
                       WHERE table_name='delimp_precursors'
                         AND column_name IN ('irt','iim','protein_group')""")
        have_cols = {row[0] for row in cur.fetchall()}
        want_cols = (("irt", "REAL"), ("iim", "REAL"), ("protein_group", "TEXT"))
        missing = [(c, t) for c, t in want_cols if c not in have_cols]
        if missing:
            # Each ADD COLUMN needs an AccessExclusiveLock on this 23M-row shared table. If it
            # queues behind a long write (a 2-hour bulk COPY during the ingestion ramp) it blocks
            # EVERY query DB-wide until the COPY ends (the 2026-06-15 PG-Farm stall; re-hit
            # 2026-06-17 when protein_group was first added). So FAIL FAST: a short lock_timeout in
            # a savepoint — if we can't grab the lock in 3s we skip the add this round (a later
            # ingest that lands in a gap adds it) and proceed WITHOUT that column, never blocking.
            for c, typ in missing:
                try:
                    cur.execute("SAVEPOINT addcol")
                    cur.execute("SET LOCAL lock_timeout = '3s'")
                    cur.execute(f"ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS {c} {typ}")
                    cur.execute("RELEASE SAVEPOINT addcol")
                    have_cols.add(c)
                    print(f"  added delimp_precursors.{c}")
                except psycopg2.Error as e:  # LockNotAvailable / QueryCanceled -> skip, don't block
                    cur.execute("ROLLBACK TO SAVEPOINT addcol")
                    print(f"  [skip] could not add {c} now ({type(e).__name__}); a later ingest will")
            cur.execute("RESET lock_timeout")
        # write protein_group only if the column exists (so an ingest before the one-time add
        # still succeeds, just without the link until a later run backfills it)
        write_pg = "protein_group" in have_cols
        # idempotent: remove any prior ingest of this output_dir (cascades to proteins/precursors/srf
        # + the PRIVATE provenance row — without this, re-ingests orphaned old provenance rows).
        # Carry the FASTA provenance across the delete. This block is a DELETE-then-INSERT, so it
        # is an upsert in every way that matters: anything the re-ingest fails to RE-DERIVE comes
        # back NULL and overwrites a good value. That is how spectrum_lance.register() dropped the
        # link rate 92.8% -> 55.1% and cost 577 datasets a restore. A re-derive here fails whenever
        # output_dir is no longer mounted on the ingest host — the normal case for the 1,940
        # Spectronaut rows, whose output_dir is a Windows path — so treat the stored row as the
        # fallback and only let a fresh detection win.
        prior_fa = {}
        prior_ver = None
        cur.execute("""SELECT id, fasta_path, fasta_md5, fasta_n_proteins, contaminant_lib,
                              search_engine_version
                       FROM delimp_searches WHERE output_dir=%s""", (output_dir,))
        for sid, _fp, _md5, _np, _cl, _ev in cur.fetchall():
            if _ev and prior_ver is None:
                prior_ver = _ev
            for k, v in (("fasta_path", _fp), ("fasta_md5", _md5),
                         ("fasta_n_proteins", _np), ("contaminant_lib", _cl)):
                if v is not None and prior_fa.get(k) is None:
                    prior_fa[k] = v
            cur.execute("DELETE FROM delimp_precursors WHERE search_id=%s", (sid,))
            cur.execute("DELETE FROM delimp_proteins   WHERE search_id=%s", (sid,))
            cur.execute("DELETE FROM search_raw_files  WHERE search_id=%s", (sid,))
            cur.execute("DELETE FROM delimp_search_provenance WHERE search_id=%s", (sid,))
            cur.execute("DELETE FROM delimp_searches    WHERE id=%s", (sid,))
        # STABLE search_id: deterministic from output_dir (the idempotency key) via uuid5, so
        # re-ingesting the SAME search reuses the SAME id. This keeps deep links / customer
        # bookmarks / LIMS linkage stable across re-ingests and stops orphan accumulation
        # (previously uuid4() minted a fresh id every run). output_dir is already the idempotency
        # key, so it's the right basis.
        _SEARCH_NS = uuid.UUID("5f6b1c9e-2d3a-4e7b-9c1d-fab4c0d5e600")
        search_id = str(uuid.uuid5(_SEARCH_NS, output_dir.rstrip("/")))
        raw_paths = {run: os.path.join(output_dir, run + (".d" if platform == "timstof" else ".raw")) for run in runs}
        # n_protein_groups_total: same lockless-catalog-check-then-guarded-ALTER discipline as the
        # delimp_precursors block above. delimp_searches is small (~2k rows) so the ALTER is cheap,
        # but the lock_timeout guard stays — an AccessExclusiveLock that queues behind a long write
        # still blocks readers of THIS table, and every page hits it.
        cur.execute("""SELECT column_name FROM information_schema.columns
                       WHERE table_name='delimp_searches' AND column_name='n_protein_groups_total'""")
        has_npg = bool(cur.fetchone())
        if not has_npg:
            try:
                cur.execute("SAVEPOINT addnpg")
                cur.execute("SET LOCAL lock_timeout = '3s'")
                cur.execute("ALTER TABLE delimp_searches ADD COLUMN IF NOT EXISTS n_protein_groups_total INTEGER")
                cur.execute("RELEASE SAVEPOINT addnpg")
                cur.execute("RESET lock_timeout")
                has_npg = True
                print("  added delimp_searches.n_protein_groups_total")
            except psycopg2.Error as e:
                cur.execute("ROLLBACK TO SAVEPOINT addnpg")
                print(f"  [skip] could not add n_protein_groups_total now ({type(e).__name__}); a later ingest will")
        # ENGINE VERSION (2026-07-27): search_engine_version existed but nothing wrote it — it was
        # NULL for all 1,891 Spectronaut searches. Sniff it from the export's sidecar files
        # (setup.txt / RunOverview / DIA-NN log); None when the sidecars aren't next to the report,
        # which is the case for reports extracted from the Flinders archive (parquet only).
        try:
            from engine_version import detect as _detect_version
            engine_ver = _detect_version(engine, report, output_dir)
        except Exception:  # noqa: BLE001 - never fail an ingest over a version string
            engine_ver = None
        # Same clobber exposure as the FASTA fields, and it costs more here because the column has
        # been BACKFILLED: search_engine_version went from 27/72 DIA-NN searches to 70/72 on
        # 2026-08-25 by reading sidecars reachable from Hive but not from the ingesting host. Since
        # this is a DELETE-then-INSERT, a re-ingest that cannot re-detect writes NULL straight over
        # that recovered value. Detection needs a sidecar beside the report, which archived
        # parquet-only exports do not have, so failing to re-detect is the NORMAL case, not an edge
        # one -- 654 searches are unversioned for exactly that reason.
        #
        # A fresh detection always wins: re-exporting to the same output_dir with a newer
        # Spectronaut is a real scenario, and that export carries its own sidecar, so detection
        # succeeds and the new version is correct. The stored value is only a fallback for "could
        # not tell", which is never a reason to forget what we already knew.
        if not engine_ver and prior_ver:
            engine_ver = prior_ver
            print(f"  engine version: kept {prior_ver} from the previous ingest "
                  f"(re-detect found no sidecar)", flush=True)
        else:
            print(f"  engine version: {engine_ver or 'not found (no setup.txt/log beside the report)'}")
        # Which DATABASE the search used. Same contract as engine_version above: best effort,
        # never fatal. Without it the corpus cannot tell a user whether two searches are even
        # comparable -- entries-per-gene is what decides whether a protein-count gap is a real
        # depth difference or an artefact of database redundancy.
        try:
            from engine_fasta import detect as _detect_fasta
            fa = _detect_fasta(engine, report, output_dir) or {}
        except Exception:  # noqa: BLE001 - never fail an ingest over provenance
            fa = {}
        # Only carry the stored values forward when this run did not identify a DIFFERENT
        # database. md5 and entry count describe one specific file; a freshly resolved path
        # with the previous row's md5 beside it is worse than a NULL, because it reads as
        # verified provenance.
        kept: list[str] = []
        if prior_fa and fa.get("fasta_path") in (None, prior_fa.get("fasta_path")):
            kept = [k for k in prior_fa if fa.get(k) is None]
            for k in kept:
                fa[k] = prior_fa[k]
        print(f"  fasta: {fa.get('fasta_path') or 'not found (no setup/log beside the report)'}"
              + (f"  n={fa['fasta_n_proteins']}" if fa.get("fasta_n_proteins") else "")
              + (f"  [kept from previous ingest: {','.join(kept)}]" if kept else ""))
        if has_npg:
            cur.execute("""INSERT INTO delimp_searches (id,search_name,output_dir,submitted_at,search_engine,
                           search_engine_version,pipeline_id,pipeline_version,n_raw_files,n_precursors_total,
                           n_proteins_total,n_protein_groups_total,status,ingested_schema_version,
                           fasta_path,fasta_md5,fasta_n_proteins,contaminant_lib)
                           VALUES (%s,%s,%s,NOW(),%s,%s,%s,%s,%s,%s,%s,%s,'completed',%s,%s,%s,%s,%s)""",
                        (search_id, search_name, output_dir, engine, engine_ver, f"{engine}-uploader", _versions().pipeline_stamp(),
                         len(runs), len(recs), n_proteins, n_protein_groups, SCHEMA_VERSION,
                         fa.get("fasta_path"), fa.get("fasta_md5"), fa.get("fasta_n_proteins"),
                         fa.get("contaminant_lib")))
        else:  # column not there yet — keep the OLD semantics rather than silently mixing the two
            cur.execute("""INSERT INTO delimp_searches (id,search_name,output_dir,submitted_at,search_engine,
                           search_engine_version,pipeline_id,pipeline_version,n_raw_files,n_precursors_total,
                           n_proteins_total,status,ingested_schema_version,
                           fasta_path,fasta_md5,fasta_n_proteins,contaminant_lib)
                           VALUES (%s,%s,%s,NOW(),%s,%s,%s,%s,%s,%s,%s,'completed',%s,%s,%s,%s,%s)""",
                        (search_id, search_name, output_dir, engine, engine_ver, f"{engine}-uploader", _versions().pipeline_stamp(),
                         len(runs), len(recs), n_protein_groups, SCHEMA_VERSION,
                         fa.get("fasta_path"), fa.get("fasta_md5"), fa.get("fasta_n_proteins"),
                         fa.get("contaminant_lib")))
        # per-run max RT (≈ gradient length) for gradient_minutes
        run_max_rt = {}
        for x in recs:
            rr = str(x["run"]); v = x.get("rt")
            if v is not None and (rr not in run_max_rt or v > run_max_rt[rr]):
                run_max_rt[rr] = v
        # SPD detection chain (mirrors DE-LIMP R/helpers_instrument.R): filename, then the
        # EvoSep method name in HyStarMetadata.xml inside the .d. EvoSep SPD->gradient map.
        _SPD_GRAD = {30: 44.0, 60: 21.0, 100: 11.5, 200: 5.5, 300: 2.3, 500: 2.2}
        _spd_fn = re.compile(r"(\d+)\s*spd\b|(\d+)[- ]samples[- ]per[- ]day", re.I)
        _spd_method = re.compile(r"(\d+)[- ]samples[- ]per[- ]day", re.I)

        def _detect_spd(run):
            m = _spd_fn.search(run)                       # 1) filename (e.g. 60SPD / 60-samples-per-day)
            if m:
                return float(m.group(1) or m.group(2))
            for cand in (os.path.join(output_dir, run + ".d"),          # 2) HyStarMetadata.xml in the .d
                         os.path.join(os.path.dirname(output_dir.rstrip("/")), run + ".d")):
                hy = os.path.join(cand, "HyStarMetadata.xml")
                if os.path.exists(hy):
                    try:
                        mm = _spd_method.search(open(hy, errors="replace").read())
                        if mm:
                            return float(mm.group(1))
                    except OSError:
                        pass
            return None

        # go-forward raw-instrument metadata (FRAN_COLUMN_AUDIT Tier B #2/#3): the stored raw_path is
        # SYNTHETIC (<.sne>/<run>.d), so index the ACTUAL .d/.raw beside the .sne project folder and read
        # each via raw_metadata (Bruker analysis.tdf / Thermo .raw). Best-effort: nothing here can fail
        # the ingest, and ON CONFLICT COALESCEs so a re-ingest that can't locate the raw never wipes
        # metadata a prior raw-access pass already filled.
        # NOTE: this used to swallow every failure, including `raw_metadata` not being importable at
        # all. It stayed that way for months and silently NULLed the whole instrument block on every
        # ingest. Locating the raws is genuinely best-effort, but a missing module is a bug — so the
        # import is no longer inside the try, and an empty index says so out loud.
        from raw_metadata import read_raw_metadata
        import glob as _glob
        _rawbase = os.path.dirname(output_dir or "")
        _rawidx = {}
        try:
            if _rawbase and os.path.isdir(_rawbase):
                for _pat in ("*.d", "*.raw", "*/*.d", "*/*.raw"):
                    for _p in _glob.glob(os.path.join(_rawbase, _pat)):
                        _rawidx.setdefault(os.path.splitext(os.path.basename(_p))[0], _p)
        except OSError as e:
            print(f"  raw-metadata: cannot index raws under {_rawbase}: {e}", flush=True)
        if not _rawidx:
            print(f"  raw-metadata: no .d/.raw found beside {_rawbase or '(no output_dir)'} — "
                  f"instrument fields will fall back to COALESCE", flush=True)
        _rawmeta_cache = {}

        def _runmeta(run):
            if run not in _rawidx:
                return {}
            ap = _rawidx[run]
            if ap not in _rawmeta_cache:
                try:
                    _rawmeta_cache[ap] = read_raw_metadata(ap, with_size=False) or {}
                except Exception:
                    _rawmeta_cache[ap] = {}
            return _rawmeta_cache[ap]

        # raw_files (ON CONFLICT update anonymized name) + sample_metadata + junction
        for run in runs:
            rp = raw_paths[run]
            md = _runmeta(run)
            # acquisition: prefer the real value sniffed from the raw; else DIA-NN-on-timsTOF = diaPASEF.
            # Acquisition type is a property of the SEARCH, not of the raw header. A result ingested
            # from Spectronaut or DIA-NN is DIA — that is a far stronger signal than anything a
            # Thermo header yields (see raw_metadata.read_thermo: the canonical ScanFilter `d` flag
            # is absent from `-m 0` metadata, and STAN's heuristics are not reliable). The raw's own
            # method name still wins when present, because on Bruker it is the exact method.
            #
            # CAVEAT, deliberately recorded: DIA-NN can now run DDA, so engine=='diann' is not proof
            # of DIA. Current exposure is small — 71 of 1,963 searches (2,025 of 19,874 raws) — and of
            # the DIA-NN logs reachable on disk, none carried a DDA marker. If that changes, the
            # authoritative source is the DIA-NN log, which engine_version.py already opens.
            acq = md.get("acquisition_method") or _acq_for(engine, platform)
            spd = _detect_spd(run)
            # gradient: EvoSep map if SPD known, else the observed RT span as a proxy
            grad = (_SPD_GRAD.get(int(spd)) if (spd and int(spd) in _SPD_GRAD)
                    else (round(float(run_max_rt[run]), 2) if run_max_rt.get(run) else None))
            # NOT named _im/_is. `_im` is a MODULE-LEVEL function (ion mobility, used at line ~364
            # and in both precursor COPY paths); binding that name anywhere inside ingest() makes it
            # a local for the WHOLE function, so the earlier call raises UnboundLocalError and every
            # Spectronaut ingest fails fast. Caught fleet-wide by win-2 on 2026-08-26.
            _imodel, _iserial = _norm_instrument(md.get("instrument_model"),
                                                 md.get("instrument_serial"))
            _plat = _resolve_platform(md.get("platform") or platform, _imodel, md.get("mobility_min"))
            cur.execute("""INSERT INTO raw_files (raw_path,raw_basename,raw_name_anonymized,platform,
                           acquisition_method,samples_per_day,gradient_minutes,
                           instrument_model,instrument_serial,acquisition_date,
                           mass_range_min,mass_range_max,mobility_min,mobility_max,
                           n_ms1_frames,n_ms2_frames,file_size_bytes,instrument_metadata_json,
                           ingested_schema_version)
                           VALUES (%s,%s,%s,%s,%s,%s,%s, %s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s, %s)
                           ON CONFLICT (raw_path) DO UPDATE SET
                           raw_name_anonymized=EXCLUDED.raw_name_anonymized,
                           acquisition_method=EXCLUDED.acquisition_method,
                           samples_per_day=EXCLUDED.samples_per_day,
                           gradient_minutes=EXCLUDED.gradient_minutes,
                           instrument_model=COALESCE(EXCLUDED.instrument_model, raw_files.instrument_model),
                           instrument_serial=COALESCE(EXCLUDED.instrument_serial, raw_files.instrument_serial),
                           acquisition_date=COALESCE(EXCLUDED.acquisition_date, raw_files.acquisition_date),
                           mass_range_min=COALESCE(EXCLUDED.mass_range_min, raw_files.mass_range_min),
                           mass_range_max=COALESCE(EXCLUDED.mass_range_max, raw_files.mass_range_max),
                           mobility_min=COALESCE(EXCLUDED.mobility_min, raw_files.mobility_min),
                           mobility_max=COALESCE(EXCLUDED.mobility_max, raw_files.mobility_max),
                           n_ms1_frames=COALESCE(EXCLUDED.n_ms1_frames, raw_files.n_ms1_frames),
                           n_ms2_frames=COALESCE(EXCLUDED.n_ms2_frames, raw_files.n_ms2_frames),
                           file_size_bytes=COALESCE(EXCLUDED.file_size_bytes, raw_files.file_size_bytes),
                           instrument_metadata_json=COALESCE(EXCLUDED.instrument_metadata_json, raw_files.instrument_metadata_json)""",
                        (rp, run, sanitize(run), _plat, acq, spd, grad,
                         _imodel, _iserial, md.get("acquisition_date"),
                         md.get("mass_range_min"), md.get("mass_range_max"), md.get("mobility_min"), md.get("mobility_max"),
                         md.get("n_ms1_frames"), md.get("n_ms2_frames"), md.get("file_size_bytes"),
                         md.get("instrument_metadata_json"), SCHEMA_VERSION))
            cur.execute("""INSERT INTO delimp_sample_metadata (raw_path,sample_type,organism_taxon_id,organism_name,ingested_schema_version)
                           VALUES (%s,'study_sample',%s,%s,%s) ON CONFLICT (raw_path) DO UPDATE
                           SET organism_taxon_id=EXCLUDED.organism_taxon_id, organism_name=EXCLUDED.organism_name""",
                        (rp, taxon, organism_name, SCHEMA_VERSION))
            cur.execute("INSERT INTO search_raw_files (search_id,raw_path,n_precursors) VALUES (%s,%s,%s)",
                        (search_id, rp, sum(1 for x in recs if str(x["run"]) == run)))
        psycopg2.extras.execute_values(cur,
            "INSERT INTO delimp_proteins (search_id,raw_path,protein_group,gene,n_unique_peptides,n_precursors,intensity,normalized_intensity,pg_q_value,is_contaminant,ingested_schema_version) VALUES %s",
            [(search_id, raw_paths[k[0]], k[1] or "UNKNOWN", a["gene"], len(a["peps"]), a["n"],
              (a["int"] if a["has_int"] else None), (a["nint"] or None), a["pgq"],
              bool(re.search(r"KRT|keratin|cont_|contaminant", str(k[1]) + str(a["gene"]), re.I)), SCHEMA_VERSION)
             for k, a in prot.items()], page_size=2000)
        # protein_group on each precursor = the peptide<->protein link (enables exact, fast
        # coverage + per-protein quant in the app; see FRAN_REINGEST_AUDIT.md). Included only when
        # the column exists (write_pg) — kept LAST before ingested_schema_version in tuple/header.
        def _pg(x):
            v = x.get("protein_group")
            return str(v) if v else None
        if write_pg:
            prec_rows = [(search_id, raw_paths[str(x["run"])], x["stripped_seq"], x["modified_seq_diann"], x["modified_seq_proforma"],
                  x["mods"], x["n_mods"], int(x["charge"]) if x["charge"] else None, _flt(x["precursor_mz"]), _flt(x["rt"]),
                  _irt(x.get("irt")), _im(x["im"]), _im(x.get("iim")), _flt(x["q_value"]), _flt(x["global_q_value"]), _flt(x["pg_q_value"]),
                  _flt(x["intensity"]), _flt(x["normalized_intensity"]), _pg(x), SCHEMA_VERSION) for x in recs]
        else:
            prec_rows = [(search_id, raw_paths[str(x["run"])], x["stripped_seq"], x["modified_seq_diann"], x["modified_seq_proforma"],
                  x["mods"], x["n_mods"], int(x["charge"]) if x["charge"] else None, _flt(x["precursor_mz"]), _flt(x["rt"]),
                  _irt(x.get("irt")), _im(x["im"]), _im(x.get("iim")), _flt(x["q_value"]), _flt(x["global_q_value"]), _flt(x["pg_q_value"]),
                  _flt(x["intensity"]), _flt(x["normalized_intensity"]), SCHEMA_VERSION) for x in recs]
        prec_cols = _PREC_COLS if write_pg else _PREC_COLS.replace("protein_group,", "")
        if BULK_COPY:
            # COPY is the fastest bulk path (esp. on HIVE, campus-LAN to PG Farm). Safe here because
            # the search's prior rows were already deleted by output_dir, so there's no ON CONFLICT.
            _copy_precursors(cur, prec_rows, prec_cols)
        else:
            psycopg2.extras.execute_values(cur,
                f"INSERT INTO delimp_precursors ({prec_cols}) VALUES %s",
                prec_rows, page_size=5000)
        conn.commit()
        print(f"  COMMITTED search_id={search_id}: {len(recs):,} precursors, "
              f"{len({k[1] for k in prot}):,} distinct proteins ({len(prot):,} protein×run), {len(runs)} runs.")
        # PRIVATE provenance layer: full real names + every raw-file location + parsed
        # customer/PI/project, for internal customer-data tracking + future coreomics/sample-
        # submission linkage. Never blocks the ingest; the public layer stays sanitized.
        try:
            from provenance import record_provenance
            raw_files = [{"name": r, "path": raw_paths.get(r, "")} for r in runs]
            pv = record_provenance(conn, search_id, search_name, output_dir, report, raw_files)
            print(f"  provenance: scope={pv['scope']} client={pv['client']} pi={pv['pi']} "
                  f"project={pv['project']} ({len(raw_files)} raw files)")
        except Exception as e:  # noqa: BLE001 - provenance is best-effort, never fail the ingest
            print(f"  [warn] provenance not recorded: {str(e)[:80]}")
        # OBSERVED-SPECTRUM LANE: write the real acquired spectrum (fragments + MS1 envelope + DIA
        # window + predicted-vs-observed RT/intensity) to a per-search Lance dataset and record it
        # in delimp_spectrum_lane. Best-effort — precursors are already committed, so a lane hiccup
        # must never fail the ingest. Disabled unless --lance-dir is given.
        if WRITE_FRAGMENTS and engine == "spectronaut" and SPECTRUM_LANCE_DIR:
            try:
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                import backfill_fragments as bf
                import spectrum_lance as sln
                sln.ensure_registry(conn)
                _, lpath, n_prec, n_frag, md5, ver = bf.process_one(report, SPECTRUM_LANCE_DIR, dry=False)
                if lpath and n_prec == -1:
                    # RESUME SKIP: the dataset already exists on disk, so process_one returns the
                    # sentinel (-1, -1, None, None) INSTEAD of real counts — registering that would
                    # overwrite a good row with junk (and int(None) raises, which is how this was
                    # found: a re-ingest logged "spectrum lane not written"). Keep the existing row;
                    # only if the dataset is on disk but UNregistered do we re-parse for real values.
                    cur.execute("SELECT 1 FROM delimp_spectrum_lane WHERE lance_path=%s", (lpath,))
                    if cur.fetchone():
                        print(f"  spectrum lane: {lpath} (dataset already present, registry row kept)")
                        lpath = None
                    else:
                        _, lpath, n_prec, n_frag, md5, ver = bf.process_one(
                            report, SPECTRUM_LANCE_DIR, dry=False, resume=False)
                if lpath:
                    sln.register(conn, search_id, search_name, lpath, n_prec, n_frag, md5, ver)
                    print(f"  spectrum lane: {lpath}  ({n_prec:,} prec / {n_frag:,} frag, registered)")
            except Exception as e:  # noqa: BLE001 - spectrum lane best-effort, never fail the ingest
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                # Print the TRACEBACK, not just str(e). This handler used to log `str(e)[:120]`, so
                # a "tuple index out of range" from somewhere inside a 34 GB parse arrived with no
                # file, no line and no frame — undiagnosable after the fact, and the run costs hours
                # to reproduce. An error you cannot locate is barely better than a silent one.
                import traceback as _tb
                print(f"  [warn] spectrum lane not written: {e!r}", flush=True)
                _tb.print_exc()

            # SEPARATE try: the XIC lane reads the .xic.db files and shares nothing with the
            # spectrum lane but the report path. It used to live inside the block above, so the
            # allDog spectrum-lane failure skipped the chromatogram lane entirely — the lane was
            # never attempted, and the log said nothing about it. Independent lanes, independent
            # failure domains.
            try:
                # OBSERVED-CHROMATOGRAM LANE: if this export also dumped the "All XIC" SQLite dbs
                # (--setXICExportDirectory), store the full elution profiles in their own Lance
                # dataset + delimp_xic_lane registry. Same best-effort contract as the spectrum lane.
                if XIC_DIR:
                    import xic_lance as xln
                    xln.ensure_registry(conn)
                    # Own directory, NOT the spectrum-lane dir: verify_spectrum_lane.py and
                    # plan_spectrum_backfill.py glob "*.lance" there, and "<x>.xic.lance" would
                    # be silently counted as a spectrum dataset.
                    xic_out = XIC_LANCE_DIR or os.path.join(
                        os.path.dirname(SPECTRUM_LANCE_DIR.rstrip("/")) or ".", "xic_lance")
                    xpath, xn_prec, xn_tr, xmd5, xver = xln.process_one(
                        report, XIC_DIR, xic_out, search_id=search_id,
                        search_name=search_name)
                    if xpath:
                        xln.register(conn, search_id, search_name, xpath, xn_prec, xn_tr, xmd5, xver)
                        print(f"  XIC lane: {xpath}  ({xn_prec:,} prec / {xn_tr:,} traces, registered)")
                    else:
                        print("  [warn] XIC lane: no traces matched the report's precursors")
            except Exception as e:  # noqa: BLE001 - XIC lane best-effort, never fail the ingest
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                import traceback as _tb
                print(f"  [warn] XIC lane not written: {e!r}", flush=True)
                _tb.print_exc()
    except Exception as e:
        conn.rollback(); raise
    finally:
        conn.close()


BULK_COPY = False         # set by --bulk-copy; uses COPY for the big precursor insert (fast on HIVE)
ALLOW_DUPLICATE = False   # set by --allow-duplicate; bypasses the raw-set duplicate guard in ingest()
WRITE_FRAGMENTS = True    # write the observed-spectrum Lance lane (Spectronaut fragment-level reports)
SPECTRUM_LANCE_DIR = None # dir for per-search Lance datasets (set by --lance-dir); None disables the lane
XIC_DIR = None            # dir of Spectronaut *.xic.db All-XIC dbs (set by --xic-dir); None disables the XIC lane
XIC_LANCE_DIR = None      # where the .xic.lance datasets go (set by --xic-lance-dir); defaults to a sibling 'xic_lance' dir

_PREC_COLS = ("search_id,raw_path,stripped_seq,modified_seq_diann,modified_seq_proforma,mods,n_mods,"
              "charge,precursor_mz,rt,irt,im,iim,q_value,global_q_value,pg_q_value,intensity,"
              "normalized_intensity,protein_group,ingested_schema_version")


def _copy_cell(v):
    """Format one value for COPY text format: None -> \\N, escape \\ \\t \\n \\r in text."""
    if v is None:
        return r"\N"
    s = str(v)
    if any(c in s for c in "\\\t\n\r"):
        s = s.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
    return s


def _copy_precursors(cur, rows, cols=None):
    """Bulk-load delimp_precursors via COPY ... FROM STDIN (text format). ~5-10x faster than
    batched INSERT for millions of rows; the win is largest on a fast PG link (HIVE campus-LAN).
    `cols` is the column header (defaults to the full _PREC_COLS); pass a reduced list when a
    column (e.g. protein_group) isn't present yet so the row tuples match."""
    import io
    cols = cols or _PREC_COLS
    buf = io.StringIO()
    buf.writelines("\t".join(_copy_cell(c) for c in r) + "\n" for r in rows)
    buf.seek(0)
    cur.copy_expert(f"COPY delimp_precursors ({cols}) FROM STDIN", buf)


def _clean_gene(g):
    """Junk gene strings ('NaN'/'nan'/''/'None' — from pandas NaN or empty report cells) -> None,
    so the DB stores NULL (shown as '—') instead of the literal 'NaN' on protein/species pages."""
    if g is None:
        return None
    s = str(g).strip()
    return None if s.lower() in ("nan", "none", "na", "null", "") else s


def _flt(v):
    try:
        f = float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None
    if f is None or f != f:      # f != f is True ONLY for NaN — Spectronaut emits NaN for some
        return None              # EG.Qvalue; store NULL, never a literal NaN (breaks math/ML/sorts)
    # delimp_precursors numeric columns are PostgreSQL `real` (float4). Spectronaut q/p-values
    # can be ~1e-46, which UNDERFLOWS float4 (min ~1e-38) -> "out of range for type real".
    # Clamp the underflow to 0 (an infinitesimal q-value is effectively 0); drop float4
    # overflow. (We must NOT widen the column — ALTER on this shared table = lock hazard.)
    af = abs(f)
    if af != 0.0 and af < 1e-37:
        return 0.0
    if af > 3.4e38:
        return None
    return f


def _im(v):
    """Ion mobility (1/K0) sanity check. DIA-NN writes 0.0 for precursors whose mobility
    was never determined (and Orbitrap data has none at all). A real 1/K0 is ~0.5-1.7;
    0/negative/absurd means 'no ion mobility' -> store NULL so it can't pollute the IM plot."""
    f = _flt(v)
    return f if (f is not None and 0.3 < f < 2.5) else None


def _irt(v):
    """Indexed retention time sanity check. On the Biognosys iRT scale real peptides sit
    roughly -60..+170 (corpus: 99.9% within [-60,167]). Occasional mis-predicted values are
    wildly out of range (we've seen -2900 and 3e12) and would blow out the iRT scatter axis.
    Clamp the implausible ones to NULL (generous bounds so no legitimate value is dropped)."""
    f = _flt(v)
    return f if (f is not None and -100.0 < f < 300.0) else None


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("searchdir")
    ap.add_argument("--engine", default="diann",
                    choices=["diann", "spectronaut", "fragpipe", "radiant"])
    ap.add_argument("--organism-name", default=None)
    ap.add_argument("--taxon", type=int, default=None)
    ap.add_argument("--name", default=None)
    ap.add_argument("--output-dir", default=None, help="stable provenance/idempotency key (e.g. the archived zip path) — use when the report is a temp extract")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-duplicate", action="store_true",
                    help="ingest even if another output_dir already has the same raw-file set and "
                         "precursor count (default: skip, see the duplicate guard)")
    ap.add_argument("--bulk-copy", action="store_true", help="use COPY for the precursor insert (much faster on a fast PG link, e.g. HIVE)")
    ap.add_argument("--no-fragments", action="store_true", help="skip the observed-spectrum Lance lane (precursors only)")
    ap.add_argument("--lance-dir", default=None, help="dir for per-search Lance spectrum datasets (enables the observed-spectrum lane)")
    ap.add_argument("--xic-dir", default=None, help="dir of Spectronaut *.xic.db All-XIC dbs (enables the observed-chromatogram Lance lane; needs --lance-dir)")
    ap.add_argument("--xic-lance-dir", default=None, help="where to write .xic.lance datasets (default: a sibling 'xic_lance' dir next to --lance-dir)")
    a = ap.parse_args()
    BULK_COPY = a.bulk_copy
    ALLOW_DUPLICATE = a.allow_duplicate
    WRITE_FRAGMENTS = not a.no_fragments
    SPECTRUM_LANCE_DIR = a.lance_dir
    XIC_DIR = a.xic_dir
    XIC_LANCE_DIR = a.xic_lance_dir
    ingest(a.searchdir, a.engine, a.organism_name, a.taxon, a.name, a.dry_run, a.output_dir)
