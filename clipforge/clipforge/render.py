"""Сборка filter_complex: нормализация клипов, склейка с переходом, текст."""
from __future__ import annotations

import base64
import hashlib
import os
from typing import Callable, Optional

from .analyze import ClipPlan
from .config import Settings, TextStyle, resolve_font
from .ffmpeg import atempo_chain, escape_filter_path, h264_encoder, run

Progress = Optional[Callable[[float], None]]
Log = Optional[Callable[[str], None]]


def _vcodec_args(settings: Settings) -> list[str]:
    """Аргументы видеокодека: -crf/-preset только там, где они поддерживаются."""
    enc = h264_encoder()
    args = ["-c:v", enc]
    if enc in ("libx264", "h264_nvenc", "h264_qsv"):
        args += ["-preset", settings.x264_preset, "-crf", str(settings.crf)]
    elif enc == "libopenh264":
        args += ["-b:v", "6M"]                # openh264 не понимает -crf
    return args


# ------------------------------------------------------------ вписывание в кадр

def _uniq_filter(settings: Settings) -> str:
    """Микро-уникализация: зум/сдвиг/цвет — незаметно глазу, меняет хэш кадра."""
    parts: list[str] = []
    zoom = max(1.0, float(settings.zoom or 1.0))
    if zoom > 1.0005:
        zw = int(settings.width * zoom) & ~1
        zh = int(settings.height * zoom) & ~1
        parts.append(f"scale={zw}:{zh}")
        cx = (zw - settings.width) // 2 + int(settings.shift_x)
        cy = (zh - settings.height) // 2 + int(settings.shift_y)
        cx = max(0, min(zw - settings.width, cx))
        cy = max(0, min(zh - settings.height, cy))
        parts.append(f"crop={settings.width}:{settings.height}:{cx}:{cy}")
    if (abs(settings.brightness) > 0.001 or abs(settings.contrast - 1.0) > 0.001
            or abs(settings.saturation - 1.0) > 0.001):
        b = max(-1.0, min(1.0, settings.brightness))
        c = max(0.5, min(2.0, settings.contrast))
        s = max(0.0, min(3.0, settings.saturation))
        parts.append(f"eq=brightness={b:.4f}:contrast={c:.4f}:saturation={s:.4f}")
    return ",".join(parts)


def _fit_filter(settings: Settings) -> str:
    w, h, fps = settings.width, settings.height, settings.fps
    if settings.fill == "crop":
        return (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h},fps={fps},setsar=1,format=yuv420p")
    if settings.fill == "pad":
        return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,fps={fps},setsar=1,format=yuv420p")
    # blur: размытая заливка полей — самый аккуратный вариант для вертикали
    return (
        f"split=2[bg][fg];"
        f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
        f"gblur=sigma=28,eq=brightness=-0.12[bgb];"
        f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,fps={fps},setsar=1,format=yuv420p"
    )


def _normalize_filter(settings: Settings) -> str:
    """Вписывание в формат + уникализация одной цепочкой."""
    fit = _fit_filter(settings)
    uniq = _uniq_filter(settings)
    return f"{fit},{uniq}" if uniq else fit


# ------------------------------------------------------------ один клип

def build_clip_filter(plan: ClipPlan, settings: Settings, with_audio: bool) -> str:
    parts: list[str] = []
    labels_v: list[str] = []
    labels_a: list[str] = []

    for i, seg in enumerate(plan.segments):
        speed = max(0.05, seg.speed)
        parts.append(
            f"[0:v]trim=start={seg.start:.4f}:end={seg.end:.4f},"
            f"setpts=(PTS-STARTPTS)/{speed:.6f}[v{i}]"
        )
        labels_v.append(f"[v{i}]")
        if with_audio:
            parts.append(
                f"[0:a]atrim=start={seg.start:.4f}:end={seg.end:.4f},"
                f"asetpts=PTS-STARTPTS,{atempo_chain(speed)}[a{i}]"
            )
            labels_a.append(f"[a{i}]")

    n = len(plan.segments)
    if with_audio:
        pairs = "".join(labels_v[i] + labels_a[i] for i in range(n))
        parts.append(f"{pairs}concat=n={n}:v=1:a=1[cv][ca]")
        parts.append(f"[cv]{_normalize_filter(settings)}[outv]")
        parts.append("[ca]aresample=async=1:first_pts=0,aformat=sample_rates=48000:channel_layouts=stereo[outa]")
    else:
        parts.append(f"{''.join(labels_v)}concat=n={n}:v=1:a=0[cv]")
        parts.append(f"[cv]{_normalize_filter(settings)}[outv]")
    return ";".join(parts)


def render_clip(plan: ClipPlan, settings: Settings, dst: str,
                on_progress: Progress = None, on_log: Log = None) -> float:
    with_audio = settings.keep_audio and plan.info.has_audio
    args = ["-i", plan.info.path,
            "-filter_complex", build_clip_filter(plan, settings, with_audio),
            "-map", "[outv]"]
    if with_audio:
        args += ["-map", "[outa]", "-c:a", "aac", "-b:a", settings.audio_bitrate, "-ar", "48000"]
    elif settings.keep_audio:
        # у исходника нет звука — подкладываем тишину, иначе рассыплется acrossfade
        args = ["-i", plan.info.path, "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-filter_complex", build_clip_filter(plan, settings, False),
                "-map", "[outv]", "-map", "1:a", "-shortest",
                "-c:a", "aac", "-b:a", settings.audio_bitrate]
    else:
        args += ["-an"]
    args += _vcodec_args(settings)
    args += ["-pix_fmt", "yuv420p", "-movflags", "+faststart", dst]
    run(args, total_seconds=plan.out_duration, on_progress=on_progress, on_log=on_log)
    return plan.out_duration


# ------------------------------------------------------------ склейка

def build_concat_filter(durations: list[float], settings: Settings, with_audio: bool) -> tuple[str, float]:
    n = len(durations)
    d = max(0.0, float(settings.crossfade))
    if n == 1:
        d = 0.0
    if d > 0:
        d = min(d, min(durations) * 0.45)
    # у xfade есть минимальная длительность; при коротких клипах ужимаем переход
    if d > 0 and min(durations) < 0.6:
        d = max(0.0, min(d, min(durations) - 0.1))

    if d <= 0.02:
        pairs = "".join(f"[{i}:v]" + (f"[{i}:a]" if with_audio else "") for i in range(n))
        f = f"{pairs}concat=n={n}:v=1:a={1 if with_audio else 0}[cat]"
        f += "[outa]" if with_audio else ""
        return f, sum(durations)

    parts: list[str] = []
    prev_v, prev_a = "[0:v]", "[0:a]"
    total = durations[0]
    for i in range(1, n):
        offset = total - d
        out_v = f"[xv{i}]" if i < n - 1 else "[cat]"
        parts.append(f"{prev_v}[{i}:v]xfade=transition={settings.transition}:"
                     f"duration={d:.3f}:offset={offset:.3f}{out_v}")
        if with_audio:
            out_a = f"[xa{i}]" if i < n - 1 else "[outa]"
            parts.append(f"{prev_a}[{i}:a]acrossfade=d={d:.3f}:c1=tri:c2=tri{out_a}")
            prev_a = out_a
        prev_v = out_v
        total = total + durations[i] - d
    return ";".join(parts), total


def clip_offsets(durations: list[float], settings: Settings) -> list[float]:
    """Начало каждого клипа в итоговом ролике — нужно для подписей по этапам."""
    d = max(0.0, float(settings.crossfade))
    if len(durations) > 1 and d > 0:
        d = min(d, min(durations) * 0.45)
    else:
        d = 0.0
    offsets, cursor = [], 0.0
    for dur in durations:
        offsets.append(cursor)
        cursor += dur - d
    return offsets


# ------------------------------------------------------------ текст

def _meta_token(settings: Settings) -> str:
    """Случайный токен в метаданных — файлы с одинаковым контентом имеют разные хэши."""
    seed = f"{settings.zoom}|{settings.shift_x}|{settings.shift_y}|{settings.brightness}|{settings.contrast}|{settings.saturation}"
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return "cf-" + base64.urlsafe_b64encode(digest[:9]).decode("ascii").rstrip("=")


def _wrap(text: str, width: int) -> str:
    if width <= 0:
        return text
    out_lines: list[str] = []
    for raw in text.splitlines() or [""]:
        line = ""
        for word in raw.split():
            probe = f"{line} {word}".strip()
            if len(probe) <= width or not line:
                line = probe
            else:
                out_lines.append(line)
                line = word
        out_lines.append(line)
    return "\n".join(out_lines)


def _y_expr(style: TextStyle, height: int) -> str:
    if style.position == "top":
        return f"{int(height * 0.10)}"
    if style.position == "center":
        return "(h-text_h)/2"
    if style.position == "custom":
        return str(int(style.y))
    return f"h*0.74-text_h/2"        # нижняя треть


def _x_expr(style: TextStyle) -> str:
    return str(int(style.x)) if style.position == "custom" else "(w-text_w)/2"


def _color(value: str) -> str:
    """#rrggbb из UI → 0xrrggbb, понятный ffmpeg. Именованные цвета не трогаем."""
    v = (value or "").strip()
    if v.startswith("#") and len(v) in (7, 9):
        return "0x" + v[1:]
    if v.startswith("#") and len(v) == 4:
        return "0x" + "".join(c * 2 for c in v[1:])
    return v or "white"


def build_drawtext(style: TextStyle, settings: Settings, textfile: str,
                   total_duration: float) -> str:
    scale = settings.height / 1920.0 if settings.height >= settings.width else settings.width / 1920.0
    size = max(14, int(round(style.size * max(0.5, scale))))
    pad = max(6, int(round(style.box_padding * max(0.5, scale))))

    start = max(0.0, float(style.start))
    end = total_duration if style.duration <= 0 else min(total_duration, start + style.duration)
    fade = max(0.0, min(float(style.fade), (end - start) / 2 if end > start else 0.0))

    opts = [
        f"textfile='{escape_filter_path(textfile)}'",
        f"fontfile='{escape_filter_path(resolve_font(style.font, style.text))}'",
        f"fontsize={size}",
        f"fontcolor={_color(style.color)}",
        f"x={_x_expr(style)}",
        f"y={_y_expr(style, settings.height)}",
        f"line_spacing={max(0, int(style.line_spacing * max(0.5, scale)))}",
        "text_align=C",
    ]
    if style.box:
        opts += [f"box=1:boxcolor={_color(style.box_color)}@{max(0.0, min(1.0, style.box_opacity)):.2f}",
                 f"boxborderw={pad}"]
    if style.shadow:
        opts += ["shadowcolor=black@0.65", f"shadowx={max(1, int(2 * scale))}",
                 f"shadowy={max(1, int(2 * scale))}"]

    if fade > 0.05:
        alpha = (f"if(lt(t,{start:.3f}),0,"
                 f"if(lt(t,{start + fade:.3f}),(t-{start:.3f})/{fade:.3f},"
                 f"if(lt(t,{end - fade:.3f}),1,"
                 f"if(lt(t,{end:.3f}),({end:.3f}-t)/{fade:.3f},0))))")
        opts.append(f"alpha='{alpha}'")
    opts.append(f"enable='between(t,{start:.3f},{end:.3f})'")
    return "drawtext=" + ":".join(opts)


def concat_and_overlay(clips: list[str], durations: list[float], settings: Settings,
                       text_files: list[tuple[TextStyle, str]], dst: str,
                       on_progress: Progress = None, on_log: Log = None) -> float:
    with_audio = settings.keep_audio
    chain, total = build_concat_filter(durations, settings, with_audio)

    draw = [build_drawtext(style, settings, path, total) for style, path in text_files]
    if draw:
        chain += ";[cat]" + ",".join(draw) + "[outv]"
    else:
        chain += ";[cat]null[outv]"

    args: list[str] = []
    for c in clips:
        args += ["-i", c]
    args += ["-filter_complex", chain, "-map", "[outv]"]
    if with_audio:
        args += ["-map", "[outa]", "-c:a", "aac", "-b:a", settings.audio_bitrate]
    else:
        args += ["-an"]
    args += _vcodec_args(settings)
    args += ["-pix_fmt", "yuv420p", "-movflags", "+faststart",
             "-metadata", f"comment={_meta_token(settings)}"]
    args += [dst]
    run(args, total_seconds=total, on_progress=on_progress, on_log=on_log)
    return total
