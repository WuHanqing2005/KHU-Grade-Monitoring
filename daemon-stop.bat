@echo off
chcp 65001 >nul
python "%~dp0main.py" --daemon-stop
echo.
pause
