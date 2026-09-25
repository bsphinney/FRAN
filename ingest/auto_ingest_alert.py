"""auto_ingest_alert.py -- one Slack message when auto-ingest is stuck.

The week of 2026-09-17 produced twelve consecutive "0 ingested" runs and nobody found out, because
the only record was a log file nobody reads. auto_ingest_state.record_run() decides WHEN a message
is due (N consecutive runs with work and no progress; once per episode, again after 24 h); this
module only knows how to deliver it.

Rules copied from STAN's notifier (stan/notify.py), for the same reasons:

  1. NEVER RAISE. This runs at the end of an ingest job whose real work is already done. A dead
     webhook, a DNS blip or a Slack outage costs a log line, not a failed job.
  2. THE WEBHOOK URL IS A BEARER CREDENTIAL. Anyone holding it can post into the channel, and the
     job log lands on a group-readable share. It is never printed, never returned, and scrubbed out
     of every error string -- urllib puts the URL it was given into some of its exception text.
  3. Only https://hooks.slack.com/ is ever posted to. Anything else in the file is treated as "not
     configured": a typo'd or planted value must not become a channel that exfiltrates the run
     summary.
  4. A short timeout (<= 10 s), so a hung Slack cannot hold a SLURM allocation.

The webhook is read at RUN time from a file the cron's user (brettsp) can read, so rotating it
needs no redeploy and it never appears in the code, the environment of other jobs, or the repo.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request

WEBHOOK_FILE = os.environ.get("FRAN_SLACK_WEBHOOK_FILE",
                              "/quobyte/proteomics-grp/.config/skill_slack_webhook")
TIMEOUT_S = 10
_PREFIX = "https://hooks.slack.com/"
_HOOK_RE = re.compile(r"https?://hooks\.slack\.com/\S*")
# The path alone is as good as the URL to anyone who knows the host, and an error body can echo
# back just the path -- so a bare /services/T…/B…/… is scrubbed too, as the skill's notifier does.
_PATH_RE = re.compile(r"/services/T[A-Za-z0-9]+/B[A-Za-z0-9]+/[A-Za-z0-9]+")


def scrub(text, hook: str | None = None) -> str:
    """Remove the webhook from text: the exact URL we used, its path, each long path segment (Slack's
    token segment is 24 characters), and anything else shaped like a webhook or its path. Mirrors
    _scrub in the skill's scripts/notify_slack.py."""
    out = str(text)
    if hook:
        out = out.replace(hook, "<webhook>")
        try:
            path = urllib.parse.urlsplit(hook).path
        except ValueError:
            path = ""
        if len(path) > 8:
            out = out.replace(path, "<webhook>")
        for seg in path.split("/"):
            if len(seg) >= 12:
                out = out.replace(seg, "<token>")
    out = _HOOK_RE.sub("<webhook>", out)
    return _PATH_RE.sub("<webhook>", out)


def read_webhook(path: str | None = None):
    """(url, None) or (None, why-not). `why-not` names the file, never its contents."""
    p = path or WEBHOOK_FILE
    try:
        with open(p, encoding="utf-8") as fh:
            url = fh.read().strip()
    except FileNotFoundError:
        return None, f"no webhook file at {p}"
    except OSError as e:
        return None, f"cannot read webhook file {p} ({type(e).__name__})"
    if not url:
        return None, f"webhook file {p} is empty"
    if not url.startswith(_PREFIX):
        return None, f"webhook file {p} does not hold a {_PREFIX} URL; not posting"
    return url, None


def post(text: str, hook: str | None = None, timeout: float = TIMEOUT_S, _urlopen=None):
    """POST one plain-text message. Returns (sent, note); never raises; note never holds the URL.

    `_urlopen` is for tests only."""
    try:
        if hook is None:
            hook, why = read_webhook()
            if hook is None:
                return False, why
        if not hook.startswith(_PREFIX):
            return False, "refusing to post to a non-Slack URL"
        opener = _urlopen or urllib.request.urlopen
        req = urllib.request.Request(hook, data=json.dumps({"text": text}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with opener(req, timeout=min(float(timeout), TIMEOUT_S)) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            if 200 <= int(status) < 300:
                return True, "sent"
            return False, f"Slack answered HTTP {status}"
    except Exception as e:  # noqa: BLE001 -- a notifier never takes down its caller
        body = ""
        try:
            if hasattr(e, "read"):
                body = " " + e.read().decode("utf-8", "replace")[:120]
        except Exception:  # noqa: BLE001
            pass
        return False, scrub(f"Slack post failed: {type(e).__name__}: {e}{body}", hook)


def stuck_text(host: str, runs: int, since: str | None, eligible: int | None, history: list,
               last_abort: str | None, n_quarantined: int, log_hint: str | None) -> str:
    """The message. The first line has to stand alone on a phone lock screen."""
    q = "unknown (the scan failed)" if eligible is None else f"{eligible} eligible"
    lines = [f":warning: FRAN auto-ingest is stuck: {runs} consecutive runs with work to do and "
             f"nothing ingested ({q}).",
             f"host {host}; stuck since {since or 'this run'}."]
    if last_abort:
        lines.append(f"Last run stopped early: {last_abort}.")
    tot = {k: sum(int(h.get(k) or 0) for h in history) for k in ("fail", "systemic", "dup", "ok")}
    lines.append(f"Last {len(history)} run(s): {tot['ok']} ingested, {tot['dup']} duplicate, "
                 f"{tot['fail']} failed, {tot['systemic']} stopped by a systemic error.")
    if n_quarantined:
        lines.append(f"{n_quarantined} candidate(s) quarantined; list them with "
                     f"`auto_ingest.py --list-quarantine`.")
    if log_hint:
        lines.append(f"Log: {log_hint}")
    return "\n".join(lines)


def compose(host: str, due: list, res: dict, eligible: int | None, stopped: str | None,
            n_quarantined: int, log_hint: str | None) -> str:
    """One message for everything due this run (see AttemptStore.record_run for the kinds)."""
    stuck = next((d for d in due if d["kind"] == "stuck"), None)
    others = [d for d in due if d["kind"] != "stuck"]
    lines = []
    if stuck:
        lines.append(stuck_text(host, res["consecutive_zero"], res.get("episode_since"), eligible,
                                res.get("history") or [], stopped, n_quarantined, None))
    if others:
        if not stuck:
            lines.append(f":warning: FRAN auto-ingest needs a person: {len(others)} item(s) it "
                         f"will not resolve on its own (host {host}).")
        for d in others:
            lines.append(f"• {d['key'].split(':', 1)[1]}: {d['detail']}" if d["kind"] == "human"
                         else f"• {d['detail']}")
    if log_hint:
        lines.append(f"Log: {log_hint}")
    return "\n".join(lines)
