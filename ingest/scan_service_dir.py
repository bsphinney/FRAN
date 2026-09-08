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
