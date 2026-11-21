@echo off
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0" || exit /b 1
if /i "%~1"=="--gui-worker" goto worker

set "EZR_ROOT=%CD%"
set "EZR_GUI_SCRIPT=%CD%\launcher\windows_gui.ps1"
powershell -NoLogo -NoProfile -Command "Get-ChildItem -LiteralPath $env:EZR_ROOT -Recurse -Force -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue" >nul 2>&1
start "" powershell -NoLogo -NoProfile -STA -WindowStyle Hidden -Command "try { & ([ScriptBlock]::Create((Get-Content -Raw -Encoding UTF8 -LiteralPath $env:EZR_GUI_SCRIPT))) } catch { Add-Type -AssemblyName System.Windows.Forms; [void][System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'EZR Standalone') }"
exit /b 0

:worker
set "EZR_PY=.\runtime\python-windows-x64\python.exe"
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" set "EZR_PY=.\runtime\python-windows-arm64\python.exe"
if /i "%PROCESSOR_ARCHITEW6432%"=="ARM64" set "EZR_PY=.\runtime\python-windows-arm64\python.exe"
"%EZR_PY%" -B -u -X utf8 ".\launcher\standalone.py" gui-worker
exit /b %ERRORLEVEL%
