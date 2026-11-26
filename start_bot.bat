@echo off
rem TS Minicab controller - restarts itself if it ever stops with an error.
rem Mode comes from config.yaml "dry_run": true = dry run, false = LIVE (real bids and posts).
rem There is no "type LIVE" question here, so it can start by itself (Task Scheduler).
cd /d "%~dp0"
chcp 65001 >nul
title TS Minicab controller
set PYTHONIOENCODING=utf-8

rem Make sure the Python packages are installed (quick when they already are).
python -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  echo.
  echo Could not install the Python packages. Check the internet connection, then run:
  echo     python -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

:loop
python run_bot.py --yes
if %ERRORLEVEL%==0 goto end
if %ERRORLEVEL%==3 goto setup
echo Stopped with an error - restarting in 30 seconds (Ctrl+C to cancel)...
timeout /t 30
goto loop
:setup
echo.
echo Setup problem (see the message above) - fix it, then start this file again.
:end
pause
