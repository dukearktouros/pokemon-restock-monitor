@echo off
title Pokemon Restock Monitor
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 goto run_py
where python >nul 2>nul
if not errorlevel 1 goto run_python
echo Python is not installed. Get it from https://www.python.org/downloads/
echo During install, tick the box "Add python.exe to PATH".
goto end
:run_py
py app.py
goto end
:run_python
python app.py
:end
pause
