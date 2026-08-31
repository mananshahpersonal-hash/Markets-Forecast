@echo off
REM ============================================================
REM  Market Helper - one-click GitHub update
REM  Put this file in your repo folder. After you unzip new
REM  files over the folder, just double-click this. It stages
REM  everything, commits, and pushes to GitHub. Streamlit then
REM  auto-rebuilds in ~2 minutes.
REM ============================================================

REM Work in the folder this script lives in (the repo root).
cd /d "%~dp0"

echo.
echo ============================================
echo   Market Helper - pushing update to GitHub
echo ============================================
echo   Folder: %cd%
echo.

REM Make sure this is actually a git repo.
if not exist ".git" (
    echo  ERROR: This folder is not a git repository.
    echo  This .bat must sit inside your cloned repo folder
    echo  (the one that has a hidden .git folder in it^).
    echo.
    pause
    exit /b 1
)

REM Stage every change (new, modified, deleted).
git add -A

REM If nothing changed, say so and stop.
git diff --cached --quiet
if %errorlevel%==0 (
    echo  Nothing to update - the files here already match
    echo  what's on GitHub. Did you unzip the new files over
    echo  this folder first?
    echo.
    pause
    exit /b 0
)

REM Commit with a timestamped message.
for /f "tokens=1-5 delims=/: " %%a in ("%date% %time%") do set STAMP=%%a-%%b-%%c_%%d%%e
git commit -m "Update %STAMP%"

REM Push to the current branch on origin.
echo.
echo  Pushing to GitHub...
git push
if %errorlevel% neq 0 (
    echo.
    echo  PUSH FAILED. Common causes:
    echo   - not signed in to git (run: git push  once in a terminal
    echo     and complete the browser sign-in^)
    echo   - your branch name isn't 'main' (try: git push origin HEAD^)
    echo.
    pause
    exit /b 1
)

echo.
echo  ============================================
echo   DONE. GitHub updated. Streamlit will rebuild
echo   in about 2 minutes. Check the version line in
echo   the app to confirm.
echo  ============================================
echo.
pause
