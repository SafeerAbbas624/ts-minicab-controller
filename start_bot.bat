@echo off
rem TS Minicab controller - restarts itself if it ever stops with an error.
rem Mode comes from config.yaml "dry_run": true = dry run, false = LIVE (real bids and posts).
rem There is no "type LIVE" question here, so it can start by itself (Task Scheduler).
cd /d "%~dp0"
chcp 65001 >nul
title TS Minicab controller
set PYTHONIOENCODING=utf-8
:loop
python run_bot.py --yes
if %ERRORLEVEL%==0 goto end
echo Stopped with an error - restarting in 30 seconds (Ctrl+C to cancel)...
timeout /t 30
goto loop
:end
pause
