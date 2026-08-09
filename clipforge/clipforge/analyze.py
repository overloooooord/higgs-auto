"""Детектор «мёртвого времени» и планировщик сегментов.

Правило из ТЗ: если в кадре ничего не меняется дольше порога — это пауза
(ожидание загрузки страницы, курсор стоит). Такую паузу либо ускоряем,
либо вырезаем. Статику в начале и конце файла режем всегда.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .config import Settings
from .ffmpeg import MediaInfo, run_capture

FREEZE_START = re.compile(r"freeze_start:\s*([\d.]+)")
FREEZE_END = re.compile(r"freeze_end:\s*([\d.]+)")
FREEZE_DUR = re.compile(r"freeze_duration:\s*([\d.]+)")


@dataclass
class Segment:
    start: float
    end: float
    speed: float = 1.0

    @property
    def src_duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def out_duration(self) -> float:
        return self.src_duration / self.speed if self.speed > 0 else 0.0


@dataclass
class ClipPlan:
    info: MediaInfo
    segments: list[Segment]
    freezes: list[tuple[float, float]]
    trimmed_head: float = 0.0
    trimmed_tail: float = 0.0
    global_speed: float = 1.0

    @property
    def out_duration(self) -> float:
        return sum(s.out_duration for s in self.segments)

    def summary(self) -> str:
        saved = self.info.duration - self.out_duration
        return (f"{self.info.duration:.1f}s → {self.out_duration:.1f}s "
                f"(-{max(0.0, saved):.1f}s, пауз: {len(self.freezes)}, "
                f"сегментов: {len(self.segments)}, общее ускорение x{self.global_speed:g})")


def detect_freezes(info: MediaInfo, settings: Settings) -> list[tuple[float, float]]:
    """Возвращает список интервалов (start, end), где картинка не меняется."""
    min_dur = max(0.3, min(settings.pause_threshold, 2.0) * 0.5)
    stderr = run_capture([
        "-i", info.path,
        "-map", "0:v:0",
        "-vf", f"freezedetect=n={settings.freeze_noise}:d={min_dur:.3f}",
        "-an", "-f", "null", "-",
    ])

    freezes: list[tuple[float, float]] = []
    pending: Optional[float] = None
    for line in stderr.splitlines():
        m = FREEZE_START.search(line)
        if m:
            pending = float(m.group(1))
            continue
        m = FREEZE_END.search(line)
        if m and pending is not None:
            freezes.append((pending, float(m.group(1))))
            pending = None
    if pending is not None:                      # заморозка тянется до конца файла
        freezes.append((pending, info.duration))

    return [(max(0.0, s), min(info.duration, e)) for s, e in freezes if e > s]


def _pick_global_speed(duration: float, settings: Settings) -> float:
    """Подбирает ускорение из «красивого» ряда, чтобы уложиться в лимит этапа."""
    limit = settings.max_stage_seconds
    if limit <= 0 or duration <= limit:
        return 1.0
    for candidate in (1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0):
        if candidate > settings.max_speed:
            break
        if duration / candidate <= limit:
            return candidate
    return min(settings.max_speed, max(1.0, settings.max_speed))


def plan_clip(info: MediaInfo, settings: Settings) -> ClipPlan:
    duration = info.duration
    freezes = detect_freezes(info, settings) if (settings.trim_edges or settings.pause_threshold > 0) else []

    head, tail = 0.0, duration
    if settings.trim_edges and freezes:
        first_s, first_e = freezes[0]
        if first_s <= 0.35:
            head = max(0.0, min(first_e - settings.edge_pad, duration - 0.5))
        last_s, last_e = freezes[-1]
        if last_e >= duration - 0.35:
            tail = max(head + 0.5, min(duration, last_s + settings.edge_pad))

    interior = [(s, e) for s, e in freezes
                if e > head + 0.05 and s < tail - 0.05 and (e - s) >= settings.pause_threshold]
    interior = [(max(s, head), min(e, tail)) for s, e in interior]
    interior = [(s, e) for s, e in interior if e - s >= settings.pause_threshold]

    # Раскладываем [head, tail] на чередование «действие / пауза».
    raw: list[Segment] = []
    cursor = head
    for s, e in interior:
        if s - cursor > 0.01:
            raw.append(Segment(cursor, s, 1.0))
        if settings.pause_action == "cut":
            pad = settings.edge_pad
            if pad > 0.01 and e - s > pad * 2:
                raw.append(Segment(s, s + pad, 1.0))       # оставляем короткий «вдох»
        else:
            raw.append(Segment(s, e, max(1.0, settings.pause_speed)))
        cursor = e
    if tail - cursor > 0.01:
        raw.append(Segment(cursor, tail, 1.0))
    if not raw:
        raw = [Segment(head, max(tail, head + 0.2), 1.0)]

    segments = [s for s in raw if s.src_duration >= settings.min_segment or s.speed > 1.0]
    segments = [s for s in segments if s.src_duration > 0.05] or [Segment(head, tail or duration, 1.0)]

    # Общее ускорение поверх сегментного.
    out_dur = sum(s.out_duration for s in segments) * (1.0 / max(0.1, settings.base_speed))
    global_speed = settings.base_speed * _pick_global_speed(out_dur, settings)
    if abs(global_speed - 1.0) > 1e-3:
        for s in segments:
            s.speed *= global_speed

    return ClipPlan(info=info, segments=segments, freezes=freezes,
                    trimmed_head=head, trimmed_tail=duration - tail,
                    global_speed=round(global_speed, 3))
