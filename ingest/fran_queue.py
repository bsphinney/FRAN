"""fran_queue.py — explicit ingest queue for searches produced on Hive.

WHY THIS EXISTS. Searches on Hive are found by find_uningested.py, which infers "this is a search"
from marker files. That inference has two measured failure modes (2026-09-08):

  1. detect_engine()'s catch-all `_Report.*\\.(tsv|parquet)$` test classifies ANY directory holding
     such a file as spectronaut, and scan() then prunes descent (`dirnames[:] = []`). One stray
     file, /quobyte/proteomics-grp/brett/20250910_120054_KG-human-2_Report.tsv, therefore made the
     scanner call that whole root a single search and skip all 246 entries under it -- including
     PROT_0793, which holds two finished DIA-NN searches that have never been ingested.
  2. auto_ingest sorts candidates alphabetically and takes chosen[:limit]. Export-timestamp names
     ("20250717_...") always sort ahead of alphabetic ones, so the two DIA-NN searches correctly
     dropped into incoming/ on 2026-08-26 were still uningested 13 days later.

A producer that KNOWS it just finished a search should say so, rather than leave a scanner to
guess. That is this table.

NOT a replacement for the scan: the Windows/Spectronaut FRAN_reports tree (~1,700 searches) has no
producer that registers, so find_uningested stays authoritative there. This queue is authoritative
for Hive-produced work only.

WHY THERE IS A LEASE. delimp_spectrum_regen_queue is the cautionary precedent: 1,871 rows sharing
one requested_at, no heartbeat, no lease, completion marked by a human running a script. It decayed
into a stale worklist. Here a claimed row whose claim is older than STALE_CLAIM_H returns to
'queued' automatically, and a row that fails MAX_ATTEMPTS times is parked with its error rather
than consuming the per-run budget forever.

CLI:
  python fran_queue.py initdb
  python fran_queue.py add <searchdir> --engine diann --registered-by mac-clip-fran
                          [--output-dir D] [--name N] [--organism-name O] [--taxon T]
                          [--priority P] [--host hive] [--force]
  python fran_queue.py list [--status queued|claimed|done|parked] [--limit N]
  python fran_queue.py retry <id>       # parked -> queued, attempts reset
  python fran_queue.py park  <id>       # take a row out of circulation by hand

Token: $DELIMP_PG_TOKEN_FILE or ~/.pgfarm_token (the long-lived service-account SECRET is
auto-exchanged for a JWT), or $DELIMP_PG_PASSWORD holding a JWT directly.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

STALE_CLAIM_H = 6     # a claim older than this is abandoned and the row re-queued
MAX_ATTEMPTS = 3      # after this many failures a row is parked for a human
ENGINES = ("diann", "spectronaut", "fragpipe", "radiant")

DDL = """
CREATE TABLE IF NOT EXISTS delimp_ingest_queue (
  id            bigserial PRIMARY KEY,
  output_dir    text NOT NULL UNIQUE,
  searchdir     text NOT NULL,
  engine        text NOT NULL CHECK (engine IN ('diann','spectronaut','fragpipe','radiant')),
  search_name   text,
  organism_name text,
  taxon         int,
  host          text NOT NULL,
  registered_by text NOT NULL,
  registered_at timestamptz NOT NULL DEFAULT now(),
  priority      int  NOT NULL DEFAULT 0,
  status        text NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued','claimed','done','parked')),
  claimed_by    text,
  claimed_at    timestamptz,
  attempts      int  NOT NULL DEFAULT 0,
  last_error    text,
  last_attempt_at timestamptz,
  search_id     uuid,
  -- Observed-chromatogram lane, opt-in per row. NULL xic_dir means "precursors only", which is the
  -- default and what the unattended cron has always done: lane writes are GB-scale (PROT_0793 alone
  -- is 15 GB of *.xic.parquet) and that is a storage decision, not something a scanner should make.
  -- A producer that just wrote the traces is the one who knows they exist, so it declares them here.
  xic_dir       text,
  lance_dir     text,
  xic_status    text,
  xic_error     text
)"""
INDEX_DDL = """
CREATE INDEX IF NOT EXISTS delimp_ingest_queue_ready_idx
  ON delimp_ingest_queue (status, priority DESC, registered_at)"""


def _token() -> str:
    pw = os.environ.get("DELIMP_PG_PASSWORD")
    if not pw:
        tf = os.path.expanduser(os.environ.get("DELIMP_PG_TOKEN_FILE", "~/.pgfarm_token"))
        if not os.path.exists(tf):
            sys.exit(f"No PG Farm credential: set DELIMP_PG_PASSWORD or place a token at {tf}")
        pw = open(tf).read().strip()
    # A JWT is used as-is; anything else is the long-lived secret and is exchanged for one. The
    # secret self-refreshes forever, so a token FILE's mtime says nothing about its validity.
    if pw.startswith("eyJ") and pw.count(".") == 2:
        return pw
    body = json.dumps({
        "username": os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        "secret": pw,
    }).encode()
    req = urllib.request.Request(
        "https://pgfarm.library.ucdavis.edu/auth/service-account/login",
        data=body, headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())["access_token"]


def _conn():
    import psycopg2
    return psycopg2.connect(
        host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
        dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30)


# --- validation -------------------------------------------------------------

def _detected_engine(searchdir: str) -> str | None:
    """What find_uningested would call this directory, or None.

    Reuses the scanner's OWN function so a producer and the consumer can never disagree about what
    an engine's output looks like. Spectronaut is passed the report FILE (corpus_ingest requires
    that), so probe its parent directory instead.
    """
    try:
        from find_uningested import detect_engine
    except ImportError:
        return None  # not deployed alongside; skip the check rather than refuse to register
    probe = searchdir if os.path.isdir(searchdir) else os.path.dirname(searchdir)
    try:
        return detect_engine(probe)
    except OSError:
        return None


def _already_ingested(cur, output_dir: str):
    """The search_id if this output_dir is already a corpus row, else None.

    output_dir is the corpus identity (search_id = uuid5(ns, output_dir)), so this is the same key
    the queue's UNIQUE constraint uses -- a search cannot be queued and ingested under two names.
    """
    # delimp_searches' primary key column is `id` (uuid), not `search_id`.
    cur.execute("SELECT id FROM delimp_searches WHERE output_dir = %s", (output_dir,))
    row = cur.fetchone()
    return row[0] if row else None


# --- commands ---------------------------------------------------------------

def cmd_initdb(a) -> int:
    con = _conn(); cur = con.cursor()
    cur.execute(DDL)
    cur.execute(INDEX_DDL)
    con.commit()
    print("delimp_ingest_queue ready")
    con.close()
    return 0


def cmd_add(a) -> int:
    searchdir = os.path.abspath(a.searchdir) if os.path.exists(a.searchdir) else a.searchdir
    output_dir = a.output_dir or searchdir

    # Validate HERE, in front of the person registering, rather than failing inside an unattended
    # ingest four hours later.
    if not a.force:
        if not os.path.exists(searchdir):
            sys.exit(f"searchdir does not exist here: {searchdir}\n"
                     f"  (register from a host that can see it, or pass --force)")
        found = _detected_engine(searchdir)
        if found and found != a.engine:
            sys.exit(f"engine mismatch: you said --engine {a.engine}, "
                     f"but detect_engine() sees '{found}' at {searchdir}\n"
                     f"  (pass --force to register anyway)")

    con = _conn(); cur = con.cursor()
    existing = _already_ingested(cur, output_dir)
    if existing and not a.force:
        print(f"already in the corpus as search_id={existing}; not queued")
        con.close()
        return 0
    try:
        cur.execute("""
            INSERT INTO delimp_ingest_queue
              (output_dir, searchdir, engine, search_name, organism_name, taxon,
               host, registered_by, priority, xic_dir, lance_dir)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (output_dir) DO NOTHING
            RETURNING id""",
            (output_dir, searchdir, a.engine, a.name, a.organism_name, a.taxon,
             a.host, a.registered_by, a.priority, a.xic_dir, a.lance_dir))
        row = cur.fetchone()
        con.commit()
    finally:
        pass
    if row:
        print(f"queued id={row[0]}  {a.engine}  {searchdir}")
    else:
        # ON CONFLICT DO NOTHING: registering twice is idempotent, not an error.
        cur.execute("SELECT id, status FROM delimp_ingest_queue WHERE output_dir=%s", (output_dir,))
        i, st = cur.fetchone()
        print(f"already queued id={i} (status={st}); nothing to do")
    con.close()
    return 0


def cmd_list(a) -> int:
    con = _conn(); cur = con.cursor()
    where, params = "", []
    if a.status:
        where, params = "WHERE status = %s", [a.status]
    cur.execute(f"""
        SELECT id, status, engine, registered_by, registered_at, attempts,
               COALESCE(search_name, searchdir), last_error,
               CASE WHEN xic_dir IS NULL THEN '' ELSE COALESCE(xic_status,'xic:pending') END
          FROM delimp_ingest_queue {where}
         ORDER BY status, priority DESC, registered_at
         LIMIT %s""", params + [a.limit])
    rows = cur.fetchall()
    if not rows:
        print("queue is empty" + (f" for status={a.status}" if a.status else ""))
    for i, st, eng, by, at, att, what, err, xic in rows:
        line = f"{i:>5}  {st:<8}{eng:<12}{str(at)[:16]}  by={by:<16}"
        if att:
            line += f" attempts={att}"
        print(line + (f" [{xic}]" if xic else "") + f"  {str(what)[:70]}")
        if err:
            print(f"        last_error: {err[:150]}")
    con.close()
    return 0


def _set_status(row_id: int, status: str, reset_attempts: bool) -> int:
    con = _conn(); cur = con.cursor()
    cur.execute(f"""UPDATE delimp_ingest_queue
                       SET status=%s, claimed_by=NULL, claimed_at=NULL
                           {', attempts=0, last_error=NULL' if reset_attempts else ''}
                     WHERE id=%s RETURNING id, status""", (status, row_id))
    row = cur.fetchone()
    con.commit(); con.close()
    if not row:
        sys.exit(f"no queue row id={row_id}")
    print(f"id={row[0]} -> {row[1]}")
    return 0


def cmd_retry(a) -> int:
    return _set_status(a.id, "queued", reset_attempts=True)


def cmd_park(a) -> int:
    return _set_status(a.id, "parked", reset_attempts=False)


# --- consumer API (used by auto_ingest.py) ----------------------------------

def claim_batch(con, limit: int, claimed_by: str) -> list[dict]:
    """Atomically claim up to `limit` ready rows and return them as dicts.

    Ready means 'queued', OR 'claimed' with a lease older than STALE_CLAIM_H -- an ingester that
    died mid-row releases its work automatically instead of wedging the queue.

    FOR UPDATE SKIP LOCKED makes two concurrent ingesters safe without a separate lock: each takes
    a disjoint set rather than blocking or double-claiming.
    """
    cur = con.cursor()
    cur.execute("""
        UPDATE delimp_ingest_queue q
           SET status='claimed', claimed_by=%s, claimed_at=now()
         WHERE q.id IN (
               SELECT id FROM delimp_ingest_queue
                WHERE status='queued'
                   OR (status='claimed' AND claimed_at < now() - make_interval(hours => %s))
                ORDER BY priority DESC, registered_at
                LIMIT %s
                FOR UPDATE SKIP LOCKED)
     RETURNING id, output_dir, searchdir, engine, search_name, organism_name, taxon, attempts,
               xic_dir, lance_dir""",
        (claimed_by, STALE_CLAIM_H, limit))
    cols = ["id", "output_dir", "searchdir", "engine", "search_name",
            "organism_name", "taxon", "attempts", "xic_dir", "lance_dir"]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    con.commit()
    return rows


def mark_done(con, row_id: int, search_id=None) -> None:
    cur = con.cursor()
    cur.execute("""UPDATE delimp_ingest_queue
                      SET status='done', search_id=%s, last_attempt_at=now(), last_error=NULL
                    WHERE id=%s""", (search_id, row_id))
    con.commit()


def mark_failed(con, row_id: int, error: str) -> str:
    """Record a failure; park the row once it has burned MAX_ATTEMPTS. Returns the new status."""
    cur = con.cursor()
    cur.execute("""
        UPDATE delimp_ingest_queue
           SET attempts = attempts + 1,
               last_error = %s,
               last_attempt_at = now(),
               claimed_by = NULL, claimed_at = NULL,
               status = CASE WHEN attempts + 1 >= %s THEN 'parked' ELSE 'queued' END
         WHERE id = %s
     RETURNING status""", (str(error)[:4000], MAX_ATTEMPTS, row_id))
    status = cur.fetchone()[0]
    con.commit()
    return status


def mark_xic(con, row_id: int, status: str, error: str | None = None) -> None:
    """Record the XIC lane's outcome SEPARATELY from the precursor ingest.

    The lane is a bonus, not a precondition: precursors are already committed by the time it runs,
    so a lane failure must never send the row back to 'queued' and re-ingest them.
    """
    cur = con.cursor()
    cur.execute("""UPDATE delimp_ingest_queue SET xic_status=%s, xic_error=%s WHERE id=%s""",
                (status, (str(error)[:4000] if error else None), row_id))
    con.commit()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("initdb").set_defaults(fn=cmd_initdb)

    p = sub.add_parser("add", help="register a finished search for ingest")
    p.add_argument("searchdir")
    p.add_argument("--engine", required=True, choices=ENGINES)
    p.add_argument("--registered-by", required=True, help="your node name, e.g. mac-clip-fran")
    p.add_argument("--output-dir", default=None,
                   help="stable identity key (default: searchdir). search_id = uuid5(ns, this)")
    p.add_argument("--name", default=None, help="human search name")
    p.add_argument("--organism-name", default=None)
    p.add_argument("--taxon", type=int, default=None)
    p.add_argument("--host", default="hive", help="where searchdir is readable (default: hive)")
    p.add_argument("--priority", type=int, default=0, help="higher goes first")
    p.add_argument("--xic-dir", default=None,
                   help="observed-chromatogram source dir; setting it OPTS IN to the XIC lane "
                        "(DIA-NN: walked recursively for *.xic.parquet, so per-run subdirs are fine)")
    p.add_argument("--lance-dir", default=None,
                   help="where the .xic.lance dataset is written (default: FRAN_XIC_LANCE_DIR)")
    p.add_argument("--force", action="store_true",
                   help="skip existence/engine/duplicate checks")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("list")
    p.add_argument("--status", choices=("queued", "claimed", "done", "parked"))
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("retry", help="parked -> queued, attempts reset")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_retry)

    p = sub.add_parser("park", help="take a row out of circulation")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_park)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
