"""win_preflight.py — is THIS Windows computer ready to ingest Spectronaut experiments into FRAN?

  py win_preflight.py

Read-only apart from two things: a temporary file it creates and deletes in FRAN_reports (to prove
the report archive is writable), and its own report. The report is printed AND written to
FRAN_SNE_export\\node_reports\\<HOST>\\preflight.log, which Hive can read, so the result can be
checked from the other side without anyone copying text out of a window.

It prints no token and no password.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import string
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import node_report  # noqa: E402

DLL = r"C:\Program Files (x86)\Biognosys\Spectronaut\bin\Spectronaut.dll"
EXPORT_BASE = r"C:\fran_sne_one"
NEEDED = ["corpus_ingest.py", "spectronaut_to_corpus.py", "versions.py", "organism.py", "sne_one.py",
          "sne_xic_ingest.py", "find_missed_sne.py", "find_uningested.py", "refresh_leaderboards.py",
          "FRAN.rs"]
LINES, BAD = [], []


def say(ok, what, detail=""):
    mark = {True: "[ok]  ", False: "[FAIL]", None: "[note]"}[ok]
    LINES.append(f"{mark} {what}{(': ' + detail) if detail else ''}")
    if ok is False:
        BAD.append(what)


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def check():
    """Run every check. Returns (report text, list of failed checks). Writes the report to the share."""
    del LINES[:], BAD[:]
    say(None, "computer", f"{node_report.host()}  python {sys.version.split()[0]}  {time.strftime('%Y-%m-%d %H:%M')}")
    say(None, "ingest folder", HERE)

    missing = [m for m in ("pandas", "pyarrow", "psycopg2", "numpy") if not _can_import(m)]
    say(not missing, "python packages", ("missing " + ", ".join(missing) +
        "  ->  py -m pip install pandas pyarrow psycopg2-binary numpy") if missing else "pandas pyarrow psycopg2 numpy")

    gone = [f for f in NEEDED if not os.path.isfile(os.path.join(HERE, f))]
    say(not gone, "ingest scripts present", ("missing " + ", ".join(gone)) if gone else f"{len(NEEDED)} files")

    say(os.path.isfile(DLL), "Spectronaut", DLL)
    say(shutil.which("dotnet") is not None, "dotnet on PATH")

    reports = os.path.join(os.path.dirname(HERE), "FRAN_reports")
    try:
        probe = os.path.join(reports, f"_preflight_{node_report.host()}_{os.getpid()}.tmp")
        with open(probe, "w") as fh:
            fh.write("x")
        os.remove(probe)
        say(True, "report archive writable", reports)
    except OSError as e:
        say(False, "report archive writable", f"{reports}: {e}")

    try:
        base = EXPORT_BASE if os.path.isdir(EXPORT_BASE) else os.path.splitdrive(EXPORT_BASE)[0] + "\\"
        free = shutil.disk_usage(base).free / 1e9
        left = sum(1 for e in os.listdir(EXPORT_BASE)) if os.path.isdir(EXPORT_BASE) else 0
        say(free >= 60, "free space for exports", f"{free:.0f} GB free on C: (need 60); {left} folder(s) in {EXPORT_BASE}")
    except OSError as e:
        say(False, "free space for exports", str(e))

    if os.name == "nt":
        import ctypes
        kinds = {2: "removable", 3: "local", 4: "network", 5: "cd"}
        drives = []
        for letter in string.ascii_uppercase:
            k = ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\")
            if k in kinds:
                drives.append(f"{letter}: {kinds[k]}")
        say(None, "drives", ", ".join(drives))

    # database: token, connection, and the version gate (the ingest refuses to run on a mismatch)
    tokfile = os.environ.get("DELIMP_PG_TOKEN_FILE") or os.path.expanduser("~/.pgfarm_token")
    if not (os.environ.get("DELIMP_PG_PASSWORD") or os.path.exists(tokfile)):
        say(False, "database token", f"none found (expected a file at {tokfile})")
    elif "psycopg2" in missing:
        say(False, "database", "cannot test without psycopg2")
    else:
        try:
            import psycopg2
            from refresh_leaderboards import _token
            con = psycopg2.connect(host="pgfarm.library.ucdavis.edu", port=5432,
                dbname="uc-davis-genome-center-proteomics-core/delimp",
                user="genome-proteomics-service-account", password=_token(), sslmode="require",
                connect_timeout=30, options="-c statement_timeout=60000")
            con.set_session(readonly=True, autocommit=True)
            cur = con.cursor()
            cur.execute("SELECT count(*), count(*) FILTER (WHERE search_engine='spectronaut') FROM delimp_searches")
            n, sn = cur.fetchone()
            say(True, "database", f"connected; {n:,} searches, {sn:,} Spectronaut")
            cur.execute("SELECT file, md5, gate, published_at FROM delimp_ingest_manifest")
            stale_refuse, stale_warn, when = [], [], None
            for fn, want, gate, pub in cur.fetchall():
                when = pub if when is None or (pub and pub > when) else when
                p = os.path.join(HERE, fn)
                if os.path.isfile(p) and md5(p) != want:
                    (stale_refuse if gate == "refuse" else stale_warn).append(fn)
            say(not stale_refuse, "version gate",
                ("WOULD REFUSE: " + ", ".join(stale_refuse) + " differ from the published manifest")
                if stale_refuse else f"corpus-writing scripts match the manifest of {str(when)[:16]}")
            if stale_warn:
                say(None, "other scripts that differ from the manifest", ", ".join(sorted(stale_warn)))
            con.close()
        except SystemExit as e:
            say(False, "database", f"token problem: {e}")
        except Exception as e:  # noqa: BLE001
            say(False, "database", repr(e)[:300])

    LINES.append("RESULT: " + ("READY" if not BAD else "NOT READY -- " + "; ".join(BAD)))
    text = "\n".join(LINES)
    print(text)
    p = node_report.report("preflight", "=" * 60 + "\n" + text)
    print(f"\nreport written to {p}" if p else "\n(could not write the report to the share)")
    return text, list(BAD)


def main():
    _, bad = check()
    sys.exit(0 if not bad else 1)


def _can_import(m):
    try:
        __import__(m)
        return True
    except Exception:  # noqa: BLE001
        return False


if __name__ == "__main__":
    main()
