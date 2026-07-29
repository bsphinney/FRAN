#!/bin/bash
# Ingest the Ver_15 Spectronaut search into the FRAN PG corpus + the Lance spectrum lane.
set -euo pipefail
cd /quobyte/proteomics-grp/brett/glendon/fran_ingest
export DELIMP_PG_TOKEN_FILE=/quobyte/proteomics-grp/brett/.pgfarm_token
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
REPORT="/quobyte/proteomics-grp/brett/sn21/20260723_1529_Ver_15/20260723_145847_Ver_15_Report.parquet"
LANCE=/quobyte/proteomics-grp/brett/glendon/spectra_lance
exec "$PY" corpus_ingest.py "$REPORT" --engine spectronaut --name Ver_15 "$@"
