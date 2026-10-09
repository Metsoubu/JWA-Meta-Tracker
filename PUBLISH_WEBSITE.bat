@echo off
rem ============================================================
rem  JWA Meta Tracker - upload this folder to your GitHub
rem  repository so it becomes (or updates) your free website.
rem  First time: see "Put it online" in README.md (3 short steps).
rem  After that: just double-click this file whenever files change.
rem ============================================================
setlocal
cd /d "%~dp0"
title JWA Meta Tracker - Publish website

where git >nul 2>nul
if errorlevel 1 (
  echo Git is needed for this step. Install it from https://git-scm.com/download/win and try again.
  start "" https://git-scm.com/download/win
  pause
  exit /b 1
)

set "REPO="
if exist ".git" for /f "delims=" %%R in ('git remote get-url origin 2^>nul') do set "REPO=%%R"

if defined REPO (
  echo Publishing to: %REPO%
  echo ^(To use a different repository, type its address now; otherwise just press Enter.^)
  set "NEWREPO="
  set /p NEWREPO=Repository address:
) else (
  echo Paste the address of your GitHub repository and press Enter.
  echo Example: https://github.com/yourname/jwa-meta-tracker
  set "NEWREPO="
  set /p NEWREPO=Repository address:
)
if defined NEWREPO set "REPO=%NEWREPO%"
if not defined REPO exit /b 1

if not exist ".git" git init -b main >nul
rem A neutral author name keeps your personal e-mail address out of the public history.
git config user.name "JWA Meta Tracker"
git config user.email "jwa-meta-tracker@users.noreply.github.com"
git remote remove origin >nul 2>nul
git remote add origin %REPO%
git add -A
git commit -m "Publish JWA Meta Tracker" >nul 2>nul
echo.
echo Uploading... (GitHub may open a window asking you to sign in - that is expected.)
git pull --rebase origin main >nul 2>nul
git push -u origin main
if errorlevel 1 (
  echo.
  echo The upload did not finish. Check the repository address and that you signed in, then try again.
  pause
  exit /b 1
)
echo.
echo Done! In a minute or two the website shows the new version.
echo Open your repository on GitHub and click "Actions" to watch it.
pause
endlocal
