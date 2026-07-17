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

SCHEMA_VERSION = "1.0.0"
_UNIMOD = {"UniMod:4": 4, "UniMod:35": 35, "UniMod:1": 1, "UniMod:21": 21, "UniMod:7": 7}


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
    """Yield normalized per-precursor dicts from DIA-NN parquet or Spectronaut."""
    if engine == "spectronaut":
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from spectronaut_to_corpus import iter_records
        yield from iter_records(report_path)
        return
    import pandas as pd
    # DIA-NN 1.8/1.9 wrote report.tsv (tab-separated); 2.0+ writes parquet. Read either.
    if str(report_path).lower().endswith((".tsv", ".txt", ".csv")):
        df = pd.read_csv(report_path, sep="\t", low_memory=False)
    else:
        df = pd.read_parquet(report_path)
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
    if has_real_im or (looks_dotd and not looks_raw):
        platform = "timstof"
    else:
        platform = "orbitrap"
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
    print(f"[{engine}] {search_name}: {len(recs):,} precursors, {len(runs)} runs, {len(prot):,} protein×run, platform={platform}")
    if dry:
        print("  DRY RUN — sample precursor:", {k: recs[0][k] for k in ("run", "stripped_seq", "charge", "rt", "im", "q_value", "protein_group")})
        print("  (no DB writes)")
        return

    import psycopg2.extras
    conn = _conn(); conn.autocommit = False
    try:
        cur = conn.cursor()
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
        cur.execute("SELECT id FROM delimp_searches WHERE output_dir=%s", (output_dir,))
        for (sid,) in cur.fetchall():
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
        cur.execute("""INSERT INTO delimp_searches (id,search_name,output_dir,submitted_at,search_engine,
                       pipeline_id,n_raw_files,n_precursors_total,n_proteins_total,status,ingested_schema_version)
                       VALUES (%s,%s,%s,NOW(),%s,%s,%s,%s,%s,'completed',%s)""",
                    (search_id, search_name, output_dir, engine, f"{engine}-uploader",
                     len(runs), len(recs), len({k[1] for k in prot}), SCHEMA_VERSION))
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

        # raw_files (ON CONFLICT update anonymized name) + sample_metadata + junction
        for run in runs:
            rp = raw_paths[run]
            # acquisition: DIA-NN is a DIA tool; on timsTOF (.d) that's diaPASEF.
            acq = "diaPASEF" if platform == "timstof" else "DIA"
            spd = _detect_spd(run)
            # gradient: EvoSep map if SPD known, else the observed RT span as a proxy
            grad = (_SPD_GRAD.get(int(spd)) if (spd and int(spd) in _SPD_GRAD)
                    else (round(float(run_max_rt[run]), 2) if run_max_rt.get(run) else None))
            cur.execute("""INSERT INTO raw_files (raw_path,raw_basename,raw_name_anonymized,platform,
                           acquisition_method,samples_per_day,gradient_minutes,ingested_schema_version)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (raw_path) DO UPDATE SET
                           raw_name_anonymized=EXCLUDED.raw_name_anonymized,
                           acquisition_method=EXCLUDED.acquisition_method,
                           samples_per_day=EXCLUDED.samples_per_day,
                           gradient_minutes=EXCLUDED.gradient_minutes""",
                        (rp, run, sanitize(run), platform, acq, spd, grad, SCHEMA_VERSION))
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
                _, lpath, n_prec, n_frag, md5, ver = bf.process_one(report, SPECTRUM_LANCE_DIR, dry=False)
                if lpath:
                    sln.ensure_registry(conn)
                    sln.register(conn, search_id, search_name, lpath, n_prec, n_frag, md5, ver)
                    print(f"  spectrum lane: {lpath}  ({n_prec:,} prec / {n_frag:,} frag, registered)")
            except Exception as e:  # noqa: BLE001 - spectrum lane best-effort, never fail the ingest
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                print(f"  [warn] spectrum lane not written: {str(e)[:120]}")
    except Exception as e:
        conn.rollback(); raise
    finally:
        conn.close()


BULK_COPY = False  # set by --bulk-copy; uses COPY for the big precursor insert (fast on HIVE)
WRITE_FRAGMENTS = True    # write the observed-spectrum Lance lane (Spectronaut fragment-level reports)
SPECTRUM_LANCE_DIR = None # dir for per-search Lance datasets (set by --lance-dir); None disables the lane

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
    if f is None:
        return None
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
    ap.add_argument("--engine", default="diann", choices=["diann", "spectronaut"])
    ap.add_argument("--organism-name", default=None)
    ap.add_argument("--taxon", type=int, default=None)
    ap.add_argument("--name", default=None)
    ap.add_argument("--output-dir", default=None, help="stable provenance/idempotency key (e.g. the archived zip path) — use when the report is a temp extract")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--bulk-copy", action="store_true", help="use COPY for the precursor insert (much faster on a fast PG link, e.g. HIVE)")
    ap.add_argument("--no-fragments", action="store_true", help="skip the observed-spectrum Lance lane (precursors only)")
    ap.add_argument("--lance-dir", default=None, help="dir for per-search Lance spectrum datasets (enables the observed-spectrum lane)")
    a = ap.parse_args()
    BULK_COPY = a.bulk_copy
    WRITE_FRAGMENTS = not a.no_fragments
    SPECTRUM_LANCE_DIR = a.lance_dir
    ingest(a.searchdir, a.engine, a.organism_name, a.taxon, a.name, a.dry_run, a.output_dir)
