"""Настройки, пресеты, поиск шрифтов."""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from typing import Optional

# Порядок склейки задан ТЗ и не меняется.
STAGES: tuple[tuple[str, str], ...] = (
    ("hooks", "Зацепка"),
    ("screen_google", "Поиск в Google"),
    ("domain_check", "Проверка домена"),
    ("prompt_input", "Ввод промпта"),
    ("payout", "Выплата"),
)
STAGE_KEYS = tuple(k for k, _ in STAGES)

# Зона текста по умолчанию для каждого этапа. Важные элементы (строка Google,
# домен, поле промокода, баланс) двигаются от дубля к дублю, поэтому текст
# держим в противоположной части кадра. При съёмке оставляйте эту зону пустой.
DEFAULT_TEXT_ZONES: dict[str, str] = {
    "hooks": "top",            # хук — сверху
    "screen_google": "bottom", # поиск Google — снизу (строка поиска сверху)
    "domain_check": "top",     # проверка домена — сверху (домен снизу/по центру)
    "prompt_input": "bottom",  # ввод промокода — снизу (поле сверху)
    "payout": "center",        # выплата — по центру
}

VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".mpg", ".mpeg", ".ts")

PRESETS = {
    "reels": {"width": 1080, "height": 1920, "fps": 30, "label": "Reels / TikTok / Shorts 9:16"},
    "youtube": {"width": 1920, "height": 1080, "fps": 30, "label": "YouTube 16:9"},
    "square": {"width": 1080, "height": 1080, "fps": 30, "label": "Квадрат 1:1"},
}

FONT_CANDIDATES = ("Montserrat", "Inter", "Roboto", "Poppins", "Open Sans",
                   "Liberation Sans", "DejaVu Sans")


@dataclass
class TextStyle:
    text: str = ""
    font: str = "auto"              # имя семейства, путь к .ttf или "auto"
    size: int = 64                  # кегль для высоты 1920; масштабируется под другой формат
    color: str = "white"
    position: str = "bottom"        # top | center | bottom | custom
    x: int = 0                      # для position=custom (пиксели итогового кадра)
    y: int = 0
    box: bool = True                # полупрозрачная подложка
    box_color: str = "black"
    box_opacity: float = 0.45
    box_padding: int = 28
    shadow: bool = True
    fade: float = 0.4               # плавное появление/исчезание, сек (0 = выкл)
    start: float = 0.0              # когда показать, сек от начала ролика
    duration: float = 0.0           # 0 = до конца
    wrap: int = 22                  # авто-перенос, символов в строке (0 = выкл)
    line_spacing: int = 12


@dataclass
class Settings:
    # Формат
    preset: str = "reels"
    width: int = 1080
    height: int = 1920
    fps: int = 30
    fill: str = "blur"              # blur | pad | crop — как вписать кадр в формат

    # Умный монтаж
    trim_edges: bool = True         # резать статику в начале/конце
    edge_pad: float = 0.15          # сколько статики оставить на стыке, сек
    pause_threshold: float = 1.0    # пауза длиннее — считается «зависанием»
    pause_action: str = "speed"     # speed | cut
    pause_speed: float = 5.0        # во сколько раз ускорять зависания
    max_stage_seconds: float = 12.0 # если этап длиннее — общее ускорение
    max_speed: float = 4.0          # потолок общего ускорения
    base_speed: float = 1.0         # ускорение всего материала (1 = нет)
    freeze_noise: float = 0.003     # чувствительность детектора статики (0.001..0.02)
    min_segment: float = 0.20       # короче — сегмент выбрасывается

    # Склейка
    crossfade: float = 0.5          # длительность перехода, сек (0 = встык)
    transition: str = "fade"        # любой xfade-переход: fade, fadeblack, slideleft...

    # Звук
    keep_audio: bool = False
    audio_bitrate: str = "192k"

    # Кодек
    crf: int = 20
    x264_preset: str = "medium"

    # Уникализация (заполняется пакетным режимом при --uniquify)
    zoom: float = 1.0               # лёгкий зум кадра (1.01..1.05)
    shift_x: int = 0                # сдвиг кадра после зума, пиксели
    shift_y: int = 0
    brightness: float = 0.0         # -1..1
    contrast: float = 1.0           # ~0.97..1.04
    saturation: float = 1.0         # ~0.95..1.06

    # Текст
    text: TextStyle = field(default_factory=TextStyle)
    stage_captions: dict = field(default_factory=dict)  # {stage_key: "подпись"}
    text_zones: dict = field(default_factory=dict)      # {stage_key: "top"|"center"|"bottom"|"avoid"}

    def apply_preset(self) -> "Settings":
        p = PRESETS.get(self.preset)
        if p:
            self.width, self.height, self.fps = p["width"], p["height"], p["fps"]
        return self

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        data = dict(data or {})
        text_data = data.pop("text", {}) or {}
        captions = data.pop("stage_captions", {}) or {}
        zones = data.pop("text_zones", {}) or {}
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in known}
        s = cls(**clean)
        tf = {f for f in TextStyle.__dataclass_fields__}
        s.text = TextStyle(**{k: v for k, v in text_data.items() if k in tf})
        s.stage_captions = {k: str(v) for k, v in captions.items() if k in STAGE_KEYS and str(v).strip()}
        s.text_zones = {k: str(v).strip().lower() for k, v in zones.items()
                        if k in STAGE_KEYS and str(v).strip().lower() in ("top", "center", "bottom", "avoid")}
        if s.preset in PRESETS:
            s.apply_preset()
        return s


# ---------------------------------------------------------------- шрифты

def _fc_match(family: str) -> Optional[str]:
    """fc-match всегда что-то возвращает, поэтому проверяем, что семейство совпало."""
    if not shutil.which("fc-match"):
        return None
    try:
        out = subprocess.run(["fc-match", "-f", "%{family}|%{file}", family],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return None
    got_family, _, path = out.partition("|")
    key = family.replace(" ", "").lower()
    if path and key in got_family.replace(" ", "").lower():
        return path
    return None


def _cmap_codepoints(path: str) -> Optional[set]:
    """Минимальный парсер cmap (формат 4 и 12) — чтобы не тащить fonttools."""
    import struct
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        if data[:4] == b"ttcf":
            offset = struct.unpack(">I", data[12:16])[0]
        else:
            offset = 0
        num_tables = struct.unpack(">H", data[offset + 4:offset + 6])[0]
        cmap_off = None
        for i in range(num_tables):
            rec = offset + 12 + i * 16
            if data[rec:rec + 4] == b"cmap":
                cmap_off = struct.unpack(">I", data[rec + 8:rec + 12])[0]
                break
        if cmap_off is None:
            return None
        n = struct.unpack(">H", data[cmap_off + 2:cmap_off + 4])[0]
        best = None
        for i in range(n):
            rec = cmap_off + 4 + i * 8
            pid, eid = struct.unpack(">HH", data[rec:rec + 4])
            sub = cmap_off + struct.unpack(">I", data[rec + 4:rec + 8])[0]
            fmt = struct.unpack(">H", data[sub:sub + 2])[0]
            if (pid, eid) in ((3, 10), (0, 4), (0, 6)) and fmt == 12:
                best = (sub, fmt)
                break
            if (pid, eid) in ((3, 1), (0, 3)) and fmt == 4 and best is None:
                best = (sub, fmt)
        if best is None:
            return None
        sub, fmt = best
        points: set = set()
        if fmt == 4:
            seg2 = struct.unpack(">H", data[sub + 6:sub + 8])[0]
            seg = seg2 // 2
            ends = struct.unpack(f">{seg}H", data[sub + 14:sub + 14 + seg2])
            starts_off = sub + 16 + seg2
            starts = struct.unpack(f">{seg}H", data[starts_off:starts_off + seg2])
            for s, e in zip(starts, ends):
                if s == 0xFFFF:
                    continue
                points.update(range(s, min(e, s + 4096) + 1))
        else:
            groups = struct.unpack(">I", data[sub + 12:sub + 16])[0]
            for g in range(min(groups, 3000)):
                off = sub + 16 + g * 12
                s, e = struct.unpack(">II", data[off:off + 8])
                points.update(range(s, min(e, s + 4096) + 1))
        return points
    except Exception:
        return None


def font_supports(path: str, sample: str) -> bool:
    chars = {ord(c) for c in sample if c.strip() and ord(c) > 0x7F}
    if not chars:
        return True
    points = _cmap_codepoints(path)
    if points is None:
        return True                      # не смогли прочитать — не мешаем пользователю
    missing = chars - points
    return not missing


def _local_font_dirs() -> list[str]:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [
        os.path.join(here, "fonts"),
        os.path.expanduser("~/.fonts"),
        os.path.expanduser("~/Library/Fonts"),
        "C:/Windows/Fonts",
        "/usr/share/fonts",
        "/usr/local/share/fonts",
        "/System/Library/Fonts",
    ]


def _norm(name: str) -> str:
    return name.replace(" ", "").replace("-", "").replace("_", "").lower()


def _weight_rank(name: str) -> int:
    """Чем меньше — тем лучше для крупного текста поверх видео."""
    n = _norm(name)
    if "italic" in n or "oblique" in n:
        return 90
    for i, w in enumerate(("extrabold", "semibold", "bold", "black", "medium", "regular", "book")):
        if w in n:
            return i
    return 50


def _scan_fonts() -> list[dict]:
    found: dict[str, str] = {}
    for d in _local_font_dirs():
        if not os.path.isdir(d):
            continue
        try:
            for path in glob.glob(os.path.join(d, "**", "*.[to]tf"), recursive=True):
                name = os.path.splitext(os.path.basename(path))[0]
                found.setdefault(name.replace("_", " "), path)
        except OSError:
            continue
    return [{"name": n, "path": p} for n, p in sorted(found.items())]


def available_fonts() -> list[dict]:
    """Список шрифтов для выпадающего списка в UI: сначала рекомендованные."""
    fonts = _scan_fonts()
    preferred: list[dict] = []
    for fam in FONT_CANDIDATES:
        matches = [f for f in fonts if _norm(fam) in _norm(f["name"])]
        matches.sort(key=lambda f: _weight_rank(f["name"]))
        if matches:
            preferred.append(matches[0])
    seen = {f["path"] for f in preferred}
    return preferred + [f for f in fonts if f["path"] not in seen]


def resolve_font(spec: str = "auto", sample: str = "") -> str:
    """Имя семейства / путь → путь к ttf.

    Если в тексте есть кириллица (или другая не-латиница), шрифты без нужных
    глифов пропускаются — иначе ffmpeg нарисует пустые прямоугольники.
    """
    spec = (spec or "auto").strip()
    if spec and spec.lower() != "auto" and os.path.isfile(spec):
        return spec

    fonts = _scan_fonts()

    def pick(candidates: list[dict]) -> Optional[str]:
        candidates = sorted(candidates, key=lambda f: _weight_rank(f["name"]))
        for f in candidates:
            if font_supports(f["path"], sample):
                return f["path"]
        return None

    if spec and spec.lower() != "auto":
        key = _norm(spec)
        hit = pick([f for f in fonts if key in _norm(f["name"])])
        if hit:
            return hit
        fc = _fc_match(spec)
        if fc and os.path.isfile(fc) and font_supports(fc, sample):
            return fc

    for fam in FONT_CANDIDATES:
        key = _norm(fam)
        hit = pick([f for f in fonts if key in _norm(f["name"])])
        if hit:
            return hit
        fc = _fc_match(fam)
        if fc and os.path.isfile(fc) and font_supports(fc, sample):
            return fc

    for fallback in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                     "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                     "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf",
                     "/System/Library/Fonts/Helvetica.ttc"):
        if os.path.isfile(fallback) and font_supports(fallback, sample):
            return fallback

    any_font = pick(fonts)
    if any_font:
        return any_font
    raise RuntimeError(
        "Не найден шрифт с нужными символами. Положите .ttf (например Montserrat "
        "с поддержкой кириллицы) в папку fonts/ рядом с проектом."
    )
