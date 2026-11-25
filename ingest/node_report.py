"""node_report.py — let a Windows ingest node leave its status where Hive (and the Mac) can read it.

There is no shell from Hive or the Mac to the Windows computers. The Flinders share is the one place
both sides see: <share>\\Data\\FRAN_SNE_export on Windows is
/nfs/lssc0/flinders/proteomics/Data/FRAN_SNE_export on Hive. Each node appends to its OWN files in

    FRAN_SNE_export/node_reports/<HOST>/<kind>.log          (one writer per file)

Standard library only, and it never raises: a status line that cannot be written must not stop an
ingest. Never pass it a token or a password.
"""
from __future__ import annotations

import os
import socket
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def host():
    return socket.gethostname().split(".")[0] or "unknown"


def report_dir(base=None):
    return os.path.join(base or HERE, "node_reports", host())


def report(kind, text, base=None):
    """Append one timestamped entry to node_reports/<HOST>/<kind>.log. Returns the path, or None."""
    try:
        d = report_dir(base)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"{kind}.log")
        with open(p, "a", encoding="utf-8") as fh:
            for line in str(text).splitlines() or [""]:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
        return p
    except Exception:  # noqa: BLE001 -- best-effort by design
        return None
