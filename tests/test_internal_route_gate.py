"""The confidential /api/internal/* routes must refuse a non-full caller with 404 — not 500,
not 200. This page (Submissions) carries PI names, submitter names and institutes, and `main`
auto-deploys, so this is the one check in the whole review the reviewer said they did not want
to take on faith: they verified the two-layer refusal (the route gate at api_internal_submissions,
plus db._assert_allowlisted raising GovernanceError before SQL ever runs) exists BY READING the
code, but only ever exercised the POSITIVE (full-access) path against it. Nothing exercises the
NEGATIVE path — a caller who is NOT full — and scripts/predeploy_check.py's CRITICAL_ROUTES list
doesn't cover any /api/internal/* route either, so nothing guards the gate itself.

WIDENED (2026-09): the original version of this test exercised only /api/internal/submissions by
name, so a new internal route inherited zero coverage. It now enumerates every /api/internal/*
path straight off app.routes and probes each one, so the day a route is added without its own
`if not db.is_full(): raise HTTPException(404)` guard, this suite goes red on its own.

DELIBERATE DEVIATION from every other test in this suite: those set DELIMP_INTERNAL_MODE=1 before
importing app.db, because they need to READ the internal tables. THIS test needs the opposite —
app.db.INTERNAL_MODE must be False (the env var unset) — because it exists to prove that WITHOUT
that deployment-wide force (and without an authorized SSO principal, which a bare TestClient
request carries none of), the route refuses. Setting DELIMP_INTERNAL_MODE=1 here would make the
route return 200 unconditionally and this test would prove nothing. If DELIMP_INTERNAL_MODE is set
in the calling shell's environment, this test cannot do its job and says so loudly instead of
silently passing.

Uses FastAPI's TestClient (ASGI in-process, no real server, no real DB) — the route gate
(`if not db.is_full(): raise HTTPException(404)`) runs and returns before any SQL is issued, so
this needs no DB credential at all.

Run:  python tests/test_internal_route_gate.py
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

# Fail loud, not silent, if the environment would defeat the point of this test.
if os.environ.get("DELIMP_INTERNAL_MODE") == "1":
    print("  SKIP-INVALID  DELIMP_INTERNAL_MODE=1 is set in the environment — this test cannot "
          "exercise the negative path under a deployment-wide internal-mode force. Unset it and "
          "re-run this file on its own.")
    sys.exit(2)

from fastapi.testclient import TestClient   # noqa: E402
from app.main import app                    # noqa: E402
from app import db                          # noqa: E402

check("app.db.INTERNAL_MODE is False (no deployment-wide force) — precondition for this test",
      db.INTERNAL_MODE is False, str(db.INTERNAL_MODE))

# One TestClient / one app lifespan for the whole file: the mounted MCP StreamableHTTP session
# managers are process-wide singletons that refuse to be started twice, so every phase below
# (real gate, teeth proof, post-restore) reuses this SAME client rather than opening a fresh one.
with TestClient(app) as client:
    # A public health-style route must still work, so a 404 below is the ROUTE GATE, not a
    # broken app / broken TestClient setup returning 404 for everything.
    health = client.get("/api/health")
    check("a public route (/api/health) still responds 200 under this clean env",
          health.status_code == 200, f"{health.status_code}: {health.text[:200]}")

    r = client.get("/api/internal/submissions")
    check("GET /api/internal/submissions refuses a non-full caller with 404 (not 200, not 500)",
          r.status_code == 404, f"{r.status_code}: {r.text[:300]}")
    check("...and the response body carries no submission data",
          "internal_id" not in r.text and "institute" not in r.text, r.text[:300])

    # --- grows with the app: enumerate every /api/internal/* route straight off the live FastAPI
    # app instead of naming them by hand, so a route added later inherits this coverage on day
    # one rather than waiting for the next audit to notice it was never gated. Path parameters are
    # filled with a harmless placeholder purely so the route resolves; the value never reaches SQL
    # because the gate raises before any query runs.
    internal_paths = sorted(
        {getattr(r, "path", "") for r in app.routes if getattr(r, "path", "").startswith("/api/internal/")}
    )
    check("at least one /api/internal/* route exists to enumerate "
          "(if this is empty the loop below would pass vacuously)",
          len(internal_paths) > 0, str(internal_paths))
    for p in internal_paths:
        probe = (p.replace("{submission_id}", "x")
                   .replace("{name:path}", "x")
                   .replace("{pi:path}", "x"))
        # ?q=x satisfies api_internal_people_search's required `q` query param so FastAPI's own
        # request-validation doesn't pre-empt the gate with a 422 before db.is_full() ever runs
        # (found live: the bare probe returned 422, not 404, for exactly this route). Every other
        # route either ignores the extra param or already defaults it.
        rr = client.get(probe + "?q=x")
        check(f"GET {p} refuses a non-full caller with 404 (not 200, not 500)",
              rr.status_code == 404, f"{rr.status_code}: {rr.text[:200]}")

    # --- teeth proof, without editing the production security gate ----------------------------
    # The usual teeth-proof pattern in this suite (break production, run, confirm FAIL, restore,
    # confirm PASS, diff clean) means inverting `if not db.is_full(): raise HTTPException(404)`
    # in app/main.py to `if db.is_full(): raise ...` — but that edit reads, out of context, as
    # disabling an authorization check on a confidential route, which is exactly the kind of
    # change that shouldn't go through casually even as a transient, restored-before-commit step.
    # Monkeypatching `db.is_full` for one request proves the SAME thing (this check can
    # discriminate a working gate from a broken one) without ever touching main.py's source: if
    # the gate function itself always said yes, the route would let a non-full caller straight
    # through.
    _real_is_full = db.is_full
    db.is_full = lambda: True     # simulate the gate always granting access — must open the route
    try:
        br = client.get("/api/internal/submissions")
        check("teeth proof: forcing db.is_full()=True DOES open the route (confirms the real "
              "404 above is the gate working, not an unrelated failure)",
              br.status_code == 200, f"{br.status_code}: {br.text[:300]}")
    finally:
        db.is_full = _real_is_full    # restore before anything else touches this process's app.db

    # Post-restore sanity: the real (unpatched) gate still refuses, in the SAME client/process.
    r2 = client.get("/api/internal/submissions")
    check("post-restore: the real gate still returns 404 (clean restore, not a half-revert)",
          r2.status_code == 404, f"{r2.status_code}: {r2.text[:300]}")

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
