@echo off
setlocal
REM ============================================================================
REM  install.bat - install Python dependencies for the home automation server
REM ============================================================================

set "LOCAL_ROOT=%~dp0"
set "LOCAL_ROOT=%LOCAL_ROOT:~0,-1%"

if not exist "%LOCAL_ROOT%\requirements.txt" (
    echo [FAIL] requirements.txt not found in %LOCAL_ROOT%
    exit /b 1
)

echo.
echo ============================================================
echo  Installing Python dependencies
echo  from: %LOCAL_ROOT%\requirements.txt
echo ============================================================
echo.

python -m pip install --upgrade pip
if %ERRORLEVEL% NEQ 0 (
    echo [FAIL] pip upgrade failed - is Python on PATH?
    exit /b 1
)

python -m pip install -r "%LOCAL_ROOT%\requirements.txt"
if %ERRORLEVEL% NEQ 0 (
    echo [FAIL] dependency install failed
    exit /b 1
)

echo.
echo ============================================================
echo  DONE - all dependencies installed
echo ============================================================
exit /b 0
