"""Оркестратор: вход (5 файлов) → анализ → нормализация → склейка → текст."""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .analyze import ClipPlan, plan_clip
from .config import DEFAULT_TEXT_ZONES, STAGES, STAGE_KEYS, VIDEO_EXT, Settings, TextStyle
from .ffmpeg import MediaInfo, ensure_decodable, probe
from . import render

Reporter = Callable[[str, float, str], None]   # (stage, 0..1, сообщение)


def _noop(stage: str, pct: float, msg: str) -> None:
    if msg:
        print(f"[{pct * 100:5.1f}%] {msg}")


@dataclass
class Result:
    output: str
    duration: float
    source_duration: float
    plans: list[ClipPlan] = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def saved(self) -> float:
        return max(0.0, self.source_duration - self.duration)


def collect_inputs(folder: str) -> dict[str, str]:
    """Ищет 5 файлов: по подпапкам stage/ или по префиксу stage_* в имени."""
    found: dict[str, str] = {}
    for key, _ in STAGES:
        sub = os.path.join(folder, key)
        if os.path.isdir(sub):
            files = sorted(f for f in os.listdir(sub) if f.lower().endswith(VIDEO_EXT))
            if files:
                found[key] = os.path.join(sub, files[0])
                continue
        candidates = sorted(
            f for f in os.listdir(folder)
            if f.lower().endswith(VIDEO_EXT) and f.lower().startswith(key.lower())
        )
        if candidates:
            found[key] = os.path.join(folder, candidates[0])
    missing = [k for k in STAGE_KEYS if k not in found]
    if missing:
        raise FileNotFoundError(
            "Не найдены файлы для этапов: " + ", ".join(missing) +
            ".\nОжидается либо подпапка с именем этапа, либо файл с таким префиксом "
            "(например hooks.mp4 / hooks_01.mov)."
        )
    return found


def _zone_to_position(zone: str) -> str:
    """Зона из настроек в позицию TextStyle."""
    return {"top": "top", "center": "center", "bottom": "bottom", "avoid": "top"}.get(zone, "bottom")


def _split_main_text(text: str, order: list[str], zones: dict) -> dict[str, str]:
    """Распределяет главный текст по этапам.

    Разделитель ' || ' — части идут на этапы по порядку (пустая часть = без
    текста на этом этапе). Без разделителя весь текст показывается только на
    первом этапе (хук), чтобы не дублироваться на всех пяти.
    """
    parts = [p.strip() for p in text.split(" || ")]
    result: dict[str, str] = {}
    if len(parts) > 1:
        for i, key in enumerate(order):
            if i < len(parts) and parts[i]:
                result[key] = parts[i]
    elif order:
        result[order[0]] = text
    return result


def build(inputs: dict[str, str], settings: Settings, output: str,
          report: Optional[Reporter] = None, workdir: Optional[str] = None) -> Result:
    report = report or _noop
    started = time.time()
    settings.apply_preset()

    order = [k for k in STAGE_KEYS if k in inputs]
    if not order:
        raise ValueError("Нет входных файлов.")

    tmp = workdir or tempfile.mkdtemp(prefix="clipforge_")
    os.makedirs(tmp, exist_ok=True)
    cleanup = workdir is None

    try:
        # 1. Анализ ------------------------------------------------------
        plans: list[ClipPlan] = []
        for i, key in enumerate(order):
            label = dict(STAGES)[key]
            report("analyze", 0.05 * i / max(1, len(order)), f"Анализирую «{label}»…")
            info: MediaInfo = probe(inputs[key])
            ensure_decodable(info)
            plan = plan_clip(info, settings)
            plans.append(plan)
            report("analyze", 0.05 * (i + 1) / len(order),
                   f"  {label}: {plan.summary()}")

        # 2. Нормализация каждого клипа ---------------------------------
        clips, durations = [], []
        span = 0.75
        for i, (key, plan) in enumerate(zip(order, plans)):
            label = dict(STAGES)[key]
            dst = os.path.join(tmp, f"{i:02d}_{key}.mp4")
            base = 0.05 + span * i / len(order)
            report("render", base, f"Монтирую «{label}» ({i + 1}/{len(order)})…")
            render.render_clip(
                plan, settings, dst,
                on_progress=lambda p, b=base: report("render", b + span * p / len(order), ""),
            )
            clips.append(dst)
            durations.append(plan.out_duration)

        # 3. Текстовые слои ---------------------------------------------
        # Зоны по этапам: пользовательские перекрывают дефолтные, но каждый
        # этап получает зону из DEFAULT_TEXT_ZONES, если своя не задана.
        zones = {**DEFAULT_TEXT_ZONES, **(settings.text_zones or {})}

        text_files: list[tuple[TextStyle, str]] = []
        offsets = render.clip_offsets(durations, settings)
        for i, key in enumerate(order):
            caption = (settings.stage_captions or {}).get(key, "").strip()
            if not caption:
                continue
            style = TextStyle(**{**settings.text.__dict__})
            style.text = caption
            style.position = _zone_to_position(zones.get(key, "top"))
            style.size = int(settings.text.size * 0.72)
            style.start = offsets[i] + 0.15
            style.duration = max(0.6, durations[i] - 0.3)
            path = os.path.join(tmp, f"caption_{i}.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(render._wrap(caption, max(12, int(style.wrap * 1.3))))
            text_files.append((style, path))

        # 3b. Главный текст кусками по этапам, каждый в своей зоне.
        if settings.text.text.strip():
            zone_text = _split_main_text(settings.text.text.strip(), order, zones)
            for i, key in enumerate(order):
                chunk = zone_text.get(key, "").strip()
                if not chunk:
                    continue
                style = TextStyle(**{**settings.text.__dict__})
                style.text = chunk
                style.position = _zone_to_position(zones.get(key, "bottom"))
                style.start = offsets[i] + 0.15
                style.duration = max(0.6, durations[i] - 0.3)
                path = os.path.join(tmp, f"ztext_{i}.txt")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(render._wrap(chunk, settings.text.wrap))
                text_files.append((style, path))

        # 4. Склейка + текст --------------------------------------------
        report("concat", 0.82, "Склеиваю с переходами и накладываю текст…")
        os.makedirs(os.path.dirname(os.path.abspath(output)) or ".", exist_ok=True)
        total = render.concat_and_overlay(
            clips, durations, settings, text_files, output,
            on_progress=lambda p: report("concat", 0.82 + 0.17 * p, ""),
        )

        source_duration = sum(p.info.duration for p in plans)
        report("done", 1.0, f"Готово: {os.path.basename(output)} — {total:.1f} сек")
        return Result(output=output, duration=total, source_duration=source_duration,
                      plans=plans, elapsed=time.time() - started)
    finally:
        if cleanup:
            shutil.rmtree(tmp, ignore_errors=True)
