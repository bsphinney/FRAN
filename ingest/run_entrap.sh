#!/bin/bash
set -euo pipefail
cd /quobyte/proteomics-grp/brett/glendon/fran_ingest
export DELIMP_PG_TOKEN_FILE=/quobyte/proteomics-grp/brett/.pgfarm_token
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
D="/quobyte/proteomics-grp/brett/sn21/20260727_102317_Dog yeast entrapment SN21"
exec "$PY" corpus_ingest.py \
  "$D/20260720_101232_Dog yeast entrapment SN21_Report.parquet" \
  --engine spectronaut --name "Dog_yeast_entrapment_SN21" \
  --lance-dir /quobyte/proteomics-grp/brett/glendon/spectra_lance \
  --xic-dir "$D/20260720_101232_Dog yeast entrapment SN21_XIC-DBs" \
  --xic-lance-dir /quobyte/proteomics-grp/brett/glendon/xic_lance "$@"
