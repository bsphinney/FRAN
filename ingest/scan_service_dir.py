"""scan_service_dir.py — inventory the service share so the un-ingested list stops rotting.

WHY THIS EXISTS. delimp_submission_service_dir was written ONCE, on 2026-06-24, by an "ai-disk-match"
whose source TSVs lived in a Claude scratchpad that no longer exists. It holds 1,862 rows -- 1,624
submissions whose data is on the share and NOT in FRAN -- and it has known nothing since June. It is
also load-bearing: build_resubmit_brief() reads service_folder / service_folder_win to tell a
HIVE Claude where the raw data is, so every submission after PROT_0724 gets a brief with no paths.

This inventories the share deterministically. Matching a folder to a CoreOmics submission is a
SEPARATE, conservative pass (see match_submissions) that never overwrites a human-reviewed row.
"""
from __future__ import annotations

import os

SERVICE_ROOT = os.environ.get("FRAN_SERVICE_ROOT",
                              "/nfs/lssc0/flinders/proteomics/Data/lab/service")
WIN_ROOT = r"R:\Data\lab\service"

# Campus-level entries that are not client folders. Non-directories are skipped anyway; these are
# the directories that would otherwise be walked as if they were campuses.
SKIP_AT_CAMPUS = {"Thumbs.db"}
SKIP_PREFIXES = ("htrms_quarantine_",)

# Track paths that could not be read during the most recent walk.
_UNREADABLE_PATHS: list[str] = []


def get_unreadable_paths() -> list[str]:
    """Paths that could not be read during the most recent walk."""
    return _UNREADABLE_PATHS.copy()


def win_path(rel: str) -> str:
    """'on_campus/A/B' -> the R: spelling stored in service_folder_win."""
    return WIN_ROOT + "\\" + rel.replace("/", "\\")


def count_runs(path: str) -> int | None:
    """Raw acquisitions directly under `path`: .d directories plus .raw files.

    Returns None if the path could not be read (OSError).
    Counts symlinked .d directories and .raw files (resolving the link for type testing).
    Does NOT descend into a .d -- it is one acquisition stored as a directory of instrument files,
    so walking into it would count its internals as runs.
    """
    n = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                low = e.name.lower()
                # follow_symlinks=True so symlinked .d dirs and .raw files are counted
                if e.is_dir(follow_symlinks=True) and low.endswith(".d"):
                    n += 1
                elif e.is_file(follow_symlinks=True) and low.endswith(".raw"):
                    n += 1
    except OSError:
        return None
    return n


def _skip(name: str) -> bool:
    return name in SKIP_AT_CAMPUS or name.startswith(SKIP_PREFIXES)


def walk_projects(root: str = SERVICE_ROOT) -> list[dict]:
    """Every campus/client/project(/subproject) folder on the share, with its run count.

    Depth-3 (campus/client/project) is the share's usual shape, but off-campus institutions
    routinely nest one level deeper (campus/institution/lab/project) -- e.g.
    off_campus/UC-Berkeley/ChangChris held 0 direct runs and 40-odd real projects underneath.
    So depth 3 is adaptive: a depth-3 directory with ZERO direct runs AND at least one
    subdirectory is treated as an intermediate, not a project -- it is not recorded itself;
    its children are recorded as depth-4 rows instead, and the walk stops there (no depth 5).
    A depth-3 directory that has runs stays a row even if it ALSO has subdirectories (a project
    routinely holds a results/ folder alongside its raw data) -- it is not descended into, and
    only its own direct run_count is recorded. A .d acquisition is itself a directory, so "has
    subdirectories" alone would misclassify a project full of .d dirs as an intermediate; the
    zero-runs condition guards against that, since such a project has run_count > 0.
    An unreadable directory (count_runs -> None) is never treated as an intermediate: None is
    not 0, so it is recorded as unreadable and not descended into, full stop.

    Walks into symlinked directories but tracks visited real paths at campus/client/intermediate
    levels to avoid infinite loops. At the final (project) level, includes all discovered
    projects even if symlinked, so each unique service_folder path appears in the output.

    NOTE: visited_realpaths is global to the walk, so two distinct symlinked siblings pointing
    at the same real target would silently drop the second — same shape as the loop case, no signal.

    NOTE: OSError at the top-level os.scandir(root) does a bare return out, unlike campus/client
    branches which append to _UNREADABLE_PATHS. Only matters if service root itself is unreadable.

    NOTE: with follow_symlinks=True, a self-referential symlink (ELOOP) can make is_dir() raise
    inside count_runs(), and the surrounding except OSError then marks the whole project as
    unreadable rather than skipping just the one bad entry.
    """
    global _UNREADABLE_PATHS
    _UNREADABLE_PATHS = []
    out: list[dict] = []
    # Track visited realpaths to prevent walking into symlinks that point to ancestors
    visited_realpaths: set[str] = set()
    root_real = os.path.realpath(root)
    visited_realpaths.add(root_real)

    try:
        campuses = sorted(e.name for e in os.scandir(root)
                          if e.is_dir(follow_symlinks=True) and not _skip(e.name))
    except OSError:
        return out

    for campus in campuses:
        cpath = os.path.join(root, campus)
        cpath_real = os.path.realpath(cpath)
        if cpath_real in visited_realpaths:
            continue
        visited_realpaths.add(cpath_real)

        try:
            clients = sorted(e.name for e in os.scandir(cpath) if e.is_dir(follow_symlinks=True))
        except OSError:
            _UNREADABLE_PATHS.append(cpath)
            continue

        for client in clients:
            clpath = os.path.join(cpath, client)
            clpath_real = os.path.realpath(clpath)
            if clpath_real in visited_realpaths:
                continue
            visited_realpaths.add(clpath_real)

            try:
                projects = sorted(e.name for e in os.scandir(clpath)
                                  if e.is_dir(follow_symlinks=True))
            except OSError:
                _UNREADABLE_PATHS.append(clpath)
                continue

            for project in projects:
                rel = f"{campus}/{client}/{project}"
                abs_path = os.path.join(clpath, project)

                # At project level: include all discovered projects, even symlinked ones,
                # so each unique service_folder path appears. No need to track visited_realpaths
                # here because we're not recursing into projects -- except in the zero-runs
                # intermediate case just below, which is the one place this level does recurse.

                run_count = count_runs(abs_path)
                if run_count is None:
                    _UNREADABLE_PATHS.append(abs_path)
                    out.append({"service_folder": rel, "service_folder_win": win_path(rel),
                                "campus": campus, "abs_path": abs_path, "run_count": None})
                    continue

                if run_count == 0:
                    # Zero DIRECT runs: could be a genuinely empty project, or an institution/lab
                    # intermediate one level short of the real projects. Only descend if it
                    # actually has subdirectories -- a project full of .d dirs never reaches this
                    # branch at all, since each .d gives it run_count > 0 above.
                    try:
                        subentries = sorted(e.name for e in os.scandir(abs_path)
                                            if e.is_dir(follow_symlinks=True))
                    except OSError:
                        subentries = None

                    if subentries is None:
                        # Became unreadable between count_runs() succeeding and this scan.
                        _UNREADABLE_PATHS.append(abs_path)
                        out.append({"service_folder": rel, "service_folder_win": win_path(rel),
                                    "campus": campus, "abs_path": abs_path, "run_count": None})
                        continue

                    if subentries:
                        abs_path_real = os.path.realpath(abs_path)
                        if abs_path_real in visited_realpaths:
                            continue
                        visited_realpaths.add(abs_path_real)

                        # Depth 4, stop here -- do not recurse further regardless of what these
                        # children look like.
                        for sub in subentries:
                            rel4 = f"{rel}/{sub}"
                            abs4 = os.path.join(abs_path, sub)
                            run_count4 = count_runs(abs4)
                            if run_count4 is None:
                                _UNREADABLE_PATHS.append(abs4)
                            out.append({"service_folder": rel4,
                                        "service_folder_win": win_path(rel4),
                                        "campus": campus, "abs_path": abs4,
                                        "run_count": run_count4})
                        continue  # the depth-3 intermediate itself is not a row

                    # Zero runs, no subdirectories: a genuinely empty project. Falls through to
                    # be recorded below, same as before this function became depth-adaptive.

                out.append({"service_folder": rel, "service_folder_win": win_path(rel),
                            "campus": campus, "abs_path": abs_path,
                            "run_count": run_count})

    return out


def service_relpath_from_path(path: str) -> str | None:
    """'…/lab/service/<a>/<b>/<c>/…' -> the FULL remainder 'a/b/c/…', else None.

    Accepts both \\ and / separators and both R:\\Data\\lab\\service\\ and
    /nfs/…/lab/service/ prefixes; matches the lab/service marker case-insensitively.
    Unlike service_folder_from_path, does NOT truncate to three components -- this is what lets
    mark_in_fran compare against a row's service_folder whatever depth it actually landed at
    (3 for a normal project, 4 for one recovered from an intermediate).
    Returns None when the path is not under lab/service, or has no components after it.
    Does not strip or alter case in the returned components.
    """
    # Normalize separators to / and find the marker position
    norm_path = path.replace("\\", "/").lower()
    marker = "lab/service/"
    idx = norm_path.find(marker)
    if idx < 0:
        return None
    # Start after the marker; get the original case from the input
    remainder = path.replace("\\", "/")[idx + len(marker):]
    parts = [p for p in remainder.split("/") if p]
    if not parts:
        return None
    return "/".join(parts)


def service_folder_from_path(path: str) -> str | None:
    """'…/lab/service/<campus>/<client>/<project>/…' -> 'campus/client/project', else None.

    Takes EXACTLY the first three components after the marker.
    Returns None when the path is not under lab/service, or has fewer than three components.
    """
    full = service_relpath_from_path(path)
    if full is None:
        return None
    parts = full.split("/")
    if len(parts) < 3:
        return None
    return "/".join(parts[:3])


def ingested_folders(con) -> set[str]:
    """FULL share-relative paths (not truncated to 3 components) of ingested search output.

    Queries delimp_search_provenance.output_dir (where not null) and extracts the full
    lab/service/… remainder of each. Full paths, not a fixed-depth key, because mark_in_fran
    must work whether a row landed at depth 3 (a normal project) or depth 4 (one recovered from
    a zero-run intermediate) -- only the untruncated remainder can tell whether an ingested
    output sits at or below a row at whatever depth that row is.

    CONSEQUENCE: searches whose output_dir is not a service path (roughly 578 of 2,044)
    do not contribute, so some folders will read "not ingested" when they actually are.
    That is the safe direction — it costs someone an afternoon checking, whereas marking
    at client level hides genuinely un-ingested work, the failure this table exists to prevent.
    """
    cur = con.cursor()
    cur.execute("""SELECT output_dir FROM delimp_search_provenance
                    WHERE output_dir IS NOT NULL""")
    ingested = set()
    for (output_dir,) in cur.fetchall():
        full = service_relpath_from_path(output_dir)
        if full is not None:
            ingested.add(full)
    return ingested


def mark_in_fran(rows: list[dict], ingested: set[str]) -> None:
    """Set row['in_fran'] when an ingested path equals the row's service_folder, or sits below
    it (starts with service_folder + "/").

    `ingested` holds FULL, untruncated paths (see ingested_folders), so this works whether a row
    is a depth-3 project or a depth-4 one recovered from an intermediate: a shallower ingested
    key never matches a deeper row (it can't start with a longer string), so a client-level key
    still marks nothing, exactly as it always has. And because intermediate directories are
    never rows (walk_projects descends past them), a deep ingested path under one sibling project
    can only ever match that project's own row, never a sibling's -- there is no shared
    depth-3 "intermediate" row left for a shallow-ish key to over-claim across.
    """
    for r in rows:
        sf = r["service_folder"]
        r["in_fran"] = any(p == sf or p.startswith(sf + "/") for p in ingested)


# delimp_service_dir_inventory is one row per FOLDER, and the scanner owns every column in it —
# there is no attribution column here to protect. Human-reviewed submission matching (the
# 2026-06-24 ai-disk-match rows, and match_submissions() after it) lives in the separate
# delimp_submission_service_dir, which this scanner never writes. Two tables, not one, because a
# single folder can hold several submissions' worth of runs (see the migration's comment) and a
# scan of the folder must never be able to clobber that judgement.
UPSERT_SQL = """
INSERT INTO delimp_service_dir_inventory
  (service_folder, service_folder_win, campus, run_count, in_fran, scanned_at)
VALUES (%s, %s, %s, %s, %s, now())
ON CONFLICT (service_folder) DO UPDATE SET
  service_folder_win = EXCLUDED.service_folder_win,
  campus             = EXCLUDED.campus,
  run_count          = EXCLUDED.run_count,
  in_fran            = EXCLUDED.in_fran,
  scanned_at         = now()
"""


def upsert(con, rows: list[dict]) -> int:
    with con.cursor() as cur:
        for r in rows:
            try:
                # Bracket access (not .get()) ensures loud failure if mark_in_fran() was skipped,
                # so a wiring bug surfaces as a KeyError rather than silent all-False data.
                cur.execute(UPSERT_SQL, (r["service_folder"], r["service_folder_win"], r["campus"],
                                         r["run_count"], bool(r["in_fran"])))
            except Exception as e:
                raise RuntimeError(f"upsert failed on {r['service_folder']!r}") from e
    con.commit()
    return len(rows)


def _conn():
    """PG Farm, via the same file-based credential every ingest script uses."""
    import json, urllib.request, psycopg2
    pw = os.environ.get("DELIMP_PG_PASSWORD")
    if not pw:
        tf = os.path.expanduser(os.environ.get("DELIMP_PG_TOKEN_FILE", "~/.pgfarm_token"))
        if not os.path.exists(tf):
            raise SystemExit(f"No PG Farm credential: set DELIMP_PG_PASSWORD or place one at {tf}")
        pw = open(tf).read().strip()
    if not (pw.startswith("eyJ") and pw.count(".") == 2):
        body = json.dumps({"username": "genome-proteomics-service-account", "secret": pw}).encode()
        req = urllib.request.Request(
            "https://pgfarm.library.ucdavis.edu/auth/service-account/login",
            data=body, headers={"Content-Type": "application/json"})
        pw = json.loads(urllib.request.urlopen(req, timeout=30).read())["access_token"]
    return psycopg2.connect(host="pgfarm.library.ucdavis.edu", port=5432,
                            dbname="uc-davis-genome-center-proteomics-core/delimp",
                            user="genome-proteomics-service-account", password=pw,
                            sslmode="require", connect_timeout=30)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--root", default=SERVICE_ROOT)
    ap.add_argument("--limit", type=int, default=0, help="stop after N folders (testing)")
    a = ap.parse_args(argv)

    print(f"walking {a.root} …", flush=True)
    rows = walk_projects(a.root)
    if a.limit:
        rows = rows[:a.limit]
    con = _conn()
    mark_in_fran(rows, ingested_folders(con))
    # count_runs() returns None for a directory that could not be read — deliberately distinct
    # from 0 = genuinely empty. Fold None to 0 only for this total; never for the per-row display
    # below, where collapsing "unreadable" into "empty" is exactly the failure this table exists
    # to avoid.
    n_runs = sum(r["run_count"] or 0 for r in rows)
    n_fran = sum(1 for r in rows if r["in_fran"])
    unreadable = get_unreadable_paths()
    print(f"  {len(rows)} project folders, {n_runs} runs, {n_fran} already in FRAN, "
          f"{len(rows) - n_fran} not, {len(unreadable)} unreadable", flush=True)
    if unreadable:
        print(f"  {len(unreadable)} unreadable path(s) (could not be scanned, NOT counted as 0):")
        for u in unreadable[:10]:
            print(f"    {u}")
        if len(unreadable) > 10:
            print(f"    … and {len(unreadable) - 10} more")
    if not a.apply:
        for r in rows[:15]:
            runs_str = f"{r['run_count']:>4} runs" if r["run_count"] is not None else "  ?? runs"
            print(f"    {'IN FRAN ' if r['in_fran'] else 'on share'} {runs_str}  "
                  f"{r['service_folder']}")
        print("dry run — nothing written. Re-run with --apply.")
        con.close()
        return 0
    n = upsert(con, rows)
    cur = con.cursor()
    cur.execute("SELECT count(*), count(*) FILTER (WHERE scanned_at IS NOT NULL) "
                "FROM delimp_service_dir_inventory")
    tot, scanned = cur.fetchone()
    print(f"upserted {n}; table now {tot} rows, {scanned} carrying a scanned_at", flush=True)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
