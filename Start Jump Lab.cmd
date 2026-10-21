@echo off
cd /d "%~dp0"
echo Bertec Jump Lab: http://127.0.0.1:8765
echo Keep this window open. Press Ctrl+C to stop.
".venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 8765 --workers 1
pause
