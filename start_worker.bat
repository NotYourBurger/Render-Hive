@echo off
REM Run on each rendering PC. EDIT the SERVER and GPU lines below.
cd /d "%~dp0"
python -m pip install -q requests

REM ====== EDIT THESE ======
set SERVER=http://192.168.1.10:8080
set GPU=0
set DEVICE=OPTIX
REM ========================

python worker.py --server %SERVER% --gpu %GPU% --device %DEVICE%
pause
