@echo off
cd /d %~dp0
echo Checking modular application...
C:\PythonEnvs\euqilegna\Scripts\python.exe -c "from euqilegna_engine import CompilerOrchestrator; from platform_api.app import app; print(app.title); print('Modular imports OK')"
pause
