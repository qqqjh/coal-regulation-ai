@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%run_atomic_rag_pipeline_v1.ps1" %*
exit /b %ERRORLEVEL%
