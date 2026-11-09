@echo off
rem qml2lyrx - double-click to convert.
rem   1. drop a folder (or a .zip) onto this file, or
rem   2. leave the .gpkg / .zip files in geopackages\ (next to this file), or
rem   3. double-click and type, or drop, the path when asked.
rem
rem Note: "%~dp0." ends with a dot on purpose. "%~dp0" ends with a backslash,
rem and cmd turns the trailing backslash + quote into an escaped quote - the
rem program would receive the path with a quote glued to the end.
setlocal
cd /d "%~dp0"

set "PASTA=%~1"

if not defined PASTA if exist "%~dp0geopackages" (
  call :has_data "%~dp0geopackages"
  if not errorlevel 1 set "PASTA=%~dp0geopackages\."
)

if not defined PASTA (
  echo.
  echo Drop the data folder into this window and press Enter,
  echo or paste the path here.
  echo.
  set /p "PASTA=  > "
)

if not defined PASTA goto :cancelled
set "PASTA=%PASTA:"=%"

rem dropped a .zip? use the extracted folder with the same name, if it exists
if /i "%PASTA:~-4%"==".zip" set "PASTA=%PASTA:~0,-4%"

rem dropped a file (a .gpkg for instance)? use the folder that contains it
if exist "%PASTA%" if not exist "%PASTA%\" (
  for %%F in ("%PASTA%") do set "PASTA=%%~dpF."
)

if not exist "%PASTA%\" goto :not_found

call :has_data "%PASTA%"
if not errorlevel 1 goto :convert
goto :no_data

:convert
rem the .py (when present) is always newer than the .exe; the .exe is for people
rem who receive the tool without Python
if exist "%~dp0qml2lyrx.py" (
  python "%~dp0qml2lyrx.py" --pasta "%PASTA%"
) else if exist "%~dp0qml2lyrx.exe" (
  "%~dp0qml2lyrx.exe" --pasta "%PASTA%"
) else if exist "%~dp0dist\qml2lyrx.exe" (
  "%~dp0dist\qml2lyrx.exe" --pasta "%PASTA%"
) else (
  echo.
  echo No qml2lyrx.py or qml2lyrx.exe in this folder.
  goto :finish
)

if errorlevel 1 (
  echo.
  echo Finished with errors - see above.
) else (
  echo.
  echo Done. The .lyrx and .stylx files are in the out\ folder.
)
goto :finish

:no_data
echo.
echo No GeoPackages or zips in: %PASTA%
echo Put the files in geopackages\ or choose another folder.
goto :finish

:not_found
echo.
echo Not found: %PASTA%
echo If you dropped a file, drop the folder that contains it.
echo If it is a .zip, extract it first.

:cancelled
:finish
echo.
pause
exit /b 0

rem --------------------------------------------------------------- helpers
:has_data
dir /b /s "%~1\*.gpkg" >nul 2>&1
if not errorlevel 1 exit /b 0
dir /b /s "%~1\*.zip" >nul 2>&1
if not errorlevel 1 exit /b 0
exit /b 1
