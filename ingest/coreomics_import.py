"""coreomics_import.py — refresh coreomics_submissions_cache / coreomics_samples_cache from the
CoreOmics API.

WHY THIS EXISTS. The cache was populated ONCE, on 2026-06-17, and nothing refreshed it after.
Every one of its 4,408 rows carries the same `imported_at` and the same `source_export`, there was
no importer anywhere in version control, and no cron referenced it. By 2026-09-08 it had drifted 83
days: it stopped at PROT_0724 while CoreOmics was at PROT_0804, so submission PROT_0793 -- ProtiFi
LLC -- simply did not exist as far as FRAN was concerned. A snapshot that looks like an integration
is worse than no integration, because nothing about it announces that it is stale.

That is the same failure shape as delimp_spectrum_regen_queue (one requested_at, one shot, never
repeated) and as the pg_dump backups that quietly stopped for 40 days. So this script is written to
be RE-RUN, and it stamps every row with the run that wrote it.

API NOTES, established by probing (there is no published doc to hand):
  * Base is https://ucdavis.coreomics.com/server/api ; the bare root 404s, /submissions/ is real.
  * Auth is a Django REST token: `Authorization: Token <40 hex>`. Read from a FILE, never a flag --
    argv is world-readable on the compute nodes (see fran_db_backup.sbatch for the same reasoning).
  * `?lab=PROTEOMICS` is REQUIRED. Without it the endpoint returns count=0 rather than an error,
    and `?lab=3` (the numeric lab id, which the payload shows) returns HTTP 500.
  * The LIST view omits `samples`, `sample_prep` and `send_date`; only the per-submission DETAIL
    view has them. So samples cost one request each, which is why detail fetches are incremental by
    default -- 4,488 detail requests per run would make this too expensive to schedule.

Usage:
    export FRAN_COREOMICS_TOKEN_FILE=/quobyte/proteomics-grp/brett/.coreomics_token
    python coreomics_import.py                 # dry run: report what WOULD change
    python coreomics_import.py --apply         # upsert new/changed submissions + their samples
    python coreomics_import.py --apply --full  # re-fetch every detail, not just changed ones
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

API = os.environ.get("FRAN_COREOMICS_API", "https://ucdavis.coreomics.com/server/api")
LAB = os.environ.get("FRAN_COREOMICS_LAB", "PROTEOMICS")
PAGE_SIZE = 200


def _api_token() -> str:
    """The CoreOmics API token, from a file. Never accepted as an argument.

    A token on the command line lands in argv, and /proc on the compute nodes has no hidepid.
    """
    p = os.environ.get("FRAN_COREOMICS_TOKEN_FILE") or os.path.expanduser("~/.coreomics_token")
    if not os.path.exists(p):
        sys.exit(f"No CoreOmics token at {p}. Set FRAN_COREOMICS_TOKEN_FILE.")
    tok = open(p).read().strip()
    if not tok:
        sys.exit(f"CoreOmics token file {p} is empty")
    return tok


def _pg_token() -> str:
    pw = os.environ.get("DELIMP_PG_PASSWORD")
    if not pw:
        tf = os.path.expanduser(os.environ.get("DELIMP_PG_TOKEN_FILE", "~/.pgfarm_token"))
        if not os.path.exists(tf):
            sys.exit(f"No PG Farm credential: set DELIMP_PG_PASSWORD or place a token at {tf}")
        pw = open(tf).read().strip()
    if pw.startswith("eyJ") and pw.count(".") == 2:
        return pw
    body = json.dumps({"username": os.environ.get("DELIMP_PG_USER",
                                                  "genome-proteomics-service-account"),
                       "secret": pw}).encode()
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
        password=_pg_token(), sslmode="require", connect_timeout=30)


def _get(url: str, tok: str, tries: int = 3):
    """GET with a couple of retries. A transient 5xx must not abandon a half-done refresh."""
    last = None
    for i in range(tries):
        req = urllib.request.Request(url, headers={"Authorization": f"Token {tok}",
                                                   "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code < 500:
                raise
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET failed after {tries} tries: {url} ({last})")


def _flat(v):
    """CoreOmics answers are sometimes a scalar, sometimes a one-element list, sometimes a dict.

    proteomics_type comes back as ["DIA (Quantitative)"]; storing that as a Python repr is how you
    end up grepping for "['DIA" later. Flatten to text, and keep NULL as NULL.
    """
    if v is None or v == "" or v == []:
        return None
    if isinstance(v, list):
        return "; ".join(str(x) for x in v if x not in (None, "")) or None
    if isinstance(v, dict):
        return json.dumps(v)
    return str(v)


_BAD_DATES: dict[str, str] = {}       # submission_id -> the value we could not parse


def _date(v, sid=None):
    """send_date is a DATE column but CoreOmics stores it as free text.

    Real values seen include "2/5/2020f" -- a typo with a trailing letter -- which aborts the whole
    transaction with InvalidDatetimeFormat halfway through a refresh. Parse what we can, keep NULL
    for the rest, and RECORD the rejects so a bad date is visible at the end of the run instead of
    silently becoming an empty cell.
    """
    v = _flat(v)
    if not v:
        return None
    t = v.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d-%m-%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    try:                                   # an ISO timestamp rather than a bare date
        return datetime.fromisoformat(t.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    # Deliberately NOT repaired. "2/5/2020f" is obviously "2/5/2020" with a stray keystroke, but
    # guessing turns a visible typo into an invisible wrong date, and this column feeds turnaround
    # reporting. NULL plus a named reject is the honest answer; the fix belongs in CoreOmics.
    if sid:
        _BAD_DATES[sid] = t
    return None


def _sub_row(rec: dict, detail: dict | None, source: str) -> tuple:
    """Map an API record onto coreomics_submissions_cache's columns."""
    sd = dict((detail or {}).get("submission_data") or {})
    sd.update({k: v for k, v in (rec.get("submission_data") or {}).items() if k not in sd})
    typ = rec.get("type") or {}
    payload = detail or rec
    blob = json.dumps(payload, sort_keys=True, default=str)
    return (
        rec.get("id"),
        rec.get("internal_id"),
        _flat(typ.get("name") if isinstance(typ, dict) else typ),
        rec.get("submitted"),
        _flat(rec.get("status")),
        _date(sd.get("send_date"), rec.get("id")),
        _flat(rec.get("first_name")), _flat(rec.get("last_name")), _flat(rec.get("email")),
        _flat(rec.get("pi_first_name")), _flat(rec.get("pi_last_name")), _flat(rec.get("pi_email")),
        _flat(rec.get("institute")),
        (rec.get("table_count") or {}).get("Samples"),
        _flat(sd.get("organism")), _flat(sd.get("species")),
        _flat(sd.get("prot_or_pep")), _flat(sd.get("proteomics_type")),
        _flat(sd.get("mass_spec_wanted")), _flat(sd.get("sample_prep")),
        _flat(sd.get("gradient_length")), _flat(sd.get("dia")), _flat(sd.get("tmt")),
        _flat(sd.get("description")), _flat(sd.get("other_info")),
        _flat(sd.get("biohazard")), _flat(sd.get("pathogenic")),
        _flat(sd.get("nih_s10_user")), _flat(sd.get("is_nih_major_user")),
        _flat(sd.get("transgenic")), _flat(sd.get("po_account_number")),
        json.dumps(payload, default=str),
        source, hashlib.md5(blob.encode()).hexdigest(),
    )


SUB_SQL = """
INSERT INTO coreomics_submissions_cache
 (submission_id, internal_id, type, submitted_at, status, send_date,
  submitter_first_name, submitter_last_name, submitter_email,
  pi_first_name, pi_last_name, pi_email, institute, num_samples,
  organism, species, prot_or_pep, proteomics_type, mass_spec_wanted, sample_prep,
  gradient_length, dia, tmt, description, other_info, biohazard, pathogenic,
  nih_s10_user, is_nih_major_user, transgenic, po_account_number,
  raw_payload, source_export, source_export_md5, imported_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s, now())
ON CONFLICT (submission_id) DO UPDATE SET
  internal_id=EXCLUDED.internal_id, type=EXCLUDED.type, submitted_at=EXCLUDED.submitted_at,
  status=EXCLUDED.status, send_date=COALESCE(EXCLUDED.send_date, coreomics_submissions_cache.send_date),
  submitter_first_name=EXCLUDED.submitter_first_name, submitter_last_name=EXCLUDED.submitter_last_name,
  submitter_email=EXCLUDED.submitter_email, pi_first_name=EXCLUDED.pi_first_name,
  pi_last_name=EXCLUDED.pi_last_name, pi_email=EXCLUDED.pi_email, institute=EXCLUDED.institute,
  num_samples=EXCLUDED.num_samples, organism=EXCLUDED.organism, species=EXCLUDED.species,
  prot_or_pep=EXCLUDED.prot_or_pep, proteomics_type=EXCLUDED.proteomics_type,
  mass_spec_wanted=EXCLUDED.mass_spec_wanted,
  -- COALESCE the detail-only fields: a LIST-view refresh must not blank values a previous DETAIL
  -- fetch filled, or an incremental run would erase samples/sample_prep/send_date it never saw.
  sample_prep=COALESCE(EXCLUDED.sample_prep, coreomics_submissions_cache.sample_prep),
  gradient_length=EXCLUDED.gradient_length, dia=EXCLUDED.dia, tmt=EXCLUDED.tmt,
  description=EXCLUDED.description, other_info=EXCLUDED.other_info,
  biohazard=EXCLUDED.biohazard, pathogenic=EXCLUDED.pathogenic,
  nih_s10_user=EXCLUDED.nih_s10_user, is_nih_major_user=EXCLUDED.is_nih_major_user,
  transgenic=EXCLUDED.transgenic, po_account_number=EXCLUDED.po_account_number,
  raw_payload=EXCLUDED.raw_payload, source_export=EXCLUDED.source_export,
  source_export_md5=EXCLUDED.source_export_md5, imported_at=now()
"""

SAMP_SQL = """
INSERT INTO coreomics_samples_cache
 (submission_id, unique_id, sample_name, condition_name, amt_to_inject, internal_id,
  internal_notes, imported_at)
VALUES (%s,%s,%s,%s,%s,%s,%s, now())
ON CONFLICT (submission_id, unique_id) DO UPDATE SET
  sample_name=EXCLUDED.sample_name, condition_name=EXCLUDED.condition_name,
  amt_to_inject=EXCLUDED.amt_to_inject, internal_id=EXCLUDED.internal_id,
  internal_notes=EXCLUDED.internal_notes, imported_at=now()
"""


def fetch_all(tok: str, limit: int = 0) -> list[dict]:
    url = f"{API}/submissions/?lab={LAB}&page_size={PAGE_SIZE}"
    out: list[dict] = []
    while url:
        d = _get(url, tok)
        out.extend(d.get("results") or [])
        print(f"  fetched {len(out)}/{d.get('count')}", flush=True)
        if limit and len(out) >= limit:
            return out[:limit]
        nxt = d.get("next")
        # The API hands back http:// next-links behind an https service; following them verbatim
        # would silently downgrade the transport carrying the token.
        url = nxt.replace("http://", "https://", 1) if nxt else None
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--full", action="store_true",
                    help="re-fetch every submission's detail, not just new/changed ones")
    ap.add_argument("--limit", type=int, default=0, help="stop after N submissions (testing)")
    a = ap.parse_args(argv)

    tok = _api_token()
    run_at = datetime.now(timezone.utc).isoformat()
    source = f"api:{API}:{run_at}"

    print(f"=== coreomics import {run_at}  (lab={LAB}, {'APPLY' if a.apply else 'DRY RUN'})",
          flush=True)
    recs = fetch_all(tok, a.limit)
    print(f"API returned {len(recs)} submissions", flush=True)

    con = _conn()
    cur = con.cursor()
    cur.execute("SELECT submission_id, raw_payload->>'updated' FROM coreomics_submissions_cache")
    have = dict(cur.fetchall())
    new = [r for r in recs if r.get("id") not in have]
    changed = [r for r in recs if r.get("id") in have and (r.get("updated") or "") != (have.get(r.get("id")) or "")]
    print(f"  cache holds {len(have)}; new={len(new)} changed={len(changed)}", flush=True)
    if not a.apply:
        for r in new[:15]:
            print(f"    NEW  {r.get('internal_id') or r.get('id')}  {r.get('institute')}")
        if len(new) > 15:
            print(f"    ... and {len(new)-15} more new")
        print("dry run — nothing written. Re-run with --apply.")
        con.close()
        return 0

    need_detail = {r["id"] for r in (recs if a.full else new + changed)}
    print(f"  detail fetches needed: {len(need_detail)}", flush=True)

    n_sub = n_samp = 0
    for i, rec in enumerate(recs, 1):
        detail = None
        if rec.get("id") in need_detail:
            try:
                detail = _get(f"{API}/submissions/{rec['id']}/", tok)
            except Exception as e:  # noqa: BLE001
                print(f"    detail FAILED for {rec.get('internal_id') or rec['id']}: {e}",
                      flush=True)
        cur.execute(SUB_SQL, _sub_row(rec, detail, source))
        n_sub += 1
        for s in ((detail or {}).get("submission_data") or {}).get("samples") or []:
            uid = s.get("unique_id")
            if not uid:
                continue          # PK is (submission_id, unique_id); a null uid cannot be stored
            cur.execute(SAMP_SQL, (rec["id"], uid, _flat(s.get("sample_name")),
                                   _flat(s.get("condition_name")), _flat(s.get("amt_2_inject")),
                                   _flat(s.get("internal_id")), _flat(s.get("internal_notes"))))
            n_samp += 1
        if i % 250 == 0:
            con.commit()
            print(f"  ...{i}/{len(recs)} submissions, {n_samp} samples", flush=True)
    con.commit()

    cur.execute("SELECT count(*), max(internal_id) FROM coreomics_submissions_cache")
    tot, mx = cur.fetchone()
    print(f"=== done: {n_sub} submissions upserted, {n_samp} sample rows; "
          f"cache now {tot} rows, max internal_id {mx}", flush=True)
    if _BAD_DATES:
        print(f"  {len(_BAD_DATES)} unparseable send_date value(s), stored as NULL:", flush=True)
        for sid, val in list(_BAD_DATES.items())[:10]:
            print(f"    {sid}: {val!r}", flush=True)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
