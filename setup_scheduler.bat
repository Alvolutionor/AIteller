@echo off
REM AIteller - Windows Task Scheduler Setup
REM Run as Administrator

set SCRIPT_DIR=%~dp0

REM Use uv to run within project .venv
set RUN=uv run --project %SCRIPT_DIR% python -m src.main

REM Collect: every 3 hours
schtasks /create /tn "AIteller-Collect" /tr "%RUN% collect" /sc HOURLY /mo 3 /f

REM Report daily: every day at 08:00
schtasks /create /tn "AIteller-Report-Daily" /tr "%RUN% report daily" /sc DAILY /st 08:00 /f

REM Send daily: every day at 08:05 (after report generation)
schtasks /create /tn "AIteller-Send-Daily" /tr "%RUN% send daily" /sc DAILY /st 08:05 /f

REM Report weekly: every Sunday at 09:00
schtasks /create /tn "AIteller-Report-Weekly" /tr "%RUN% report weekly" /sc WEEKLY /d SUN /st 09:00 /f

REM Send weekly: every Sunday at 09:05
schtasks /create /tn "AIteller-Send-Weekly" /tr "%RUN% send weekly" /sc WEEKLY /d SUN /st 09:05 /f

REM Cleanup: daily at 03:00
schtasks /create /tn "AIteller-Cleanup" /tr "%RUN% cleanup" /sc DAILY /st 03:00 /f

echo Tasks created successfully!
echo.
echo Commands:
echo   uv run python -m src.main collect          Collect from all sources
echo   uv run python -m src.main report daily     Generate daily PDF
echo   uv run python -m src.main report weekly    Generate weekly PDF
echo   uv run python -m src.main send daily       Send latest daily PDF
echo   uv run python -m src.main send weekly      Send latest weekly PDF
echo   uv run python -m src.main status           Show status
echo   uv run python -m src.main cleanup          Clean old data
echo.
echo To verify: schtasks /query /tn "AIteller-*"
pause
