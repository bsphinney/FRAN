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
    """Every campus/client/project folder on the share, with its run count.

    Depth is fixed at three because that IS the share's shape and the format already stored in
    service_folder. A project's own subdirectories are its data, not more projects.

    Walks into symlinked directories but tracks visited real paths at campus/client levels
    to avoid infinite loops. At project level, includes all discovered projects even if
    symlinked, so each unique service_folder path appears in the output.

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
                # here because we're not recursing into projects.

                run_count = count_runs(abs_path)
                if run_count is None:
                    _UNREADABLE_PATHS.append(abs_path)

                out.append({"service_folder": rel, "service_folder_win": win_path(rel),
                            "campus": campus, "abs_path": abs_path,
                            "run_count": run_count})

    return out


def service_folder_from_path(path: str) -> str | None:
    """'…/lab/service/<campus>/<client>/<project>/…' -> 'campus/client/project', else None.

    Accepts both \\ and / separators and both R:\\Data\\lab\\service\\ and
    /nfs/…/lab/service/ prefixes; matches the lab/service marker case-insensitively.
    Takes EXACTLY the first three components after the marker.
    Returns None when the path is not under lab/service, or has fewer than three components.
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
    # Split on /, filter empty components, and take first three
    parts = [p for p in remainder.split("/") if p]
    if len(parts) < 3:
        return None
    return "/".join(parts[:3])


def ingested_folders(con) -> set[str]:
    """Project-level service_folder values that already have at least one FRAN search.

    Queries delimp_search_provenance.output_dir (where not null), extracts the full
    service_folder path (campus/client/project), and returns the set of unique project paths.

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
        folder = service_folder_from_path(output_dir)
        if folder is not None:
            ingested.add(folder)
    return ingested


def mark_in_fran(rows: list[dict], ingested: set[str]) -> None:
    """Set row['in_fran'] for exact service_folder matches in the ingested set."""
    for r in rows:
        r["in_fran"] = r["service_folder"] in ingested


# The DO UPDATE list is deliberately short. submission_id / match_confidence / clue / matched_by /
# matched_at are NOT refreshed: the 2026-06-24 ai-disk-match rows encode human-reviewed judgement
# (clues like "submitter+date+organism") that this scanner cannot re-derive, and silently replacing
# them with a weaker guess would be a regression nobody would notice. The scanner owns the
# INVENTORY columns; matching owns the attribution columns, and only via match_submissions().
UPSERT_SQL = """
INSERT INTO delimp_submission_service_dir
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
