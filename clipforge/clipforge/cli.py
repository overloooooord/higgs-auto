"""CLI: python -m clipforge.cli --input ./raw --text "..." --out ready.mp4"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .config import PRESETS, STAGES, STAGE_KEYS, Settings, TextStyle, available_fonts
from .ffmpeg import FFmpegError, check_tools
from .pipeline import build, collect_inputs


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="clipforge",
        description="Автомонтаж вертикальных роликов из 5 исходников "
                    "(hooks → screen_google → domain_check → prompt_input → payout).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    src = p.add_argument_group("источники")
    src.add_argument("-i", "--input", help="папка с исходниками (подпапки или префиксы этапов)")
    for key, label in STAGES:
        src.add_argument(f"--{key.replace('_', '-')}", dest=key, help=f"файл этапа «{label}»")
    src.add_argument("-o", "--out", default="output.mp4", help="итоговый файл")

    fmt = p.add_argument_group("формат")
    fmt.add_argument("--preset", choices=list(PRESETS), default="reels",
                     help="reels 1080x1920 / youtube 1920x1080 / square 1080x1080")
    fmt.add_argument("--fill", choices=["blur", "pad", "crop"], default="blur",
                     help="как вписать кадр: размытый фон / чёрные поля / обрезка")
    fmt.add_argument("--fps", type=int, default=0, help="кадры в секунду (0 = из пресета)")

    cut = p.add_argument_group("умный монтаж")
    cut.add_argument("--pause-threshold", type=float, default=1.0,
                     help="статика длиннее этого (сек) считается паузой")
    cut.add_argument("--pause-action", choices=["speed", "cut"], default="speed")
    cut.add_argument("--pause-speed", type=float, default=5.0, help="ускорение пауз")
    cut.add_argument("--max-stage", type=float, default=12.0,
                     help="лимит длительности этапа, сек (0 = без лимита)")
    cut.add_argument("--max-speed", type=float, default=4.0, help="потолок общего ускорения")
    cut.add_argument("--speed", type=float, default=1.0, help="базовое ускорение всего материала")
    cut.add_argument("--no-trim", action="store_true", help="не резать статику по краям")
    cut.add_argument("--freeze-noise", type=float, default=0.003,
                     help="чувствительность детектора статики (больше = грубее)")

    join = p.add_argument_group("склейка и звук")
    join.add_argument("--crossfade", type=float, default=0.5, help="переход между этапами, сек")
    join.add_argument("--transition", default="fade", help="тип xfade-перехода")
    join.add_argument("--audio", action="store_true", help="сохранить звук исходников")
    join.add_argument("--crf", type=int, default=20, help="качество H.264 (меньше = лучше)")
    join.add_argument("--x264-preset", default="medium")

    txt = p.add_argument_group("текст")
    txt.add_argument("-t", "--text", default="", help="текст поверх видео (\\n — перенос)")
    txt.add_argument("--font", default="auto", help="имя шрифта или путь к .ttf")
    txt.add_argument("--font-size", type=int, default=64, help="кегль для формата 1080x1920")
    txt.add_argument("--font-color", default="white")
    txt.add_argument("--text-pos", choices=["top", "center", "bottom", "custom"], default="bottom")
    txt.add_argument("--text-x", type=int, default=0)
    txt.add_argument("--text-y", type=int, default=0)
    txt.add_argument("--no-box", action="store_true", help="без полупрозрачной подложки")
    txt.add_argument("--box-opacity", type=float, default=0.45)
    txt.add_argument("--text-fade", type=float, default=0.4, help="плавное появление, сек")
    txt.add_argument("--text-start", type=float, default=0.0)
    txt.add_argument("--text-duration", type=float, default=0.0, help="0 = до конца ролика")
    txt.add_argument("--wrap", type=int, default=22, help="перенос строки, символов (0 = выкл)")
    txt.add_argument("--caption", action="append", default=[], metavar="STAGE=ТЕКСТ",
                     help="подпись поверх конкретного этапа, можно повторять")

    misc = p.add_argument_group("прочее")
    misc.add_argument("--batch", action="store_true", help="пакетный режим: сборка из 5 папок (1..5 / hooks..payout)")
    misc.add_argument("--texts", help="файл с текстами (по одной строке на ролик) для пакетного режима")
    misc.add_argument("--out-dir", default="output", help="папка для готовых файлов в пакетном режиме")
    misc.add_argument("--delete-used", action="store_true",
                      help="удалять исходники после успешной сборки каждого ролика (только --batch)")
    misc.add_argument("--uniquify", action="store_true",
                      help="микро-уникализация каждого ролика: зум/сдвиг/цвет/метаданные (только --batch)")
    misc.add_argument("--text-zone", action="append", default=[], metavar="STAGE=ЗОНА",
                      help="позиция текста на этапе: top/center/bottom/avoid, можно повторять")
    misc.add_argument("--config", help="JSON с настройками (перекрывается флагами)")
    misc.add_argument("--dry-run", action="store_true", help="только анализ, без рендера")
    misc.add_argument("--list-fonts", action="store_true", help="показать найденные шрифты")
    misc.add_argument("--json", action="store_true", help="итоговый отчёт в JSON")
    return p


def settings_from_args(a: argparse.Namespace) -> Settings:
    """Флаги CLI перекрывают --config, но только те, что реально заданы."""
    base = {}
    if a.config:
        with open(a.config, encoding="utf-8") as fh:
            base = json.load(fh)
    s = Settings.from_dict(base)

    defaults = make_parser().parse_args([])

    def given(name: str) -> bool:
        return getattr(a, name) != getattr(defaults, name, None)

    def take(arg_name: str, field: str, transform=lambda v: v) -> None:
        if given(arg_name):
            setattr(s, field, transform(getattr(a, arg_name)))

    take("preset", "preset")
    s.apply_preset()
    if a.fps:
        s.fps = a.fps
    take("fill", "fill")
    take("no_trim", "trim_edges", lambda v: not v)
    take("pause_threshold", "pause_threshold")
    take("pause_action", "pause_action")
    take("pause_speed", "pause_speed")
    take("max_stage", "max_stage_seconds")
    take("max_speed", "max_speed")
    take("speed", "base_speed")
    take("freeze_noise", "freeze_noise")
    take("crossfade", "crossfade")
    take("transition", "transition")
    take("audio", "keep_audio")
    take("crf", "crf")
    take("x264_preset", "x264_preset")

    t: TextStyle = s.text
    if given("text"):
        t.text = (a.text or "").replace("\\n", "\n")
    for arg_name, field in (("font", "font"), ("font_size", "size"), ("font_color", "color"),
                            ("text_pos", "position"), ("text_x", "x"), ("text_y", "y"),
                            ("box_opacity", "box_opacity"), ("text_fade", "fade"),
                            ("text_start", "start"), ("text_duration", "duration"),
                            ("wrap", "wrap")):
        if given(arg_name):
            setattr(t, field, getattr(a, arg_name))
    if a.no_box:
        t.box = False

    for item in a.caption:
        key, _, value = item.partition("=")
        if key.strip() in STAGE_KEYS and value.strip():
            s.stage_captions[key.strip()] = value.strip()

    for item in a.text_zone:
        key, _, value = item.partition("=")
        if key.strip() in STAGE_KEYS and value.strip().lower() in ("top", "center", "bottom", "avoid"):
            s.text_zones[key.strip()] = value.strip().lower()
    return s


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)

    if args.list_fonts:
        for f in available_fonts()[:60]:
            print(f"{f['name']:<38} {f['path']}")
        return 0

    try:
        check_tools()
    except FFmpegError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2

    try:
        settings = settings_from_args(args)

        if args.batch:
            from .batch import run_batch
            base_dir = args.input or "."
            run_batch(
                base_dir=base_dir,
                output_dir=args.out_dir,
                settings=settings,
                texts_file=args.texts,
                dry_run=args.dry_run,
                delete_used=args.delete_used,
                uniquify=args.uniquify,
            )
            return 0

        inputs: dict[str, str] = {}
        explicit = {k: getattr(args, k) for k in STAGE_KEYS if getattr(args, k, None)}
        if args.input:
            inputs = collect_inputs(args.input)
        inputs.update(explicit)
        if not inputs:
            print("Укажите --input ПАПКА (или --batch) или файлы по этапам (--hooks ... --payout ...).", file=sys.stderr)
            return 2
        missing = [k for k in STAGE_KEYS if k not in inputs]
        if missing:
            print("Внимание: нет этапов " + ", ".join(missing) + " — собираю из того, что есть.")
        for key, path in inputs.items():
            if not os.path.isfile(path):
                raise FileNotFoundError(f"Файл не найден: {path}")

        if args.dry_run:
            from .analyze import plan_clip
            from .ffmpeg import probe
            total_in = total_out = 0.0
            for key in STAGE_KEYS:
                if key not in inputs:
                    continue
                plan = plan_clip(probe(inputs[key]), settings)
                total_in += plan.info.duration
                total_out += plan.out_duration
                print(f"{key:<14} {plan.summary()}")
            print(f"{'ИТОГО':<14} {total_in:.1f}s → {total_out:.1f}s")
            return 0

        result = build(inputs, settings, args.out)
        if args.json:
            print(json.dumps({
                "output": result.output, "duration": round(result.duration, 2),
                "source_duration": round(result.source_duration, 2),
                "saved": round(result.saved, 2), "elapsed": round(result.elapsed, 1),
            }, ensure_ascii=False))
        else:
            print(f"\nГотово: {result.output}")
            print(f"Длительность: {result.duration:.1f} сек "
                  f"(исходники {result.source_duration:.1f} сек, вырезано {result.saved:.1f} сек)")
            print(f"Время сборки: {result.elapsed:.1f} сек")
        return 0
    except (FFmpegError, FileNotFoundError, ValueError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
