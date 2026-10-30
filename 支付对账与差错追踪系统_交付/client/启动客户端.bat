@echo off
chcp 65001 >nul
cd /d "%~dp0"

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw app.py
    exit /b
)

where python >nul 2>nul
if %errorlevel%==0 (
    start "" python app.py
    exit /b
)

echo 未找到 Python，请先安装 Python 3.10+ 并勾选 "Add to PATH"。
pause
