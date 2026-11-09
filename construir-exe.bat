@echo off
rem Constroi o qml2lyrx.exe (precisa de Python instalado neste computador)
cd /d "%~dp0"

python -m pip install --upgrade pyinstaller
python -m PyInstaller --onefile --name qml2lyrx qml2lyrx.py

if exist "dist\qml2lyrx.exe" (
  copy /y "dist\qml2lyrx.exe" "%~dp0qml2lyrx.exe" >nul
  echo.
  echo Feito: qml2lyrx.exe na pasta, e a copia em dist\
) else (
  echo.
  echo Falhou - ver os erros acima.
)
pause
