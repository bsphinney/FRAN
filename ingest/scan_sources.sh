#!/bin/bash
echo "=== FRAN_SNE_export root (first 8) ==="
ls /nfs/lssc0/flinders/proteomics/Data/FRAN_SNE_export/ 2>/dev/null | head -8
echo "entries: $(ls /nfs/lssc0/flinders/proteomics/Data/FRAN_SNE_export/ 2>/dev/null | wc -l)"
echo
echo "=== non-parquet files under FRAN_reports (depth 3) ==="
find /nfs/lssc0/flinders/proteomics/Data/FRAN_reports -maxdepth 3 -type f ! -name '*.parquet' 2>/dev/null | head -10
echo
echo "=== any setup.txt / AnalysisLog / RunSummaries anywhere on the share ==="
find /nfs/lssc0/flinders/proteomics/Data/FRAN_SNE_export -maxdepth 4 \( -name '*setup.txt' -o -name '*Analysis*og*.txt' -o -name '*RunOverview.tsv' -o -name '*.zip' \) 2>/dev/null | head -10
