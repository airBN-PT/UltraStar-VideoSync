@echo off
chcp 65001 >nul
cd /d "%~dp0"
py UltraStar_VideoSync.py
if errorlevel 1 pause
