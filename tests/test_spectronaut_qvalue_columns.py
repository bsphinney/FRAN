"""Spectronaut q-value column resolution.

Regression cover for the ingest gap where `global_q_value` was hardcoded to None with the
comment "Spectronaut: EG.Qvalue only (no separate global)". Spectronaut DOES export
EG.GlobalPrecursorQvalue -- it is present and populated in every FRAN (Normal) report
checked -- so `delimp_precursors.global_q_value` was null for every Spectronaut search in
the corpus, and experiment-wide (global-FDR) comparisons could not be made from FRAN.
"""
import importlib.util
import os

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "ingest", "spectronaut_to_corpus.py")


def _mod():
    spec = importlib.util.spec_from_file_location("spectronaut_to_corpus", _SRC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# Real column names from a Spectronaut "FRAN (Normal)" export (parquet form). Note the
# report carries NINE q-value columns; only two of them are the ones we want.
PARQUET_HDR = [
    "R_FileName", "EG_ModifiedSequence", "FG_Charge", "PG_ProteinGroups", "PG_Genes",
    "EG_Qvalue", "EG_GlobalPrecursorQvalue", "PG_Qvalue", "EG_IsDecoy",
    "EG_MaxChannelQvalue", "EG_MinChannelQvalue", "EG_AvgProfileQvalue",
    "EG_MaxProfileQvalue", "EG_MinProfileQvalue", "EG_PercentileQvalue", "FG_Qvalue",
]
# TSV exports use the dotted prefix instead of the underscore.
TSV_HDR = [c.replace("_", ".", 1) for c in PARQUET_HDR]


@pytest.mark.parametrize("hdr,q,gq", [
    (PARQUET_HDR, "EG_Qvalue", "EG_GlobalPrecursorQvalue"),
    (TSV_HDR, "EG.Qvalue", "EG.GlobalPrecursorQvalue"),
])
def test_resolves_both_qvalues(hdr, q, gq):
    cols = _mod().resolve_columns(hdr)
    assert cols.get("q_value") == q
    assert cols.get("global_q_value") == gq


@pytest.mark.parametrize("hdr", [PARQUET_HDR, TSV_HDR])
def test_global_never_leaks_into_run_level(hdr):
    """A report lacking a plain EG.Qvalue must not land the GLOBAL value in q_value.

    q_value's loose fallback used to be `EG.*Qvalue`, which matches EG.GlobalPrecursorQvalue.
    That would silently apply an experiment-wide FDR as if it were run-level -- exactly the
    filter asymmetry that produces wrong cross-engine comparisons.
    """
    no_plain = [c for c in hdr if c not in ("EG_Qvalue", "EG.Qvalue")]
    cols = _mod().resolve_columns(no_plain)
    assert cols.get("q_value") not in ("EG_GlobalPrecursorQvalue", "EG.GlobalPrecursorQvalue")
    assert cols.get("global_q_value") in ("EG_GlobalPrecursorQvalue", "EG.GlobalPrecursorQvalue")


def test_global_qvalue_is_read_not_hardcoded():
    """The record builder must read the column, not emit a literal None."""
    src = open(_SRC, encoding="utf-8").read()
    assert '"global_q_value": None' not in src, \
        "global_q_value is hardcoded to None again -- see this module's docstring"
    assert '"global_q_value": _f(r, cols, "global_q_value")' in src
