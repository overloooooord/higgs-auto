"""Тонкая обёртка над ffmpeg/ffprobe. Никаких внешних зависимостей."""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional


class FFmpegError(RuntimeError):
    pass


def _has_filter(exe: str, filt: str) -> bool:
    try:
        out = subprocess.run([exe, "-hide_banner", "-filters"],
                             capture_output=True, text=True, timeout=15).stdout
        return re.search(rf"^\s*\S+\s+{re.escape(filt)}\s", out, re.M) is not None
    except Exception:
        return False


def _find(name: str) -> str:
    exe = os.environ.get(name.upper() + "_BIN")
    if exe:
        return exe
    if name == "ffprobe":
        # рядом с выбранным ffmpeg — из той же сборки
        sibling = os.path.join(os.path.dirname(ffmpeg_bin()), "ffprobe")
        if os.path.isfile(sibling):
            return sibling
    # сначала полные сборки (со всеми кодеками/фильтрами), потом системный PATH
    search_dirs = sorted(glob.glob(os.path.expanduser("~/.local/opt/ffmpeg-*/bin"))
                         + glob.glob(os.path.expanduser("~/.local/opt/ffmpeg-*-static")))
    candidates = [os.path.join(d, name) for d in search_dirs
                  if os.path.isfile(os.path.join(d, name)) and os.access(os.path.join(d, name), os.X_OK)]
    sys_exe = shutil.which(name)
    if sys_exe:
        candidates.append(sys_exe)
    if name == "ffmpeg" and len(candidates) > 1:
        # нам обязательно нужен drawtext — берём первую сборку, где он есть
        with_text = [c for c in candidates if _has_filter(c, "drawtext")]
        if with_text:
            return with_text[0]
    if candidates:
        return candidates[0]
    raise FFmpegError(
        f"Не найден {name}. Установите FFmpeg и добавьте его в PATH "
        f"(или задайте переменную окружения {name.upper()}_BIN)."
    )


def ffmpeg_bin() -> str:
    return _find("ffmpeg")


def ffprobe_bin() -> str:
    return _find("ffprobe")


def check_tools() -> str:
    out = subprocess.run([ffmpeg_bin(), "-version"], capture_output=True, text=True)
    ffprobe_bin()
    return out.stdout.splitlines()[0] if out.stdout else "ffmpeg"


_H264_CANDIDATES = ("libx264", "libopenh264", "h264_nvenc", "h264_qsv", "h264_vaapi", "h264_v4l2m2m")
_encoder_cache: Optional[str] = None


def h264_encoder() -> str:
    """Первый доступный H.264 энкодер. libx264 предпочтителен, но в сборках
    без него (fedora/rpmfusion-free) берём openh264 или аппаратный."""
    global _encoder_cache
    if _encoder_cache:
        return _encoder_cache
    out = subprocess.run([ffmpeg_bin(), "-hide_banner", "-encoders"],
                         capture_output=True, text=True).stdout
    for name in _H264_CANDIDATES:
        if re.search(rf"^\s*V\S*\s+{re.escape(name)}\s", out, re.M):
            _encoder_cache = name
            return name
    raise FFmpegError(
        "В этой сборке ffmpeg нет ни одного H.264 энкодера "
        f"(искали: {', '.join(_H264_CANDIDATES)}). Установите полный ffmpeg с libx264."
    )


@dataclass
class MediaInfo:
    path: str
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    rotation: int = 0
    vcodec: str = ""

    @property
    def is_vertical(self) -> bool:
        return self.height >= self.width


def probe(path: str) -> MediaInfo:
    cmd = [
        ffprobe_bin(), "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", path,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise FFmpegError(f"ffprobe не смог прочитать файл {os.path.basename(path)}:\n{res.stderr.strip()}")
    data = json.loads(res.stdout or "{}")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise FFmpegError(f"В файле {os.path.basename(path)} нет видеодорожки.")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    duration = _to_float(data.get("format", {}).get("duration")) or _to_float(video.get("duration")) or 0.0
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)

    rotation = 0
    for sd in video.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = int(float(sd["rotation"])) % 360
    tag_rot = (video.get("tags") or {}).get("rotate")
    if tag_rot:
        rotation = int(float(tag_rot)) % 360
    if rotation in (90, 270):
        width, height = height, width

    fps = 30.0
    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "30/1"
    try:
        num, _, den = rate.partition("/")
        fps = float(num) / float(den or 1) if float(den or 1) else 30.0
    except (ValueError, ZeroDivisionError):
        fps = 30.0
    if not fps or fps > 240:
        fps = 30.0

    return MediaInfo(path=path, duration=duration, width=width, height=height,
                     fps=round(fps, 3), has_audio=has_audio, rotation=rotation,
                     vcodec=str(video.get("codec_name") or ""))


def ensure_decodable(info: "MediaInfo") -> None:
    """Падает с понятной ошибкой, если ffmpeg не может декодировать файл."""
    codec = info.vcodec or "unknown"
    out = subprocess.run([ffmpeg_bin(), "-hide_banner", "-decoders"],
                         capture_output=True, text=True).stdout
    if re.search(rf"^\s*V\S*\s+{re.escape(codec)}\s", out, re.M):
        return
    hint = ""
    if codec in ("hevc", "h265"):
        hint = (" Видео с iPhone снимается в HEVC, а в этой сборке ffmpeg нет его декодера.\n"
                "  Варианты:\n"
                "   • sudo dnf install ffmpeg --allowerasing   (RPM Fusion, полный ffmpeg)\n"
                "   • статическая сборка: https://johnvansickle.com/ffmpeg/\n"
                "   • на iPhone: Настройки → Камера → Форматы → Most Compatible (будет H.264)")
    raise FFmpegError(
        f"ffmpeg не умеет декодировать {codec} (файл: {os.path.basename(info.path)}).{hint}"
    )


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


PROGRESS_RE = re.compile(r"out_time_us=(\d+)")


def run(
    args: Iterable[str],
    total_seconds: float = 0.0,
    on_progress: Optional[Callable[[float], None]] = None,
    on_log: Optional[Callable[[str], None]] = None,
) -> str:
    """Запускает ffmpeg, отдаёт прогресс 0..1 и возвращает stderr."""
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-y"]
    cmd += list(args)
    if on_progress:
        cmd += ["-progress", "pipe:1", "-nostats"]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    err_lines: list[str] = []

    if on_progress and proc.stdout is not None:
        for line in proc.stdout:
            m = PROGRESS_RE.search(line)
            if m and total_seconds > 0:
                done = int(m.group(1)) / 1_000_000.0
                on_progress(max(0.0, min(1.0, done / total_seconds)))
    if proc.stderr is not None:
        for line in proc.stderr:
            err_lines.append(line.rstrip())
            if on_log:
                on_log(line.rstrip())
    proc.wait()
    stderr = "\n".join(err_lines)
    if proc.returncode != 0:
        tail = "\n".join(err_lines[-25:])
        raise FFmpegError(f"ffmpeg завершился с ошибкой (код {proc.returncode}):\n{tail}")
    if on_progress:
        on_progress(1.0)
    return stderr


def run_capture(args: Iterable[str]) -> str:
    """Запуск ради stderr (детекторы). Ошибки не считаются фатальными."""
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin"] + list(args)
    res = subprocess.run(cmd, capture_output=True, text=True)
    return res.stderr


def escape_filter_path(path: str) -> str:
    """Экранирование пути внутри filter_complex (drawtext textfile/fontfile)."""
    out = path.replace("\\", "/")
    out = out.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return out


def atempo_chain(speed: float) -> str:
    """atempo принимает 0.5..2.0 надёжно — раскладываем ускорение в цепочку."""
    speed = max(0.05, float(speed))
    parts: list[str] = []
    remaining = speed
    while remaining > 2.0:
        parts.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        parts.append("atempo=0.5")
        remaining /= 0.5
    if abs(remaining - 1.0) > 1e-3:
        parts.append(f"atempo={remaining:.6f}")
    return ",".join(parts) if parts else "anull"
