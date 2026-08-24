@echo off
cd /d "%~dp0"

:: 1. Kiem tra va khoi dong egp-edoc-agent.exe neu chua chay
tasklist /FI "IMAGENAME eq egp-edoc-agent.exe" 2>NUL | find /I /N "egp-edoc-agent.exe" >nul
if "%ERRORLEVEL%"=="1" (
    start "" "C:\MPI\EGP-AGENT\egp-edoc-agent.exe"
)

:: 2. Kich hoat moi truong ao .venv
call .venv\Scripts\activate.bat

:: 3. Chay main.py
python main.py