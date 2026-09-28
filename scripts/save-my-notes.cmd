@echo off
REM ============================================================================
REM  Save my notes  -  double-click this to file away today's ccmem notes.
REM
REM  Copy this file into the top folder of a project you're using Claude Code in,
REM  then just double-click it when you're done for the day. No typing required.
REM
REM  It runs in whatever folder it lives in (so it saves notes for THAT project),
REM  collects the !mem: notes from your latest session, and stores them so they
REM  are ready next time.
REM ============================================================================

setlocal
cd /d "%~dp0"

echo Saving your notes for this project...
echo   (%CD%)
echo.

REM Prefer the installed 'ccmem' command; fall back to 'python -m ccmem.cli'
REM (which only works when run from inside the ccmem repo itself).
where ccmem >nul 2>nul
if %errorlevel%==0 (
    set "CCMEM=ccmem"
) else (
    set "CCMEM=python -m ccmem.cli"
)

%CCMEM% capture --latest
if errorlevel 1 goto :oops
%CCMEM% generate
if errorlevel 1 goto :oops

echo.
echo   Done. Your notes are saved and will be waiting next time.
echo   You can close this window.
echo.
pause
exit /b 0

:oops
echo.
echo   Hmm - your notes could not be saved just now.
echo   Most often this means there was no session to read yet, or ccmem's
echo   one-time setup has not been done. Ask whoever helped you set this up.
echo.
pause
exit /b 1
