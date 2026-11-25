@echo off
rem Double-click me on a Spectronaut computer: check, find and ingest every .sne FRAN lacks.
rem Safe to run again at any time. Progress is also written to node_reports\<this computer>\.
title FRAN ingest
py "%~dp0win_ingest_all.py" %*
echo.
echo Finished. You can close this window.
pause
