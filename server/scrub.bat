@echo off
cd /d "%~dp0"
.venv\Scripts\python.exe cli.py scrub >> scrub.log 2>&1
