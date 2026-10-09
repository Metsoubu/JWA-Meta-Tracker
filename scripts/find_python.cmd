@echo off
rem Sets PY to a command that runs Python 3.10 or newer, or leaves PY empty.
rem If Python is missing, offers to install it with winget (Windows Package Manager).
set "PY="
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
  if not errorlevel 1 set "PY=py -3"
)
if defined PY goto :eof
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto :eof

echo.
echo  Python 3.10 or newer is required but was not found on this computer.
echo.
where winget >nul 2>nul
if errorlevel 1 goto :manual
choice /C YN /M " Install Python 3.13 now (free, from python.org via Windows Package Manager)"
if errorlevel 2 goto :manual
winget install --exact --id Python.Python.3.13 --scope user --accept-package-agreements --accept-source-agreements
echo.
echo  Python was installed. Please close this window and double-click the file again.
pause
exit /b 1

:manual
echo  Please install Python from https://www.python.org/downloads/windows/
echo  (tick "Add python.exe to PATH" during installation), then try again.
start "" https://www.python.org/downloads/windows/
pause
exit /b 1
