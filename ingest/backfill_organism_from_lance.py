r"""backfill_organism_from_lance.py — infer each run's organism from the Lance spectrum lane.

PROVENANCE — read this before trusting the output.

The Lance `organism` column maps to Spectronaut's **`PEP.AllOccurringOrganisms`**
(backfill_fragments.py:66). That is a PEPTIDE-LEVEL OCCURRENCE annotation: every organism in the
searched FASTA whose proteins contain that peptide. It is NOT "the organism of the sample".

So what this script computes is: *which organism's proteins account for the most
organism-UNIQUE identified peptides in this run*. For a single-organism FASTA plus contaminants that
is the sample organism. For a genuinely multi-organism search (host+pathogen, the dog+yeast
entrapment) it is the dominant contributor, which may not be "the sample".

That is EVIDENCE, not a record — so it is written to the purpose-built
`delimp_sample_metadata.predicted_organism_*` slot (name, taxon, confidence, method,
n_peptides_scored, top3_json), which exists for exactly this and is currently populated for 1 of
19,874 rows. **`organism_name` is never touched**: conflating an inference with a curated fact is how
you get a corpus nobody can audit.

WHY THIS SOURCE AT ALL. Every search ran against a proteome, so every run has an organism — FRAN just
never recorded which. `delimp_searches.fasta_path` is filled for 157 of 1,963 searches, `fasta_md5`
and `fasta_n_proteins` for none, and `corpus_ingest.py` never writes any of them. The archive does not
carry it either: Spectronaut's AnalysisLog logs the operation ("Digesting Fasta...") but never the
filename, and the `.params` files are 126-byte export summaries. The report's own organism annotation
is what survived.

METHOD. Only organism-UNIQUE peptides vote. A ';'-joined value ('Cicer arietinum;Homo sapiens') means
the peptide occurs in BOTH, which is uninformative about which one the sample is — counting it toward
each would let shared peptides decide the answer. Those are tallied separately and reported.
Contaminants are why a mode is needed at all: measured runs show 'Octopus vulgaris' 1,185 vs
'Bos taurus' 72 (BSA/trypsin), 'Cicer arietinum' 15,438 vs 'Homo sapiens' 80 (keratin).

    python ingest/backfill_organism_from_lance.py            # dry run
    python ingest/backfill_organism_from_lance.py --apply
"""
import argparse
import functools
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
print = functools.partial(print, flush=True)   # noqa: A001

METHOD = "lance_pep_all_occurring_organisms_unique_mode/1.0"
MIN_SHARE = 0.60      # modal organism must hold this share of organism-unique peptides
MIN_ROWS = 20         # and the run needs at least this many unique-organism rows


def _conn():
    import psycopg2
    from refresh_leaderboards import _token
    return psycopg2.connect(
        host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
        dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30,
        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=10,
        options="-c statement_timeout=600000")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit-datasets", type=int, default=0)
    a = ap.parse_args()

    import lance
    conn = _conn(); conn.autocommit = False
    cur = conn.cursor()

    cur.execute("""
        SELECT DISTINCT rf.raw_basename, lr.lance_path
        FROM delimp_sample_metadata m
        JOIN raw_files rf ON rf.raw_path = m.raw_path
        JOIN delimp_spectrum_lane_runs lr ON lr.run = rf.raw_basename
        WHERE m.organism_name IS NULL OR m.organism_taxon_id IS NULL""")
    rows = cur.fetchall()
    by_ds = defaultdict(set)
    for run, path in rows:
        by_ds[path].add(run)
    print(f"gap runs: {len({r[0] for r in rows}):,} across {len(by_ds):,} datasets")

    dsets = sorted(by_ds)
    if a.limit_datasets:
        dsets = dsets[:a.limit_datasets]

    resolved, weak, missing_ds = {}, {}, 0
    for i, path in enumerate(dsets, 1):
        if not os.path.exists(path):
            missing_ds += 1
            continue
        want = by_ds[path]
        try:
            t = lance.dataset(path).scanner(columns=["run", "organism"]).to_table().to_pydict()
        except Exception as e:  # noqa: BLE001
            print(f"  skip {os.path.basename(path)[:44]}: {str(e)[:50]}")
            continue
        uniq = defaultdict(Counter)   # run -> organism -> n (organism-UNIQUE peptides only)
        shared = Counter()            # run -> n multi-organism peptides (uninformative)
        runs, orgs = t["run"], t["organism"]
        for j in range(len(runs)):
            r = runs[j]
            if r not in want:
                continue
            o = orgs[j]
            if not o:
                continue
            parts = [p.strip() for p in str(o).split(";") if p.strip()]
            if len(parts) == 1:
                uniq[r][parts[0]] += 1
            else:
                shared[r] += 1
        for r, cnt in uniq.items():
            total = sum(cnt.values())
            top = cnt.most_common(3)
            if total < MIN_ROWS:
                weak[r] = (top, 0.0, total); continue
            name, n = top[0]
            share = n / total
            if share < MIN_SHARE:
                weak[r] = (top, share, total); continue
            resolved[r] = {"name": name, "share": share, "n": total,
                           "shared": shared.get(r, 0),
                           "top3": [{"organism": o2, "n": n2} for o2, n2 in top]}
        if i % 50 == 0:
            print(f"  [{i}/{len(dsets)}] resolved {len(resolved):,}")
    print(f"\nresolved {len(resolved):,} runs | ambiguous {len(weak):,} | "
          f"datasets absent {missing_ds}")

    tally = Counter(v["name"] for v in resolved.values())
    print("\ntop organisms inferred:")
    for n, k in tally.most_common(15):
        print(f"  {n[:44]:46s} {k:>6,} runs")
    if weak:
        print("\nleft as NOT PREDICTED (no dominant organism-unique signal):")
        for r, (top, share, tot) in list(weak.items())[:5]:
            print(f"  {r[:34]:36s} share={share:.2f} n={tot} {top}")

    if not a.apply:
        print("\nDRY RUN — re-run with --apply. organism_name is never modified.")
        conn.close(); return

    import psycopg2.extras
    payload = [(v["name"], float(v["share"]), METHOD, int(v["n"]),
                json.dumps(v["top3"]), run) for run, v in resolved.items()]
    psycopg2.extras.execute_batch(cur, """
        UPDATE delimp_sample_metadata m
           SET predicted_organism_name = %s,
               predicted_organism_confidence = %s,
               predicted_organism_method = %s,
               predicted_organism_n_peptides_scored = %s,
               predicted_organism_top3_json = %s::jsonb,
               predicted_organism_at = now()
          FROM raw_files rf
         WHERE rf.raw_path = m.raw_path AND rf.raw_basename = %s""", payload, page_size=500)
    conn.commit()
    print(f"\npredicted_organism_* written for {len(payload):,} runs")

    # Resolve the predicted taxon from names that already carry an unambiguous taxon.
    cur.execute("""
        UPDATE delimp_sample_metadata m
           SET predicted_organism_taxon_id = src.taxon
          FROM (SELECT organism_name, min(organism_taxon_id) AS taxon
                FROM delimp_sample_metadata
                WHERE organism_name IS NOT NULL AND organism_taxon_id IS NOT NULL
                GROUP BY 1 HAVING count(DISTINCT organism_taxon_id) = 1) src
         WHERE m.predicted_organism_name = src.organism_name
           AND m.predicted_organism_taxon_id IS NULL""")
    print(f"predicted taxon resolved for {cur.rowcount:,} rows")
    conn.commit()

    cur.execute("""SELECT count(*), count(organism_name), count(organism_taxon_id),
                          count(predicted_organism_name), count(predicted_organism_taxon_id)
                   FROM delimp_sample_metadata""")
    n, nm, tx, pn, pt = cur.fetchone()
    print(f"\ncoverage: organism_name {nm:,}/{n:,} | taxon {tx:,} | "
          f"predicted_name {pn:,} | predicted_taxon {pt:,}")
    cur.execute("""SELECT count(*) FROM delimp_sample_metadata
                   WHERE organism_name IS NULL AND predicted_organism_name IS NOT NULL""")
    print(f"runs that had NO organism and now have a prediction: {cur.fetchone()[0]:,}")

    import versions as V
    V.record_run(cur, "organism_from_lance", "1.0.0", notes=f"{len(payload)} runs, {METHOD}")
    conn.commit(); conn.close()
    print("DONE")


if __name__ == "__main__":
    main()
