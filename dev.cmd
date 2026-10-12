@echo off
rem Double-click to start Health AI for local development (see dev.ps1).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev.ps1" %*
if errorlevel 1 pause
