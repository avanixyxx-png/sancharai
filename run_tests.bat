@echo off
REM ===================================================================
REM  SancharAI - double-click to run every test.
REM
REM  Each test file prints "ALL PASSED" or "N FAILED: [...]" on its last
REM  line. A file that crashes before reaching that line prints neither,
REM  which is why this checks for the verdict rather than the exit code -
REM  a suite that dies halfway must not be counted as a pass.
REM
REM  enabledelayedexpansion + !VAR! is required: plain %VAR% inside a for
REM  loop is expanded once, before the loop runs, so the counters would
REM  always report zero.
REM ===================================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

set PASSED=0
set FAILED=0
set FAILNAMES=

for %%F in (tests\test_*.py) do (
    echo.
    echo ============================================================
    echo   %%~nxF
    echo ============================================================
    py "%%F" > "%TEMP%\sanchar_test_out.txt" 2>&1
    type "%TEMP%\sanchar_test_out.txt"
    findstr /C:"ALL PASSED" "%TEMP%\sanchar_test_out.txt" >nul
    if errorlevel 1 (
        echo.
        echo   ^>^>^> %%~nxF DID NOT PASS
        set /a FAILED+=1
        set FAILNAMES=!FAILNAMES! %%~nxF
    ) else (
        set /a PASSED+=1
    )
)

del "%TEMP%\sanchar_test_out.txt" 2>nul

echo.
echo ============================================================
echo   !PASSED! file(s) passed, !FAILED! file(s) failed
if !FAILED! GTR 0 echo   Failed:!FAILNAMES!
echo ============================================================
echo.
pause
