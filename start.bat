@echo off
:: Upgrade Agent — one-command launcher
:: Starts: FastAPI (port 8000) + Next.js (port 3000) + Claude Router (embedded)
::
:: Usage:
::   start.bat                  normal start
::   start.bat --no-reload      disable FastAPI hot-reload
::   start.bat --streamlit      use Streamlit UI instead of Next.js
::   start.bat --port-api 9000  custom API port
::   start.bat --port-web 4000  custom web port

cd /d "%~dp0"
py start.py %*
