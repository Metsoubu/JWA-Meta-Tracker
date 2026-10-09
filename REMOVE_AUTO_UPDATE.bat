@echo off
rem ============================================================
rem  JWA Meta Tracker - turn OFF automatic updates.
rem  Your saved data is kept. Run START.bat to turn them back on.
rem ============================================================
setlocal
cd /d "%~dp0"
title JWA Meta Tracker - Remove automatic updates
call "%~dp0scripts\find_python.cmd"
if not defined PY exit /b 1

choice /C YN /M "Turn off automatic updates? (your data is kept)"
if errorlevel 2 exit /b 0
%PY% "%~dp0tracker.py" schedule remove
echo.
echo Note: START.bat turns automatic updates back on the next time you open it.
pause
endlocal
