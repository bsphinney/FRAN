#!/usr/bin/env python3
"""
FRAN pre-deploy gate — static checks + a no-credential smoke test.
No DB, no network, no Azure. Run from anywhere:  python scripts/predeploy_check.py

Exit 0 = safe to deploy. Exit 1 = BLOCK.

The gate deliberately runs with NO DELIMP_PG_* credential and asserts the app boots
DEGRADED (/health -> 200 with connected:false). If a credential leaks into the
environment the gate fails loudly rather than passing for the wrong reason.

Environment:
  FRAN_GATE_SKIP_DEPS=1   skip check 2c (third-party lazy-import resolution). Set this
                          for local runs where requirements.txt is not installed. NEVER
                          set it in CI — 2c is the check that pins requirements.txt to
                          the code (app/observed_spectrum.py lazily imports `lance` and
                          `azure.identity`; dropping either from requirements.txt is
                          otherwise invisible until a production request 500s).
"""
from __future__ import annotations

import ast
import importlib.util
import logging
import os
import pathlib
import re
import sys
import time
import warnings

logging.disable(logging.CRITICAL)          # silence MCP session-manager chatter
warnings.filterwarnings("ignore")

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "app"
sys.path.insert(0, str(ROOT))

# Scrub by PREFIX, not by an enumerated list: app/db.py:_conn_kwargs reads
# DELIMP_PG_SECRET, DELIMP_PG_PASSWORD, DELIMP_PG_SECRET_FILE, DELIMP_PG_TOKEN_FILE,
# DELIMP_PG_HOST/PORT/DB/USER/SSLMODE. Missing any one of them lets a developer machine
# connect for real, silently voiding the "boots degraded" property this gate asserts.
for _v in [k for k in list(os.environ) if k.startswith("DELIMP_PG")]:
    os.environ.pop(_v, None)
os.environ.pop("DELIMP_INTERNAL_MODE", None)

BLOCK: list[str] = []
WARN: list[str] = []
t0 = time.time()


def block(msg: str) -> None:
    BLOCK.append(msg)


def warn(msg: str) -> None:
    WARN.append(msg)


# ============================================================================
# 1. PARSE — every app/**/*.py must parse under the 3.11 grammar.
#    feature_version pins the grammar to App Service's runtime
#    (linuxFxVersion = PYTHON|3.11) so 3.12+-only syntax written on a newer local
#    interpreter fails here, not half-way through an Oryx build on a B1 with no slot.
# ============================================================================
py_files = sorted(APP.rglob("*.py"))
py_files = [p for p in py_files if "__pycache__" not in p.parts]
if not py_files:
    print(f"FATAL: no python files under {APP}", file=sys.stderr)
    sys.exit(1)

trees: dict[pathlib.Path, ast.Module] = {}
for p in py_files:
    try:
        trees[p] = ast.parse(p.read_text(encoding="utf-8"), filename=str(p),
                             feature_version=(3, 11))
    except SyntaxError as e:
        block(f"[1 syntax] {p.relative_to(ROOT)}:{e.lineno}: {e.msg}")


# ============================================================================
# 2. IMPORT RESOLUTION.
#    This codebase performs ~40 imports INSIDE function bodies (`from . import
#    export_report`, `annotation`, `lca`, `koina`, `fragments`, `observed_spectrum`, ...),
#    so `import app.main` succeeds even when the target module does not exist. Those
#    are guaranteed 500s on the first request that reaches the line.
#
#    2a  `from . import X`      -> app/X.py OR app/X/ (app/ is a namespace package,
#                                  so a subpackage directory is valid and must not block)
#    2b  `from .mod import name` -> `name` must be a top-level binding of app/mod.py
#    2c  `import third_party`    -> importlib.util.find_spec must resolve it
# ============================================================================
def _is_local_module(name: str) -> bool:
    if (APP / f"{name}.py").exists():
        return True
    d = APP / name
    # app/ has no __init__.py: it is a namespace package. A directory containing .py
    # files (with or without __init__.py) is a legitimate `from . import X` target.
    return d.is_dir() and (any(d.glob("*.py")) or (d / "__init__.py").exists())


def _toplevel_names(mod: str) -> set[str] | None:
    f = APP / f"{mod}.py"
    if not f.exists():
        f = APP / mod / "__init__.py"
    if not f.exists():
        return None                      # package without __init__: cannot introspect
    tree = trees.get(f)
    if tree is None:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            return None
    names: set[str] = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            names.add(n.target.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, (ast.If, ast.Try, ast.With)):
            names.add("*")               # conditional definitions: give up, do not block
    return names


SKIP_DEPS = os.environ.get("FRAN_GATE_SKIP_DEPS") == "1"
_thirdparty_seen: set[str] = set()
n_rel = n_tp = 0

for p, tree in trees.items():
    rel = p.relative_to(ROOT)
    for node in ast.walk(tree):
        # ---- 2a / 2b: relative imports -------------------------------------
        if isinstance(node, ast.ImportFrom) and node.level and node.level >= 1:
            if node.module is None:                       # from . import a, b
                for alias in node.names:
                    n_rel += 1
                    if not _is_local_module(alias.name):
                        block(f"[2a import] {rel}:{node.lineno} `from . import {alias.name}` "
                              f"-> neither app/{alias.name}.py nor app/{alias.name}/ exists")
            else:                                          # from .mod import name
                head = node.module.split(".")[0]
                n_rel += 1
                if not _is_local_module(head):
                    block(f"[2a import] {rel}:{node.lineno} `from .{node.module} import ...` "
                          f"-> neither app/{head}.py nor app/{head}/ exists")
                elif "." not in node.module:
                    have = _toplevel_names(node.module)
                    if have is not None and "*" not in have:
                        for alias in node.names:
                            if alias.name != "*" and alias.name not in have:
                                block(f"[2b import] {rel}:{node.lineno} "
                                      f"`from .{node.module} import {alias.name}` -> "
                                      f"app/{node.module}.py defines no top-level "
                                      f"{alias.name!r}")
        # ---- 2c: absolute / third-party imports ----------------------------
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom):
                if node.level:            # relative, handled above
                    continue
                heads = [(node.module or "").split(".")[0]]
            else:
                heads = [a.name.split(".")[0] for a in node.names]
            for head in heads:
                if not head or head in ("app",) or _is_local_module(head):
                    continue
                if head in sys.stdlib_module_names or head in _thirdparty_seen:
                    continue
                _thirdparty_seen.add(head)
                n_tp += 1
                if SKIP_DEPS:
                    continue
                try:
                    found = importlib.util.find_spec(head) is not None
                except (ImportError, ValueError):
                    found = False
                if not found:
                    block(f"[2c import] {rel}:{node.lineno} `{head}` is imported but not "
                          f"installed -> missing from requirements.txt (500 at first use)")

print(f"  [2] relative imports checked: {n_rel}   third-party roots: {n_tp}"
      f"{'  (2c SKIPPED)' if SKIP_DEPS else ''}")


# ============================================================================
# 3. TABLE GOVERNANCE.
#    app/db.py:_assert_allowlisted() checks FORBIDDEN first, then
#      allowed = (PUBLIC_TABLES | _INTERNAL_TABLES) if internal else PUBLIC_TABLES
#    so the scope matters. Two distinct failures are caught here:
#      3a  a tables= literal that is in no set at all  -> GovernanceError, dead endpoint
#      3b  SQL that FROM/JOINs a KNOWN table the call does not declare -> the read is
#          never allowlist-checked (an undeclared JOIN silently bypasses governance)
# ============================================================================
try:
    from app import db
except Exception as e:                                     # pragma: no cover
    block(f"[3 governance] cannot import app.db: {type(e).__name__}: {e}")
    db = None


def _sql_literal(node) -> str:
    """The literal parts of a SQL argument (plain str, f-string, or `a` + `b`)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return " ".join(v.value for v in node.values
                        if isinstance(v, ast.Constant) and isinstance(v.value, str))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _sql_literal(node.left) + " " + _sql_literal(node.right)
    return ""


# Only identifiers that are ACTUAL TABLES can be undeclared reads. Matching every
# identifier after FROM/JOIN false-positives on ordinary Postgres: EXTRACT(EPOCH FROM col),
# SUBSTRING(x FROM 1), TRIM(BOTH ' ' FROM x), CROSS JOIN unnest(...), JOIN LATERAL (...),
# FROM generate_series(...), and WITH RECURSIVE t AS (...).
_REF_RE = re.compile(r'\b(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)', re.I)

if db is not None:
    PUBLIC = {t.lower() for t in db.PUBLIC_TABLES}
    INTERNAL = {t.lower() for t in getattr(db, "_INTERNAL_TABLES", frozenset())}
    FORBIDDEN = {t.lower() for t in getattr(db, "FORBIDDEN_TABLES", frozenset())}
    KNOWN = PUBLIC | INTERNAL | FORBIDDEN
    # Forbidden EVERYWHERE (public and internal): db.py narrows FORBIDDEN by _INTERNAL_TABLES
    # for internal requests, so FORBIDDEN - INTERNAL can never be read by any scope.
    HARD_FORBIDDEN = FORBIDDEN - INTERNAL

    n_lit = n_dyn = n_sql = 0
    for p, tree in trees.items():
        rel = p.relative_to(ROOT)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "tables":
                    continue
                v = kw.value

                # --- 3b undeclared read -------------------------------------
                if isinstance(v, (ast.List, ast.Tuple, ast.Set)) and node.args:
                    sql = _sql_literal(node.args[0])
                    if sql:
                        n_sql += 1
                        decl = {e.value.lower() for e in v.elts
                                if isinstance(e, ast.Constant) and isinstance(e.value, str)}
                        refs = {m.lower() for m in _REF_RE.findall(sql)}
                        for u in sorted((refs & KNOWN) - decl):
                            block(f"[3b governance] {rel}:{node.lineno} SQL reads {u!r} but "
                                  f"tables= does not declare it -> read is never "
                                  f"allowlist-checked")

                # --- 3a declared-table validity -----------------------------
                if isinstance(v, (ast.List, ast.Tuple, ast.Set)):
                    for elt in v.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            n_lit += 1
                            t = elt.value.lower().strip()
                            if t in HARD_FORBIDDEN:
                                block(f"[3a governance] {rel}:{elt.lineno} table {elt.value!r} "
                                      f"is in FORBIDDEN_TABLES and is NOT un-forbidden for "
                                      f"internal requests -> GovernanceError in every scope")
                            elif t not in (PUBLIC | INTERNAL):
                                block(f"[3a governance] {rel}:{elt.lineno} table {elt.value!r} "
                                      f"is in neither PUBLIC_TABLES nor _INTERNAL_TABLES "
                                      f"-> GovernanceError at runtime (dead endpoint)")
                            elif t in FORBIDDEN:
                                # e.g. the coreomics caches: legal ONLY for an internal-scope
                                # request. 10+ legitimate call sites do this today, so this
                                # cannot be a BLOCK — see the note in the plan.
                                warn(f"[3a governance] {rel}:{elt.lineno} table {elt.value!r} "
                                     f"is internal-only (forbidden in the public scope); this "
                                     f"call site must be reachable only for full/internal access")
                        else:
                            n_dyn += 1
                            warn(f"[3a governance] {rel}:{getattr(elt, 'lineno', v.lineno)} "
                                 f"non-literal table name (cannot verify statically)")
                else:
                    n_dyn += 1
                    warn(f"[3a governance] {rel}:{v.lineno} non-literal tables= "
                         f"(cannot verify statically)")
    print(f"  [3] tables= literals verified: {n_lit}  (unverifiable: {n_dyn})")
    print(f"  [3b] SQL bodies cross-checked against their tables=: {n_sql}")


# ============================================================================
# 3c. STRAY % IN A SQL BODY.
#
# psycopg2 does `sql % args` whenever a query binds parameters, so ANY % in the SQL text that is
# not a placeholder is a landmine: '58.7% populated' in a comment raises
#   ValueError: unsupported format character 'p'
# BEFORE the statement ever reaches Postgres, so the endpoint 500s on every input including ones
# that match nothing — which is what killed species search site-wide. A literal LIKE 'foo%' pattern
# written into the SQL breaks the same way (that is why _REAL_ORG_PRED uses starts_with()).
#
# Legal: %s, %(name)s, and an escaped %%. Everything else blocks the deploy.
_PLACEHOLDER_RE = re.compile(r'%(?:s|\([a-zA-Z_][a-zA-Z0-9_]*\)s|%)')

n_pct = 0
for f in py_files:
    rel = f.relative_to(ROOT)
    try:
        tree = ast.parse(f.read_text(), filename=str(f))
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        fn = node.func
        name = getattr(fn, "attr", None) or getattr(fn, "id", None)
        if name not in ("query", "query_one", "execute", "query_scalar"):
            continue
        sql = _sql_literal(node.args[0])
        if not sql or "%" not in sql:
            continue
        n_pct += 1
        leftover = _PLACEHOLDER_RE.sub("", sql)
        if "%" in leftover:
            for i, line in enumerate(sql.splitlines(), 1):
                if "%" in _PLACEHOLDER_RE.sub("", line):
                    block(f"[3c sql-%] {rel}:{node.lineno} (+{i}) stray '%' in a SQL body: "
                          f"{line.strip()[:80]!r} -- psycopg2 will raise ValueError before the "
                          f"query runs. Move prose comments into Python; use %% or starts_with().")
                    break
print(f"  [3c] SQL bodies scanned for stray '%': {n_pct}")


# ============================================================================
# 3d. COLUMN-LESS INSERT in the ingest writers.
#
# `INSERT INTO t VALUES (...)` with no column list binds POSITIONALLY. Postgres accepts fewer values
# than columns and fills the rest with defaults, so the statement keeps working after the table grows
# -- silently writing NULL into every new column. That is exactly how delimp_precursor_xic ended up
# with is_consensus / n_runs_averaged / modified_seq / trace_rt_basis unset for a whole ingest, and
# it is invisible until a consumer trusts the missing field.
#
# Scans ingest/ (not app/, which is read-only) for INSERTs that name no columns.
_INS_RE = re.compile(r'INSERT\s+INTO\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+VALUES', re.I)

n_ins = 0
for f in sorted((ROOT / "ingest").rglob("*.py")):
    if "__pycache__" in f.parts:
        continue
    try:
        text = f.read_text()
    except Exception:  # noqa: BLE001
        continue
    for i, line in enumerate(text.splitlines(), 1):
        m = _INS_RE.search(line)
        if m:
            n_ins += 1
            block(f"[3d insert] {f.relative_to(ROOT)}:{i} INSERT INTO {m.group(1)} with no column "
                  f"list -- binds positionally and silently NULLs any column added later. "
                  f"Name the columns explicitly.")
print(f"  [3d] column-less INSERTs in ingest/: {n_ins}")


# ----------------------------------------------------------------------------
# [3e] DDL that is split on ';' in Python, while carrying a ';' inside a '--' comment.
#
#      Postgres parses '--' comments correctly, so `cur.execute(WHOLE_DDL)` is safe no matter what
#      the prose contains. A Python-side `DDL.split(";")` is not: a semicolon in a sentence ends the
#      chunk early, leaving a fragment that is pure comment text -- truthy in Python, an empty query
#      to Postgres. On 2026-08-13 this raised "can't execute an empty query" in ensure_registry and
#      killed every writer AFTER it had computed its result: 141 runs scored, none recorded.
#
#      Flagged only when BOTH appear in the same module, so the safe whole-block executors
#      (spectrum_lance.py) are not false-positived for having prose in their DDL.
# ----------------------------------------------------------------------------
n_ddl = 0
for f in sorted((ROOT / "ingest").rglob("*.py")):
    src = f.read_text(encoding="utf-8", errors="replace")
    if not re.search(r'\.split\(\s*["\'];["\']\s*\)', src):
        continue
    for m in re.finditer(r'(\w+)\s*=\s*"""(.*?)"""', src, re.S):
        name, body = m.group(1), m.group(2)
        if not re.search(r'\b(CREATE|ALTER)\s+TABLE|\bCREATE\s+INDEX', body, re.I):
            continue
        base = src[: m.start()].count("\n")
        for i, ln in enumerate(body.splitlines(), 1):
            s = ln.lstrip()
            if s.startswith("--") and ";" in s:
                n_ddl += 1
                block(f"[3e ddl] {f.relative_to(ROOT)}:{base + i + 1} ';' inside a comment in "
                      f"{name}, and this module splits SQL on ';' -- the split will cut the comment "
                      f"and emit an empty query. Drop the semicolon, or execute the block whole.")
print(f"  [3e] DDL comment/semicolon collisions: {n_ddl}")


# ============================================================================
# 4. TEMPLATE PLACEHOLDERS (advisory only).
#    The BLOCKING version of this check lives in step 5: it scans the RENDERED page
#    for leftover __X__ tokens, which is behavioural and immune to how main.py spells
#    the substitution. The source-regex below is coupled to one exact spelling
#    (chained `.replace("__X__"` with double quotes) and would hard-block a page that
#    renders perfectly if anyone switched to single quotes or a dict loop. WARN only.
# ============================================================================
tpl_path = APP / "templates" / "index.html"
if not tpl_path.exists():
    block(f"[4 template] {tpl_path.relative_to(ROOT)} is missing (truncated artifact?)")
else:
    tpl = tpl_path.read_text(encoding="utf-8")
    main_src = (APP / "main.py").read_text(encoding="utf-8")
    replaced = set(re.findall(r'\.replace\(\s*["\'](__[A-Z_]+__)["\']', main_src))
    in_tpl = set(re.findall(r'(?<![.\w])(__[A-Z_]+__)', tpl))   # skips window.__FRAN_*__
    for ph in sorted(in_tpl - replaced):
        warn(f"[4 template] {ph} appears in index.html but no `.replace(\"{ph}\"` was found "
             f"in main.py (step 5 renders the page and will BLOCK if it really leaks)")
    for ph in sorted(replaced - in_tpl):
        warn(f"[4 template] main.py replaces {ph} but it is absent from index.html (dead)")


# ============================================================================
# 5. NO-CREDENTIAL SMOKE — build the real app, run the real lifespan (both MCP
#    StreamableHTTP session managers), serve real requests.
#    Catches: a broken lifespan ("Task group is not initialized"), a missing
#    app/static directory (StaticFiles raises at import), a renamed critical route,
#    a leftover template placeholder, a Pydantic schema error — all with zero creds.
# ============================================================================
CRITICAL_ROUTES = {
    "/", "/health", "/api/health", "/version", "/static",
    "/mcp", "/mcp-auth", "/api/overview", "/api/counts", "/api/searches",
    "/api/peptide/{stripped_seq}", "/api/protein/{protein_group:path}",
}

try:
    import app.main as M
    paths = {getattr(r, "path", "") for r in M.app.routes}
    missing = sorted(CRITICAL_ROUTES - paths)
    if missing:
        block(f"[5 routes] critical route(s) missing from the app: {missing}")
    print(f"  [5] app built: version={M.APP_VERSION} routes={len(paths)}")

    # Builds the OpenAPI document; catches response_model / Pydantic schema-generation
    # errors that a bare import does not. (Path-operation signatures are validated at
    # decoration time, i.e. during the import above — not here.)
    M.app.openapi()

    from fastapi.testclient import TestClient
    with TestClient(M.app) as c:                      # `with` = lifespan runs
        r = c.get("/version")
        if r.status_code != 200:
            block(f"[5 smoke] GET /version -> {r.status_code} (expected 200)")
        elif r.json().get("version") != M.APP_VERSION:
            block(f"[5 smoke] /version reports {r.json().get('version')!r} "
                  f"but APP_VERSION is {M.APP_VERSION!r}")

        r = c.get("/api/health")
        if r.status_code != 200:
            block(f"[5 smoke] GET /api/health -> {r.status_code} (expected 200 even with no DB)")
        else:
            # Positive assertion that the credential scrub worked. If this is True the
            # gate reached a real database and every "boots degraded" claim below is void.
            if r.json().get("connected") is not False:
                block("[5 smoke] /api/health reports connected!=false — a DELIMP_PG_* "
                      "credential leaked into the gate environment; the degraded-boot "
                      "assertion is void")

        r = c.get("/")
        if r.status_code != 200:
            block(f"[5 smoke] GET / -> {r.status_code} (expected 200)")
        else:
            html = r.text
            leftover = sorted(set(re.findall(r'(?<![.\w])(__[A-Z_]+__)', html)))
            if leftover:
                block(f"[5 smoke] rendered page still contains placeholders: {leftover}")
            if "/static/app.js" not in html:
                block("[5 smoke] rendered page does not reference /static/app.js")

        # Both MCP StreamableHTTP mounts need their task group entered by the shared
        # AsyncExitStack lifespan. If one is dropped the app still boots and / still
        # renders 200 — but every request to that mount raises "Task group is not
        # initialized". Only a real MCP handshake surfaces it.
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "predeploy-gate", "version": "1"}}}
        hdrs = {"Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"}
        for mount in ("/mcp", "/mcp-auth"):
            try:
                r = c.post(mount + "/", json=init, headers=hdrs)
                if r.status_code != 200:
                    block(f"[5 mcp] {mount} initialize -> {r.status_code} (expected 200)")
            except Exception as ex:
                block(f"[5 mcp] {mount} initialize raised "
                      f"{type(ex).__name__}: {str(ex)[:120]}")
except Exception as e:
    import traceback
    block(f"[5 smoke] app failed to build/serve with no DB credential: "
          f"{type(e).__name__}: {e}")
    traceback.print_exc()


# ============================================================================
print()
for w in WARN:
    print(f"WARN  {w}")
for b in BLOCK:
    print(f"ERROR {b}", file=sys.stderr)

dt = round(time.time() - t0, 2)
if BLOCK:
    print(f"\nFAIL in {dt}s — {len(BLOCK)} blocking problem(s). Deploy stopped.", file=sys.stderr)
    sys.exit(1)
print(f"\nPASS in {dt}s — {len(py_files)} py files, {len(WARN)} warning(s).")
