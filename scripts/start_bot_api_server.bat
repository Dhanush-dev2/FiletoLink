@echo off
rem ============================================================================
rem FileToLink - Window 1: Telegram Local Bot API Server
rem ============================================================================
rem Usage (from a terminal in the project root):
rem   scripts\start_bot_api_server.bat <path-to-telegram-bot-api.exe> <api_id> <api_hash>
rem
rem - <api_id> / <api_hash>: free values from https://my.telegram.org (the same
rem   ones as TELEGRAM_API_ID / TELEGRAM_API_HASH in .env). Passed as arguments
rem   so this script never contains or prints secrets.
rem - --dir must match TELEGRAM_BOT_WORK_DIR in .env (default: telegram_bot_data).
rem - This directory is the server's own working storage; FileToLink copies
rem   received files out of it, so it is disposable.
rem
rem If you run the server inside WSL2 (recommended, see README), start it in a
rem WSL terminal instead of using this script - the Linux binary cannot run
rem from cmd.exe.
rem Runs in the foreground; stop with Ctrl+C.
rem ============================================================================

set "EXE=%~1"
set "API_ID=%~2"
set "API_HASH=%~3"

if "%EXE%"=="" (
    echo No Local Bot API Server binary given.
    echo Usage: scripts\start_bot_api_server.bat ^<path-to-telegram-bot-api.exe^> ^<api_id^> ^<api_hash^>
    echo See README.md - "Deployment and operations".
    exit /b 1
)
if "%API_ID%"=="" (
    echo Missing api_id ^(from https://my.telegram.org^).
    exit /b 1
)
if "%API_HASH%"=="" (
    echo Missing api_hash ^(from https://my.telegram.org^).
    exit /b 1
)

cd /d "%~dp0.."
if not exist "telegram_bot_data" mkdir "telegram_bot_data"

echo [Window 1] Starting Local Bot API Server on http://127.0.0.1:8081 ...
"%EXE%" --local --api-id=%API_ID% --api-hash=%API_HASH% --dir=telegram_bot_data --http-port=8081
