@echo off
rem qml2lyrx - duplo-clique para converter.
rem   1. arrasta uma pasta (ou um .zip) para cima deste ficheiro, ou
rem   2. deixa os .gpkg/.zip em geopackages\ (ao lado deste ficheiro), ou
rem   3. duplo-clique e escreve/arrasta o caminho quando ele perguntar.
rem
rem Nota: "%~dp0." leva um ponto no fim de proposito. "%~dp0" acaba em
rem barra invertida, e o cmd transforma a barra+aspa final numa aspa
rem escapada - o programa recebia o caminho com uma aspa colada.
setlocal
cd /d "%~dp0"

set "PASTA=%~1"

if not defined PASTA if exist "%~dp0geopackages" (
  call :tem_dados "%~dp0geopackages"
  if not errorlevel 1 set "PASTA=%~dp0geopackages\."
)

if not defined PASTA (
  echo.
  echo Arrasta a pasta dos dados para esta janela e carrega Enter,
  echo ou cola o caminho aqui.
  echo.
  set /p "PASTA=  > "
)

if not defined PASTA goto :cancelado
set "PASTA=%PASTA:"=%"

rem arrastaram um .zip? usar a pasta descomprimida com o mesmo nome, se existir
if /i "%PASTA:~-4%"==".zip" set "PASTA=%PASTA:~0,-4%"

rem arrastaram um ficheiro (um .gpkg, por exemplo)? usar a pasta que o contem
if exist "%PASTA%" if not exist "%PASTA%\" (
  for %%F in ("%PASTA%") do set "PASTA=%%~dpF."
)

if not exist "%PASTA%\" goto :nao_existe

call :tem_dados "%PASTA%"
if errorlevel 1 goto :sem_dados

rem o .py (se existir) esta sempre mais actualizado que o .exe; o .exe e para
rem quem recebe a ferramenta sem Python
if exist "%~dp0qml2lyrx.py" (
  python "%~dp0qml2lyrx.py" --pasta "%PASTA%"
) else if exist "%~dp0qml2lyrx.exe" (
  "%~dp0qml2lyrx.exe" --pasta "%PASTA%"
) else if exist "%~dp0dist\qml2lyrx.exe" (
  "%~dp0dist\qml2lyrx.exe" --pasta "%PASTA%"
) else (
  echo.
  echo Nao encontro o qml2lyrx.py nem o qml2lyrx.exe nesta pasta.
  goto :fim
)

if errorlevel 1 (
  echo.
  echo Terminou com erros - ver acima.
) else (
  echo.
  echo Concluido. Os .lyrx e .stylx estao na pasta out\.
)
goto :fim

:sem_dados
echo.
echo Esta pasta nao tem GeoPackages nem zips: %PASTA%
echo Poe os ficheiros em geopackages\ ou indica outra pasta.
goto :fim

:nao_existe
echo.
echo Nao encontro: %PASTA%
echo Se arrastaste um ficheiro, arrasta a pasta onde ele esta.
echo Se e um .zip, descomprime-o primeiro.

:cancelado
:fim
echo.
pause
exit /b 0

rem ---------------------------------------------------------------- apoio
:tem_dados
dir /b /s "%~1\*.gpkg" >nul 2>&1
if not errorlevel 1 exit /b 0
dir /b /s "%~1\*.zip" >nul 2>&1
if not errorlevel 1 exit /b 0
exit /b 1
