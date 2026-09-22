@echo off
REM ---------------------------------------------------------------------
REM  copy_raw_d_safe.bat -- drop-in replacement for the raw .d copier.
REM
REM  The copier this replaces is one line, of this shape:
REM
REM      robocopy <instrument raw dir> \\<server>\protcore\Data\raw_data\... /E /Z /FFT
REM
REM  It has no completeness gate, so it copies a .d mid-acquisition; and no
REM  /XF, so it carries analysis.tdf-wal to the destination, where it sits
REM  beside a finished-looking analysis.tdf until something opens that
REM  database read-write and SQLite truncates the frame index. 350 .d on
REM  this cluster have been destroyed that way; 113 more are one read-write
REM  open from it.
REM
REM  This runs the same source -> destination copy, but refuses any .d that
REM  has not demonstrably finished acquiring, never transports the SQLite
REM  side files, and verifies every copy against its source. Same shape, so
REM  a scheduled task or shortcut only needs its path pointed here.
REM
REM  It NEVER deletes anything, on either side.
REM
REM  FIRST RUN: append -DryRun -Show and read the outcomes for a day before
REM  letting it copy. See README.md.
REM ---------------------------------------------------------------------

setlocal

REM ===================== EDIT THESE TWO LINES ==========================
REM Exactly the two paths the robocopy one-liner had, in the same order.
set "SRC=D:\Data"
set "DST=\\128.120.208.2\protcore\Data\raw_data\tTOF_HT"
REM =====================================================================

REM Set to 20 if this runs ON the acquiring PC, to hand bandwidth back to
REM the instrument. Leave at 0 on a separate copy node.
set "IPG=0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0bruker_d_copy.ps1" ^
  "%SRC%" "%DST%" -InterPacketGapMs %IPG% %*

exit /b %ERRORLEVEL%
