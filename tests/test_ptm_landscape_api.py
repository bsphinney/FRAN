"""The PTM landscape endpoint is PUBLIC — it must serve anonymously and leak no paths."""
import os, sys
os.environ.setdefault("DELIMP_PG_TOKEN_FILE", "/Users/brettphinney/.pgfarm_token")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from fastapi.testclient import TestClient                  # noqa: E402
from app.main import app                                   # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

c = TestClient(app)
r = c.get("/api/ptm_landscape")
check("200 for an anonymous caller", r.status_code == 200, str(r.status_code))
j = r.json()
# app.main.ok() is a redaction wrapper (privacy.redact(_json_safe(data), reveal)), not an
# envelope — it does NOT add an "ok" key. Verified empirically: the live response is exactly
# {"landscape": {...}}. So the check here is for the "landscape" key itself, not a bare "ok" flag.
check("carries a landscape object", isinstance(j.get("landscape"), dict), str(j)[:200])
lc = j.get("landscape") or {}
check("carries coverage + summary + searches",
      {"coverage", "summary", "searches"} <= set(lc), str(sorted(lc)))
check("searches is non-empty", len(lc.get("searches") or []) > 0)
body = r.text
check("no path-shaped value in the public payload",
      "output_dir" not in body and ":\\" not in body and "/Volumes/" not in body
      and "/nfs/" not in body and "/quobyte/" not in body)

# search_name is redacted at the HTTP boundary for anonymous callers (app/privacy.py _NAME_KEYS).
# Verdicts must be limited to the three allowed labels, checked on the parsed values rather than
# the raw body text — a sha1-hashed search_name can contain substrings like "fa" and a redacted
# name could in principle contain "fail", so a raw-text scan is fragile.
verdicts = {(s.get("verdict") or "") for s in (lc.get("searches") or [])}
check("verdicts are only the three allowed labels",
      verdicts <= {"", "enriched", "low for an enrichment", "incidental"}, str(sorted(verdicts)))

print()
if FAILS: print(f"FAILED ({len(FAILS)}): {', '.join(FAILS)}"); sys.exit(1)
print("all checks passed")
