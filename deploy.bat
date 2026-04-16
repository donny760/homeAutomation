@echo off
setlocal

set SRC=%~dp0
set DST=\\server-04\Applications\projects\homeAutomation

echo.
echo Deploying to %DST%
echo.

echo [1/2] Copying server.py...
copy /Y "%SRC%server.py" "%DST%\server.py"
if errorlevel 1 goto :error

echo.
echo [2/2] Mirroring static\frontend...
robocopy "%SRC%static\frontend" "%DST%\static\frontend" /MIR /NFL /NDL /NJH /NJS /NP
if errorlevel 8 goto :error

echo.
echo Deploy complete. Restart services manually.
endlocal
exit /b 0

:error
echo.
echo Deploy FAILED.
endlocal
exit /b 1
