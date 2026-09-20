@echo off
rem ============================================================================
rem FileToLink - Window 2: the Telegram bot (real mode, long polling)
rem ============================================================================
rem Requires Window 1 (Local Bot API Server) to be running first: this bot
rem pings it at startup and exits with a clear error if it is unreachable.
rem Credentials and configuration come from .env (BOT_TOKEN, TELEGRAM_API_ID,
rem TELEGRAM_API_HASH, ...) - this script never contains secrets.
rem Runs in the foreground; stop with Ctrl+C.
rem ============================================================================

cd /d "%~dp0.."

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo [Window 2] Starting the FileToLink bot (python run.py --telegram) ...
"%PY%" run.py --telegram
