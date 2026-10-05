@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt
if errorlevel 1 (pause & exit /b 1)
for %%N in (conditions_config small_body_config satellite_config config) do if not exist "%%N.json" copy "%%N.example.json" "%%N.json" >nul
echo Setup complete. Run Setup Conditions.cmd to configure forecasts.
pause
