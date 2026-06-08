@echo off
REM Machine with two GPUs: launch one worker per GPU in separate windows.
cd /d "%~dp0"
python -m pip install -q requests
set SERVER=http://192.168.1.10:8080
start "RenderHive GPU0" cmd /k python worker.py --server %SERVER% --gpu 0 --device OPTIX
start "RenderHive GPU1" cmd /k python worker.py --server %SERVER% --gpu 1 --device OPTIX
