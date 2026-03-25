@echo off
chcp 65001 >nul
cd /d "%~dp0"
python park_radio_server.py
pause
