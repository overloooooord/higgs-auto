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

echo [1/4] Checking Google Chrome...
reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" >nul 2>&1
if errorlevel 1 (
    reg query "HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" >nul 2>&1
    if errorlevel 1 (
        echo [!] Google Chrome not found. Installing Chrome automatically...
        powershell -Command "$p = '$env:TEMP\chrome_installer.exe'; Write-Host 'Downloading Chrome...'; (New-Object System.Net.WebClient).DownloadFile('https://dl.google.com/chrome/install/latest/chrome_installer.exe', $p); Write-Host 'Installing Chrome...'; Start-Process $p -ArgumentList '/silent /install' -Wait; Remove-Item $p"
        echo [OK] Chrome installation complete!
    ) else (
        echo [OK] Chrome found
    )
) else (
    echo [OK] Chrome found
)

echo.
echo [2/4] Updating pip...
python -m pip install --upgrade pip

echo.
echo [3/4] Installing requirements...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [!] Error installing dependencies
    pause
    exit /b 1
)

echo.
echo [4/4] Checking seleniumbase...
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
