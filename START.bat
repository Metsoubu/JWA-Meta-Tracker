@echo off
rem ============================================================
rem  JWA Meta Tracker - double-click to open your dashboard.
rem  First run: sets up the database and automatic updates.
rem ============================================================
setlocal
cd /d "%~dp0"
title JWA Meta Tracker
call "%~dp0scripts\find_python.cmd"
if not defined PY exit /b 1

%PY% "%~dp0tracker.py" start %*
if errorlevel 1 (
  echo.
  echo  Something went wrong. Double-click VIEW_UPDATE_LOG.bat for details.
  pause
)
endlocal
