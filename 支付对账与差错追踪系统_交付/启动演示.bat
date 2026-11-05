@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PY=python"
where python >nul 2>nul
if errorlevel 1 (
  where py >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] Python 3 was not found.
    echo Install Python 3.10+ and make sure "python" or "py" is on PATH.
    pause
    exit /b 1
  )
  set "PY=py -3"
)

if not exist "webapp\.venv\Scripts\python.exe" (
  echo [1/3] Creating the virtual environment...
  %PY% -m venv "webapp\.venv"
  if errorlevel 1 goto :error

  echo [2/3] Installing dependencies. The first run can take 1-3 minutes...
  "webapp\.venv\Scripts\python.exe" -m pip install --upgrade pip
  "webapp\.venv\Scripts\python.exe" -m pip install -r "webapp\requirements.txt"
  if errorlevel 1 goto :error
)

set "ADMIN_INIT_PASSWORD=admin123"
set "EMPLOYEE_INIT_PASSWORD=emp123"
set "JWT_SECRET=demo-only-secret-change-in-production"
set "LLM_PROVIDER=mock"
set "DB_PATH=%~dp0webapp\demo.db"
set "DOCS_DIR=%~dp0webapp\docs"

echo [3/3] Starting the demo server...
start "jinrong-demo" cmd /k ""%~dp0webapp\run_server.bat""
timeout /t 3 >nul
start "" "http://127.0.0.1:8000"
exit /b 0

:error
echo.
echo [ERROR] Setup failed. Check the messages above.
pause
exit /b 1
