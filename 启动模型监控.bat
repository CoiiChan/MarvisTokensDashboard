@echo off
REM ============================================================
REM Marvis Model Monitor Launcher
REM 
REM Before running: update PYTHON_PATH to your Marvis Python
REM executable (found in MarvisAgent\runtime\python311\)
REM ============================================================

REM --- CONFIG: adjust to your MarvisAgent version ---
set PYTHON_PATH=C:\Program Files\Tencent\Marvis\MarvisAgent\VERSION_HERE\runtime\python311\python.exe

echo Stopping old process on port 19999...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":19999.*LISTENING"') do taskkill /F /PID %%a 2>nul

start "" "%PYTHON_PATH%" "%~dp0model_monitor.py"
timeout /t 2 >nul
start http://127.0.0.1:19999
