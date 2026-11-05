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
    echo Install Python 3.12 or 3.13 and make sure "python" or "py" is on PATH.
    pause
    exit /b 1
  )
  set "PY=py -3"
)

rem Prefer a Python version with prebuilt numpy wheels.
py -3.12 -c "import sys" >nul 2>nul
if not errorlevel 1 (
  set "PY=py -3.12"
) else (
  py -3.13 -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PY=py -3.13"
)

set "VENV_PY=webapp\.venv\Scripts\python.exe"

rem A half-created venv must not be treated as ready.
if not exist "%VENV_PY%" goto :install
"%VENV_PY%" -c "import fastapi, uvicorn, numpy, jinja2, jwt, multipart" >nul 2>nul
if errorlevel 1 goto :install
goto :run

:install
echo [1/3] Creating the virtual environment...
%PY% -m venv "webapp\.venv"
if errorlevel 1 goto :error

echo [2/3] Installing dependencies. The first run needs network and may take 1-3 minutes...
"%VENV_PY%" -m pip install --upgrade pip
"%VENV_PY%" -m pip install --only-binary=:all: -r "webapp\requirements.txt"
if errorlevel 1 (
  echo.
  echo [ERROR] Dependency install failed.
  echo Use Python 3.12 or 3.13, then delete webapp\.venv and try again.
  pause
  exit /b 1
)

:run
set "ADMIN_INIT_PASSWORD=admin123"
set "EMPLOYEE_INIT_PASSWORD=emp123"
set "JWT_SECRET=demo-only-secret-change-in-production"
set "LLM_PROVIDER=mock"
set "DB_PATH=%~dp0webapp\demo.db"
set "DOCS_DIR=%~dp0webapp\docs"

echo [3/3] Starting the demo server...
start "jinrong-demo" cmd /k ""%~dp0webapp\run_server.bat""

echo Waiting for http://127.0.0.1:8000 ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$u='http://127.0.0.1:8000/api/v1/health'; for($i=0;$i -lt 60;$i++){ try { $r=Invoke-WebRequest -UseBasicParsing -Uri $u -TimeoutSec 2; if($r.StatusCode -eq 200){ exit 0 } } catch {}; Start-Sleep -Milliseconds 500 }; exit 1"
if errorlevel 1 (
  echo.
  echo [ERROR] The server did not become ready.
  echo Keep the "jinrong-demo" window open and read its error message.
  pause
  exit /b 1
)

start "" "http://127.0.0.1:8000"
exit /b 0

:error
echo.
echo [ERROR] Setup failed. Check the messages above.
pause
exit /b 1
