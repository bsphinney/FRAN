"""versions.py — one place that says which code produced which artefact.

Why this exists. Before it, three version constants lived in three files and none of them reached the
database:

  * `corpus_ingest.SCHEMA_VERSION` was written into every `ingested_schema_version` column — and is
    `"1.0.0"` for all 1,963 searches and all 19,874 raw_files. It records the *schema* version, which
    has never changed, so it discriminates nothing about the *code*.
  * `xic_extractor.VERSION` ("1.4.0") was never persisted anywhere at all.
  * `app.main.APP_VERSION` was served at `/version` and recorded against nothing.

That gap has already cost real work twice, and would have cost more:

  * The XIC extractor stamps MS2 events with the MS1 frame's time, putting fragment apexes ~0.263 s
    early. `CHANGELOG_xic_extractor.md` warns "any lane built before this fix carries the shift" —
    but with no recorded extractor version, "which datasets are affected?" is unanswerable except by
    rebuilding all of them.
  * `spectrum_lance.content_md5` silently mis-verified every dataset large enough to read multi-chunk
    until 2026-07-27. Datasets written before and after that fix are indistinguishable on disk.

`STORAGE_DESIGN.md` §8.4 asks for exactly this, for the same reason: "version the aggregates
alongside the corpus revision they were built from, so a stale prior is detectable rather than
silently wrong." A stale artefact you can *identify* is a scheduling problem; one you cannot is a
correctness problem.

Two mechanisms, deliberately separate:

  1. `delimp_component_version` — append-only log of every (component, version, git_sha) that has run
     against this corpus, and when. Answers "what has touched this database?"
  2. `writer_version` / `extractor_version` columns on the lane registries. Answer "which code wrote
     *this* dataset?" — the question you need when a defect is found after the fact.

Bump the constant in the same commit that changes the component's behaviour. A version that lags the
code is worse than no version, because it is trusted.
"""
import os
import subprocess

# --- schema (migration) version -------------------------------------------------------------
# Tracks `delimp_schema_version`, i.e. the shape of the tables. NOT a code version — do not bump it
# for behaviour changes; that is what the component versions below are for.
SCHEMA_VERSION = "1.0.0"

# --- component (code) versions ---------------------------------------------------------------
# Semver. Bump patch for fixes that cannot change output, minor for output-affecting changes, major
# for a format change that makes old artefacts unreadable.
CORPUS_INGEST_VERSION = "1.1.0"          # 1.1.0: raw_metadata restored; instrument block no longer silently NULL
SPECTRUM_LANE_WRITER_VERSION = "1.2.0"   # 1.2.0: frg_excluded + frg_chan_interference appended   # 1.1.0: content_md5 combine_chunks fix (2026-07-27)
XIC_LANE_WRITER_VERSION = "1.0.0"        # Spectronaut All-XIC -> Lance
XIC_TRACE_LANE_WRITER_VERSION = "0.2.0"  # pilot; still the [9,32] layout, pre-corpus-scale
RAW_METADATA_VERSION = "1.0.0"           # Bruker analysis.tdf + Thermo TRFP readers


def fran_app_version():
    """The web app's version, read out of `app/main.py` rather than copied here.

    The app cannot import this module — the Dockerfile ships only `app/` — so `APP_VERSION` has to
    live there. Restating it here would create exactly the two-sources-of-truth drift this module
    exists to remove, so parse it instead. None if the app tree isn't present (e.g. on Hive, which
    runs a loose copy of `ingest/` alone)."""
    import re
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "main.py")
    try:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r'\s*APP_VERSION\s*=\s*["\']([^"\']+)["\']', line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return None


def xic_extractor_version():
    """The extractor's own version, imported lazily so this module stays dependency-free.

    Distinct from XIC_TRACE_LANE_WRITER_VERSION: the writer arranges rows, the extractor produces the
    numbers in them. The ~0.263 s MS2 timestamp shift is an *extractor* defect, so it is the
    extractor version that says whether a given trace is affected.
    """
    try:
        from xic_extractor import VERSION
        return VERSION
    except Exception:
        return None


def git_sha(short=True):
    """Revision of the checkout this code is running from, as `<repo>@<sha>`, or None.

    The repo name is not decoration. Hive runs `ingest/` as a loose copy inside the unrelated
    `glendon` repo, so a bare `rev-parse` there returns glendon's HEAD — a real sha, from the wrong
    project, indistinguishable from a FRAN one once it is sitting in a database column. Qualifying it
    (`glendon@7961e27` vs `FRAN@0d55345`) makes a misattributed revision impossible to read as a
    FRAN revision."""
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        sha = subprocess.run(["git", "rev-parse", "--short" if short else "HEAD", "HEAD"],
                             cwd=here, capture_output=True, text=True, timeout=10).stdout.strip()
        if not sha:
            return None
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             cwd=here, capture_output=True, text=True, timeout=10).stdout.strip()
        return f"{os.path.basename(top)}@{sha}" if top else sha
    except Exception:
        return None


DDL = """
CREATE TABLE IF NOT EXISTS delimp_component_version (
    id           BIGSERIAL PRIMARY KEY,
    component    TEXT NOT NULL,          -- e.g. corpus_ingest, xic_extractor, spectrum_lane_writer
    version      TEXT NOT NULL,
    git_sha      TEXT,                   -- NULL when run from the loose Hive copy (not a git repo)
    host         TEXT,
    notes        TEXT,
    recorded_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS delimp_component_version_component_idx
    ON delimp_component_version (component, recorded_at DESC);
"""


def ensure_table(cur):
    for stmt in filter(str.strip, DDL.split(";")):
        cur.execute(stmt)


def record(cur, component, version, notes=None):
    """Append one component-version observation. Cheap; call once per run, not per row."""
    ensure_table(cur)
    cur.execute(
        """INSERT INTO delimp_component_version (component, version, git_sha, host, notes)
           VALUES (%s,%s,%s,%s,%s)""",
        (component, version, git_sha(), os.uname().nodename[:64], notes))


def record_run(cur, component, version, notes=None):
    """record() that never raises — version tracking must not be able to fail an ingest."""
    try:
        record(cur, component, version, notes)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  version: could not record {component} {version}: {str(e)[:80]}", flush=True)
        return False


def stamp():
    """Every component version at once — for logs and for the app's /version endpoint."""
    return {
        "schema": SCHEMA_VERSION,
        "corpus_ingest": CORPUS_INGEST_VERSION,
        "spectrum_lane_writer": SPECTRUM_LANE_WRITER_VERSION,
        "xic_lane_writer": XIC_LANE_WRITER_VERSION,
        "xic_trace_lane_writer": XIC_TRACE_LANE_WRITER_VERSION,
        "xic_extractor": xic_extractor_version(),
        "raw_metadata": RAW_METADATA_VERSION,
        "fran_app": fran_app_version(),
        "git_sha": git_sha(),
    }


if __name__ == "__main__":
    for k, v in stamp().items():
        print(f"{k:24s} {v}")
