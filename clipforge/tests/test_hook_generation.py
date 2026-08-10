"""Регрессионные проверки исполнительной части генератора хуков.

Реальный Chrome не поднимается: SeleniumBase подменяется заглушкой, а шаги
сценария — тестовыми функциями. Проверяется логика управления задачами:
строгое N × M, добор при сбоях, ротация хука при NSFW, таймаут попытки и
отмена.

Запуск:
    python3 clipforge/tests/test_hook_generation.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))
sys.path.insert(0, os.path.dirname(_HERE))

# --- Заглушка seleniumbase до импорта генератора ---------------------------
_fake = types.ModuleType("seleniumbase")


class FakeSB:
    """Минимальный контекст-менеджер вместо реального браузера."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_fake.SB = lambda **kw: FakeSB()
sys.modules.setdefault("seleniumbase", _fake)

from clipforge.generate_hooks import HookGenerator, NSFWError  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    print(f"{mark} {name}" + (f" — {detail}" if detail else ""))


def make_gen(tmp: str, models: int = 3, videos: int = 4, **kwargs):
    """Готовит генератор с фиктивными файлами и подменёнными шагами сценария."""
    v = os.path.join(tmp, "vids")
    m = os.path.join(tmp, "models")
    o = os.path.join(tmp, "out")
    for d in (v, m, o):
        os.makedirs(d, exist_ok=True)
    for i in range(videos):
        open(os.path.join(v, f"h{i}.mp4"), "w").write("x")
    for i in range(models):
        open(os.path.join(m, f"p{i}.jpg"), "w").write("x")

    gen = HookGenerator(videos_dir=v, models_dir=m, output_dir=o,
                        anymessage_key="test", headless=True, **kwargs)
    # Все шаги, требующие сети и браузера, — пустышки.
    gen._warmup_driver = lambda *a, **k: None
    gen._register = lambda sb: "test@example.com"
    gen._complete_onboarding = lambda sb, **k: None
    gen._setup_generation = lambda sb, video, photo: None
    gen._check_nsfw_error = lambda sb: None
    gen._verify_video_attached = lambda sb, attempts=12: True
    return gen, o


def case(name: str, fn) -> None:
    tmp = tempfile.mkdtemp(prefix="cf_test_")
    try:
        fn(tmp)
    except AssertionError as exc:
        check(name, False, str(exc))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- 1. Успех: ровно N × M файлов ----------------------------------------

def t_happy_path(tmp: str) -> None:
    gen, out = make_gen(tmp)

    def download(sb, dst):
        with open(dst, "wb") as fh:
            fh.write(b"0" * 2000)
        return True

    gen._generate_and_download = download
    code = gen.run(count=0, workers=3, generations_per_model=2)
    files = [f for f in os.listdir(out) if f.endswith(".mp4")]
    check("N×M: 3 фото × 2 = 6 файлов", len(files) == 6 and code == 0,
          f"файлов {len(files)}, код {code}")


# --- 2. Добор при сбоях ---------------------------------------------------

def t_backfill(tmp: str) -> None:
    gen, out = make_gen(tmp, max_retries=0)
    state = {"n": 0}
    lock = threading.Lock()

    def download(sb, dst):
        with lock:
            state["n"] += 1
            n = state["n"]
        if n % 2 == 0:                      # каждая вторая попытка падает
            raise RuntimeError("искусственный сбой")
        with open(dst, "wb") as fh:
            fh.write(b"0" * 2000)
        return True

    gen._generate_and_download = download
    code = gen.run(count=0, workers=1, generations_per_model=2)
    files = [f for f in os.listdir(out) if f.endswith(".mp4")]
    check("Замещающие задачи добирают до N×M при сбоях",
          len(files) == 6 and code == 0, f"файлов {len(files)}, код {code}")


# --- 3. NSFW: ротация хука без расхода повторов ---------------------------

def t_nsfw_rotation(tmp: str) -> None:
    gen, _ = make_gen(tmp, models=1, videos=4, max_retries=0, nsfw_rotations=3)
    seen: list[str] = []

    gen._setup_generation = lambda sb, video, photo: seen.append(
        os.path.basename(video))
    gen._check_nsfw_error = lambda sb: (_ for _ in ()).throw(
        NSFWError("[nsfw] flagged as inappropriate"))
    gen._generate_and_download = lambda sb, dst: (_ for _ in ()).throw(
        AssertionError("до генерации доходить не должно"))

    code = gen.run(count=1, workers=1, generations_per_model=1)
    first = seen[:4]
    check("NSFW: 4 разных хука в рамках одной задачи",
          len(first) == 4 and len(set(first)) == 4, f"хуки {first}")
    check("NSFW: предохранитель ограничивает общее число заходов",
          len(seen) <= 8 and code == 1, f"заходов {len(seen)}, код {code}")


# --- 4. Таймаут попытки не вешает run() ----------------------------------

def t_attempt_timeout(tmp: str) -> None:
    gen, _ = make_gen(tmp, models=1, timeout_gen=2, timeout_attempt=3,
                      max_retries=0)
    gen._generate_and_download = lambda sb, dst: (time.sleep(30), True)[1]

    started = time.time()
    code = gen.run(count=1, workers=1, generations_per_model=1)
    took = time.time() - started
    check("Таймаут попытки: run() возвращает управление",
          code == 1 and took < 120, f"код {code}, заняло {took:.0f}с")


# --- 5. Отмена завершает run() -------------------------------------------

def t_cancel(tmp: str) -> None:
    gen, _ = make_gen(tmp, models=2, timeout_gen=5, timeout_attempt=10)
    gen._generate_and_download = lambda sb, dst: (time.sleep(20), True)[1]

    cancel = threading.Event()
    threading.Timer(2.0, cancel.set).start()
    started = time.time()
    code = gen.run(count=0, workers=2, generations_per_model=1,
                   cancel_event=cancel)
    took = time.time() - started
    check("Отмена: run() завершается без зависания",
          code == 1 and took < 90, f"код {code}, заняло {took:.0f}с")


def main() -> int:
    case("N×M при успехе", t_happy_path)
    case("добор до N×M", t_backfill)
    case("ротация при NSFW", t_nsfw_rotation)
    case("таймаут попытки", t_attempt_timeout)
    case("отмена", t_cancel)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\nИТОГ: {passed}/{len(RESULTS)} проверок пройдено")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
