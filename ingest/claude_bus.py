"""claude_bus.py — inter-Claude coordination over the shared PG Farm DB (not SMB files).

Every FRAN node (win-1/win-2/win-forge/mac-1) already authenticates to the same Postgres, so
the DB is the right coordination bus: concurrent-safe, queryable, no flaky share, no file locks.
This replaces ad-hoc check-ins buried in the markdown board. The markdown board stays for
human-readable DECISIONS; liveness + node-to-node messages live HERE.

Two tiny additive tables (created on `initdb`, never touch the corpus tables):
  delimp_claude_heartbeat(node PK, host, drives, status, current_lane, items_left, note, updated_at)
  delimp_claude_messages(id, from_node, to_node, body, created_at, acked_at)

CLI:
  python claude_bus.py initdb
  python claude_bus.py checkin <node> --status ACTIVE --lane "R: share .sne" --left 1900 --note "wave 6"
                                       [--host WIN2 --drives "K: R:"]
  python claude_bus.py who                      # liveness table; flags rows >2h stale as DOWN
  python claude_bus.py post <from> <to|ALL> "message body"
  python claude_bus.py inbox <node> [--ack]     # messages to <node> or ALL; --ack marks them read
Token: ~/.pgfarm_token (service-account SECRET auto-exchanged) or $DELIMP_PG_PASSWORD (JWT).
"""
import os, sys, json, argparse, urllib.request

STALE_MIN = 120  # a heartbeat older than this => node treated as DOWN


def _token():
    secret = open(os.path.expanduser(os.environ.get("DELIMP_PG_TOKEN_FILE", "~/.pgfarm_token"))).read().strip()
    if secret.startswith("ey") and secret.count(".") == 2:
        return secret
    req = urllib.request.Request(
        "https://pgfarm.library.ucdavis.edu/auth/service-account/login",
        data=json.dumps({"username": os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
                         "secret": secret}).encode(),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())["access_token"]


def _conn():
    import psycopg2
    return psycopg2.connect(
        host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
        dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30)


HEARTBEAT_DDL = """
CREATE TABLE IF NOT EXISTS delimp_claude_heartbeat (
    node         TEXT PRIMARY KEY,
    host         TEXT,
    drives       TEXT,
    status       TEXT,
    current_lane TEXT,
    items_left   INTEGER,
    note         TEXT,
    updated_at   TIMESTAMPTZ DEFAULT now()
)"""
MESSAGES_DDL = """
CREATE TABLE IF NOT EXISTS delimp_claude_messages (
    id         BIGSERIAL PRIMARY KEY,
    from_node  TEXT,
    to_node    TEXT,                 -- a node name or 'ALL'
    body       TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    acked_at   TIMESTAMPTZ
)"""


def initdb():
    c = _conn(); cur = c.cursor()
    cur.execute(HEARTBEAT_DDL); cur.execute(MESSAGES_DDL); c.commit()
    print("[claude_bus] tables ready: delimp_claude_heartbeat, delimp_claude_messages")
    c.close()


def checkin(a):
    c = _conn(); cur = c.cursor()
    cur.execute(HEARTBEAT_DDL); cur.execute(MESSAGES_DDL)
    cur.execute("""
        INSERT INTO delimp_claude_heartbeat (node,host,drives,status,current_lane,items_left,note,updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s, now())
        ON CONFLICT (node) DO UPDATE SET
          host=COALESCE(EXCLUDED.host, delimp_claude_heartbeat.host),
          drives=COALESCE(EXCLUDED.drives, delimp_claude_heartbeat.drives),
          status=EXCLUDED.status, current_lane=EXCLUDED.current_lane,
          items_left=EXCLUDED.items_left, note=EXCLUDED.note, updated_at=now()
    """, (a.node, a.host, a.drives, a.status, a.lane, a.left, a.note))
    c.commit(); print(f"[claude_bus] {a.node} checked in: {a.status} | {a.lane} | left={a.left}")
    c.close()


def who():
    c = _conn(); cur = c.cursor()
    cur.execute(HEARTBEAT_DDL)
    cur.execute(f"""
        SELECT node, status, current_lane, items_left, note,
               updated_at, EXTRACT(EPOCH FROM (now()-updated_at))/60.0 AS age_min
        FROM delimp_claude_heartbeat ORDER BY updated_at DESC""")
    rows = cur.fetchall()
    if not rows:
        print("[claude_bus] no heartbeats yet"); c.close(); return
    print(f"{'node':10} {'live':6} {'status':8} {'age':>8}  lane / note")
    for node, status, lane, left, note, ts, age in rows:
        live = "DOWN" if age > STALE_MIN else "alive"
        flag = "🔴" if age > STALE_MIN else "🟢"
        print(f"{node:10} {flag}{live:5} {str(status or ''):8} {age:6.0f}m  {lane or ''} | left={left} | {note or ''}")
    c.close()


def post(a):
    c = _conn(); cur = c.cursor()
    cur.execute(MESSAGES_DDL)
    cur.execute("INSERT INTO delimp_claude_messages (from_node,to_node,body) VALUES (%s,%s,%s) RETURNING id",
                (a.frm, a.to, a.body))
    print(f"[claude_bus] msg #{cur.fetchone()[0]} {a.frm}->{a.to}"); c.commit(); c.close()


def inbox(a):
    c = _conn(); cur = c.cursor()
    cur.execute(MESSAGES_DDL)
    cur.execute("""SELECT id,from_node,to_node,body,created_at FROM delimp_claude_messages
                   WHERE (to_node=%s OR to_node='ALL') AND acked_at IS NULL ORDER BY created_at""", (a.node,))
    rows = cur.fetchall()
    for mid, frm, to, body, ts in rows:
        print(f"  #{mid} [{ts:%Y-%m-%d %H:%M}] {frm}->{to}: {body}")
    if not rows:
        print("  (no unread messages)")
    if a.ack and rows:
        cur.execute("UPDATE delimp_claude_messages SET acked_at=now() WHERE id = ANY(%s)", ([r[0] for r in rows],))
        c.commit(); print(f"  [acked {len(rows)}]")
    c.close()


def main():
    # Windows consoles default to cp1252 → the 🟢/🔴 flags in who() crash with UnicodeEncodeError.
    # Reconfigure stdout to utf-8 (replace on any holdout) so the bus works on every node. (win-forge)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("initdb")
    ci = sub.add_parser("checkin"); ci.add_argument("node")
    ci.add_argument("--host", default=None); ci.add_argument("--drives", default=None)
    ci.add_argument("--status", default="ACTIVE"); ci.add_argument("--lane", default=None)
    ci.add_argument("--left", type=int, default=None); ci.add_argument("--note", default=None)
    sub.add_parser("who")
    po = sub.add_parser("post"); po.add_argument("frm"); po.add_argument("to"); po.add_argument("body")
    ib = sub.add_parser("inbox"); ib.add_argument("node"); ib.add_argument("--ack", action="store_true")
    a = ap.parse_args()
    {"initdb": lambda: initdb(), "checkin": lambda: checkin(a), "who": lambda: who(),
     "post": lambda: post(a), "inbox": lambda: inbox(a)}[a.cmd]()


if __name__ == "__main__":
    main()
