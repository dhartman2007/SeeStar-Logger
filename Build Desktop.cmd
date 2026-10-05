@echo off
cd /d "%~dp0"
set "COMPILER=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not exist "%COMPILER%" set "COMPILER=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"
"%COMPILER%" /nologo /target:winexe /out:SeestarLogger.exe /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /reference:System.Web.Extensions.dll LoggerWindow.cs
if errorlevel 1 pause
