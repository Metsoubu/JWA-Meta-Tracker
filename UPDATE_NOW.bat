@echo off
rem ============================================================
rem  JWA Meta Tracker - check for new leaderboard data right now.
rem  (Not needed normally: updates run automatically every 12 hours.)
rem ============================================================
setlocal
cd /d "%~dp0"
title JWA Meta Tracker - Update now
call "%~dp0scripts\find_python.cmd"
if not defined PY exit /b 1

echo Checking for new leaderboard data...
echo.
%PY% "%~dp0tracker.py" collect --trigger manual
echo.
pause
endlocal
