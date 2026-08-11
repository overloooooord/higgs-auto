@echo off
chcp 65001 >nul
title ClipForge - Install

echo ==========================================
echo        ClipForge - Install
echo ==========================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [!] Python not found.
    echo     Download Python from https://python.org
    pause
    exit /b 1
)

echo [1/3] Updating pip...
python -m pip install --upgrade pip

echo [2/3] Installing requirements...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [!] Error installing dependencies
    pause
    exit /b 1
)

echo [3/3] Checking seleniumbase...
python -c "import seleniumbase; print('seleniumbase OK:', seleniumbase.__version__)"
if errorlevel 1 (
    echo [!] seleniumbase check failed
    pause
    exit /b 1
)

echo.
echo ==========================================
echo   Installation Complete!
echo.
echo   To launch UI:
echo   set ANYMESSAGE_KEY=your_key
echo   python run.py ui
echo ==========================================
echo.
pause
