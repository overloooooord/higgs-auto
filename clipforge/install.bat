@echo off
chcp 65001 >nul
title ClipForge — Установка зависимостей
color 0A

echo.
echo  +==========================================+
echo  |       ClipForge -- Установка            |
echo  +==========================================+
echo.

:: Проверяем Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [!] Python не найден.
    echo     Скачай Python 3.11 или 3.12 с https://python.org
    echo     При установке поставь галочку "Add to PATH"
    pause
    exit /b 1
)

for /f "tokens=2 delims= " %%V in ('python --version 2^>^&1') do set PYVER=%%V
echo [OK] Python %PYVER%

echo.
echo [1/3] Обновляю pip...
python -m pip install --upgrade pip --quiet

echo.
echo [2/3] Устанавливаю зависимости...
python -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo [!] Ошибка при установке зависимостей
    pause
    exit /b 1
)
echo [OK] Зависимости установлены

echo.
echo [3/3] Проверка установки...
python -c "import seleniumbase; print('[OK] seleniumbase', seleniumbase.__version__)"
if errorlevel 1 (
    echo [!] seleniumbase не установлен
    pause
    exit /b 1
)

echo.
echo  +==========================================+
echo  |   Установка завершена!                  |
echo  |                                          |
echo  |   Запуск UI:                             |
echo  |   set ANYMESSAGE_KEY=твой_ключ          |
echo  |   python run.py ui                       |
echo  |                                          |
echo  |   Браузер откроется сам, или зайди:     |
echo  |   http://127.0.0.1:8420/                |
echo  +==========================================+
echo.
echo  ПРИМЕЧАНИЕ: chromedriver скачается
echo  автоматически при первом запуске.
echo.
pause
