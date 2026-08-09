#!/usr/bin/env python3
"""Единая точка входа.

    python run.py ui                     — открыть веб-интерфейс
    python run.py build --input ./raw -t "текст"   — собрать из командной строки
    python run.py check                  — проверить, что всё на месте
"""
from __future__ import annotations

import sys


def main() -> int:
    argv = sys.argv[1:]
    cmd = argv[0] if argv else "ui"

    if cmd in ("ui", "gui", "web"):
        from clipforge.server import serve
        host, port, browser = "127.0.0.1", 8420, True
        rest = argv[1:]
        for i, a in enumerate(rest):
            if a == "--port" and i + 1 < len(rest):
                port = int(rest[i + 1])
            elif a == "--host" and i + 1 < len(rest):
                host = rest[i + 1]
            elif a == "--no-browser":
                browser = False
        serve(host, port, browser)
        return 0

    if cmd in ("build", "cli"):
        from clipforge.cli import main as cli_main
        return cli_main(argv[1:])

    if cmd == "batch":
        from clipforge.cli import main as cli_main
        return cli_main(["--batch"] + argv[1:])

    if cmd in ("generate-hooks", "hooks"):
        from clipforge.generate_hooks import main as hooks_main
        return hooks_main(argv[1:])

    if cmd == "check":
        from clipforge.config import resolve_font
        from clipforge.ffmpeg import FFmpegError, check_tools
        try:
            print("ffmpeg:", check_tools())
        except FFmpegError as exc:
            print("ffmpeg:", exc)
            return 2
        print("шрифт (латиница):", resolve_font("auto", "Hello"))
        print("шрифт (кириллица):", resolve_font("auto", "Привет"))
        print("Python:", sys.version.split()[0])
        print("Всё готово.")
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
