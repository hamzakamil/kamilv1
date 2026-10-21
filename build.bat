@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo HATA: python bulunamadi. Once Python 3.12 kurun ve PATH'e ekleyin.
    exit /b 1
)

if not exist .venv (
    echo .venv olusturuluyor...
    python -m venv .venv
    if errorlevel 1 exit /b 1
)

set PY=.venv\Scripts\python.exe

echo Bagimliliklar kuruluyor...
"%PY%" -m pip install --disable-pip-version-check --upgrade pip
if errorlevel 1 exit /b 1
"%PY%" -m pip install --disable-pip-version-check -r requirements.txt pyinstaller
if errorlevel 1 exit /b 1

echo Derleniyor (birkac dakika surebilir)...
"%PY%" -m PyInstaller --noconfirm --clean --noupx --onefile --noconsole --name KamilV1 main.py
if errorlevel 1 exit /b 1

copy /y dist\KamilV1.exe dist\setup.exe >nul
if errorlevel 1 exit /b 1

echo.
echo Tamam: dist\KamilV1.exe ve dist\setup.exe hazir.
echo Karsi bilgisayarda Python kurulu olmasina gerek yoktur.
exit /b 0
