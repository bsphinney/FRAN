"""A submission number must reach the submission page.

PROT_0793 is ProtiFi LLC. Before this change the only way to reach it was its hex id
(1ed8b74497e4), which nobody has in hand.

Run:  python tests/test_submission_lookup.py
"""
import os, re, sys
from datetime import date
os.environ["DELIMP_INTERNAL_MODE"] = "1"          # internal tables; see app/db.py
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import queries
from app.db import query                            # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

n = queries.normalize_submission_ref
check("PROT_0793 normalises to itself", n("PROT_0793") == "PROT_0793", repr(n("PROT_0793")))
check("lowercase prot_0793 normalises", n("prot_0793") == "PROT_0793", repr(n("prot_0793")))
check("bare 0793 normalises", n("0793") == "PROT_0793", repr(n("0793")))
check("bare 793 zero-pads", n("793") == "PROT_0793", repr(n("793")))
check("whitespace is tolerated", n("  PROT_0793 ") == "PROT_0793", repr(n("  PROT_0793 ")))
check("a hex id is NOT a number", n("1ed8b74497e4") is None, repr(n("1ed8b74497e4")))
check("a name is not a number", n("ProtiFi") is None, repr(n("ProtiFi")))
check("empty is None", n("") is None)
# 5 digits is not this scheme; refusing beats silently truncating to 4
check("five digits is refused", n("12345") is None, repr(n("12345")))
# the collision this whole scheme is built to avoid: some real submission_ids are all-numeric
# (14 of 4,488, measured 2026-09-08) — the {1,4} cap, not luck, is what keeps this None
check("a 12-digit all-numeric hex id is refused", n("123456789012") is None, repr(n("123456789012")))

d = queries.internal_submission("PROT_0793")
sub = d.get("submission") or {}
check("PROT_0793 resolves to the ProtiFi submission",
      (sub.get("institute") or "") == "ProtiFi LLC", repr(sub.get("institute")))
check("resolving by number returns the same submission as the hex id",
      d.get("submission_id") == queries.internal_submission("1ed8b74497e4").get("submission_id"),
      f'{d.get("submission_id")} vs hex lookup')
check("an unknown number returns no submission, without raising",
      (queries.internal_submission("PROT_9999") or {}).get("submission") is None)

# --- search must find a submission by number, and submissions with no searches -------------
r = queries.internal_people_search("PROT_0793", 50)
check("searching PROT_0793 returns something", (r.get("total") or 0) > 0, str(r.get("total")))
check("...and it names the right institute",
      any("protifi" in str(x.get("institute") or x.get("co_institute") or "").lower()
          for x in r.get("rows") or []), "no ProtiFi row")

r2 = queries.internal_people_search("0793", 50)
check("bare 0793 also finds it", (r2.get("total") or 0) > 0, str(r2.get("total")))

# A submission with NO linked FRAN search must still be findable. PROT_0804 (UC Davis, 2026-09-08)
# has none; the old query was rooted in delimp_search_provenance so it could not return one.
r3 = queries.internal_people_search("PROT_0804", 50)
check("a submission with no linked search is still findable",
      (r3.get("total") or 0) > 0, str(r3.get("total")))

# "PROT_0804" alone can't discriminate whether the SUBS query's `OR co.internal_id = %(ref)s` arm
# does anything — it's already a substring ILIKE-matchable against itself. PROT_0804 has NO linked
# search, so it can ONLY surface via that subs query (never via the rows query, which is rooted in
# delimp_search_provenance). A separator variant like "prot-804" is NOT an ILIKE substring of
# "PROT_0804" (hyphen vs underscore), so reaching it here isolates the ref-equality arm inside the
# SUBS query specifically — see the I3 block below for the equivalent isolation of the ROWS query.
r3b = queries.internal_people_search("prot-804", 50)
check("a separator variant ('prot-804') still finds PROT_0804 through the subs-query ref arm",
      any(x.get("kind") == "submission" and x.get("internal_id") == "PROT_0804" for x in r3b.get("rows") or []),
      f'rows={[(x.get("kind"), x.get("internal_id")) for x in r3b.get("rows") or []]}')

# PROT_0793 is a poor witness for the provenance-side co.internal_id match: its two "linked"
# searches actually have p.coreomics_submission_id = NULL (linkage_status='unlinked') and only
# surface because p.real_search_name literally contains the substring "PROT_0793" — a
# pre-existing clause unrelated to this change. So PROT_0793 alone cannot prove the
# `co.internal_id` provenance clause does anything. PROT_0652 (hex 652c08d115d8) is a genuine
# FK-linked witness: 6 searches with p.coreomics_submission_id actually set to its hex id.
# Reaching those 6 real search rows (not a bare submission stub) by number, through the
# provenance branch, is exactly what the co.internal_id provenance clause is for.
# Neither "PROT_0652" nor "0652" isolates the ROWS query's `OR co.internal_id = %(ref)s` arm —
# both are ILIKE-matchable substrings of "PROT_0652" on their own, so this loop alone would still
# pass with that arm deleted. "prot-652" (hyphen, not the stored underscore) breaks the ILIKE
# match and can only reach these rows through ref-equality — this is the I3 witness the ROWS query
# was missing (the SUBS query's equivalent is proven by "prot-804" above).
for term in ("PROT_0652", "0652", "prot-652"):
    r4 = queries.internal_people_search(term, 50)
    check(f"{term} reaches PROT_0652's real searches, not just a submission stub",
          any(x.get("kind") == "search" and x.get("search_engine") for x in r4.get("rows") or []),
          f'rows={[(x.get("kind"), x.get("search_engine")) for x in r4.get("rows") or []]}')


# --- the submissions list -------------------------------------------------------------------
L = queries.internal_submissions(limit=25)
check("list returns submissions", len(L.get("submissions") or []) > 0, str(len(L.get("submissions") or [])))
# A one-directional >= cannot catch inflation: dropping `co.internal_id IS NOT NULL` from the
# total COUNT (app/queries.py) makes total 4,488 (every coreomics_submissions_cache row, numbered
# or not) and >= 790 would still pass — the page header would read "4,488 numbered submissions".
# The number is DERIVED, not pinned: it was 790 on 2026-09-08 and 791 a day later when PROT_0805
# arrived, so a hardcoded literal is a test that fails on healthy data. Equality against an
# independently-computed count keeps the teeth (de-gating makes `total` jump to every submission,
# ~4,489, while this stays at the numbered subset) without breaking as the corpus grows.
_numbered = query("SELECT count(*) AS n FROM coreomics_submissions_cache WHERE internal_id IS NOT NULL",
                  tables=["coreomics_submissions_cache"])[0]["n"]
check("list total equals the numbered-submission count", (L.get("total") or 0) == _numbered,
      f'total={L.get("total")} vs numbered={_numbered}')
check("...and that is a strict subset of all submissions (the gate is doing work)",
      _numbered < query("SELECT count(*) AS n FROM coreomics_submissions_cache",
                        tables=["coreomics_submissions_cache"])[0]["n"],
      str(_numbered))
first = (L.get("submissions") or [{}])[0]
for k in ("internal_id", "institute", "n_searches", "num_samples", "submitted_at"):
    check(f"row carries {k}", k in first, sorted(first.keys())[:12])
check("locations_as_of is reported", L.get("locations_as_of") is not None)

# "newest first" must hold over the WHOLE page, on the key the query actually sorts by
# (submitted_at), not just the first row (a first-row-only check can't tell "sorted" from
# "unsorted but starts high"). NULLs are sorted LAST by the query (NULLS LAST) — an unknown
# submission date must not jump to the top and falsify "newest first" — so NULL is mapped to
# date.min here, the smallest possible key, to keep the non-increasing assertion and the SQL's
# NULLS LAST in agreement.
keys = [(s.get("submitted_at") or date.min) for s in (L.get("submissions") or [])]
check("newest first",
      all(keys[i] >= keys[i + 1] for i in range(len(keys) - 1)),
      [str(k) for k in keys[:6]])

# Every returned row must carry a real, non-null PROT_#### internal_id — not a key-presence check.
# Dropping `co.internal_id IS NOT NULL` from the ROWS query (app/queries.py) leaves the "row
# carries internal_id" check above green (`internal_id: None` still satisfies key-presence), but
# the page would render `null` in the Submission column with `go('submission','null')` click
# targets for any coreomics_submissions_cache row that predates a number being assigned.
_PROT_RE = re.compile(r"^PROT_\d{4}$")
bad_ids = [s.get("internal_id") for s in (L.get("submissions") or []) if not (s.get("internal_id") and _PROT_RE.match(str(s["internal_id"])))]
check("every row has a real PROT_#### internal_id (none null, none malformed)",
      not bad_ids, bad_ids[:5])

# --- the shipped ORDER BY actually says NULLS LAST -------------------------------------------
# The synthetic block below proves the ASSERTION would catch a NULLS-FIRST-shaped list. It does
# NOT prove the QUERY produces a NULLS-LAST-shaped one: internal_submissions() returns
# `query()`'s rows untouched, so monkeypatching query() bypasses the SQL entirely — the ORDER BY
# never executes. Verified 2026-09-09 by a re-reviewer: flipping the REAL SQL to NULLS FIRST left
# the whole suite ALL PASS. Live data cannot witness it either (0 of 790 numbered submissions have
# a NULL submitted_at), so the only thing that binds a check to the clause is the clause itself.
# This asserts on the shipped source, the same way test_submission_analyzed_definition.py asserts
# on the condition shipped in app.js.
_qsrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "queries.py")).read()
_orders = re.findall(r"ORDER BY\s+co\.submitted_at\s+DESC[^\n]*", _qsrc)
check("every submitted_at ORDER BY is present", len(_orders) >= 2, f"found {len(_orders)}")
check("every submitted_at ORDER BY sorts undated LAST",
      all("NULLS LAST" in o for o in _orders),
      f"offending: {[o for o in _orders if 'NULLS LAST' not in o]}")

# --- synthetic witness for "NULLS LAST" ------------------------------------------------------
# Live data has ZERO numbered submissions with a NULL submitted_at (measured against the live DB,
# 2026-09-08: 0/790), so the "newest first" check above can't witness a NULLS-FIRST regression —
# there's no undated row that could jump to the top. Unlike internal_collaborators() (which sorts
# in Python), internal_submissions() sorts entirely inside the SQL text itself
# (`ORDER BY co.submitted_at DESC NULLS LAST`), so there's no Python sort step to feed synthetic
# input through the way the collaborator-recency synthetic block does. Instead: monkeypatch
# queries.query (as that block does) to return canned rows in place of the `rows` SELECT, and call
# the REAL internal_submissions() end to end — this still proves the function doesn't reorder or
# otherwise interfere with what comes back (it doesn't; `submissions: rows` is a straight pass
# -through), and lets the exact same "newest first" assertion used on live data above run against
# both a NULLS-LAST-shaped and a NULLS-FIRST-shaped synthetic result.
_SYN_SUBS = [   # what `ORDER BY submitted_at DESC NULLS LAST` actually produces
    {"internal_id": "PROT_9001", "submission_id": "s1", "institute": "X", "pi": None, "submitter": None,
     "num_samples": None, "submitted_at": date(2026, 8, 1), "n_searches": 0,
     "in_fran": False, "run_count": None, "service_folder": None, "service_folder_win": None},
    {"internal_id": "PROT_9002", "submission_id": "s2", "institute": "X", "pi": None, "submitter": None,
     "num_samples": None, "submitted_at": date(2025, 1, 1), "n_searches": 0,
     "in_fran": False, "run_count": None, "service_folder": None, "service_folder_win": None},
    {"internal_id": "PROT_9003", "submission_id": "s3", "institute": "X", "pi": None, "submitter": None,
     "num_samples": None, "submitted_at": None, "n_searches": 0,                       # undated, LAST
     "in_fran": False, "run_count": None, "service_folder": None, "service_folder_win": None},
]
_real_query = queries.query
def _fake_submissions_query(sql, *args, **kwargs):
    if "ORDER BY co.submitted_at" in sql:
        return list(_CURRENT_SYN)
    if "EXISTS (SELECT 1 FROM delimp_search_provenance p" in sql:
        return 0
    if "MAX(matched_at)" in sql:
        return [{"d": None}]
    if "COUNT(*) FROM coreomics_submissions_cache co" in sql:
        return len(_CURRENT_SYN)
    return _real_query(sql, *args, **kwargs)

queries.query = _fake_submissions_query
try:
    _CURRENT_SYN = _SYN_SUBS                                        # undated LAST — NULLS LAST shape
    ok_result = queries.internal_submissions(limit=25)
    ok_keys = [(s.get("submitted_at") or date.min) for s in ok_result.get("submissions") or []]
    check("synthetic: NULLS-LAST-shaped data (undated submission last) passes 'newest first'",
          all(ok_keys[i] >= ok_keys[i + 1] for i in range(len(ok_keys) - 1)),
          [str(k) for k in ok_keys])

    _CURRENT_SYN = [_SYN_SUBS[2], _SYN_SUBS[0], _SYN_SUBS[1]]        # undated FIRST — NULLS FIRST shape
    broken_result = queries.internal_submissions(limit=25)
    broken_keys = [(s.get("submitted_at") or date.min) for s in broken_result.get("submissions") or []]
    check("synthetic: NULLS-FIRST-shaped data (undated submission first) FAILS 'newest first'",
          not all(broken_keys[i] >= broken_keys[i + 1] for i in range(len(broken_keys) - 1)),
          [str(k) for k in broken_keys])
finally:
    queries.query = _real_query      # restore before any other test module imports queries

F = queries.internal_submissions(q="ProtiFi", limit=25)
check("filtering by institute works",
      any((s.get("internal_id") == "PROT_0793") for s in F.get("submissions") or []),
      [s.get("internal_id") for s in (F.get("submissions") or [])][:5])
P = queries.internal_submissions(q="0793", limit=25)
check("filtering by bare number works",
      any((s.get("internal_id") == "PROT_0793") for s in P.get("submissions") or []),
      [s.get("internal_id") for s in (P.get("submissions") or [])][:5])

# A bare number cannot discriminate whether `OR co.internal_id = %(ref)s` does anything:
# internal_id is literally "PROT_" + the zero-padded number, so "0793" is already a *substring*
# of "PROT_0793" and `co.internal_id ILIKE %(like)s` matches it on its own — the check above
# would pass even with the ref-equality arm deleted. A separator/case variant like "prot-793" is
# NOT a substring of "PROT_0793" (the "-" vs "_" and the missing zero-pad break ILIKE), so it can
# only be found through normalize_submission_ref -> co.internal_id = %(ref)s. This is the arm
# that makes Task 1's normalize_submission_ref actually do something inside this query — do not
# "simplify" this back to a bare-number check; that would silently delete the coverage.
R = queries.internal_submissions(q="prot-793", limit=25)
check("filtering by a normalized ref (separator variant) works",
      any((s.get("internal_id") == "PROT_0793") for s in R.get("submissions") or []),
      [s.get("internal_id") for s in (R.get("submissions") or [])][:5])

# PROT_0793 is a poor witness for n_searches: its two "linked" searches actually carry
# p.coreomics_submission_id = NULL (linkage_status='unlinked') and only appear under it via a
# real_search_name substring match (see the block above) — so a correct FK-joined n_searches is
# 0 for PROT_0793. PROT_0652 (hex 652c08d115d8) is the proven FK-linked witness: 6 searches with
# p.coreomics_submission_id actually set to its hex id. Use PROT_0652 here so a future reader
# does not "simplify" this back to PROT_0793.
S652 = queries.internal_submissions(q="0652", limit=25)
sub652 = next((s for s in (S652.get("submissions") or []) if s.get("internal_id") == "PROT_0652"), {})
check("PROT_0652 is present via q=0652",
      bool(sub652), [s.get("internal_id") for s in (S652.get("submissions") or [])][:5])
# A one-directional >= 1 cannot catch de-correlation: if the n_searches subquery's WHERE clause
# lost its `p.coreomics_submission_id = co.submission_id` correlation (app/queries.py), EVERY row
# would count all 378 FK-linked searches in the corpus and >= 1 would still pass — the whole
# tri-state page would collapse to "everything is in FRAN". PROT_0652 has exactly 6, measured
# against the live DB (2026-09-08); assert equality.
check("PROT_0652 shows exactly its 6 genuinely FK-linked searches",
      sub652.get("n_searches") == 6, sub652.get("n_searches"))

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
