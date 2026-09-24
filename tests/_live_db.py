"""Opt-in gate for tests that touch the LIVE corpus.

WHY THIS EXISTS. Five tests carried `os.environ.setdefault("DELIMP_PG_TOKEN_FILE",
"/Users/brettphinney/.pgfarm_token")` at import. On the machine where that file exists -- which is
the machine anyone actually runs the suite on -- "run the tests" therefore CONNECTED TO PRODUCTION
with no flag, no prompt and nothing in the output saying so. test_ingest_gate.py went further and
ran INSERT/UPDATE/DELETE against delimp_ingest_manifest, the table the staleness gate reads to
decide whether any node may ingest. Its writes were inside rolled-back SAVEPOINTs and a
post-publish audit found the table intact (92/92 rows, zero md5 mismatches), so no harm was done
-- but "the rollback worked" is not a safety property you want to be relying on seven runs a day.

Reported 2026-09-24 by a session that had run the full suite ~7 times under a "no DB access" rule
and only then discovered the tests were supplying the credential themselves.

THE SKIP IS LOUD ON PURPOSE. It exits 0, so a suite run is not failed by a machine that simply has
no corpus access -- but it prints SKIPPED and names the variable, because a silent skip is how a
test that has stopped testing anything goes unnoticed. test_internal_route_gate.py already warns
about exactly this: a gate test that cannot reach the thing it gates "would pass vacuously".

The token path is no longer hardcoded to one person's home directory either.
"""
import os
import sys

FLAG = "FRAN_LIVE_DB_TESTS"


def require_live_db(what: str) -> None:
    """Exit 0 with a visible SKIPPED line unless the caller opted in to touching production."""
    if os.environ.get(FLAG) != "1":
        print(f"SKIPPED — {what} reads the LIVE corpus and is opt-in.")
        print(f"          Set {FLAG}=1 to run it (and DELIMP_PG_TOKEN_FILE if not already set).")
        sys.exit(0)
    os.environ.setdefault("DELIMP_PG_TOKEN_FILE", os.path.expanduser("~/.pgfarm_token"))
