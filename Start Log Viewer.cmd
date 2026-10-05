@echo off
cd /d "%~dp0"
python web_viewer.py --open %*
pause
