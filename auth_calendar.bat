@echo off
cd /d "%~dp0"
echo ========================================================
echo  Google Calendar Authorization for Family Center
echo ========================================================
echo.
echo Launching Google OAuth consent in your browser...
echo Please select your Google account and click "Allow".
echo.
python get_token.py
echo.
pause

