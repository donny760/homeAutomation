@echo off
cd /d D:\Projects\homeAutomation

echo.
echo ========================================
echo  Home Automation -- Git Commit
echo ========================================
echo.

git add -A
git status
echo.

echo Generating commit message...
claude -p "Read CONTEXT.md and the staged git diff. Write a single concise commit message (1-2 sentences) summarizing what changed. Output ONLY the message, nothing else." > %TEMP%\commitmsg.txt

set /p MSG=<%TEMP%\commitmsg.txt
echo.
echo Commit message: %MSG%
echo.

git commit -m "%MSG%"
git push origin main

echo.
echo Done!
pause
