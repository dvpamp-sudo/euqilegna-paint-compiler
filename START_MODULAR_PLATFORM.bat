@echo off
cd /d %~dp0
echo Starting Euqilegna Modular Compiler Platform 10.0...
C:\PythonEnvs\euqilegna\Scripts\python.exe -m uvicorn platform_api.app:app --host 0.0.0.0 --port 8100
pause
