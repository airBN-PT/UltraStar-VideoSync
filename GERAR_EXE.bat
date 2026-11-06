@echo off
setlocal
chcp 65001 >nul
title UltraStar Video Sync - Criar EXE
cd /d "%~dp0"

where py >nul 2>nul
if not errorlevel 1 (
  set "PY=py"
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo ERRO: Python nao encontrado. Instala Python no Windows.
    pause
    exit /b 1
  )
  set "PY=python"
)

echo.
echo [1/2] A instalar ou atualizar dependencias de compilacao...
%PY% -m pip install -r requirements.txt pyinstaller
if errorlevel 1 (
  echo ERRO ao instalar dependencias.
  pause
  exit /b 1
)

echo.
echo [2/2] A compilar o executavel Windows...
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --name UltraStar_VideoSync --collect-all numpy --collect-all scipy UltraStar_VideoSync.py
if errorlevel 1 (
  echo ERRO na compilacao. Consulta as mensagens acima.
  pause
  exit /b 1
)

echo.
echo SUCESSO!
echo O executavel encontra-se em:
echo %CD%\dist\UltraStar_VideoSync.exe
echo.
echo IMPORTANTE: instala FFmpeg ou coloca ffmpeg.exe e ffprobe.exe ao lado do EXE.
pause
