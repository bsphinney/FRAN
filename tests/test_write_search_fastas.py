"""corpus_ingest.write_search_fastas(): one row per database, and the clobber rules.

A re-ingest that finds nothing must keep the stored rows, and a setup file's names must never
replace the counted rows read from a .sne -- the same failure as fran-upsert-clobber, where a
re-derivation that could not resolve wrote NULL over good data.
"""
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_INGEST = os.path.join(_HERE, "..", "ingest")
sys.path.insert(0, _INGEST)

_spec = importlib.util.spec_from_file_location("corpus_ingest", os.path.join(_INGEST, "corpus_ingest.py"))
ci = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci)


class FakeCur:
    """Just enough of a cursor: the table exists, and rows are kept per search."""

    def __init__(self, table=True, stored=None):
        self.table = table
        self.rows = {k: list(v) for k, v in (stored or {}).items()}
        self._result = None
        self.log = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.log.append(s)
        if s.startswith("SELECT to_regclass"):
            self._result = ("delimp_search_fastas" if self.table else None,)
        elif s.startswith("SELECT source FROM delimp_search_fastas"):
            have = self.rows.get(params[0])
            self._result = (have[0]["source"],) if have else None
        elif s.startswith("DELETE FROM delimp_search_fastas"):
            self.rows.pop(params[0], None)
        elif s.startswith("INSERT INTO delimp_search_fastas"):
            self.rows.setdefault(params[0], []).append(
                {"ordinal": params[1], "file_name": params[2], "is_contaminant": params[3],
                 "n_entries": params[4], "source": params[9]})

    def fetchone(self):
        return self._result


SNE = {"source": "sne", "source_path": "/x.sne", "databases": [
    {"file_name": "Universal Contaminant Protein FASTA.fasta", "is_contaminant": True, "n_entries": 381},
    {"file_name": "gg_HoSa_rUP00005640.fasta", "is_contaminant": False, "n_entries": 20385}]}
SETUP = {"source": "sn_setup", "source_path": "/x_setup.txt", "databases": [
    {"file_name": "gg_HoSa_rUP00005640.fasta", "is_contaminant": False, "n_entries": None}]}


def test_every_database_is_a_row_contaminants_included():
    cur = FakeCur()
    assert ci.write_search_fastas(cur, "sid", SNE) == 2
    assert [(r["ordinal"], r["is_contaminant"]) for r in cur.rows["sid"]] == [(0, True), (1, False)]


def test_finding_nothing_keeps_what_is_stored():
    cur = FakeCur(stored={"sid": [{"source": "sne", "ordinal": 0}]})
    assert ci.write_search_fastas(cur, "sid", {"databases": []}) == 0
    assert ci.write_search_fastas(cur, "sid", None) == 0
    assert len(cur.rows["sid"]) == 1


def test_a_setup_file_never_replaces_sne_rows():
    cur = FakeCur()
    ci.write_search_fastas(cur, "sid", SNE)
    assert ci.write_search_fastas(cur, "sid", SETUP) == 0
    assert [r["n_entries"] for r in cur.rows["sid"]] == [381, 20385]


def test_a_fresh_sne_read_replaces_older_rows():
    cur = FakeCur()
    ci.write_search_fastas(cur, "sid", SETUP)
    assert ci.write_search_fastas(cur, "sid", SNE) == 2
    assert [r["source"] for r in cur.rows["sid"]] == ["sne", "sne"]


def test_no_table_yet_is_a_no_op():
    cur = FakeCur(table=False)
    assert ci.write_search_fastas(cur, "sid", SNE) == 0
    assert not any(s.startswith("INSERT") for s in cur.log)
