@echo off
REM Run this on ONE machine (the master). It hosts the dashboard.
cd /d "%~dp0"
python -m pip install -q flask
python coordinator.py --port 8080
pause
