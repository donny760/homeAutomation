@echo off
setlocal enabledelayedexpansion
REM ============================================================================
REM  deploy.bat - ship powerwall dashboard + automation files to server-04
REM
REM  What it does:
REM    1. Build the frontend (next build + copy to static\frontend)
REM    2. Back up server's runtime databases
REM    3. Copy Python source + frontend bundle + config to server
REM
REM  This script does NOT stop or start services.
REM  Restart the service manually after deploy.
REM
REM  Run from:  d:\projects\homeAutomation\deploy.bat
REM  Requires:  write access to \\server-04\Applications\projects\homeAutomation
REM ============================================================================

REM ===== CONFIG =====
set "LOCAL_ROOT=%~dp0"
set "LOCAL_ROOT=%LOCAL_ROOT:~0,-1%"
set "SERVER_HOST=server-04"
set "SERVER_PATH=\\%SERVER_HOST%\Applications\projects\homeAutomation"

echo.
echo ============================================================
echo  Powerwall dashboard deploy  -  %DATE% %TIME%
echo  local:   %LOCAL_ROOT%
echo  target:  %SERVER_PATH%
echo ============================================================

REM ----- Sanity check paths -----
if not exist "%LOCAL_ROOT%\server.py" (
    echo [FAIL] Local root not found: %LOCAL_ROOT%
    goto :fail
)
if not exist "%SERVER_PATH%" (
    echo [FAIL] Cannot reach server path: %SERVER_PATH%
    echo        Check SMB access / network / credentials.
    goto :fail
)

REM ----- Step 1/3: Build frontend -----
echo.
echo [1/3] Building frontend bundle...
pushd "%LOCAL_ROOT%\frontend"
call npm run deploy
set BUILD_RC=%ERRORLEVEL%
popd
if %BUILD_RC% NEQ 0 (
    echo [FAIL] Frontend build failed with code %BUILD_RC%
    goto :fail
)
if not exist "%LOCAL_ROOT%\static\frontend\index.html" (
    echo [FAIL] Build succeeded but static\frontend\index.html missing
    goto :fail
)
echo       frontend bundle built OK

REM ----- Step 2/3: Back up server runtime state -----
echo.
echo [2/3] Backing up server's runtime state...
if exist "%SERVER_PATH%\powerwall.db" (
    copy /Y "%SERVER_PATH%\powerwall.db" "%SERVER_PATH%\powerwall.db.pre-deploy" >nul
    echo       powerwall.db     -^> powerwall.db.pre-deploy
)
if exist "%SERVER_PATH%\rules.log" (
    copy /Y "%SERVER_PATH%\rules.log" "%SERVER_PATH%\rules.log.pre-deploy" >nul
    echo       rules.log        -^> rules.log.pre-deploy
)
if exist "%SERVER_PATH%\network_devices.json" (
    copy /Y "%SERVER_PATH%\network_devices.json" "%SERVER_PATH%\network_devices.json.pre-deploy" >nul
    echo       network_devices.json -^> network_devices.json.pre-deploy
)

REM ----- Step 3/3: Copy files -----
echo.
echo [3/3] Copying files to server...

REM Python source
for %%f in (
    server.py
    rules.py
    fetch_rates.py
    backfill.py
    network_devices.py
    requirements.txt
) do (
    if exist "%LOCAL_ROOT%\%%f" (
        copy /Y "%LOCAL_ROOT%\%%f" "%SERVER_PATH%\%%f" >nul
        if !ERRORLEVEL! NEQ 0 (
            echo [FAIL] Could not copy %%f
            goto :fail
        )
        echo       %%f
    )
)

REM Config files (holidays, rates)
for %%f in (
    holidays.json
    rates.json
) do (
    if exist "%LOCAL_ROOT%\%%f" (
        copy /Y "%LOCAL_ROOT%\%%f" "%SERVER_PATH%\%%f" >nul
        if !ERRORLEVEL! NEQ 0 (
            echo [FAIL] Could not copy %%f
            goto :fail
        )
        echo       %%f
    )
)

REM Frontend bundle ? mirror so stale hashed chunks are cleaned up
echo       static\frontend\ mirroring...
robocopy "%LOCAL_ROOT%\static\frontend" "%SERVER_PATH%\static\frontend" /MIR /NFL /NDL /NJH /NJS /NC /NS /NP >nul
set ROBO_RC=!ERRORLEVEL!
if !ROBO_RC! GEQ 8 (
    echo [FAIL] robocopy returned !ROBO_RC! - codes 8 or higher indicate errors
    goto :fail
)
echo       static\frontend\ mirrored OK - robocopy rc=!ROBO_RC!

REM NOTE: .env is intentionally NOT overwritten - server keeps its own credentials.
REM       If you need to update .env, copy it manually after verifying new fields.
REM NOTE: powerwall.db, abode.pickle, rules.log, and
REM       network_devices.json are runtime state on the server and are NOT overwritten.

echo.
echo ============================================================
echo  DEPLOY COMPLETE
echo ============================================================
echo.
echo  Dashboard:  http://%SERVER_HOST%:5001
echo.
echo  Sanity checks:
echo    curl http://%SERVER_HOST%:5001/api/config
echo    curl http://%SERVER_HOST%:5001/api/readings/latest
echo.
echo  Rollback (if something went wrong):
echo    Stop the service first, then:
echo    copy /Y "%SERVER_PATH%\powerwall.db.pre-deploy"  "%SERVER_PATH%\powerwall.db"
echo    copy /Y "%SERVER_PATH%\rules.log.pre-deploy"     "%SERVER_PATH%\rules.log"
echo    copy /Y "%SERVER_PATH%\network_devices.json.pre-deploy" "%SERVER_PATH%\network_devices.json"
echo    Restore Python files from git, then restart the service.
echo.
exit /b 0

:fail
echo.
echo ============================================================
echo  DEPLOY FAILED - see messages above
echo ============================================================
pause
exit /b 1
