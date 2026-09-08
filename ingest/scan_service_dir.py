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


def win_path(rel: str) -> str:
    """'on_campus/A/B' -> the R: spelling stored in service_folder_win."""
    return WIN_ROOT + "\\" + rel.replace("/", "\\")


def count_runs(path: str) -> int:
    """Raw acquisitions directly under `path`: .d directories plus .raw files.

    Does NOT descend into a .d -- it is one acquisition stored as a directory of instrument files,
    so walking into it would count its internals as runs.
    """
    n = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                low = e.name.lower()
                if e.is_dir(follow_symlinks=False) and low.endswith(".d"):
                    n += 1
                elif e.is_file(follow_symlinks=False) and low.endswith(".raw"):
                    n += 1
    except OSError:
        return 0
    return n


def _skip(name: str) -> bool:
    return name in SKIP_AT_CAMPUS or name.startswith(SKIP_PREFIXES)


def walk_projects(root: str = SERVICE_ROOT) -> list[dict]:
    """Every campus/client/project folder on the share, with its run count.

    Depth is fixed at three because that IS the share's shape and the format already stored in
    service_folder. A project's own subdirectories are its data, not more projects.
    """
    out: list[dict] = []
    try:
        campuses = sorted(e.name for e in os.scandir(root)
                          if e.is_dir(follow_symlinks=False) and not _skip(e.name))
    except OSError:
        return out
    for campus in campuses:
        cpath = os.path.join(root, campus)
        try:
            clients = sorted(e.name for e in os.scandir(cpath) if e.is_dir(follow_symlinks=False))
        except OSError:
            continue
        for client in clients:
            clpath = os.path.join(cpath, client)
            try:
                projects = sorted(e.name for e in os.scandir(clpath)
                                  if e.is_dir(follow_symlinks=False))
            except OSError:
                continue
            for project in projects:
                rel = f"{campus}/{client}/{project}"
                abs_path = os.path.join(clpath, project)
                out.append({"service_folder": rel, "service_folder_win": win_path(rel),
                            "campus": campus, "abs_path": abs_path,
                            "run_count": count_runs(abs_path)})
    return out
