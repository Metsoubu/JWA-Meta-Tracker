@echo off
rem Runs the automated test suite (uses temporary folders; your real data is not touched).
setlocal
cd /d "%~dp0.."
call "%~dp0find_python.cmd"
if not defined PY exit /b 1
%PY% -m unittest discover -s tests -t . -v
pause
endlocal
