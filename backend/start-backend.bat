@echo off
:: backend/start-backend.bat

:: Detect Windows
echo 🖥️ Running backend for Windows...

:: Set IP
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /i "IPv4" ^| findstr /v "127.0.0.1"') do set IP=%%a
set IP=%IP: =%

echo 🌐 Your Local IP is: http://%IP%:3000
echo ✅ Backend API will run on: http://%IP%:8000

:: Ensure we run inside the backend folder so .env is discovered
cd /d "%~dp0"

:: Activate venv
call venv\Scripts\activate

:: Run FastAPI
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
