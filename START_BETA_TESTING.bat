@echo off
cd /d %~dp0
echo Starting Euqilegna Interactive Digital Art Studio 6.0 Beta...
C:\PythonEnvs\euqilegna\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
pause
