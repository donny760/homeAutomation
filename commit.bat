@echo off
cd /d D:\Projects\homeAutomation

echo.
echo ========================================
echo  Home Automation -- Git Commit
echo ========================================
echo.

git status
echo.

set /p MSG="Enter commit message: "

if "%MSG%"=="" (
    echo No message entered. Aborting.
    pause
    exit /b 1
)

git add -A
git commit -m "%MSG%"

echo.
echo Done!
pause
