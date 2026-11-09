@echo off
rem Build qml2lyrx.exe (requires Python installed on this machine)
cd /d "%~dp0"

python -m pip install --upgrade pyinstaller
python -m PyInstaller --onefile --name qml2lyrx qml2lyrx.py

if exist "dist\qml2lyrx.exe" (
  copy /y "dist\qml2lyrx.exe" "%~dp0qml2lyrx.exe" >nul
  echo.
  echo Done: qml2lyrx.exe in this folder, plus a copy in dist\
) else (
  echo.
  echo Failed - see the errors above.
)
pause
