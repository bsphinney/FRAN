#!/bin/bash
R=/nfs/lssc0/flinders/proteomics/Data/FRAN_reports
echo "=== $R ==="
echo "report parquets:  $(find $R -name '*_Report_FRAN*.parquet' 2>/dev/null | wc -l)"
echo "any parquet:      $(find $R -name '*.parquet' 2>/dev/null | wc -l)"
echo "setup.txt:        $(find $R -name '*setup.txt' 2>/dev/null | wc -l)"
echo "AnalysisLog:      $(find $R -iname '*analysis*log*.txt' 2>/dev/null | wc -l)"
echo "RunOverview.tsv:  $(find $R -name '*RunOverview.tsv' 2>/dev/null | wc -l)"
echo "zips:             $(find $R -name '*.zip' 2>/dev/null | wc -l)"
echo "total files:      $(find $R -type f 2>/dev/null | wc -l)"
echo
echo "=== example full path ==="
find $R -name '*_Report_FRAN*.parquet' 2>/dev/null | head -2
echo
echo "=== other report copies elsewhere on quobyte? ==="
ls -d /quobyte/proteomics-grp/brett/glendon/* 2>/dev/null | head -12
