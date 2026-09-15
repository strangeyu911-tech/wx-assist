@chcp 65001 >nul
@echo off
setlocal EnableExtensions

REM ============================================================
REM  wx-assist one-click launcher
REM
REM  1) Start the ACP bridge (idempotent - reuses a running one)
REM  2) Start the desktop app from the project venv
REM
REM  Keep this file ASCII-only: cmd.exe reads .bat as ANSI and
REM  would garble any non-ASCII bytes.
REM ============================================================

set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "PY=%ROOT%\.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo [wx-assist] venv not found: %PY%
  echo [wx-assist] See the usage guide 使用指南.html to set it up.
  echo.
  pause
  exit /b 1
)

echo.
echo [1/2] Starting ACP bridge :8790 ...
call "%ROOT%\tools\acp-bridge\start-bridge.bat"
if errorlevel 1 (
  echo.
  echo [wx-assist] WARNING: bridge failed to start.
  echo [wx-assist] AI features will not work. Log:
  echo   %ROOT%\tools\acp-bridge\bridge.log
  echo [wx-assist] Continuing anyway in 5s ...
  timeout /t 5 /nobreak >nul
)

echo.
echo [2/2] Starting wx-assist ...
cd /d "%ROOT%"

"%PY%" desktop.py
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo [wx-assist] exited with code %RC%.
  echo [wx-assist] Press any key to close this window.
  pause >nul
)

endlocal
