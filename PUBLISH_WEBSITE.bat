@echo off
rem ============================================================
rem  JWA Meta Tracker - upload this folder to your GitHub
rem  repository so it becomes a free public website.
rem  See "Put it online" in README.md first (3 short steps).
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

echo.
echo Paste the address of your GitHub repository and press Enter.
echo Example: https://github.com/yourname/jwa-meta-tracker
set /p REPO=Repository address:
if "%REPO%"=="" exit /b 1

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
echo Done! In a minute or two the website will be live. Open your repository on GitHub
echo and click "Actions" to watch it; the address is shown under Settings - Pages.
pause
endlocal
