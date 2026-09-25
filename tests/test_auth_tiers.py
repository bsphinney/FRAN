"""app.auth.resolve_access() is the ONE place a request's tier is decided — app/main.py's
_auth_mw calls db.set_scope(auth.resolve_access(request)) on every request — and the tier it
returns is what admits a caller to real PI/customer names, raw paths and the CoreOmics tables.

test_internal_route_gate.py proves an anonymous caller is refused by every /api/internal/* route,
end to end. What nothing exercised until this file is the decision itself:

  * the three dev shortcuts (X-Dev-Internal, X-Dev-Lab-Email, FRAN_DEV_INTERNAL) are INERT unless
    FRAN_DEV_AUTH=1 — the property that keeps a spoofed header from opening prod;
  * an authenticated principal with NO gate configured is refused (fail-closed on misconfig);
  * the group grant, the allow-list grant, and lab scoping each do what the docstring says;
  * a malformed principal header fails closed rather than raising.

auth.py reads FRAN_DEV_AUTH / FRAN_REQUIRED_GROUP / FRAN_ALLOWED_USERS at import time, so each
scenario sets the environment and importlib.reload()s the module. No production code changes.

Needs no DB credential and no network: requests are header-only fakes, and the one lab-tier call
into app.queries.submissions_for_email is answered by a stub, so the file runs in CI's verify gate.

TEETH: the last block re-executes auth.py with the `_DEV_AUTH and` guard stripped from
resolve_access() and checks that this suite's own assertion then FAILS — so a green run means the
guard is actually being tested, not that the test is vacuous.

Run:  python3 tests/test_auth_tiers.py
"""
import base64, importlib, json, os, sys, types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

ENV_KEYS = ("FRAN_DEV_AUTH", "FRAN_DEV_INTERNAL", "FRAN_REQUIRED_GROUP", "FRAN_ALLOWED_USERS")
_saved_env = {k: os.environ.get(k) for k in ENV_KEYS}

GROUP = "00000000-test-group-0000-000000000001"
LAB_EMAIL = "pi@lab.example.edu"
LAB_SUBS = [101, 102]

# Stub for the lab-tier lookup, installed before app.auth is imported so `from . import queries`
# inside resolve_access() binds to it and never touches the DB module.
_stub = types.ModuleType("app.queries")
_stub.submissions_for_email = lambda em: list(LAB_SUBS) if em == LAB_EMAIL else []
import app  # noqa: E402  (namespace package)
sys.modules["app.queries"] = _stub
app.queries = _stub


class _Headers(dict):
    """Case-insensitive, like Starlette's Headers — auth.py looks names up lower-cased."""
    def __init__(self, d=None):
        super().__init__({k.lower(): v for k, v in (d or {}).items()})
    def get(self, k, default=None):
        return super().get(k.lower(), default)
    def __getitem__(self, k):
        return super().__getitem__(k.lower())


class Req:
    def __init__(self, headers=None):
        self.headers = _Headers(headers)


def principal(email=None, groups=(), user_details=None):
    claims = [{"typ": "groups", "val": g} for g in groups]
    if email:
        claims.append({"typ": "preferred_username", "val": email})
    blob = {"auth_typ": "aad", "claims": claims}
    if user_details:
        blob["userDetails"] = user_details
    return {"X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(blob).encode()).decode()}


def load_auth(**env):
    """Reload app.auth under exactly this environment (all four keys cleared first)."""
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = v
    import app.auth as auth
    return importlib.reload(auth)


def tier(auth, headers=None):
    return auth.resolve_access(Req(headers))["tier"]


DEV_HEADERS = {
    "X-Dev-Internal": {"X-Dev-Internal": "1"},
    "X-Dev-Lab-Email": {"X-Dev-Lab-Email": LAB_EMAIL},
}

try:
    print("dev shortcuts are inert without FRAN_DEV_AUTH=1 (the production configuration)")
    a = load_auth(FRAN_REQUIRED_GROUP=GROUP)
    for name, h in DEV_HEADERS.items():
        check(f"{name} header alone -> public", tier(a, h) == "public", tier(a, h))
    a = load_auth(FRAN_REQUIRED_GROUP=GROUP, FRAN_DEV_INTERNAL="1")
    check("FRAN_DEV_INTERNAL=1 alone -> public", tier(a) == "public", tier(a))
    check("FRAN_DEV_INTERNAL=1 alone -> principal_is_authorized False",
          a.principal_is_authorized(Req()) is False)
    a = load_auth(FRAN_DEV_AUTH="0", FRAN_REQUIRED_GROUP=GROUP)
    check("FRAN_DEV_AUTH=0 (not '1') + X-Dev-Internal -> public",
          tier(a, DEV_HEADERS["X-Dev-Internal"]) == "public")

    print("dev shortcuts work when FRAN_DEV_AUTH=1 (local harness)")
    a = load_auth(FRAN_DEV_AUTH="1")
    check("X-Dev-Internal -> full", tier(a, DEV_HEADERS["X-Dev-Internal"]) == "full")
    r = a.resolve_access(Req(DEV_HEADERS["X-Dev-Lab-Email"]))
    check("X-Dev-Lab-Email (linked) -> lab with own submissions",
          r["tier"] == "lab" and r.get("submission_ids") == LAB_SUBS, str(r))

    print("fail-closed")
    a = load_auth(FRAN_REQUIRED_GROUP=GROUP)
    check("anonymous -> public", tier(a) == "public")
    check("malformed principal header -> public (no exception)",
          tier(a, {"X-MS-CLIENT-PRINCIPAL": "not-base64-json!!"}) == "public")
    a = load_auth()
    h = principal(email="staff@ucdavis.edu", groups=[GROUP])
    check("authenticated but NO gate configured -> not full",
          tier(a, h) != "full" and a.principal_is_authorized(Req(h)) is False, tier(a, h))

    print("grants")
    a = load_auth(FRAN_REQUIRED_GROUP=GROUP)
    check("principal in FRAN_REQUIRED_GROUP -> full", tier(a, principal(groups=[GROUP])) == "full")
    check("principal in a different group -> public",
          tier(a, principal(groups=["some-other-group"])) == "public")
    a = load_auth(FRAN_ALLOWED_USERS="Staff@UCDavis.edu, other@ucdavis.edu")
    check("allow-listed email (case-insensitive) -> full",
          tier(a, principal(email="staff@ucdavis.edu")) == "full")
    check("allow-list via X-MS-CLIENT-PRINCIPAL-NAME -> full",
          tier(a, {**principal(), "X-MS-CLIENT-PRINCIPAL-NAME": "other@ucdavis.edu"}) == "full")
    check("X-MS-CLIENT-PRINCIPAL-NAME WITHOUT a principal -> public (name header alone grants nothing)",
          tier(a, {"X-MS-CLIENT-PRINCIPAL-NAME": "other@ucdavis.edu"}) == "public")

    print("lab scoping")
    a = load_auth(FRAN_REQUIRED_GROUP=GROUP)
    r = a.resolve_access(Req(principal(email=LAB_EMAIL)))
    check("principal linked to CoreOmics -> lab, only own submission_ids",
          r["tier"] == "lab" and r.get("submission_ids") == LAB_SUBS, str(r))
    check("principal with no CoreOmics link -> public",
          tier(a, principal(email="stranger@example.org")) == "public")

    print("teeth: the dev-header assertion catches a dropped `_DEV_AUTH and` guard")
    import app.auth as _real
    src = open(_real.__file__).read()
    needle = 'if _DEV_AUTH and request.headers.get("x-dev-internal") == "1":\n        return {"tier": "full"'
    check("guard text found in auth.py (update this test if resolve_access was restructured)",
          needle in src)
    mutant = types.ModuleType("app.auth_mutant")
    mutant.__package__ = "app"
    load_auth(FRAN_REQUIRED_GROUP=GROUP)
    exec(compile(src.replace(needle, needle.replace("_DEV_AUTH and ", "", 1)), "auth_mutant", "exec"),
         mutant.__dict__)
    check("mutant (guard removed) hands X-Dev-Internal full — i.e. the assertion above has teeth",
          tier(mutant, DEV_HEADERS["X-Dev-Internal"]) == "full")
finally:
    for k, v in _saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + "; ".join(FAILS))
    sys.exit(1)
print("all auth-tier checks passed")
