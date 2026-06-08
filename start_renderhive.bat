@echo off
title RenderHive Node
cd /d "%~dp0"
color 0A

echo.
echo  ============================================
echo   RENDERHIVE - Peer-to-Peer GPU Render Farm
echo  ============================================
echo.

REM Check Python is available
python --version >nul 2>nul
if errorlevel 1 (
    echo  [ERROR] Python not found!
    echo  Please install Python 3.9+ and ensure it is in your PATH.
    echo  Download: https://www.python.org/downloads/
    pause
    exit /b 1
)

REM Create virtual environment if it doesn't exist
if not exist ".venv\Scripts\activate.bat" (
    echo  Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo  [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo  [OK] Virtual environment created.
) else (
    echo  [OK] Virtual environment found.
)

REM Activate virtual environment
call .venv\Scripts\activate.bat

REM Install dependencies if not already installed
python -c "import flask; import requests" >nul 2>nul
if errorlevel 1 (
    echo  Installing dependencies...
    pip install -q flask requests
    echo  [OK] Dependencies installed.
) else (
    echo  [OK] Dependencies already installed.
)
echo.

REM Detect GPUs
echo  Detecting GPUs...
python -c "import subprocess; out=subprocess.check_output(['nvidia-smi','--query-gpu=name','--format=csv,noheader'],text=True,stderr=subprocess.DEVNULL); gpus=[g.strip() for g in out.strip().splitlines() if g.strip()]; print(f'  [OK] Found {len(gpus)} NVIDIA GPU(s):'); [print(f'       [{i}] {g}') for i,g in enumerate(gpus)]" 2>nul || echo  [--] No NVIDIA GPUs detected (will use CPU mode)
echo.

REM Add firewall rules (silently, only if not already added)
netsh advfirewall firewall show rule name="RenderHive Discovery" >nul 2>nul
if errorlevel 1 (
    echo  Adding firewall rules for peer discovery and dashboard...
    netsh advfirewall firewall add rule name="RenderHive Discovery" dir=in action=allow protocol=UDP localport=5678 >nul 2>nul
    netsh advfirewall firewall add rule name="RenderHive Dashboard" dir=in action=allow protocol=TCP localport=8080 >nul 2>nul
    if errorlevel 1 (
        echo  [!!] Could not add firewall rules. Run as Administrator if peers can't find this PC.
    ) else (
        echo  [OK] Firewall rules added.
    )
) else (
    echo  [OK] Firewall rules already configured.
)
echo.

REM Start the node
echo  Starting RenderHive node...
echo  - This PC is both a COORDINATOR and WORKER
echo  - Other PCs running RenderHive will be auto-discovered
echo  - Open the dashboard URL shown below in your browser
echo.
echo  ============================================
echo.

python renderhive.py %*
pause
