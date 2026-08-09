"""Пакетный сборщик: сканирование 5 папок (1..5 или hooks..payout) и сборка N видео попарно."""
from __future__ import annotations

import hashlib
import os
import random
import re
import sys
from typing import Optional

from .config import STAGE_KEYS, STAGES, VIDEO_EXT, Settings, TextStyle
from .pipeline import build, Result


def _natural_key(filename: str):
    """Сортировка с учетом чисел: 1.mp4, 2.mp4 ... 10.mp4."""
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r"(\d+)", filename)]


STAGE_FOLDER_ALIASES: dict[str, list[str]] = {
    "hooks": ["1", "01", "hooks", "hook", "1_hooks", "1_hook"],
    "screen_google": ["2", "02", "screen_google", "google", "2_screen_google", "2_google"],
    "domain_check": ["3", "03", "domain_check", "domain", "3_domain_check", "3_domain"],
    "prompt_input": ["4", "04", "prompt_input", "promocode", "prompt", "4_prompt_input", "4_promocode"],
    "payout": ["5", "05", "payout", "withdraw", "5_payout", "5_withdraw"],
}


def find_stage_folders(base_dir: str) -> dict[str, str]:
    """Ищет папки для каждого из 5 этапов по алиасам."""
    if not os.path.isdir(base_dir):
        raise FileNotFoundError(f"Указанная директория не существует: {base_dir}")

    subdirs = [d for d in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, d))]
    found_folders: dict[str, str] = {}

    for stage_key in STAGE_KEYS:
        aliases = STAGE_FOLDER_ALIASES.get(stage_key, [stage_key])
        matched_dir = None
        for alias in aliases:
            for d in subdirs:
                if d.lower() == alias.lower() or d.lower().startswith(f"{alias}_") or d.lower().startswith(f"{alias}-"):
                    matched_dir = os.path.join(base_dir, d)
                    break
            if matched_dir:
                break

        if matched_dir:
            found_folders[stage_key] = matched_dir

    missing = [k for k in STAGE_KEYS if k not in found_folders]
    if missing:
        raise FileNotFoundError(
            f"Не найдены папки для этапов: {', '.join(missing)} в папке '{base_dir}'.\n"
            f"Ожидаются подпапки: 1, 2, 3, 4, 5 (или hooks, screen_google, domain_check, prompt_input, payout)."
        )

    return found_folders


def collect_batch_files(stage_folders: dict[str, str], mode: str = "sequential") -> list[dict[str, str]]:
    """Собирает файлы из каждой папки и подготавливает списки наборов (batch items).
    
    Режимы:
    - 'sequential': попарный выбор по индексам (1.mp4, 2.mp4...)
    - 'soft': этап 1 (hooks) берется по порядку, а этапы 2-5 выбираются случайно (без удаления)
    - 'hard': этап 1 берется по порядку, а этапы 2-5 выбираются случайно (с последующим удалением исходников)
    """
    stage_files: dict[str, list[str]] = {}

    for stage_key, folder_path in stage_folders.items():
        files = [
            os.path.join(folder_path, f)
            for f in os.listdir(folder_path)
            if f.lower().endswith(VIDEO_EXT)
        ]
        files.sort(key=lambda p: _natural_key(os.path.basename(p)))
        if not files:
            raise FileNotFoundError(f"В папке этапа '{stage_key}' ({folder_path}) нет видеофайлов.")
        stage_files[stage_key] = files

    items: list[dict[str, str]] = []

    if mode == "sequential":
        min_count = min(len(files) for files in stage_files.values())
        print(f"[Batch] Последовательный режим: найдено комплектов: {min_count}")
        for i in range(min_count):
            item = {stage_key: stage_files[stage_key][i] for stage_key in STAGE_KEYS}
            items.append(item)
    else:
        # soft / hard: берем каждый хук из этапа 1, а 2-5 выбираем случайно
        hooks = stage_files["hooks"]
        print(f"[Batch] Режим «{mode}»: готовим {len(hooks)} роликов (рандом для 2-5 этапов)")
        for hook_path in hooks:
            item = {"hooks": hook_path}
            for stage_key in STAGE_KEYS:
                if stage_key == "hooks":
                    continue
                pool = stage_files[stage_key]
                item[stage_key] = random.choice(pool)
            items.append(item)

    return items


def _uniq_params(item: dict[str, str], index: int) -> dict:
    """Детерминированные параметры уникализации по содержимому 5 файлов.

    Один и тот же комплект исходников всегда получает одинаковый зум/цвет,
    разные комплекты — разные. Диапазоны подобраны так, чтобы глаз не видел
    разницы, а побайтовый/перцептивный хэш кадров менялся.
    """
    h = hashlib.sha256()
    for key in STAGE_KEYS:
        path = item.get(key, "")
        h.update(key.encode())
        h.update(os.path.basename(path).encode())
        try:
            with open(path, "rb") as fh:
                h.update(fh.read(1 << 20))          # первый мегабайт — быстро и достаточно
        except OSError:
            pass
    h.update(index.to_bytes(4, "big"))
    rng = random.Random(h.digest())

    zoom = 1.0 + rng.uniform(0.005, 0.045)          # 0.5–4.5% зум
    max_shift = int(1080 * (zoom - 1.0) / 2 * 0.8)
    return {
        "zoom": round(zoom, 4),
        "shift_x": rng.randint(-max_shift, max_shift) if max_shift else 0,
        "shift_y": rng.randint(-max_shift, max_shift) if max_shift else 0,
        "brightness": round(rng.uniform(-0.025, 0.025), 4),
        "contrast": round(rng.uniform(0.97, 1.04), 4),
        "saturation": round(rng.uniform(0.95, 1.06), 4),
    }


def _delete_used(item: dict[str, str]) -> None:
    """Удаляет исходники после успешного рендера."""
    for key in STAGE_KEYS:
        path = item.get(key)
        if path and os.path.isfile(path):
            try:
                os.remove(path)
                print(f"  🗑 Удалён исходник: {os.path.basename(path)}")
            except OSError as exc:
                print(f"  ⚠ Не смог удалить {path}: {exc}")


def run_batch(
    base_dir: str,
    output_dir: str,
    settings: Settings,
    texts_file: Optional[str] = None,
    dry_run: bool = False,
    delete_used: bool = False,
    uniquify: bool = False,
    mode: str = "sequential",
) -> list[Result]:
    """Запускает пакетный рендеринг роликов."""
    # Если передан mode='hard', принудительно включаем delete_used=True
    if mode == "hard":
        delete_used = True

    stage_folders = find_stage_folders(base_dir)
    batch_items = collect_batch_files(stage_folders, mode=mode)

    texts: list[str] = []
    if texts_file and os.path.isfile(texts_file):
        with open(texts_file, encoding="utf-8") as fh:
            texts = [line.strip() for line in fh if line.strip()]

    os.makedirs(output_dir, exist_ok=True)
    results: list[Result] = []

    total_count = len(batch_items)
    print(f"\n🚀 Запуск пакетной сборки {total_count} роликов в папку '{output_dir}'...\n")

    for idx, item in enumerate(batch_items, start=1):
        out_filename = os.path.join(output_dir, f"video_{idx:03d}.mp4")

        current_settings = Settings.from_dict(settings.to_dict())

        # Уникализация комплекта
        if uniquify:
            for k, v in _uniq_params(item, idx).items():
                setattr(current_settings, k, v)
            print(f"  • Уникализация: zoom={current_settings.zoom} "
                  f"shift=({current_settings.shift_x},{current_settings.shift_y}) "
                  f"b={current_settings.brightness} c={current_settings.contrast} s={current_settings.saturation}")

        # Если есть текст для этого ролика
        if texts:
            text_index = (idx - 1) % len(texts)
            current_settings.text.text = texts[text_index]

        print(f"\n==================== Ролик {idx}/{total_count} ====================")
        for stage_key in STAGE_KEYS:
            print(f"  • {stage_key}: {os.path.basename(item[stage_key])}")
        if current_settings.text.text:
            print(f"  • Текст: «{current_settings.text.text}»")

        if dry_run:
            from .analyze import plan_clip
            from .ffmpeg import probe
            total_in = total_out = 0.0
            for k in STAGE_KEYS:
                plan = plan_clip(probe(item[k]), current_settings)
                total_in += plan.info.duration
                total_out += plan.out_duration
            print(f"  [DRY-RUN] Расчетная длительность: {total_in:.1f}s → {total_out:.1f}s")
            continue

        def _report(stage: str, pct: float, msg: str):
            if msg:
                print(f"  [{pct * 100:5.1f}%] {msg}")

        res = build(item, current_settings, out_filename, report=_report)
        results.append(res)
        print(f"✅ Готово: {out_filename} ({res.duration:.1f}s)")
        if delete_used:
            _delete_used(item)

    print(f"\n🎉 Пакетная обработка завершена! Успешно собрано роликов: {len(results)}")
    return results
