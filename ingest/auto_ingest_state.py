"""auto_ingest_state.py -- what auto_ingest remembers between runs, so no candidate can wedge the head.

THE FAILURE THIS EXISTS FOR (2026-09-17 .. 09-24). Nothing was ingested for a week. Every 4-hour run
spent 1.5-1.7 h on the SAME five alphabetically-first FRAN_reports candidates: three the duplicate
guard refused (again), one that died after 4,510 s on a NotNullViolation (again), one header-only
export (again) -- "0 ingested, 3 duplicate-skipped, 2 failed, ~181 still queued", twelve times in a
row. select() sorts scan candidates by name and _run() takes chosen[:limit]; nothing remembered
that those five had already been tried, so they were picked first every time. The drop-box entries
staged on 08-26, 09-08, 09-21 and 09-23 never appeared in any log.

fran_queue already solves this for REGISTERED searches (attempts + parked, in the database). This
is the same idea for SCAN candidates, which have no row anywhere -- so the memory is a JSON file.

WHAT IS REMEMBERED, per candidate (keyed by the real path of the export directory that was chosen,
so a fresh re-export of the same search is a new key and is tried on its own merits):

    ok / duplicate  -> 'done'. Never picked again. A duplicate is the guard saying the search is
                       already in the corpus; re-parsing a multi-GB report every 4 h to hear that
                       again is the cost this file exists to stop.
    fail            -> 'backoff' for 4 h, then 1 d (--backoff-hours), and 'quarantined' after
                       --max-failures (default 3). Quarantine is permanent until a human runs
                       `auto_ingest.py --clear <key>`.
    systemic        -> 'deferred' for one backoff step, attempts NOT charged. See classify_failure:
                       a stale deploy or an unreachable database fails EVERY candidate identically,
                       and charging those to the candidates would quarantine the whole backlog within
                       three runs of one bad scp. But a candidate deferred SYSTEMIC_REPEAT_CHARGE
                       times while other candidates kept succeeding is charged like any failure: by
                       then the "systemic" cause is evidently its own.

A candidate that is not yet eligible is simply left out of the run, so it cannot occupy a slot.

WHY A FILE AND NOT THE DATABASE. No schema migration, and the memory must still work when the thing
that is broken IS the database. It lives beside the logs on Quobyte.

CONCURRENCY -- MEASURED, NOT ASSUMED (2026-09-24, two SLURM jobs on hive-as-11-2-48 and
hive-as-11-4-42, 400 writes each to one file on /quobyte):

  * flock() does NOT exclude across nodes on Quobyte. The flock'd phase lost 578 of 800 updates,
    the same as a control with no lock at all (567).
  * A read on one node shortly after a write-tmp + fsync + rename on the other can return a TORN
    document (JSON ending mid-string). Treating that as corruption and starting clean -- what this
    file first did -- turned a transient read into the loss of the whole memory.

So: the lock is a DIRECTORY (os.mkdir is one atomic create on the metadata server, whichever node
asks), broken only when older than STALE_LOCK_S by renaming it away (only one breaker can win a
rename). A read that is short of the file's size or does not parse is RETRIED before the file is
ever called corrupt, and a corrupt file is moved aside, never deleted. Re-measured after the change
with the same two-node test: see DEPLOY_auto_ingest.md. Every candidate is additionally LEASED by
its output_dir identity, so two overlapping runs cannot corpus_ingest the same search at once.

In production only one job runs at a time (cron_auto_ingest.sh), consecutive runs are hours apart,
and this matters only when that guard fails -- which it has done once already.

NEVER TAKES DOWN THE INGEST. Any IO failure is reported as a warning, recorded in `self.degraded`,
and the run continues from what is in memory.
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import socket
import time
from datetime import datetime, timedelta, timezone

DEFAULT_STATE_FILE = os.environ.get(
    "FRAN_AUTO_INGEST_STATE",
    "/quobyte/proteomics-grp/de-limp/fran_refresh/state/auto_ingest_attempts.json")
DEFAULT_BACKOFF_HOURS = (4.0, 24.0, 72.0)
DEFAULT_MAX_FAILURES = 3
DEFAULT_STUCK_RUNS = 3
DEFAULT_REALERT_HOURS = 24.0
SYSTEMIC_REPEAT_CHARGE = 3      # deferrals after which a "systemic" candidate is charged anyway
LOCK_WAIT_S = 120
STALE_LOCK_S = 60               # a read-modify-write takes milliseconds; a lock this old is dead
READ_RETRIES = 6
ERROR_TAIL_CHARS = 1500
HISTORY_KEEP = 12
SCHEMA = 2

DONE, BACKOFF, DEFERRED, QUARANTINED = "done", "backoff", "deferred", "quarantined"
ELIGIBLE, LEASED = "eligible", "leased"

# Failures that say nothing about the candidate, with how far they reach. Matched narrowly and ONLY
# against what actually killed the process -- the last traceback and the last few lines of output --
# because a false match means a genuinely bad candidate is never charged (bounded by
# SYSTEMIC_REPEAT_CHARGE). A stray "ImportError" in a warning earlier in the output must not count,
# and a mid-COPY disconnect is deliberately NOT here: a huge candidate can cause that itself.
#
#   global : nothing else in this run can succeed either -> stop the run.
#   engine : an import failed. Imports are engine-specific more often than not (spectronaut_to_corpus,
#            radiant_to_corpus, raw_metadata, ...), so only that engine's remaining candidates are
#            held back and the others carry on. A failure that really is global blocks each engine at
#            its first candidate, which comes to the same thing.
_SYSTEMIC = (
    (re.compile(r"(?m)^REFUSING TO INGEST"), "global",
     "ingest code is stale on this host (the manifest gate refused)"),
    # Only the two phrases that mean "the command line itself is wrong". A bare `usage:` line, or
    # "error: argument --taxon: invalid int value", can come from a bad value in candidate data --
    # that is the candidate's failure and is charged like one.
    (re.compile(r"\berror: (?:unrecognized arguments|the following arguments are required)"),
     "global",
     "corpus_ingest rejected auto_ingest's command line (the two files are out of step)"),
    (re.compile(r"(?m)^(?:\w+\.)*OperationalError: (?:could not connect to server|"
                r"connection to server at .* failed|could not translate host name)"), "global",
     "the database is unreachable or refused the login"),
    (re.compile(r'(?m)^\s*File "[^"]*", line \d+, in _token\s*$'), "global",
     "the PG Farm token exchange failed"),
    (re.compile(r"(?m)^(?:\w+\.)*(?:ModuleNotFoundError|ImportError): "), "engine",
     "a module failed to import (incomplete deploy?)"),
)
_NO_MODULE = re.compile(r"No module named '([^']+)'|cannot import name '([^']+)'")


def classify_failure(stdout: str, stderr: str):
    """(scope, reason) if a failed ingest died of a SYSTEMIC cause, else (None, None)."""
    err = stderr or ""
    i = err.rfind("Traceback (most recent call last):")
    lines = [ln for ln in ((stdout or "") + "\n" + err).splitlines() if ln.strip()]
    focus = (err[i:] if i != -1 else "") + "\n" + "\n".join(lines[-6:])
    for pat, scope, why in _SYSTEMIC:
        if pat.search(focus):
            m = _NO_MODULE.search(focus) if scope == "engine" else None
            if m:
                why = f"module {(m.group(1) or m.group(2))!r} failed to import (incomplete deploy?)"
            return scope, why
    return None, None


_clock = time.time      # tests replace this to step through hours of backoff in milliseconds


def _utc(now: float | None = None) -> datetime:
    return datetime.fromtimestamp(_clock() if now is None else now, tz=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(s) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _empty() -> dict:
    return {"schema": SCHEMA, "candidates": {}, "leases": {},
            "runs": {"consecutive_zero": 0, "history": [], "in_progress": {},
                     "engine_blocked": {}, "last_success_at": None,
                     "last_success_by_engine": {}},
            "alerts": {}}


def identity_of(c: dict) -> str:
    """The output_dir a candidate would be ingested as -- what leases and per-run de-duplication
    key on. One definition: the gate and auto_ingest._ident both use it, so a trailing slash can
    never make a leased output_dir look free."""
    return str(c.get("identity") or c.get("dir") or "").rstrip("/")


class _Torn(Exception):
    """A read returned fewer bytes than the file holds."""


class AttemptStore:
    def __init__(self, path: str = DEFAULT_STATE_FILE,
                 backoff_hours=DEFAULT_BACKOFF_HOURS,
                 max_failures: int = DEFAULT_MAX_FAILURES,
                 lock_wait: float = LOCK_WAIT_S,
                 stale_lock: float = STALE_LOCK_S):
        self.path = path
        self.lock_dir = path + ".lockd"
        self.backoff_hours = tuple(float(h) for h in backoff_hours) or DEFAULT_BACKOFF_HOURS
        self.max_failures = max(1, int(max_failures))
        self.lock_wait = lock_wait
        self.stale_lock = stale_lock
        self.degraded: str | None = None     # set when the file could not be read or written
        self._mem: dict | None = None        # last good copy, used when the file cannot be read
        self._token: str | None = None       # owner token of the lock this process holds
        self._break_hook = None              # tests only: called between judging and breaking

    # ---- file IO --------------------------------------------------------------------------------

    def _warn(self, msg: str) -> None:
        self.degraded = msg
        print(f"  WARNING (attempt memory): {msg}", flush=True)

    def _read_once(self) -> dict:
        with open(self.path, "rb") as fh:
            raw = fh.read()
            size = os.fstat(fh.fileno()).st_size
        if len(raw) != size:
            raise _Torn(f"read {len(raw)} of {size} bytes")
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("candidates"), dict):
            raise ValueError("not an attempt-memory document")
        return data

    def load(self) -> dict:
        """Current state. Missing = fresh start. Unreadable is RETRIED first (a cross-node read can
        be torn -- see the module docstring); only a file that stays unparseable is moved aside."""
        data, last = None, None
        for attempt in range(READ_RETRIES):
            try:
                data = self._read_once()
                break
            except FileNotFoundError:
                data = _empty()
                break
            except (ValueError, UnicodeDecodeError, _Torn) as e:
                last = e
                time.sleep(0.2 * (attempt + 1))
            except OSError as e:
                self._warn(f"cannot read {self.path} ({e}); using the in-memory copy")
                data = self._mem if self._mem is not None else _empty()
                break
        if data is None:
            aside = f"{self.path}.corrupt-{int(time.time())}"
            try:
                os.replace(self.path, aside)
                self._warn(f"state file unreadable after {READ_RETRIES} tries ({last}); moved to "
                           f"{aside} (kept) and starting clean -- failed candidates will be retried "
                           f"once")
            except OSError as e2:
                self._warn(f"state file unreadable ({last}) and could not be moved aside ({e2})")
            data = _empty()
        base = _empty()
        for k, v in base.items():
            data.setdefault(k, v)
        for k, v in base["runs"].items():
            data["runs"].setdefault(k, v)
        self._mem = data
        return data

    def _save(self, data: dict) -> bool:
        d = os.path.dirname(self.path) or "."
        tmp = f"{self.path}.tmp.{socket.gethostname()}.{os.getpid()}"
        try:
            os.makedirs(d, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            return True
        except OSError as e:
            self._warn(f"could not write {self.path} ({e}); this run's outcomes are NOT remembered "
                       f"and failed/duplicate candidates will be re-picked next run")
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False

    def _read_owner(self, d: str):
        try:
            with open(os.path.join(d, "owner"), encoding="utf-8") as fh:
                return fh.read().strip() or None
        except OSError:
            return None

    def _lock(self) -> bool:
        """Take the directory lock. True if held; False means proceeding unlocked (warned).

        Each acquisition writes a unique owner token into the lock directory. Breaking a stale lock
        is judge -> rename -> VERIFY: the breaker notes whose lock it judged dead, renames the
        directory away, and checks the renamed directory still carries that owner. If it does not,
        the lock changed hands in between (A died, C broke A's lock and took a fresh one, and B --
        who judged A's -- renamed C's live lock); B puts it back and waits like anyone else. And
        _unlock() removes the directory only if it is still ours."""
        try:
            os.makedirs(os.path.dirname(self.lock_dir) or ".", exist_ok=True)
        except OSError as e:
            self._warn(f"cannot create {os.path.dirname(self.lock_dir)} ({e}); writing unlocked")
            return False
        me = f"{socket.gethostname()}:{os.getpid()}"
        deadline = time.monotonic() + self.lock_wait
        while True:
            token = f"{me}:{time.time_ns()}"
            try:
                os.mkdir(self.lock_dir)
            except FileExistsError:
                pass
            except OSError as e:
                self._warn(f"cannot lock {self.lock_dir} ({e}); writing unlocked")
                return False
            else:
                try:
                    with open(os.path.join(self.lock_dir, "owner"), "w", encoding="utf-8") as fh:
                        fh.write(token + "\n")
                    self._token = token
                except OSError:
                    self._token = None            # held, but unmarked: see _unlock
                return True
            try:
                age = time.time() - os.stat(self.lock_dir).st_mtime
            except OSError:
                continue                          # released between our mkdir and stat; retry
            if age > self.stale_lock:
                judged = self._read_owner(self.lock_dir)
                if self._break_hook is not None:
                    self._break_hook(judged)      # tests: interleave another process here
                grave = f"{self.lock_dir}.stale.{me.replace(':', '.')}.{time.time_ns()}"
                try:
                    os.rename(self.lock_dir, grave)
                except OSError:
                    continue                      # someone else broke or released it first
                if self._read_owner(grave) == judged:
                    shutil.rmtree(grave, ignore_errors=True)
                    print(f"  attempt memory: broke a lock {age:.0f}s old held by {judged} "
                          f"({self.lock_dir})", flush=True)
                else:
                    try:
                        os.rename(grave, self.lock_dir)
                        print(f"  attempt memory: the lock changed hands while being broken; "
                              f"restored it to its live holder ({self.lock_dir})", flush=True)
                    except OSError as e:
                        self._warn(f"moved a live lock aside by mistake and could not restore it "
                                   f"({e}); it is at {grave}")
                continue
            if time.monotonic() >= deadline:
                self._warn(f"lock {self.lock_dir} still held after {self.lock_wait:.0f}s; "
                           f"writing unlocked")
                return False
            time.sleep(0.05 + random.random() * 0.1)

    def _unlock(self) -> None:
        """Release the lock -- only if it is still OURS. A write that outlived STALE_LOCK_S may have
        had its lock broken and re-taken; removing the directory then would free someone else's."""
        owner = self._read_owner(self.lock_dir)
        if owner == self._token:                  # ours (or unmarked ours: both None)
            shutil.rmtree(self.lock_dir, ignore_errors=True)
        else:
            print(f"  WARNING (attempt memory): lock {self.lock_dir} now belongs to {owner}, not "
                  f"to this run; left in place", flush=True)
        self._token = None

    def update(self, fn):
        """Read-modify-write under the lock. `fn(data)` mutates data and returns a result."""
        held = self._lock()
        try:
            data = self.load()
            out = fn(data)
            self._save(data)
            self._mem = data
            return out
        finally:
            if held:
                self._unlock()

    # ---- per-candidate --------------------------------------------------------------------------

    def status(self, key: str, now: float | None = None, data: dict | None = None):
        """(status, record) for a candidate key as of `now`."""
        data = data if data is not None else self.load()
        rec = data["candidates"].get(key)
        if not isinstance(rec, dict) or not rec:
            return ELIGIBLE, None
        st = rec.get("status")
        if st in (DONE, QUARANTINED):
            return st, rec
        nxt = _parse(rec.get("next_eligible"))
        if st in (BACKOFF, DEFERRED) and nxt and nxt > _utc(now):
            return st, rec
        return ELIGIBLE, rec

    def _live_lease(self, data: dict, identity: str | None, now: float | None):
        lease = data["leases"].get(identity) if identity else None
        until = _parse((lease or {}).get("until"))
        return lease if (lease and until and until > _utc(now)) else None

    def gate(self, cands, now: float | None = None):
        """Split candidates into (eligible, held). held = [(candidate, status, record)].

        Scan candidates (with an 'attempt_key') are held while backed off, deferred, quarantined or
        done. ANY candidate -- queue rows too -- is held while another run holds a live lease on
        its output_dir."""
        data = self.load()
        eligible, held = [], []
        for c in cands:
            if self._live_lease(data, identity_of(c), now):
                held.append((c, LEASED, None))
                continue
            key = c.get("attempt_key")
            if not key:
                eligible.append(c)
                continue
            st, rec = self.status(key, now, data)
            (eligible.append(c) if st == ELIGIBLE else held.append((c, st, rec)))
        return eligible, held

    def claim(self, identity: str, owner: str, lease_s: float, attempt_key: str | None = None,
              now: float | None = None):
        """Lease an output_dir for this run. Returns (claimed, why_not).

        Keyed by IDENTITY (the output_dir corpus_ingest will write), whatever kind of candidate --
        scan, drop box or queue row -- asks, so two runs can never corpus_ingest one search at once.

        An EXPIRED lease that was never released means the run holding it died mid-ingest -- the
        SLURM job hit its time or memory limit, which kills this process and not just the ingest
        subprocess. That is charged to the candidate that held it as an "abandoned" attempt.
        Without it, a candidate that reliably kills the whole job would come back at the head of
        every run forever, which is the starvation this file exists to end."""
        def fn(data):
            t = _utc(now)
            lease = data["leases"].get(identity)
            if lease:
                until = _parse(lease.get("until"))
                if until and until > t and lease.get("owner") != owner:
                    return False, "being ingested by another run"
                if until and until <= t:
                    data["leases"].pop(identity, None)
                    ak = lease.get("attempt_key")
                    if ak:
                        rec = data["candidates"].setdefault(ak, {"attempts": 0})
                        self._charge(rec, "abandoned", "a previous run died while ingesting this "
                                     f"candidate (lease held by {lease.get('owner')}, never "
                                     f"released)", t)
                        if ak == attempt_key:
                            return False, f"{rec['status']} (a previous run died while ingesting it)"
            if attempt_key:
                st, _ = self.status(attempt_key, now, data)
                if st != ELIGIBLE:
                    return False, st
            data["leases"][identity] = {"owner": owner, "attempt_key": attempt_key,
                                        "until": _iso(t + timedelta(seconds=lease_s))}
            return True, None
        return self.update(fn)

    def release(self, identity: str, owner: str) -> None:
        def fn(data):
            lease = data["leases"].get(identity)
            if lease and lease.get("owner") == owner:
                data["leases"].pop(identity, None)
        self.update(fn)

    def _charge(self, rec: dict, outcome: str, error_tail: str, t: datetime) -> None:
        """One failed attempt: back off, or quarantine once max_failures is reached."""
        rec.setdefault("first_attempt", _iso(t))
        rec["last_attempt"] = _iso(t)
        rec["last_outcome"] = outcome
        n = int(rec.get("attempts", 0)) + 1
        rec["attempts"] = n
        rec["last_error_tail"] = (error_tail or "")[-ERROR_TAIL_CHARS:]
        if n >= self.max_failures:
            rec["status"] = QUARANTINED
            rec["next_eligible"] = None
            rec["quarantined_at"] = _iso(t)
        else:
            h = self.backoff_hours[min(n - 1, len(self.backoff_hours) - 1)]
            rec["status"] = BACKOFF
            rec["next_eligible"] = _iso(t + timedelta(hours=h))

    def record(self, key: str, outcome: str, error_tail: str = "", meta: dict | None = None,
               now: float | None = None, reason: str | None = None,
               scope: str | None = None) -> dict:
        """Remember one outcome: 'ok' | 'duplicate' | 'fail' | 'timeout' | 'systemic'.

        For 'systemic', `scope` is classify_failure's: after SYSTEMIC_REPEAT_CHARGE deferrals the
        candidate is charged if OTHERS succeeded since its first deferral -- for an 'engine'-scoped
        failure, others OF THE SAME ENGINE. A missing adapter module fails every candidate of its
        engine; judged against other engines' successes it would slowly quarantine that engine's
        whole backlog, one head at a time."""
        def fn(data):
            t = _utc(now)
            rec = data["candidates"].setdefault(key, {"attempts": 0})
            rec.update({k: v for k, v in (meta or {}).items() if v is not None})
            if outcome == "systemic":
                n = int(rec.get("systemic_deferrals", 0))
                first = _parse(rec.get("first_deferral"))
                runs = data["runs"]
                won = _parse(runs.get("last_success_by_engine", {}).get(rec.get("engine"))
                             if scope == "engine" else runs.get("last_success_at"))
                if n >= SYSTEMIC_REPEAT_CHARGE and first and won and won > first:
                    self._charge(rec, "systemic-repeat",
                                 f"deferred {n}x as systemic while other candidates succeeded -- "
                                 f"charged as its own failure. {reason or ''}\n{error_tail or ''}", t)
                    return dict(rec)
            if outcome not in ("ok", "duplicate", "systemic"):
                self._charge(rec, outcome, error_tail, t)
                return dict(rec)
            rec.setdefault("first_attempt", _iso(t))
            rec["last_attempt"] = _iso(t)
            rec["last_outcome"] = outcome
            if outcome == "systemic":
                rec["status"] = DEFERRED
                rec["systemic_deferrals"] = int(rec.get("systemic_deferrals", 0)) + 1
                rec.setdefault("first_deferral", _iso(t))
                rec["last_error_tail"] = ((reason or "") + "\n" +
                                          (error_tail or ""))[-ERROR_TAIL_CHARS:]
                rec["next_eligible"] = _iso(t + timedelta(hours=self.backoff_hours[0]))
            else:
                rec["status"] = DONE
                rec["next_eligible"] = None
                rec.pop("last_error_tail", None)
            return dict(rec)
        return self.update(fn)

    def by_status(self, status: str, data: dict | None = None):
        data = data if data is not None else self.load()
        return sorted((k, r) for k, r in data["candidates"].items() if r.get("status") == status)

    def clear(self, token: str):
        """Forget one candidate so the next run may pick it. `token` is the exact key or a UNIQUE
        substring of one. Returns (cleared_key, None) or (None, [candidate keys]) when ambiguous or
        not found -- never clears more than one."""
        def fn(data):
            if token in data["candidates"]:
                hits = [token]
            else:
                hits = [k for k in data["candidates"] if token in k]
            if len(hits) != 1:
                return None, hits
            data["candidates"].pop(hits[0])
            return hits[0], None
        return self.update(fn)

    # ---- per-run: the stuck detector ------------------------------------------------------------

    def begin_run(self, owner: str, now: float | None = None):
        """Note that a run started. Returns [(owner, started)] of earlier runs that never finished.

        Each of those is counted as a no-progress run: a job killed at the 8 h SLURM wall never
        reaches record_run, and without this a run that is killed every time would never count."""
        def fn(data):
            t = _utc(now)
            runs = data["runs"]
            prog = runs.setdefault("in_progress", {})
            dead = [(o, s) for o, s in prog.items() if o != owner]
            for o, s in dead:
                prog.pop(o, None)
                runs["consecutive_zero"] = int(runs.get("consecutive_zero", 0)) + 1
                runs.setdefault("history", []).append(
                    {"at": _iso(t), "progress": 0, "eligible": None, "killed": o, "started": s})
                del runs["history"][:-HISTORY_KEEP]
            prog[owner] = _iso(t)
            return dead
        return self.update(fn)

    def record_run(self, progress: int, n_eligible: int | None, summary: dict,
                   stuck_runs: int = DEFAULT_STUCK_RUNS,
                   realert_hours: float = DEFAULT_REALERT_HOURS,
                   now: float | None = None, owner: str | None = None,
                   engines_progressed=(), blocked_engines: dict | None = None,
                   needs_human: dict | None = None) -> dict:
        """Count this run; decide which alerts are due. Returns
        {consecutive_zero, alert_due, episode_since, history, due: [{key, kind, detail}]}.

        stuck      consecutive runs that had work and made no progress. progress = ingested +
                   duplicates resolved: a duplicate is now resolved exactly once, so a run that
                   clears five of them has moved the backlog; counting only ingests would page
                   someone whenever the (mostly re-export) FRAN_reports backlog served three runs of
                   duplicates. n_eligible None (the scan failed, the run crashed) counts as "work
                   exists". A run with NO eligible work is silent: it neither advances nor resets
                   the count. Progress ends the episode.
        engine:X   engine X's candidates were blocked by an import failure in this many consecutive
                   runs that tried it, even while other engines progressed -- otherwise one broken
                   adapter could hide behind the others' progress forever.
        human:K    a candidate only a person can resolve (a manifest contradicting its own search).
                   Due when first seen and again every `realert_hours` while it persists.
        Each alert is due once per episode and again every `realert_hours` while it lasts."""
        blocked_engines = blocked_engines or {}
        needs_human = needs_human or {}

        def due_key(alerts, key, t):
            a = alerts.setdefault(key, {"since": _iso(t), "last_sent": None, "n_sent": 0})
            last = _parse(a.get("last_sent"))
            return last is None or (t - last) >= timedelta(hours=realert_hours)

        def fn(data):
            t = _utc(now)
            runs, alerts = data["runs"], data["alerts"]
            if owner:
                runs.setdefault("in_progress", {}).pop(owner, None)
            hist = runs.setdefault("history", [])
            hist.append({"at": _iso(t), "progress": progress, "eligible": n_eligible, **summary})
            del hist[:-HISTORY_KEEP]
            runs["last_run"] = _iso(t)
            due = []
            if n_eligible == 0:
                pass
            elif progress > 0:
                runs["consecutive_zero"] = 0
                runs["last_success_at"] = _iso(t)
                alerts.pop("stuck", None)
            else:
                runs["consecutive_zero"] = int(runs.get("consecutive_zero", 0)) + 1
                if runs["consecutive_zero"] >= stuck_runs and due_key(alerts, "stuck", t):
                    due.append({"key": "stuck", "kind": "stuck",
                                "detail": f"{runs['consecutive_zero']} consecutive runs"})
            eb = runs.setdefault("engine_blocked", {})
            for eng in engines_progressed:
                runs.setdefault("last_success_by_engine", {})[eng] = _iso(t)
                eb.pop(eng, None)
                alerts.pop(f"engine:{eng}", None)
            for eng, why in blocked_engines.items():
                rec = eb.setdefault(eng, {"n": 0})
                rec["n"] = int(rec.get("n", 0)) + 1
                rec["reason"] = why
                if rec["n"] >= stuck_runs and due_key(alerts, f"engine:{eng}", t):
                    due.append({"key": f"engine:{eng}", "kind": "engine",
                                "detail": f"{eng} searches blocked in {rec['n']} consecutive runs: "
                                          f"{why}"})
            for k in [k for k in alerts if k.startswith("human:")]:
                if k[len("human:"):] not in needs_human:
                    alerts.pop(k)                 # resolved (repaired, removed, or ingested)
            for item, why in sorted(needs_human.items()):
                if due_key(alerts, f"human:{item}", t):
                    due.append({"key": f"human:{item}", "kind": "human", "detail": why})
            st = alerts.get("stuck") or {}
            return {"consecutive_zero": runs.get("consecutive_zero", 0),
                    "alert_due": any(d["kind"] == "stuck" for d in due),
                    "episode_since": st.get("since"), "due": due,
                    "history": list(hist[-max(stuck_runs, 1):])}
        return self.update(fn)

    def mark_alert_sent(self, now: float | None = None, keys=("stuck",)) -> None:
        """Only after the post actually landed -- an unsent alert must be retried next run."""
        def fn(data):
            t = _utc(now)
            for k in keys:
                a = data["alerts"].setdefault(k, {"since": _iso(t), "n_sent": 0})
                a["last_sent"] = _iso(t)
                a["n_sent"] = int(a.get("n_sent", 0)) + 1
        self.update(fn)
