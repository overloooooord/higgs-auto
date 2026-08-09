# ClipForge

Автомонтаж короткого ролика из пяти исходников: обрезает статику, ускоряет
зависания, склеивает этапы с переходом, накладывает текст, отдаёт готовый MP4
1080×1920 (или 1920×1080).

Зависимостей нет — только Python 3.9+ и FFmpeg в `PATH` (или полная сборка
в `~/.local/opt/ffmpeg-*` — находится автоматически).

```bash
python run.py check          # проверить окружение
python run.py ui             # веб-интерфейс на http://127.0.0.1:8420
python run.py build --input ./raw -t "Текст поверх видео" -o ready.mp4
python run.py build --input ./raw --dry-run    # посчитать без рендера
```

Порядок склейки фиксирован: `hooks → screen_google → domain_check →
prompt_input → payout`. Файлы ищутся либо в одноимённых подпапках, либо по
префиксу в имени.

## Пакетный режим (N роликов из 5 папок)

Папки `1..5` (или `hooks`, `google`, `domain`, `prompt`, `payout`), по N видео
в каждой. Ролик №1 = первое видео из каждой папки, №2 = второе и т.д.

```bash
python -m clipforge.cli --batch --input ./raw_batch --out-dir ./output \
    --texts texts.txt \
    --uniquify \          # микро-уникализация: зум/сдвиг/цвет/метаданные
    --delete-used \       # удалять 5 исходников после успешной сборки
    --text-zone hooks=top --text-zone payout=avoid   # позиция текста по этапам
```

`--uniquify` — детерминированная (по содержимому комплекта) микро-правка
кадра: зум 0.5–4.5%, сдвиг, яркость/контраст/насыщенность ±3%, случайный
токен в метаданных. Глаз разницы не видит, хэши у каждого ролика свои.

## Генерация хуков (Higgsfield)

```bash
export ANYMESSAGE_KEY="..."
python -m clipforge.generate_hooks --count 60 \
    --videos-dir ./hook_refs --models-dir ./models --output-dir ./raw_batch/1
```

Цикл: временная почта AnyMessage → регистрация → пропуск онбординга →
Kling Motion 2.6, 1080p, фон = видео из `--videos-dir` (по порядку), фото
модели из `--models-dir` → скачивание mp4. Селекторы собраны в классе `Sel`
в начале `generate_hooks.py`; при сбоях запустите с `--headed` и смотрите,
какой шаг отвалился (скриншоты `fail_*.png` складываются в output-dir).

Подробности, все параметры, рецепты и разбор «запросы или Camoufox» — в
[MANUAL.md](MANUAL.md).
