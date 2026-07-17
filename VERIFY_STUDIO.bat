@echo off
cd /d %~dp0
echo Verifying Python environment...
C:\PythonEnvs\euqilegna\Scripts\python.exe -c "import cv2, numpy, PIL, scipy, skimage, shapely, reportlab, fastapi, uvicorn; print('Dependencies OK')"
echo Verifying application imports...
C:\PythonEnvs\euqilegna\Scripts\python.exe -c "import main; print(main.app.title)"
pause
