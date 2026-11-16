"""build_run_flags.py - give every raw RUN a stable train/val/test split and a shareability class.

Design doc section 4.6 (docs/superpowers/specs/2026-11-12-training-data-retention-design.md).

SPLIT BY GROUP, ASSIGN ONCE (split_rule v3, 2026-11-13).
v1 hashed each run on its own. That kept one run's copies and searches together, but scattered the
REPLICATES of one sample across train and test, so a de novo / rescoring model would be tested on
peptides -- and near-identical spectra -- it had trained on. Published de novo benchmarks split by
project for this reason. v1 was replaced the day it was written; nothing had trained on it.

v2 links runs into leakage groups (union-find) and hashes the group:
  - CoreOmics submission (delimp_search_provenance.coreomics_submission_id) of any search of the run
  - PROT_ number in the run name (prot:<n>; the provenance ids are hashes, so these are separate keys)
  - PXD accession (pride_accession, or PXDnnnnnn in the search name / output dir / raw path)
  - the raw file's folder, but ONLY when that folder holds at most one submission. Folders like
    raw_data/ttof_ht/jul26 are monthly instrument dumps holding many projects; linking through them
    chained 23.5% of all runs into one group (measured 2026-11-13). In such a folder only the
    untagged runs are linked, to each other.
Measured on 17,679 runs: ~1,000 groups, largest ~3%, 0 of 883 single-submission folders split
across groups; runs ~89% train / ~5% val / ~6% test.

STANDARDS ARE ALWAYS TRAIN (v3). The core runs the same standards every month in different folders
(HeLa in 32 v2 groups, NIST in 11, blanks, K562, PRTC/iRT), so per-group hashing put the same HeLa
peptides on both sides: 56 standard runs were in test/val under v2. A run whose name matches a
standard joins one group per standard type (std:hela, ...), links to nothing else, and is train.
Test is therefore client / project samples only. Yeast and E. coli are NOT treated as standards:
they are often the client's own organism. Name matching misses unnamed standards; that is the
known residue.

group_key = the component's smallest submission / PROT / PXD label, else its smallest folder label,
else the run's own (std:<type> for standards). split = md5(group_key) as an integer mod 100:
0-4 test, 5-9 val, 10-99 train; std:* always train.

ONCE: after a rule's rewrite (--resplit), split is never rewritten under that rule; --resplit is
refused once any row carries the current rule. v1 -> v2 -> v3 all happened on 2026-11-13, before
anything trained on the table. A NEW run whose group already has current-rule runs inherits their split (whatever the group_key hashes to now). If new evidence merges two
groups that were already assigned different splits, that is a CONFLICT: it is reported (and the new
runs take the majority side), never silently resolved by moving existing runs.

SHAREABLE decides whether model WEIGHTS trained on the run may be published. Precedence is
deliberately conservative -- the first rule that matches wins:
  client    any search of the run carries customer provenance (client / PI / service customer /
            CoreOmics submission), or a raw copy lives under a service directory
  public    a search of the run is a public dataset (pride_accession, sharing_status 'public',
            or a PXD accession in its name or path)
  internal  the core's own QC / teaching runs (hela_qcs, shortcourse, training_DDA_data)
  unknown   no evidence either way -- treat as client
shareable and group_key ARE refreshed on every run (evidence accumulates as provenance is filled).

    python build_run_flags.py                      # dry run: distribution, nothing written
    python build_run_flags.py --apply              # nightly (fran_fill): new runs + refresh
    python build_run_flags.py --apply --resplit    # ONE-TIME rewrite of every split to the current rule
"""
import argparse
import hashlib
import os
import re
import sys
from collections import Counter, defaultdict

RULE_VERSION = "v3"
SPLIT_RULE = (RULE_VERSION + ": union-find groups (submission, PROT_ no., PXD, single-submission "
              "folder); standards (std:*) always train; int(md5(group_key)[0:8],16) % 100 -> "
              "0-4 test, 5-9 val, else train; inherited once set")

EVIDENCE_SQL = r"""
WITH runs AS (
    SELECT lower(r.raw_basename) AS run_key,
           bool_or(coalesce(r.raw_path, '') ~* '/(service|SERVICE)/'
                   OR coalesce(r.hive_path, '') ~* '/(service|SERVICE)/')               AS svc_path,
           bool_or(coalesce(r.raw_path, '') || ' ' || coalesce(r.hive_path, '')
                   ~* '(hela_qcs|shortcourse|training_DDA_data)')                       AS own_path,
           min(substring(coalesce(r.raw_path, '') || ' ' || coalesce(r.hive_path, '')
                         FROM 'PXD[0-9]{6}'))                                            AS pxd_path,
           min(nullif(btrim(r.hive_path), ''))                                           AS hive_path,
           min(r.raw_path)                                                               AS raw_path
      FROM raw_files r
     WHERE nullif(btrim(r.raw_basename), '') IS NOT NULL
     GROUP BY 1),
srch AS (
    SELECT lower(r.raw_basename) AS run_key,
           bool_or(nullif(btrim(p.client), '') IS NOT NULL
                   OR nullif(btrim(p.pi), '') IS NOT NULL
                   OR nullif(btrim(p.service_customer), '') IS NOT NULL
                   OR nullif(btrim(p.coreomics_submission_id), '') IS NOT NULL)          AS cust,
           bool_or(nullif(btrim(s.pride_accession), '') IS NOT NULL
                   OR s.sharing_status = 'public'
                   OR s.search_name ~ 'PXD[0-9]{6}' OR s.output_dir ~ 'PXD[0-9]{6}')     AS pub,
           array_agg(DISTINCT nullif(btrim(p.coreomics_submission_id), ''))              AS subs,
           min(coalesce(nullif(btrim(s.pride_accession), ''),
                        substring(s.search_name FROM 'PXD[0-9]{6}'),
                        substring(s.output_dir FROM 'PXD[0-9]{6}')))                     AS pxd
      FROM raw_files r
      JOIN search_raw_files sr ON sr.raw_path = r.raw_path
      JOIN delimp_searches s ON s.id = sr.search_id
      LEFT JOIN delimp_search_provenance p ON p.search_id = s.id
     WHERE nullif(btrim(r.raw_basename), '') IS NOT NULL
     GROUP BY 1)
SELECT runs.run_key, runs.svc_path, runs.own_path, runs.pxd_path,
       coalesce(srch.cust, false), coalesce(srch.pub, false),
       srch.subs, srch.pxd, runs.hive_path, runs.raw_path
  FROM runs LEFT JOIN srch USING (run_key)
"""

PROT = re.compile(r"prot[_-]?0*(\d{2,5})", re.I)
# Standard / QC samples by run name -> one group each, always train. Order matters (first match).
STANDARDS = [("hela", re.compile(r"hela", re.I)),
             ("nist", re.compile(r"nist", re.I)),
             ("blank", re.compile(r"blank", re.I)),
             ("k562", re.compile(r"k562", re.I)),
             ("rt_standard", re.compile(r"pierce|prtc|(^|[_-])irt([_-]|$)", re.I))]


def standard_of(run_key):
    for name, pat in STANDARDS:
        if pat.search(run_key):
            return "std:" + name
    return None


def split_of(key):
    if key.startswith("std:"):
        return "train"
    v = int(hashlib.md5(key.encode("utf-8")).hexdigest()[:8], 16) % 100
    return "test" if v < 5 else "val" if v < 10 else "train"


def shareable_of(svc_path, own_path, pxd_path, cust, pub):
    if cust:
        return "client", "a search of this run carries customer provenance"
    if svc_path:
        return "client", "a raw copy lives under a service directory"
    if pub or pxd_path:
        return "public", "public dataset (PRIDE/PXD accession or sharing_status public)"
    if own_path:
        return "internal", "core QC / teaching run (hela_qcs, shortcourse, training_DDA_data)"
    return "unknown", "no provenance either way; treat as client"


def link_keys(run_key, subs, pxd, hive_path, raw_path):
    """The run's linking labels: (submission-like labels, pxd label, folder label)."""
    tags = {"sub:" + s for s in (subs or []) if s}
    m = PROT.search(run_key)
    if m:
        tags.add("prot:" + str(int(m.group(1))))
    src = hive_path or (raw_path if (raw_path or "").startswith("/") else "")
    folder = os.path.dirname((src or "").rstrip("/")).lower()
    return sorted(tags), ("pxd:" + pxd if pxd else None), ("dir:" + folder if folder else None)


def groups(keys):
    """keys: run_key -> (tags, pxd, folder). Returns run_key -> group_key (see module docstring)."""
    folder_tags = defaultdict(set)
    for k, (tags, _, folder) in keys.items():
        if folder and not standard_of(k):
            folder_tags[folder].update(tags)
    par = {}

    def find(x):
        par.setdefault(x, x)
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x

    std = {k: standard_of(k) for k in keys}
    for k, (tags, pxd, folder) in keys.items():
        if std[k]:
            continue                      # a standard links to nothing; see the module docstring
        node = find("run:" + k)
        links = list(tags) + ([pxd] if pxd else [])
        if folder and (len(folder_tags[folder]) <= 1 or not links):
            links.append(folder)
        for x in links:
            rx = find(x)
            if rx != node:
                par[rx] = node
    members = defaultdict(list)
    labels = defaultdict(set)
    for x in list(par):
        r = find(x)
        if x.startswith("run:"):
            members[r].append(x[4:])
        else:
            labels[r].add(x)
    out = {}
    for r, runs in members.items():
        gk = (min(labels[r], key=lambda x: (x.startswith("dir:"), x)) if labels[r]
              else "run:" + min(runs))
        for k in runs:
            out[k] = gk
    for k, g in std.items():
        if g:
            out[k] = g
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--resplit", action="store_true",
                    help="ONE-TIME: rewrite every split with the v2 rule (refused once any row is v2)")
    a = ap.parse_args()
    from coreomics_import import _conn
    conn = _conn()
    with conn.cursor() as c:
        c.execute("SET statement_timeout = '20min'")
        c.execute(EVIDENCE_SQL)
        rows = c.fetchall()
        c.execute("SELECT run_key, split, split_rule FROM delimp_run_flags")
        existing = {k: (sp, rule) for k, sp, rule in c.fetchall()}
    def current(rule):
        return (rule or "").startswith(RULE_VERSION + ":")

    n_cur = sum(1 for _, rule in existing.values() if current(rule))
    if a.resplit and n_cur:
        print(f"REFUSED: {n_cur:,} row(s) already carry a {RULE_VERSION} split; --resplit is the "
              f"one-time rewrite to a new rule and would move runs a model may already be "
              f"evaluated on.", file=sys.stderr)
        return 2

    keys, share = {}, {}
    for run_key, svc, own, pxd_path, cust, pub, subs, pxd, hive_path, raw_path in rows:
        keys[run_key] = link_keys(run_key, subs, pxd or pxd_path, hive_path, raw_path)
        share[run_key] = shareable_of(svc, own, pxd_path, cust, pub)
    gk = groups(keys)

    # The split each group already holds (current-rule rows only; older rows are being replaced).
    held = defaultdict(Counter)
    if not a.resplit:
        for k, g in gk.items():
            sp, rule = existing.get(k, (None, None))
            if sp and current(rule):
                held[g][sp] += 1
    conflicts = {g: c for g, c in held.items() if len(c) > 1}

    out, dist, inherited = [], Counter(), 0
    for k, g in gk.items():
        if g.startswith("std:"):
            sp = "train"
        elif held.get(g):
            sp = held[g].most_common(1)[0][0]
            inherited += k not in existing or not current(existing[k][1])
        else:
            sp = split_of(g)
        sh, why = share[k]
        out.append((k, sp, SPLIT_RULE, g, sh, why))
        dist[(sp, sh)] += 1
    moved = Counter((existing[k][0], sp) for k, sp, *_ in out
                    if k in existing and existing[k][0] != sp)
    if moved:
        print("runs that would change side (from -> to):",
              {f"{f}->{t}": n for (f, t), n in sorted(moved.items())})
    n_groups = len(set(gk.values()))
    big = Counter(gk.values()).most_common(3)
    print(f"{len(out):,} runs in {n_groups:,} groups; largest: "
          + ", ".join(f"{g[:60]} ({n:,})" for g, n in big))
    by_split = Counter(sp for _, sp, *_ in out)
    print("split:", {s: f"{n:,} ({n / max(len(out), 1):.1%})" for s, n in sorted(by_split.items())},
          f"| new runs inheriting their group's split: {inherited:,}")
    for (sp, sh), n in sorted(dist.items()):
        print(f"  {sp:5s} {sh:8s} {n:7,}")
    if conflicts:
        print(f"CONFLICT: {len(conflicts):,} group(s) now link runs that were assigned different "
              f"splits (new evidence merged them). Existing rows are NOT moved; review:", file=sys.stderr)
        for g, c in list(conflicts.items())[:10]:
            print(f"   {g}: {dict(c)}", file=sys.stderr)
    if not a.apply:
        print("dry run: nothing written (--apply to write)")
        return 0

    from psycopg2.extras import execute_values
    with conn.cursor() as c:
        c.execute("SET lock_timeout = '5s'")
        c.execute("SET statement_timeout = '10min'")
        if a.resplit:
            # The one-time v1 -> v2 rewrite: every column, every row.
            sql = """INSERT INTO delimp_run_flags (run_key, split, split_rule, group_key, shareable,
                                                   shareable_reason)
                     VALUES %s
                     ON CONFLICT (run_key) DO UPDATE
                        SET split = EXCLUDED.split, split_rule = EXCLUDED.split_rule,
                            set_at = now(), group_key = EXCLUDED.group_key,
                            shareable = EXCLUDED.shareable,
                            shareable_reason = EXCLUDED.shareable_reason, updated_at = now()"""
        else:
            # Nightly: split is only ever INSERTED; existing rows refresh group_key and shareable.
            sql = """INSERT INTO delimp_run_flags (run_key, split, split_rule, group_key, shareable,
                                                   shareable_reason)
                     VALUES %s
                     ON CONFLICT (run_key) DO UPDATE
                        SET group_key = EXCLUDED.group_key,
                            shareable = EXCLUDED.shareable,
                            shareable_reason = EXCLUDED.shareable_reason, updated_at = now()
                      WHERE (delimp_run_flags.group_key, delimp_run_flags.shareable,
                             delimp_run_flags.shareable_reason)
                            IS DISTINCT FROM (EXCLUDED.group_key, EXCLUDED.shareable,
                                              EXCLUDED.shareable_reason)"""
        execute_values(c, sql, out, page_size=2000)
    conn.commit()
    with conn.cursor() as c:
        c.execute("""SELECT split, count(*), count(DISTINCT group_key),
                            count(*) FILTER (WHERE split_rule LIKE 'v3:%%')
                       FROM delimp_run_flags GROUP BY 1 ORDER BY 1""")
        print(f"in table (split, runs, groups, {RULE_VERSION} rows):", c.fetchall())
    conn.close()
    return 1 if conflicts else 0


if __name__ == "__main__":
    sys.exit(main())
