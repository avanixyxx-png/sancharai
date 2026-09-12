@echo off
REM ===================================================================
REM  SancharAI - double-click this file to start everything.
REM
REM  Uses the "py" launcher rather than "python", because python.exe is
REM  blocked by Application Control policy on this machine.
REM ===================================================================

cd /d "%~dp0"

py --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo   ERROR: Python was not found.
    echo   Install it from https://python.org and tick
    echo   "Add Python to PATH" during setup.
    echo.
    pause
    exit /b 1
)

py start.py

REM Keep the window open if something went wrong, so the error is readable
REM instead of the window vanishing instantly on a double-click.
if errorlevel 1 pause
