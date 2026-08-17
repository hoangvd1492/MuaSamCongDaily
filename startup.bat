@echo off
title MuaSamCongBot Runner
cd /d "%~dp0"

:: Kich hoat moi truong ao .venv
call .venv\Scripts\activate.bat

:: Chay main.py
python main.py

