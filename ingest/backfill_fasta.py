#!/usr/bin/env python3
"""backfill_fasta.py — record EVERY database each corpus search used, from the .sne where possible.

Fills delimp_search_fastas (one row per database, contaminant lists included; see
migrations/2026-10-21_search_fastas.sql) and the delimp_searches.fasta_* summary, for searches
already in the corpus. The same detector the ingest runs (engine_fasta.detect_all), replayed over
the archive -- on Hive, because that is where the .sne files are.

WHERE A SEARCH'S DATABASES COME FROM
  Spectronaut   its .sne, which embeds every database it searched, entry for entry. 2,010 rows
                record the .sne itself as output_dir, mostly as a Windows path. R:\\Data and
                K:\\Data are /nfs/lssc0/flinders/proteomics/Data here (1,674 resolve that way);
                the rest are found by name in --sne-index (a `find` of every .sne on Hive) and
                ACCEPTED ONLY IF the .sne names one of the search's own raw files -- a same-named
                .sne is otherwise a guess. About 150 live only on a Windows node's local disk
                (B:\\Automatic_SNE_storage) and need this script run there; it runs unchanged,
                because a recorded path that exists locally is used as is.
  DIA-NN        every --fasta on the logged command line, counted when the file is readable.

WHAT IT WRITES TO delimp_searches -- the summary names main_database(): the largest database that
is neither a contaminant list nor an iRT file.
  fill      fasta_path is NULL: set the summary.
  same      fasta_path already names that database: fill only its NULL md5 / count / contam lib.
  correct   fasta_path names a contaminant list, iRT file or sub-1,000-entry add-on of the same
            search -- cRAP lists, iRT.fasta, 1-13 entry bait constructs. The old detector took
            whichever database was listed first. Overwritten; every one is in the plan file.
  keep      fasta_path names another REAL proteome of the same search (barley beside wheat, a
            pathogen beside its host). Not overwritten -- "largest" is a convention, not a
            correction -- but its own NULL md5 / count are filled from its row.
  conflict  fasta_path names something this search's databases do not include. Left alone and
            reported: two sources disagree and neither is proof.

Dry by default: writes the plan (one line per database, plus the summary action per search) and
changes nothing. --commit applies the migration if needed and writes.

  python3 backfill_fasta.py --sne-index sne_index.tsv --plan plan.tsv
  python3 backfill_fasta.py --sne-index sne_index.tsv --plan plan.tsv --commit
"""
from __future__ import annotations

import argparse
import collections
import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine_fasta import _IRT_NAME, _sne_stream, detect_all, main_database, summarize  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations",
                         "2026-10-21_search_fastas.sql")

# Windows drive / Mac mount -> Hive. Verified on real .sne paths 2026-10-21 (R: 1,073, K: 601).
DRIVE_ROOTS = {"R:": "/nfs/lssc0/flinders/proteomics", "K:": "/nfs/lssc0/flinders/proteomics"}
MOUNTS = (("/Volumes/proteomics-grp/", "/quobyte/proteomics-grp/"),
          ("/Volumes/proteomics/", "/nfs/lssc0/flinders/proteomics/"))
FRAN_REPORTS = "/nfs/lssc0/flinders/proteomics/Data/FRAN_reports/"
RAW_EXT = (".d", ".raw", ".htrms", ".wiff", ".wiff2", ".mzml", ".dia")


def hive_path(p: str | None) -> str | None:
    """Where a recorded output_dir lives on Hive, by mapping alone (no filesystem access)."""
    if not p:
        return None
    w = p.replace("\\", "/")
    while "//" in w:
        w = w.replace("//", "/")
    if len(w) > 1 and w[1] == ":":
        root = DRIVE_ROOTS.get(w[:2].upper())
        return root + "/" + w[2:].lstrip("/") if root else None
    for mac, hive in MOUNTS:
        if w.startswith(mac):
            return hive + w[len(mac):]
    return w


def local_path(p: str | None) -> str | None:
    """The recorded path itself when it exists here -- on the Windows node that wrote it,
    B:\\Automatic_SNE_storage is a local disk Hive cannot see -- else its Hive mapping."""
    if p and os.path.exists(p):
        return p
    return hive_path(p)


def load_index(path: str | None) -> dict[str, list[tuple[int, str]]]:
    """basename.lower() -> [(size, path)] from `find ... -printf '%s\\t%T@\\t%p\\n'`."""
    idx: dict[str, list[tuple[int, str]]] = collections.defaultdict(list)
    if path:
        with open(path) as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) == 3:
                    idx[os.path.basename(parts[2]).lower()].append((int(parts[0]), parts[2]))
    return idx


def _suffix_score(a: str, b: str) -> int:
    pa, pb = a.lower().split("/"), b.lower().split("/")
    n = 0
    while n < min(len(pa), len(pb)) and pa[-1 - n] == pb[-1 - n]:
        n += 1
    return n


def sne_candidates(output_dir: str, idx: dict) -> list[tuple[str, str]]:
    """[(path, how)] to try for a Spectronaut search, best first. `how` is 'path' when the
    recorded path maps straight onto Hive, 'name' when only the file name matched -- the caller
    must then check the .sne against the search's raw files."""
    out: list[tuple[str, str]] = []
    w = output_dir.replace("\\", "/")
    if w.lower().endswith(".sne"):
        direct = local_path(output_dir)
        if direct and os.path.isfile(direct):
            out.append((direct, "path"))
        names = [os.path.basename(w)]
    elif w.startswith(FRAN_REPORTS):
        # a re-export: FRAN_reports/<name>/<ts>_<name>/ -- the .sne was <name>.sne
        names = [w[len(FRAN_REPORTS):].split("/", 1)[0] + ".sne"]
    else:
        names = []
    seen = {p for p, _ in out}
    for nm in names:
        cands = sorted(idx.get(nm.lower(), []), key=lambda t: -_suffix_score(t[1], w))
        for _, p in cands:
            if p not in seen:
                out.append((p, "name"))
                seen.add(p)
    return out


def raw_stems(raws) -> list[str]:
    out = []
    for r in raws or []:
        b = os.path.basename((r or "").replace("\\", "/"))
        for ext in RAW_EXT:
            if b.lower().endswith(ext):
                b = b[:-len(ext)]
                break
        if b:
            out.append(b)
    return out


def sne_mentions_raws(path: str, stems: list[str], nbytes: int = 32 << 20) -> bool:
    """True when the .sne's own stream names at least one of these raw files. A .sne found only
    by its file name is accepted on this and nothing less."""
    if not stems:
        return False
    try:
        with open(path, "rb") as fh:
            buf, have = [], 0
            for chunk in _sne_stream(fh):
                buf.append(chunk)
                have += len(chunk)
                if have >= nbytes:
                    break
        blob = b"".join(buf)
    except OSError:
        return False
    return any(s.encode("utf-16le") in blob for s in stems[:50])


def _base(p: str | None) -> str:
    return os.path.basename((p or "").replace("\\", "/")).lower()


# Below this, a non-contaminant database is an add-on (a bait construct, a tag, a virus), never a
# defensible choice for "the" search database: the dry run's stored values of this kind were 1-13
# entries (KimTurboID.fasta, Human RB1-vojo.fasta, uniprotkb_2024_01_17.fasta).
ADDON_MAX_ENTRIES = 1000


def _is_wrong_choice(d: dict) -> bool:
    n = d.get("n_entries")
    return bool(d.get("is_contaminant") or _IRT_NAME.search(d.get("file_name") or "")
                or (n is not None and n < ADDON_MAX_ENTRIES))


def summary_action(row: dict, dbs: list[dict]) -> tuple[str, dict | None]:
    """(action, values) for delimp_searches; see the module docstring for the actions.

    `correct` is deliberately narrow: only a stored contaminant list, iRT file or sub-1,000-entry
    add-on is replaced. A stored REAL proteome that is not the largest -- barley beside wheat,
    C. fulvum beside tomato, C. posadasii beside human -- is `keep`: for a host-pathogen study the
    smaller one may be the point, "largest" is only a convention, and every database is in
    delimp_search_fastas regardless. `keep` fills that stored database's own NULL md5 / count."""
    summ = summarize(dbs)
    if not summ:
        return "none", None
    main = main_database(dbs)
    cur = row.get("fasta_path")
    if not cur:
        return "fill", summ
    if _base(cur) in (_base(main.get("file_name")), _base(main.get("file_path"))):
        return "same", summ
    named = next((d for d in dbs if _base(cur) in (_base(d.get("file_name")),
                                                   _base(d.get("file_path")))), None)
    if named is None:
        return "conflict", summ
    if _is_wrong_choice(named):
        return "correct", summ
    return "keep", {"fasta_path": cur, "fasta_md5": named.get("file_md5"),
                    "fasta_n_proteins": named.get("n_entries"),
                    "contaminant_lib": summ["contaminant_lib"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="write; otherwise dry-run")
    ap.add_argument("--limit", type=int, default=0, help="0 = all")
    ap.add_argument("--engine", default=None, help="restrict to one engine")
    ap.add_argument("--sne-index", default=None, help="find-output TSV of every .sne on Hive")
    ap.add_argument("--fasta-root", action="append", default=[],
                    help="directory to look for FASTA files in (one level deep); repeatable")
    ap.add_argument("--plan", default="fasta_backfill_plan.tsv", help="where to write the plan")
    ap.add_argument("--redo", action="store_true",
                    help="also revisit searches that already have delimp_search_fastas rows")
    a = ap.parse_args()

    from corpus_ingest import _conn, write_search_fastas  # the ingester's own credential + writer
    cn = _conn()
    cur = cn.cursor()
    cur.execute("SELECT to_regclass('public.delimp_search_fastas')")
    have_table = cur.fetchone()[0] is not None
    if a.commit and not have_table:
        cur.execute(open(MIGRATION).read())
        cn.commit()
        have_table = True
        print("created delimp_search_fastas")

    done: set[str] = set()
    if have_table and not a.redo:
        cur.execute("SELECT DISTINCT search_id::text FROM delimp_search_fastas")
        done = {r[0] for r in cur.fetchall()}
    sql = ("SELECT id::text, search_engine, output_dir, fasta_path, fasta_md5, fasta_n_proteins, "
           "contaminant_lib FROM delimp_searches WHERE output_dir IS NOT NULL"
           + (" AND search_engine = %(eng)s" if a.engine else "") + " ORDER BY ingested_at DESC")
    cur.execute(sql, {"eng": a.engine})
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall() if r[0] not in done]
    if a.limit:
        rows = rows[:a.limit]
    cur.execute("""SELECT srf.search_id::text, rf.raw_basename FROM search_raw_files srf
                   JOIN raw_files rf USING (raw_path)""")
    raws: dict[str, list[str]] = collections.defaultdict(list)
    for sid, rb in cur.fetchall():
        raws[sid].append(rb)
    idx = load_index(a.sne_index)
    print(f"{len(rows)} searches to visit ({len(done)} already recorded); "
          f"{sum(len(v) for v in idx.values())} .sne in the index")

    tally: collections.Counter = collections.Counter()
    t0 = time.time()
    with open(a.plan, "w", newline="") as fh:
        plan = csv.writer(fh, delimiter="\t")
        plan.writerow(["search_id", "engine", "kind", "how", "source_path", "ordinal", "file_name",
                       "is_contaminant", "n_entries", "n_genes", "n_gene_entries", "content_sha256", "file_path",
                       "file_md5", "current_fasta_path", "action", "new_fasta_path",
                       "new_n_proteins"])
        for k, row in enumerate(rows, 1):
            sid, eng = row["id"], (row["search_engine"] or "").lower()
            found, how = None, None
            if eng.startswith("spectronaut"):
                for path, how_ in sne_candidates(row["output_dir"], idx):
                    if how_ == "name" and not sne_mentions_raws(path, raw_stems(raws.get(sid))):
                        tally["sne name match rejected (no shared raw file)"] += 1
                        continue
                    found = detect_all("spectronaut", None, path, a.fasta_root)
                    if found:
                        how = how_
                        break
                if not found:
                    hp = local_path(row["output_dir"])
                    if hp and os.path.isdir(hp):
                        found, how = detect_all(eng, None, hp, a.fasta_root), "dir"
            else:
                hp = local_path(row["output_dir"])
                if hp and os.path.isfile(hp):
                    hp = os.path.dirname(hp)
                if hp and os.path.isdir(hp):
                    found, how = detect_all(eng, None, hp, a.fasta_root), "dir"
            dbs = (found or {}).get("databases") or []
            if not dbs:
                tally[f"{eng}: no source reachable"] += 1
                plan.writerow([sid, eng, "unresolved"] + [""] * 11
                              + [row["fasta_path"] or "", "none", "", ""])
                continue
            action, summ = summary_action(row, dbs)
            tally[f"{eng}: {found['source']} via {how}"] += 1
            tally[f"summary {action}"] += 1
            if sum(1 for d in dbs if not d.get("is_contaminant")) > 1:
                tally["searches with more than one non-contaminant database"] += 1
            for i, d in enumerate(dbs):
                plan.writerow([sid, eng, found["source"], how, found["source_path"], i,
                               d.get("file_name") or "", int(bool(d.get("is_contaminant"))),
                               d.get("n_entries") or "", d.get("n_genes") or "",
                               d.get("n_gene_entries") or "",
                               d.get("content_sha256") or "", d.get("file_path") or "",
                               d.get("file_md5") or "", row["fasta_path"] or "", action,
                               (summ or {}).get("fasta_path") or "",
                               (summ or {}).get("fasta_n_proteins") or ""])
            if a.commit:
                write_search_fastas(cur, sid, found)
                if action == "fill":
                    cur.execute("""UPDATE delimp_searches SET fasta_path=%(p)s,
                                   fasta_md5=COALESCE(fasta_md5,%(m)s),
                                   fasta_n_proteins=COALESCE(fasta_n_proteins,%(n)s),
                                   contaminant_lib=COALESCE(contaminant_lib,%(c)s)
                                   WHERE id=%(id)s AND fasta_path IS NULL""",
                                {"p": summ["fasta_path"], "m": summ["fasta_md5"],
                                 "n": summ["fasta_n_proteins"], "c": summ["contaminant_lib"],
                                 "id": sid})
                elif action in ("same", "keep"):
                    cur.execute("""UPDATE delimp_searches SET fasta_md5=COALESCE(fasta_md5,%(m)s),
                                   fasta_n_proteins=COALESCE(fasta_n_proteins,%(n)s),
                                   contaminant_lib=COALESCE(contaminant_lib,%(c)s)
                                   WHERE id=%(id)s""",
                                {"m": summ["fasta_md5"], "n": summ["fasta_n_proteins"],
                                 "c": summ["contaminant_lib"], "id": sid})
                elif action == "correct":
                    # the stored md5 / count described the WRONG database: replace, never keep
                    cur.execute("""UPDATE delimp_searches SET fasta_path=%(p)s, fasta_md5=%(m)s,
                                   fasta_n_proteins=%(n)s,
                                   contaminant_lib=COALESCE(%(c)s, contaminant_lib)
                                   WHERE id=%(id)s AND fasta_path=%(old)s""",
                                {"p": summ["fasta_path"], "m": summ["fasta_md5"],
                                 "n": summ["fasta_n_proteins"], "c": summ["contaminant_lib"],
                                 "id": sid, "old": row["fasta_path"]})
                if k % 50 == 0:
                    cn.commit()
            if k % 100 == 0:
                print(f"  {k}/{len(rows)}  {time.time() - t0:.0f}s", flush=True)
    if a.commit:
        cn.commit()
    print(f"\nvisited {len(rows)} searches in {time.time() - t0:.0f}s; plan: {a.plan}")
    for key, n in sorted(tally.items()):
        print(f"  {n:6d}  {key}")
    print("DRY RUN — nothing written; re-run with --commit" if not a.commit else "committed")
    cn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
