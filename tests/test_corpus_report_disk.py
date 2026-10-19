"""The corpus report's PG Farm disk line (ingest/corpus_drip_report.py::disk_line).

The delimp disk filled on 2026-10-05 and took the site down. The report now carries Justin Merz's
usage query against the disk size he quoted, so these pin the arithmetic and, above all, that a
near-full or unmeasurable disk is never reported as a calm line.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingest"))
import corpus_drip_report as r                                    # noqa: E402


def test_roomy_disk_is_a_plain_line():
    line = r.disk_line(100.0, None)
    assert "⚠️" not in line
    free = r.PG_DISK_GB - r.PG_OVERHEAD_GB - 100.0
    assert f"~{free:.1f} GB free" in line


def test_disk_under_the_threshold_warns_with_the_free_space_first():
    used = r.PG_DISK_GB - r.PG_OVERHEAD_GB - 2.5                    # today's real margin
    line = r.disk_line(used, None)
    assert line.startswith("• ⚠️")
    assert "~2.5 GB free" in line
    assert line.index("GB free") < line.index("used")


def test_unmeasurable_disk_is_a_warning_not_silence():
    # On 2026-10-05 a full disk looked, from outside, exactly like a connection that never finished.
    line = r.disk_line(None, "OperationalError: timeout expired")
    assert line.startswith("• ⚠️")
    assert "timeout expired" in line


def test_growth_since_the_last_report_is_shown():
    line = r.disk_line(397.5, None, prev_used=397.0)
    assert "+0.5 GB since last report" in line


def test_no_growth_figure_without_a_previous_measurement():
    assert "since last report" not in r.disk_line(397.5, None, prev_used=None)
