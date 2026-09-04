@echo off
title Telegram TTS & Voice Cloning Bot - VieNeu TTS
cd /d "%~dp0"
echo ========================================================
echo   KHOI DONG BOT TELEGRAM TTS & VOICE CLONING (VIENEU)
echo ========================================================
echo.
if not exist .venv\Scripts\python.exe (
    echo [ERROR] Khong tim thay moi truong .venv!
    pause
    exit /b
)
.venv\Scripts\python.exe bot_tts.py
pause
