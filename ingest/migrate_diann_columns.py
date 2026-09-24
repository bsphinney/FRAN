"""Apply migrations/2026-09-23_diann_report_columns.sql -- the DIA-NN report columns FRAN discarded.

    python3 ingest/migrate_diann_columns.py            # dry run: what is missing, and what it costs
    python3 ingest/migrate_diann_columns.py --apply    # add the columns
    python3 ingest/migrate_diann_columns.py --verify   # confirm the schema matches the .sql, nothing else

`--sql PATH` runs a different additive migration instead; anything under migrations/ that is plain
`ALTER TABLE t ADD COLUMN IF NOT EXISTS c type` statements works, and gets the same refusals and the
same cost report. That is how 2026-09-23_xic_lane_source_output_dir.sql is applied -- deliberately
NOT folded into the DIA-NN file, so one column that is blocking a lane load today does not have to
wait on sign-off for thirty-five that are still under review.

Idempotent: every statement is ADD COLUMN IF NOT EXISTS, so re-running is a no-op. The .sql file is
the source of truth and carries the reasoning for every column and every exclusion; this script only
executes it and reports.

WHY A NULLABLE COLUMN WITH NO DEFAULT IS THE WHOLE DESIGN. delimp_precursors is 261 GB / 532M rows.
On PostgreSQL 16, ADD COLUMN ... NULL with no DEFAULT is a catalog-only change -- existing rows are
not touched, so the 490M Spectronaut rows that will never hold a DIA-NN value cost nothing at all.
Adding a DEFAULT (even `DEFAULT NULL::real` written explicitly, which is fine, but any non-null
default) would rewrite all 261 GB and take an AccessExclusiveLock for the duration. This is the
entire reason the table is being widened instead of given a sidecar. If you edit the .sql, keep it
that way -- the guard below refuses to run a statement with a DEFAULT clause rather than trusting
the author to remember.

The lock this DOES take is brief but real: each ALTER needs a momentary AccessExclusiveLock on the
table, which queues behind any in-flight query and blocks new ones until it is granted. A long
analytic SELECT can therefore stall an ALTER, and the ALTER then stalls everything behind it. That
is what happened on 2026-06-15 when delimp_precursors was altered. lock_timeout below keeps a
blocked ALTER from becoming a pile-up: it gives up instead, and you re-run when the table is quiet.
"""
from __future__ import annotations
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from coreomics_import import _conn as _base_conn                                  # noqa: E402

SQL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "migrations", "2026-09-23_diann_report_columns.sql")

# Bytes on disk per added column, per row that has a value. Used only for the cost report.
# `text` is variable-width, so its entry is a nominal figure for the estimate and nothing more --
# the real cost is whatever the strings are. Measure before adding a text column to a big table.
_WIDTH = {"real": 4, "double precision": 8, "boolean": 1, "integer": 4, "smallint": 2,
          "bigint": 8, "text": 16}
_VARIABLE = {"text"}


def statements(sql: str) -> list[str]:
    """Split on ';' after stripping -- comments. The file is plain ALTERs; no dollar-quoting."""
    stripped = "\n".join(re.sub(r"--.*$", "", ln) for ln in sql.splitlines())
    return [s.strip() for s in stripped.split(";") if s.strip()]


def parse(stmt: str) -> tuple[str, str, str]:
    """-> (table, column, type). Raises on anything that is not the exact shape we allow."""
    m = re.match(r"^ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+) ([\w ]+?)$", stmt.strip(),
                 re.I | re.S)
    if not m:
        raise ValueError(f"refusing a statement that is not `ALTER TABLE t ADD COLUMN IF NOT "
                         f"EXISTS c type`: {stmt.strip()[:120]!r}")
    table, col, typ = m.group(1), m.group(2), " ".join(m.group(3).split()).lower()
    if "default" in stmt.lower():
        raise ValueError(f"refusing `{table}.{col}`: a DEFAULT rewrites the whole table "
                         f"(261 GB for delimp_precursors). Add the column NULL and backfill.")
    if typ not in _WIDTH:
        raise ValueError(f"unexpected type {typ!r} for {table}.{col}; add it to _WIDTH if intended")
    return table, col, typ


def existing(cur, table: str) -> set[str]:
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_schema='public' AND table_name=%s""", (table,))
    return {r[0] for r in cur.fetchall()}


def rows_that_will_hold_a_value(cur, table: str) -> tuple[int, str]:
    """Rows of `table` that a DIA-NN ingest will actually write -> (count, what was counted).

    Falls back to a plain count for a table with no search_id: not every additive migration is
    about DIA-NN, and a cost report that crashes on an unrelated table is a cost report nobody
    runs."""
    cur.execute("""SELECT 1 FROM information_schema.columns
                    WHERE table_schema='public' AND table_name=%s AND column_name='search_id'""",
                (table,))
    if not cur.fetchone():
        cur.execute(f"SELECT count(*) FROM {table}")
        return cur.fetchone()[0], "all rows"
    cur.execute(f"""SELECT count(*) FROM {table} t JOIN delimp_searches s ON s.id = t.search_id
                     WHERE s.search_engine = 'diann'""")
    return cur.fetchone()[0], "DIA-NN rows"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="execute the ALTERs")
    ap.add_argument("--verify", action="store_true",
                    help="only check that every column in the .sql exists; exit 1 if not")
    ap.add_argument("--sql", default=SQL_PATH,
                    help="the migration to run (default: the DIA-NN report columns). Any file of "
                         "plain `ALTER TABLE t ADD COLUMN IF NOT EXISTS c type` statements works.")
    ap.add_argument("--lock-timeout-ms", type=int, default=3000,
                    help="give up rather than queue behind a long query (default 3s). Bounds how "
                         "long other queries can pile up behind a blocked ALTER — the 2026-06-15 "
                         "stall. Matches the 3s the in-ingest ALTER path already uses.")
    ap.add_argument("--abort-over-ms", type=float, default=2000.0,
                    help="roll this table back if any single ADD COLUMN exceeds this (default 2s). "
                         "A catalog-only add is sub-millisecond; seconds means it is rewriting.")
    ap.add_argument("--no-cost", action="store_true",
                    help="skip the cost report. Its row counts are two full scans of a 42M-row "
                         "join and take minutes on a busy table -- minutes during which a quiet "
                         "window can open and close again. Implied by --wait-minutes.")
    ap.add_argument("--wait-minutes", type=float, default=0.0,
                    help="keep retrying for this long while the table is busy. An ingest holds a "
                         "RowExclusiveLock on delimp_precursors for the whole of its bulk COPY "
                         "(observed: 18+ minutes), and ADD COLUMN needs AccessExclusive, so a "
                         "one-shot attempt during an ingest just loses. Retries wait for a gap "
                         "between searches instead of queueing inside one.")
    a = ap.parse_args()

    stmts = statements(open(a.sql).read())
    plan = [parse(s) for s in stmts]
    print(f"{a.sql}: {len(plan)} column(s)\n")

    con = _base_conn()
    # SET THESE WITH AUTOCOMMIT ON, THEN TURN IT OFF. A plain SET inside a transaction is itself
    # transactional: roll that transaction back and lock_timeout silently reverts to the server
    # default, which is 0 -- wait forever. The wait loop below rolls back on every poll, so a SET
    # issued inside a transaction would disarm the one guard standing between a blocked ALTER and
    # a database-wide pile-up, and would do it invisibly.
    con.autocommit = True
    cur = con.cursor()
    cur.execute("SET statement_timeout = 120000")
    cur.execute(f"SET lock_timeout = {int(a.lock_timeout_ms)}")
    con.autocommit = False
    cur.execute("SHOW lock_timeout")
    _lt = cur.fetchone()[0]
    if _lt in ("0", "0ms"):
        print(f"REFUSING: lock_timeout is {_lt!r} (no timeout). An ALTER would queue behind any "
              f"in-flight query and block every other query on the table behind it.")
        con.close()
        return 1
    print(f"lock_timeout={_lt}, abort-over={a.abort_over_ms:.0f} ms, "
          f"wait={a.wait_minutes:.0f} min\n")

    tables = sorted({t for t, _, _ in plan})
    have = {t: existing(cur, t) for t in tables}
    missing = [(t, c, ty) for t, c, ty in plan if c not in have[t]]

    for t in tables:
        mine = [(c, ty) for tt, c, ty in plan if tt == t]
        gone = [(c, ty) for c, ty in mine if c not in have[t]]
        print(f"  {t}: {len(mine)} in migration, {len(gone)} missing")
        for c, ty in mine:
            print(f"      {'ADD ' if c not in have[t] else 'ok  '} {c:30s} {ty}")

    if a.verify:
        con.rollback(); con.close()
        if missing:
            print(f"\nVERIFY FAILED: {len(missing)} column(s) absent: "
                  + ", ".join(f"{t}.{c}" for t, c, _ in missing))
            return 1
        print("\nVERIFY OK: every column in the migration exists.")
        return 0

    if not missing:
        print("\nNothing to do — all columns already present.")
        con.rollback(); con.close()
        return 0

    if a.no_cost or a.wait_minutes:
        # Those row counts are two full scans of a 42M-row join. On a table with a bulk COPY in
        # flight they take minutes -- and a quiet window can open and close inside those minutes,
        # which is the one thing a waiting apply cannot afford to miss.
        print("\n(cost report skipped — see --no-cost)")
    else:
        total = 0
        print("\ncost of the columns themselves (bytes x rows that will hold a value):")
        for t in tables:
            gone = [(c, ty) for tt, c, ty in missing if tt == t]
            if not gone:
                continue
            n, what = rows_that_will_hold_a_value(cur, t)
            width = sum(_WIDTH[ty] for _, ty in gone)
            b = width * n
            total += b
            approx = "~" if any(ty in _VARIABLE for _, ty in gone) else " "
            print(f"  {t:22s}{approx}{width:3d} B/row x {n:>12,} {what:11s} "
                  f"= {b / 2**30:6.2f} GiB")
        print(f"  {'TOTAL':22s} {'':3s}   {'':12s}             {total / 2**30:6.2f} GiB")
        if any(ty in _VARIABLE for _, _, ty in missing):
            print("  ~ = includes a variable-width column; its figure is nominal, not measured.")
        if any(t == "delimp_precursors" for t, _, _ in missing):
            # Only true of the corpus tables a re-ingest rewrites. Printing it for, say, a 60-row
            # lane registry would describe a backfill that is not going to happen.
            print("  NOTE: that is the marginal width only. The backfill is a delete-then-insert")
            print("  re-ingest, which rewrites every DIA-NN row in full; see the backfill plan.")

    if not a.apply:
        print("\nDRY RUN — re-run with --apply to add these columns.")
        con.rollback(); con.close()
        return 0

    # ONE TRANSACTION PER TABLE, not one across all of them. The first ALTER takes an
    # AccessExclusiveLock and HOLDS it until COMMIT, so a single transaction spanning both tables
    # would hold delimp_precursors locked while it works on delimp_proteins -- doubling the window
    # in which every other query on the big table is queued behind us. Per-table commits release
    # each lock as soon as that table is done. Partial application is safe to re-run: every
    # statement is ADD COLUMN IF NOT EXISTS.
    #
    # The elapsed time reported is DOMINATED BY LOCK ACQUISITION, not by the ALTER. Adding a
    # nullable column with no default is a catalog write of a few hundred microseconds whatever
    # the table weighs; if one of these reports seconds, it waited for the lock. If one reports
    # a duration that scales with table size, the assumption behind this whole migration is wrong
    # and it is rewriting -- which is what --abort-over-ms exists to catch, before it does it 35
    # times on a 261 GB table.
    import time
    deadline = time.time() + a.wait_minutes * 60

    def busy_on(table: str):
        """Is something actively writing this table right now? Ask BEFORE taking the lock.

        Attempting the ALTER blind is not free: while it waits, every new query on the table
        queues behind it, so a poll loop that attempts blindly imposes a lock_timeout-long stall
        on readers over and over. Looking first means we attempt when it is likely to work.

        ROLLBACK FIRST, every time, for two reasons that both bite on a long wait:
          * Any SELECT we have already run holds an AccessShareLock on delimp_precursors until
            the transaction ends. Polling for 90 minutes inside one transaction would pin a lock
            on a 261 GB table for 90 minutes -- blocking anyone else's DDL and holding back
            autovacuum on the busiest table in the corpus. We would be causing the exact problem
            this script is careful about.
          * now() is the TRANSACTION start time, not the clock. Without a rollback the reported
            age of the blocking query is frozen at whatever it was when the transaction opened,
            which makes the wait output silently, confusingly wrong.
        pg_stat_activity itself reads shared memory rather than an MVCC snapshot, so the busy/idle
        answer was always current -- it is the lock and the clock that need the fresh transaction.
        """
        con.rollback()
        cur.execute("""
            select coalesce(now() - query_start, interval '0'),
                   left(regexp_replace(query, '\\s+', ' ', 'g'), 60)
              from pg_stat_activity
             where datname = current_database() and pid <> pg_backend_pid()
               and state = 'active' and query ilike %s""", ("%" + table + "%",))
        rows = cur.fetchall()
        con.rollback()                          # do not hold a lock while we sleep
        return rows

    applied = []
    for t in tables:
        gone = [(c, ty) for tt, c, ty in missing if tt == t]
        if not gone:
            continue
        attempt = 0
        while True:
            attempt += 1
            while True:                         # hold off while something is actively writing
                act = busy_on(t)
                if not act or time.time() > deadline:
                    break
                age, q = act[0]
                print(f"  waiting for {t}: {len(act)} active statement(s), oldest "
                      f"{str(age)[:12]} -- {q}", flush=True)
                time.sleep(20)
            t0 = time.time()
            try:
                for c, ty in gone:
                    s0 = time.time()
                    cur.execute(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {c} {ty}")
                    ms = (time.time() - s0) * 1000
                    print(f"  added {t}.{c:30s} {ty:8s} {ms:8.1f} ms")
                    if ms > a.abort_over_ms:
                        raise RuntimeError(
                            f"{t}.{c} took {ms:.0f} ms (limit {a.abort_over_ms} ms). A nullable "
                            f"ADD COLUMN with no DEFAULT should be catalog-only and near-instant; "
                            f"this long means it is rewriting the table or fighting for the lock. "
                            f"Rolling this table back rather than doing it {len(gone)} times.")
                con.commit()
                applied.append((t, len(gone), (time.time() - t0) * 1000))
                print(f"  COMMITTED {t}: {len(gone)} column(s) in "
                      f"{(time.time() - t0) * 1000:.1f} ms (lock held for that long, then "
                      f"released) [attempt {attempt}]", flush=True)
                break
            except Exception as e:                                  # noqa: BLE001
                con.rollback()
                lock_contention = "lock" in str(e).lower() or "timeout" in str(e).lower()
                if lock_contention and time.time() < deadline:
                    left = (deadline - time.time()) / 60
                    print(f"  {t}: lock busy (attempt {attempt}); nothing added, retrying "
                          f"-- {left:.0f} min of --wait-minutes left", flush=True)
                    time.sleep(20)
                    continue
                print(f"\nROLLED BACK {t}: {type(e).__name__}: {e}")
                print("Nothing was added to this table. Earlier tables that COMMITTED are "
                      "unaffected; re-running is safe and will skip whatever already exists.")
                if lock_contention:
                    print("Lock contention, and --wait-minutes is exhausted (or was 0). The "
                          "table is busy — re-run with --wait-minutes when an ingest is running.")
                con.close()
                return 1

    after = {t: existing(cur, t) for t in tables}
    still = [(t, c) for t, c, _ in plan if c not in after[t]]
    con.close()
    if still:
        print("\nFAILED: still missing " + ", ".join(f"{t}.{c}" for t, c in still))
        return 1
    print(f"\nOK: {len(missing)} column(s) added; all {len(plan)} present.")
    for t, n, ms in applied:
        print(f"    {t:22s} {n:3d} column(s), lock held {ms:8.1f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
