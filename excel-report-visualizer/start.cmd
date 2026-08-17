@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  python server.py
) else (
  py -3 server.py
)

endlocal
