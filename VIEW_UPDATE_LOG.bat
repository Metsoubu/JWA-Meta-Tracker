@echo off
rem ============================================================
rem  JWA Meta Tracker - show update status and open the error log.
rem ============================================================
setlocal
cd /d "%~dp0"
title JWA Meta Tracker - Update log
call "%~dp0scripts\find_python.cmd"
if not defined PY exit /b 1

%PY% "%~dp0tracker.py" status
echo.
%PY% "%~dp0tracker.py" schedule status >nul 2>nul
if errorlevel 1 (
  echo  Automatic updates are NOT set up. Double-click START.bat once to set them up.
) else (
  echo  Automatic updates are set up in Windows Task Scheduler.
)
echo.
set "LOG=%LOCALAPPDATA%\JWA Meta Tracker\logs\collector.log"
if exist "%LOG%" (
  echo  Opening the detailed log in Notepad...
  start "" notepad "%LOG%"
) else (
  echo  No log file yet - no update has run.
)
echo.
pause
endlocal
