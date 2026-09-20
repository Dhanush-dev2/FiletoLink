@echo off
rem ============================================================================
rem FileToLink - Window 3: FastAPI download server (public links)
rem ============================================================================
rem Serves GET/HEAD /f/{public_id} on HOST:PORT (default 127.0.0.1:8000).
rem Independent of the bot and the Local Bot API Server - it only needs the
rem SQLite database and FileToLink-owned storage (paths resolved from .env and
rem printed at startup).
rem Tailscale Funnel (Window 4) should point at this server's HOST:PORT.
rem Runs in the foreground; stop with Ctrl+C.
rem ============================================================================

cd /d "%~dp0.."

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo [Window 3] Starting the download API (python run.py --api) ...
"%PY%" run.py --api
