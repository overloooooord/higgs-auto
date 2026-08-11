# ClipForge — AI-генерация хуков через Higgsfield

## Быстрый старт на новом ПК (Windows)

### 1. Клонируй репозиторий
```bash
git clone https://github.com/overloooooord/higgs-auto.git
cd higgs-auto\clipforge
```

### 2. Установи зависимости
Дважды кликни `install.bat` или в терминале:
```bash
install.bat
```
> Требуется Python 3.10+ и Google Chrome актуальной версии.

### 3. Запусти UI
```bash
python run.py ui
```
Откроется браузер с интерфейсом на http://127.0.0.1:8420/

---

## Структура папок

```
clipforge/
├── models/          ← Фото моделей (.png / .jpg)
├── hook_refs/       ← Видео-хуки (.mp4 / .mov)
├── raw_batch/       ← Готовые сгенерированные видео
├── hooks_out/
│   └── credentials.txt  ← Аккаунты Higgsfield (email:password)
├── install.bat      ← Установка зависимостей (Windows)
├── requirements.txt
└── run.py           ← Точка входа
```

## Настройка

В UI можно:
- Включать/выключать отдельные модели через тоггл
- Задавать кол-во видео на каждую модель (N)
- Настраивать кол-во параллельных потоков (workers)
- Указывать папку сохранения результатов

## Требования

- Python 3.10+
- Google Chrome (актуальная версия)
- Ключ AnyMessage API (задаётся в переменной окружения `ANYMESSAGE_KEY`)

```bash
set ANYMESSAGE_KEY=твой_ключ
python run.py ui
```
