@echo off
cd /d "%~dp0"
if not exist SeestarLogger.exe call Build Desktop.cmd
if not exist SeestarLogger.exe exit /b 1
start "" "%~dp0SeestarLogger.exe"
