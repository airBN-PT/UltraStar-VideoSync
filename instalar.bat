@echo off
chcp 65001 >nul
title Instalar UltraStar Video Sync
cd /d "%~dp0"
echo A instalar as bibliotecas necessárias...
py -m pip install --upgrade pip
py -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo ERRO: Não foi possível instalar as dependências.
  echo Confirma que tens Python 3.10 ou superior instalado.
  pause
  exit /b 1
)
echo.
echo Instalação concluída. Agora executa iniciar.bat
pause
