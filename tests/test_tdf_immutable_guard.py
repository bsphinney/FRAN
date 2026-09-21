"""Repo-wide guard: no Python file may open a Bruker tdf unsafely.

A read-write ``sqlite3.connect`` on an ``analysis.tdf`` checkpoints any stale
mid-acquisition WAL into the file and truncates it — the frame index is gone
and the acquisition is unrecoverable. 350 ``.d`` on the cluster were destroyed
this way before it was caught. A plain ``mode=ro`` open does not truncate but
reads *through* the stale WAL and drops an shm inside the raw ``.d``. Only
``?mode=ro&immutable=1`` is correct. See ``ingest/tdf_safe.py``.

FRAN walks tens of thousands of ``.d`` on shared storage during ingest, so a
single bad open here is multiplied across the corpus. Code review does not
catch this reliably — it looks like an ordinary database open — so this walks
the whole repository instead and fails on any tdf open not routed through a
recognised constructor in ``ingest/tdf_safe.py``.

**The exemption is per call site, not per file.** A fixture that fabricates a
synthetic tdf genuinely needs to write one, and declares that by building its
URI with ``synthetic_tdf_write_uri()``. A file-name allowlist would grow with
every new fixture until it exempted most of the suite; a named constructor
cannot, because every exempted open has to say so in its own argument list.

To prove the guard is live rather than merely present, this file contains a
planted offender — a real unsafe open the scanner must flag — and a test that
fails if the scanner stops flagging it.

Run:  python tests/test_tdf_immutable_guard.py   (or under pytest)
"""

import ast
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
REPO_ROOT = _HERE.parent
sys.path.insert(0, str(REPO_ROOT / "ingest"))

from tdf_safe import (  # noqa: E402
    connect_tdf,
    synthetic_tdf_write_uri,
    tdf_read_uri,
)

# Directories that are not this repo's source: vendored code, build output,
# caches, and agent worktrees, which hold whole stale copies of the tree and
# would otherwise be scanned as if they were live code.
SKIP_DIRS = {
    ".claude",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}

# The only call-argument forms allowed to reach sqlite3.connect for a tdf.
SAFE_CONSTRUCTORS = {"tdf_read_uri", "synthetic_tdf_write_uri"}

# Marks the deliberately unsafe line below. test_no_unsafe_tdf_opens_in_repo
# ignores lines carrying it; test_guard_catches_planted_offender asserts the
# scanner still reports it, and test_plant_marker_is_confined_to_this_file
# asserts nobody else can use it as an escape hatch.
PLANT_MARKER = "GUARD-TEST-PLANTED-OFFENDER"

_TDF_RE = re.compile(r"tdf", re.IGNORECASE)


class Violation:
    def __init__(self, path, lineno, source, reason):
        self.path = path
        self.lineno = lineno
        self.source = source
        self.reason = reason

    def __str__(self):
        try:
            rel = self.path.relative_to(REPO_ROOT)
        except ValueError:
            rel = self.path
        return f"{rel}:{self.lineno}: {self.reason}\n      {self.source.strip()}"


# ──────────────────────────────────────────────────────────────────────
#  Scanner
# ──────────────────────────────────────────────────────────────────────

def _sqlite_connect_names(tree):
    """Local names referring to sqlite3 / sqlite3.connect in this module.

    Tracks aliases: ``import sqlite3 as _sq`` is a real pattern in this
    codebase's sibling repo, and a guard that only grepped for the literal
    text ``sqlite3.connect`` would sail straight past it.
    """
    modules, bare = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sqlite3":
                    modules.add(alias.asname or "sqlite3")
        elif isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
            for alias in node.names:
                if alias.name == "connect":
                    bare.add(alias.asname or "connect")
    return modules, bare


def _is_connect_call(node, modules, bare):
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "connect":
        return isinstance(func.value, ast.Name) and func.value.id in modules
    return isinstance(func, ast.Name) and func.id in bare


def _database_arg(node):
    if node.args:
        return node.args[0]
    for kw in node.keywords:
        if kw.arg == "database":
            return kw.value
    return None


def _has_uri_true(node):
    for kw in node.keywords:
        if kw.arg == "uri":
            return isinstance(kw.value, ast.Constant) and kw.value.value is True
    return False


def _mentions_tdf(text):
    return bool(_TDF_RE.search(text))


def _assigns_a_tdf(scope, name):
    """True if ``name`` is assigned from something naming a tdf in this scope.

    Catches the indirect form ``p = os.path.join(d, "analysis.tdf")`` followed
    later by ``sqlite3.connect(p)``, where the connect call's own argument
    gives away nothing.
    """
    for node in ast.walk(scope):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        if node.value is not None and _mentions_tdf(ast.dump(node.value)):
            return True
    return False


def _targets_a_tdf(arg, source, enclosing, enclosing_name):
    """Decide whether this connect call's target is a Bruker tdf.

    Deliberately errs toward flagging: a false positive costs one call to
    ``connect_tdf``; a false negative costs somebody's raw data.
    """
    arg_src = ast.get_source_segment(source, arg) or ""
    if _mentions_tdf(arg_src):
        return True
    # A helper named for what it builds — _make_valid_tdf(path) — hides the
    # tdf in the function name while the argument is just `path`.
    if _mentions_tdf(enclosing_name):
        return True
    if isinstance(arg, ast.Name) and enclosing is not None:
        return _assigns_a_tdf(enclosing, arg.id)
    return False


def _verdict(call, arg):
    """Reason this open is unsafe, or None if it is fine."""
    if isinstance(arg, ast.Call):
        func = arg.func
        if isinstance(func, ast.Attribute):
            ctor = func.attr
        elif isinstance(func, ast.Name):
            ctor = func.id
        else:
            ctor = ""
        if ctor in SAFE_CONSTRUCTORS:
            if not _has_uri_true(call):
                return f"{ctor}() builds a URI but uri=True was not passed"
            return None
    if not _has_uri_true(call):
        return (
            "read-write open of a Bruker tdf — this checkpoints any stale WAL "
            "and truncates the frame index; use tdf_safe.connect_tdf()"
        )
    return (
        "tdf URI not built by tdf_safe — hand-built URIs miss immutable=1 and "
        "break on a .d path containing ? # or %; use tdf_safe.connect_tdf()"
    )


def scan_source(source, path):
    """Report every unsafe tdf open in one Python source string."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    modules, bare = _sqlite_connect_names(tree)
    if not modules and not bare:
        return []

    lines = source.splitlines()
    scopes = []
    violations = []

    def visit(node):
        pushed = False
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scopes.append((node, node.name))
            pushed = True
        if isinstance(node, ast.Call) and _is_connect_call(node, modules, bare):
            arg = _database_arg(node)
            if arg is not None:
                enclosing, enclosing_name = scopes[-1] if scopes else (tree, "")
                if _targets_a_tdf(arg, source, enclosing, enclosing_name):
                    reason = _verdict(node, arg)
                    if reason is not None:
                        line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
                        violations.append(Violation(path, node.lineno, line, reason))
        for child in ast.iter_child_nodes(node):
            visit(child)
        if pushed:
            scopes.pop()

    visit(tree)
    return violations


def scan_file(path):
    try:
        source = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return scan_source(source, Path(path))


def _python_files(root=REPO_ROOT):
    found = []
    for path in Path(root).rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        found.append(path)
    return found


def scan_repo(root=REPO_ROOT):
    violations = []
    for path in _python_files(root):
        violations.extend(scan_file(path))
    return violations


def _is_planted(v):
    return PLANT_MARKER in v.source


# ──────────────────────────────────────────────────────────────────────
#  The planted offender
# ──────────────────────────────────────────────────────────────────────

def _planted_offender_never_called(tdf):  # pragma: no cover - never executed
    """A genuinely unsafe tdf open, kept here so the guard has to catch it.

    If the scanner ever stops reporting the line below — a regex loosened, an
    AST walk that misses a nested scope, a skip-list that swallows tests/ —
    the repo-wide check would start passing vacuously. This makes that failure
    loud instead.
    """
    return sqlite3.connect(str(tdf))  # GUARD-TEST-PLANTED-OFFENDER


# ──────────────────────────────────────────────────────────────────────
#  Tests
# ──────────────────────────────────────────────────────────────────────

def test_no_unsafe_tdf_opens_in_repo():
    """Every tdf open in the repo goes through ingest/tdf_safe.py."""
    real = [v for v in scan_repo() if not _is_planted(v)]
    assert not real, (
        "%d unsafe Bruker tdf open(s) — a read-write or plain mode=ro open "
        "can destroy a .d. Route each through tdf_safe.connect_tdf():\n\n  %s"
        % (len(real), "\n  ".join(str(v) for v in real))
    )


def test_guard_catches_planted_offender():
    """The scanner reports the deliberately unsafe open in this file."""
    planted = [v for v in scan_file(__file__) if _is_planted(v)]
    assert len(planted) == 1, (
        "the planted offender was not caught — this guard is no longer "
        "guarding anything. Found: %s" % [str(v) for v in planted]
    )
    assert "read-write" in planted[0].reason


def test_plant_marker_is_confined_to_this_file():
    """Nobody else may use the plant marker to silence the guard."""
    users = [
        p for p in _python_files()
        if PLANT_MARKER in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert users == [Path(__file__).resolve()], (
        "the plant marker escaped this file and is now an exemption "
        "backdoor: %s" % [str(p) for p in users]
    )


_CASES = [
    ("import sqlite3\ncon = sqlite3.connect(str(tdf))\n", True),
    ('import sqlite3\ncon = sqlite3.connect(f"file:{tdf}?mode=ro", uri=True)\n', True),
    # Right parameters, wrong construction: breaks on ? # % in the path.
    ('import sqlite3\ncon = sqlite3.connect(f"file:{tdf}?mode=ro&immutable=1", uri=True)\n', True),
    ("import sqlite3 as _s\ncon = _s.connect(str(tdf))\n", True),
    ("from sqlite3 import connect\ncon = connect(str(tdf))\n", True),
    ('import sqlite3\nimport os\np = os.path.join(d, "analysis.tdf")\ncon = sqlite3.connect(p)\n', True),
    (
        "import sqlite3\nfrom tdf_safe import tdf_read_uri\n"
        "con = sqlite3.connect(tdf_read_uri(tdf), uri=True)\n",
        False,
    ),
    (
        "import sqlite3\nfrom tdf_safe import synthetic_tdf_write_uri\n"
        "con = sqlite3.connect(synthetic_tdf_write_uri(tdf), uri=True)\n",
        False,
    ),
    # Not a tdf: the Spectronaut XIC databases must keep working as they are.
    ('import sqlite3\ncon = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)\n', False),
    # Not a tdf: FRAN's own sqlite must stay read-write.
    ("import sqlite3\ncon = sqlite3.connect(str(db_path))\n", False),
]


def test_scanner_verdicts():
    """The scanner's rules, stated as cases rather than left implicit."""
    for snippet, expect_flagged in _CASES:
        flagged = bool(scan_source(snippet, Path("snippet.py")))
        assert flagged is expect_flagged, (
            "expected flagged=%s for:\n%s" % (expect_flagged, snippet)
        )


def test_safe_constructor_without_uri_true_is_flagged():
    """tdf_read_uri() passed as a plain path is still wrong, and caught."""
    snippet = (
        "import sqlite3\nfrom tdf_safe import tdf_read_uri\n"
        "con = sqlite3.connect(tdf_read_uri(tdf))\n"
    )
    violations = scan_source(snippet, Path("snippet.py"))
    assert len(violations) == 1
    assert "uri=True" in violations[0].reason


def test_exempted_constructor_round_trips():
    """The exemption is a working call site, not a name the guard humours.

    Builds a synthetic tdf through the exempted writer, reads it back through
    the immutable reader, and checks the immutable open left no WAL or shm
    behind in the .d.
    """
    with tempfile.TemporaryDirectory() as tmp:
        # URI metacharacters in the directory name, on purpose.
        d_dir = Path(tmp) / "round trip #1 100%.d"
        d_dir.mkdir()
        tdf = d_dir / "analysis.tdf"

        con = sqlite3.connect(synthetic_tdf_write_uri(tdf), uri=True)
        # WAL mode on purpose: real tdfs are written in WAL mode, and it is
        # what makes this assertion mean something. A plain mode=ro open of a
        # WAL-mode database drops analysis.tdf-shm and analysis.tdf-wal into
        # the .d and leaves them there; an immutable open does not.
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE GlobalMetadata (Key TEXT, Value TEXT)")
        con.execute("INSERT INTO GlobalMetadata VALUES ('InstrumentName', 'timsTOF HT')")
        con.commit()
        con.close()

        assert "%23" in tdf_read_uri(tdf), "the # in the path must be escaped"

        con = connect_tdf(tdf)
        try:
            row = con.execute(
                "SELECT Value FROM GlobalMetadata WHERE Key='InstrumentName'"
            ).fetchone()
        finally:
            con.close()
        assert row[0] == "timsTOF HT"

        leftovers = sorted(p.name for p in d_dir.iterdir() if p.name != "analysis.tdf")
        assert leftovers == [], "the immutable read wrote into the .d: %s" % leftovers


def test_read_bruker_uses_an_immutable_open():
    """ingest/raw_metadata.py reads a real .d — prove it reads one safely.

    The guard is static; this exercises the actual ingest reader against a
    synthetic .d and checks it neither truncates nor leaves anything behind.
    """
    sys.path.insert(0, str(REPO_ROOT / "ingest"))
    import raw_metadata

    with tempfile.TemporaryDirectory() as tmp:
        d_dir = Path(tmp) / "synthetic.d"
        d_dir.mkdir()
        tdf = d_dir / "analysis.tdf"

        con = sqlite3.connect(synthetic_tdf_write_uri(tdf), uri=True)
        # WAL mode: see test_exempted_constructor_round_trips. This is what
        # makes the leftover assertion below fail on a plain mode=ro reader.
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE GlobalMetadata (Key TEXT, Value TEXT)")
        con.executemany(
            "INSERT INTO GlobalMetadata VALUES (?, ?)",
            [
                ("InstrumentName", "timsTOF HT"),
                ("InstrumentSerialNumber", "10878"),
                ("AcquisitionDateTime", "2026-04-24T21:55:58.485-07:00"),
            ],
        )
        con.execute("CREATE TABLE Frames (Id INTEGER, MsMsType INTEGER)")
        con.executemany("INSERT INTO Frames VALUES (?, ?)", [(1, 0), (2, 9), (3, 9)])
        con.commit()
        con.close()

        size_before = tdf.stat().st_size
        meta = raw_metadata.read_bruker(str(d_dir))

        assert meta["instrument_model"] == "timsTOF HT"
        assert meta["instrument_serial"] == "10878"
        assert meta["n_ms1_frames"] == 1
        assert meta["n_ms2_frames"] == 2
        assert tdf.stat().st_size == size_before, "the read changed the tdf on disk"
        leftovers = sorted(p.name for p in d_dir.iterdir() if p.name != "analysis.tdf")
        assert leftovers == [], "read_bruker wrote into the .d: %s" % leftovers


if __name__ == "__main__":
    fails = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("  PASS  %s" % name)
            except AssertionError as e:
                fails.append(name)
                print("  FAIL  %s\n%s" % (name, e))
    print("\n%d passed, %d failed" % (
        len([n for n in globals() if n.startswith("test_")]) - len(fails), len(fails)))
    sys.exit(1 if fails else 0)
