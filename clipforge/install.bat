@echo off
chcp 65001 >nul
title ClipForge — Установка зависимостей
color 0A

echo.
echo  ╔══════════════════════════════════════════╗
echo  ║       ClipForge — Установка             ║
echo  ╚══════════════════════════════════════════╝
echo.

:: Проверяем Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [!] Python не найден. Скачай с https://python.org и поставь галочку "Add to PATH"
    pause
    exit /b 1
)
python --version

echo.
echo [1/4] Обновляю pip...
python -m pip install --upgrade pip

echo.
echo [2/4] Устанавливаю зависимости из requirements.txt...
python -m pip install -r requirements.txt

echo.
echo [3/4] Устанавливаю seleniumbase (с патчером chromedriver)...
python -m pip install seleniumbase --upgrade

echo.
echo [4/4] Патчу chromedriver под текущий Chrome...
python -m seleniumbase get chromedriver --uc

echo.
echo  ╔══════════════════════════════════════════╗
echo  ║   Установка завершена! Запуск UI:       ║
echo  ║   > python run.py ui                    ║
echo  ╚══════════════════════════════════════════╝
echo.
pause
