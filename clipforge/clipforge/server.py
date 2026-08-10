"""Локальный веб-интерфейс. Только стандартная библиотека: python -m clipforge.server"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import PRESETS, STAGES, STAGE_KEYS, VIDEO_EXT, Settings, available_fonts
from .ffmpeg import FFmpegError, check_tools, ffmpeg_bin, probe
from .jobs import CANCELLED, DONE, ERROR, MANAGER
from .pipeline import build

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
ROOT = os.path.join(tempfile.gettempdir(), "clipforge_jobs")
MAX_UPLOAD = 4 * 1024 * 1024 * 1024      # 4 ГБ на файл

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()


def job_dir(job: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "", job)[:40]
    if not safe:
        raise ValueError("bad job id")
    path = os.path.join(ROOT, safe)
    os.makedirs(path, exist_ok=True)
    return path


def get_job(job: str) -> dict:
    with LOCK:
        return JOBS.setdefault(job, {
            "state": "idle", "percent": 0.0, "stage": "", "log": [],
            "result": None, "error": None, "stats": {}, "files": {},
        })


def push_log(job: str, message: str) -> None:
    if not message:
        return
    j = get_job(job)
    with LOCK:
        j["log"].append(message)
        del j["log"][:-400]


def make_thumb(src: str, dst: str, at: float = 0.5) -> None:
    subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-ss", str(at), "-i", src,
                    "-frames:v", "1", "-vf", "scale=360:-2", dst],
                   capture_output=True, timeout=60)


def run_job(job: str, settings: Settings, inputs: dict[str, str], out_name: str) -> None:
    j = get_job(job)
    work = job_dir(job)
    output = os.path.join(work, out_name)

    def report(stage: str, pct: float, msg: str) -> None:
        with LOCK:
            j["stage"] = stage
            j["percent"] = round(max(0.0, min(1.0, pct)) * 100, 1)
        push_log(job, msg)

    try:
        with LOCK:
            j.update(state="running", percent=0.0, error=None, result=None, log=[])
        result = build(inputs, settings, output, report=report,
                       workdir=os.path.join(work, "tmp"))
        with LOCK:
            j.update(state="done", percent=100.0, result=output, stats={
                "duration": round(result.duration, 2),
                "source_duration": round(result.source_duration, 2),
                "saved": round(result.saved, 2),
                "elapsed": round(result.elapsed, 1),
                "clips": [{"stage": k, "in": round(p.info.duration, 1),
                           "out": round(p.out_duration, 1), "speed": p.global_speed,
                           "pauses": len(p.freezes)}
                          for k, p in zip([s for s in STAGE_KEYS if s in inputs], result.plans)],
            })
    except Exception as exc:                       # noqa: BLE001 — показываем причину в UI
        push_log(job, f"ОШИБКА: {exc}")
        traceback.print_exc()
        with LOCK:
            j.update(state="error", error=str(exc))
    finally:
        shutil.rmtree(os.path.join(work, "tmp"), ignore_errors=True)


def run_generate_hooks_job(job, workers: int, gen_per_model: int,
                           v_dir: str, m_dir: str, o_dir: str, headed: bool,
                           model_photo: str = "",
                           timeout_gen: int = 300, max_retries: int = 2,
                           timeout_attempt: int = 900,
                           nsfw_rotations: int = 3) -> None:
    """Рабочий поток генерации хуков.

    Состояние живёт в объекте `job`; слот менеджера освобождается в `finally`
    через `MANAGER.finalize()`. Поэтому после любого исхода — успех, отмена,
    ошибка, таймаут — приложение возвращается в «Готов к новой задаче» без
    перезапуска.
    """
    state, error, summary = DONE, None, ""
    try:
        from .generate_hooks import HookGenerator

        gen = HookGenerator(
            videos_dir=v_dir,
            models_dir=m_dir,
            model_photo=model_photo,
            output_dir=o_dir,
            anymessage_key=os.environ.get("ANYMESSAGE_KEY", "4daS8LEc7P3n0CEx2tuR5BuNiqEdOt4H"),
            headless=not headed,
            timeout_gen=timeout_gen,
            max_retries=max_retries,
            timeout_attempt=timeout_attempt,
            nsfw_rotations=nsfw_rotations,
        )

        # Итог строго N × M: M — фото моделей, N — генераций на фото.
        real_total = max(1, len(gen.models) * gen_per_model)
        MANAGER.progress(job, 0, real_total, failed=0, phase="генерация")
        MANAGER.log(job, f"🚀 Генерация: {len(gen.models)} фото × {gen_per_model} = "
                         f"{real_total} видео, воркеров: {workers}")
        MANAGER.log(job, f"   Таймаут генерации {timeout_gen}с, попытки "
                         f"{timeout_attempt}с, повторов {max_retries}, "
                         f"ротаций при NSFW {nsfw_rotations}")

        def progress_cb(done: int, total: int, message: str) -> None:
            MANAGER.progress(job, done, total, phase="генерация")
            MANAGER.log(job, message)

        code = gen.run(
            count=0,                      # 0 → run() сам считает N × M
            workers=workers,
            generations_per_model=gen_per_model,
            cancel_event=job.cancel_event,
            progress_callback=progress_cb,
        )

        if job.cancelled:
            state, summary = CANCELLED, "⏹ Генерация отменена пользователем."
        elif code == 0:
            state, summary = DONE, "✅ Генерация завершена: собрано ровно N × M."
        else:
            state = ERROR
            error = "Собрано меньше, чем N × M — подробности в логе"
            summary = "⚠ Часть генераций не удалась. Смотри лог выше."

    except Exception as exc:                            # noqa: BLE001
        traceback.print_exc()
        state, error = ERROR, str(exc)
        summary = f"❌ ОШИБКА: {exc}"
    finally:
        # Слот освобождается всегда — гарантия «чистого состояния» (п. 6).
        MANAGER.finalize(job, state, error=error, message=summary)


class Handler(BaseHTTPRequestHandler):
    server_version = "ClipForge"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------- утилиты
    def log_message(self, fmt, *args):            # тише в консоли
        if os.environ.get("CLIPFORGE_VERBOSE"):
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str = "application/json",
              extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _error(self, message: str, code: int = 400) -> None:
        self._json({"error": message}, code)

    def _file(self, path: str, ctype: str | None = None, download: str | None = None) -> None:
        if not os.path.isfile(path):
            return self._error("Файл не найден", 404)
        ctype = ctype or mimetypes.guess_type(path)[0] or "application/octet-stream"
        size = os.path.getsize(path)
        rng = self.headers.get("Range")
        extra = {"Accept-Ranges": "bytes"}
        if download:
            extra["Content-Disposition"] = f'attachment; filename="{download}"'
        start, end = 0, size - 1
        code = 200
        if rng and rng.startswith("bytes="):
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
                if start >= size:
                    return self._error("Range", 416)
                code = 206
                extra["Content-Range"] = f"bytes {start}-{end}/{size}"
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(1 << 20, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            raise ValueError("Файл слишком большой")
        data, remaining = bytearray(), length
        while remaining > 0:
            chunk = self.rfile.read(min(1 << 20, remaining))
            if not chunk:
                break
            data.extend(chunk)
            remaining -= len(chunk)
        return bytes(data)

    # ------------------------------------------------------------- роуты
    def do_GET(self) -> None:                      # noqa: N802
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path in ("/", "/index.html"):
                return self._file(os.path.join(UI_DIR, "index.html"), "text/html; charset=utf-8")
            if url.path == "/api/init":
                try:
                    ff = check_tools()
                except FFmpegError as exc:
                    return self._json({"ffmpeg": None, "error": str(exc)})
                return self._json({
                    "ffmpeg": ff,
                    "stages": [{"key": k, "label": l} for k, l in STAGES],
                    "presets": PRESETS,
                    "fonts": [f["name"] for f in available_fonts()[:80]],
                    "defaults": Settings().to_dict(),
                    "job": uuid.uuid4().hex[:16],
                })
            if url.path == "/api/sysinfo":
                return self._json(_collect_sysinfo())
            if url.path == "/api/generate_hooks_status":
                # Снимок из JobManager: состояние, прогресс, фаза и возраст
                # heartbeat — UI видит, что задача жива, и никогда не «висит».
                return self._json(MANAGER.snapshot())
            if url.path == "/api/status":
                j = get_job(q.get("job", ""))
                with LOCK:
                    return self._json({k: v for k, v in j.items() if k != "files"})
            if url.path == "/api/thumb":
                return self._file(os.path.join(job_dir(q.get("job", "")),
                                               f"thumb_{re.sub(r'[^a-z_]', '', q.get('stage', ''))}.jpg"),
                                  "image/jpeg")
            if url.path == "/api/result":
                j = get_job(q.get("job", ""))
                path = j.get("result")
                if not path:
                    return self._error("Результата ещё нет", 404)
                return self._file(path, "video/mp4",
                                  download=os.path.basename(path) if q.get("dl") else None)
            return self._error("Не найдено", 404)
        except Exception as exc:                    # noqa: BLE001
            return self._error(str(exc), 500)

    def do_HEAD(self) -> None:                      # noqa: N802
        self.do_GET()

    def do_PUT(self) -> None:                       # noqa: N802
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path != "/api/upload":
            return self._error("Не найдено", 404)
        try:
            job, stage = q.get("job", ""), q.get("stage", "")
            if stage not in STAGE_KEYS:
                return self._error("Неизвестный этап")
            work = job_dir(job)
            name = os.path.basename(q.get("name", "video.mp4"))
            ext = os.path.splitext(name)[1].lower() or ".mp4"
            dst = os.path.join(work, f"{stage}{ext}")
            for old in os.listdir(work):
                if old.startswith(stage + ".") and os.path.join(work, old) != dst:
                    os.remove(os.path.join(work, old))
            with open(dst, "wb") as fh:
                fh.write(self._body())
            info = probe(dst)
            make_thumb(dst, os.path.join(work, f"thumb_{stage}.jpg"), min(0.5, info.duration / 3))
            get_job(job)["files"][stage] = dst
            return self._json({"stage": stage, "name": name,
                               "duration": round(info.duration, 2),
                               "width": info.width, "height": info.height,
                               "fps": info.fps, "audio": info.has_audio})
        except Exception as exc:                    # noqa: BLE001
            return self._error(str(exc), 500)

    def do_POST(self) -> None:                      # noqa: N802
        url = urlparse(self.path)
        try:
            payload = json.loads(self._body() or b"{}")
        except json.JSONDecodeError:
            return self._error("Некорректный JSON")

        if url.path == "/api/generate_hooks":
            workers = max(1, int(payload.get("workers") or 1))
            gen_per_model = max(1, int(payload.get("generations_per_model") or 1))
            v_dir = (payload.get("videos_dir") or "./hook_refs").strip()
            m_dir = (payload.get("models_dir") or "./models").strip()
            o_dir = (payload.get("output_dir") or "./raw_batch/1").strip()
            model_photo = (payload.get("model_photo") or "").strip()
            headed = bool(payload.get("headed", True))
            timeout_gen = max(30, int(payload.get("timeout_gen") or 300))
            max_retries = min(3, max(0, int(payload.get("max_retries") or 2)))
            timeout_attempt = max(timeout_gen + 60,
                                  int(payload.get("timeout_attempt") or 900))
            nsfw_rotations = min(10, max(0, int(payload.get("nsfw_rotations") or 3)))

            # Атомарный захват слота: гонка двух одновременных «Старт»
            # невозможна, повторный запуск не наложится на старую задачу.
            job = MANAGER.try_start("hooks")
            if job is None:
                snap = MANAGER.snapshot()
                return self._error(
                    f"Задача уже выполняется (состояние: {snap.get('state')}). "
                    f"Дождитесь завершения, нажмите «Остановить» или «Сбросить»."
                )

            threading.Thread(
                target=run_generate_hooks_job,
                args=(job, workers, gen_per_model, v_dir, m_dir, o_dir, headed,
                      model_photo, timeout_gen, max_retries, timeout_attempt,
                      nsfw_rotations),
                daemon=True,
            ).start()
            return self._json({"ok": True, "job_id": job.job_id,
                               "workers": workers,
                               "generations_per_model": gen_per_model})

        if url.path == "/api/stop_hooks":
            # Слот НЕ освобождаем здесь: это делает рабочий поток в finally.
            # Иначе (как раньше) UI разблокировался бы, пока воркеры живы, и
            # новая задача накладывалась бы на старую.
            if not MANAGER.request_cancel():
                return self._json({"ok": True, "message": "Активных задач нет."})
            return self._json({"ok": True, "state": "cancelling",
                               "message": "Отмена запрошена, останавливаю воркеры…"})

        if url.path == "/api/reset_hooks":
            # Аварийный выход в «Готов к новой задаче»: снимаем задачу,
            # добиваем осиротевшие браузеры и чистим временные профили.
            info = MANAGER.reset()
            return self._json({"ok": True, "state": "idle", **info})

        if url.path == "/api/render":
            job = payload.get("job", "")
            j = get_job(job)
            if j["state"] == "running":
                return self._error("Задача уже выполняется")
            work = job_dir(job)
            inputs = {k: v for k, v in j["files"].items() if os.path.isfile(v)}
            if not inputs:
                for k in STAGE_KEYS:                # восстановление после перезапуска
                    for f in os.listdir(work):
                        if f.startswith(k + ".") and not f.startswith("thumb"):
                            inputs[k] = os.path.join(work, f)
            if not inputs:
                return self._error("Не загружено ни одного видео")
            try:
                settings = Settings.from_dict(payload.get("settings", {}))
            except (TypeError, ValueError) as exc:
                return self._error(f"Некорректные настройки: {exc}")
            out_name = re.sub(r"[^\w\-. ]", "", payload.get("output") or "clipforge.mp4").strip()
            if not out_name.lower().endswith(".mp4"):
                out_name += ".mp4"
            threading.Thread(target=run_job, args=(job, settings, inputs, out_name),
                             daemon=True).start()
            return self._json({"ok": True, "stages": list(inputs)})

        if url.path == "/api/reset":
            job = payload.get("job", "")
            shutil.rmtree(job_dir(job), ignore_errors=True)
            with LOCK:
                JOBS.pop(job, None)
            return self._json({"ok": True})

        if url.path == "/api/load_folder":
            job = payload.get("job", "")
            folder_input = (payload.get("folder") or "raw_batch").strip()

            # Корень проекта: .../clipforge/clipforge → .../clipforge
            here         = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.dirname(here)
            parent_root  = os.path.dirname(project_root)
            cwd          = os.getcwd()

            if os.path.isabs(folder_input):
                candidates = [folder_input]
            else:
                candidates = [
                    os.path.join(project_root, folder_input),
                    os.path.join(parent_root,  folder_input),
                    os.path.join(cwd,          folder_input),
                ]

            target_dir = next((p for p in candidates if os.path.isdir(p)), None)
            if target_dir is None:
                return self._error(f"Папка «{folder_input}» не найдена (искал в {candidates})")

            print(f"[load_folder] target_dir={target_dir}")

            # Алиасы подпапок для каждого этапа
            ALIASES: dict[str, list[str]] = {
                "hooks":        ["hooks", "hook", "1", "01", "1_hooks"],
                "screen_google":["screen_google", "google", "2", "02", "2_google"],
                "domain_check": ["domain_check", "domain", "3", "03", "3_domain"],
                "prompt_input": ["prompt_input", "prompt", "promocode", "4", "04"],
                "payout":       ["payout", "pay", "withdraw", "5", "05"],
            }

            try:
                subdirs = [d for d in os.listdir(target_dir)
                           if os.path.isdir(os.path.join(target_dir, d))]
            except OSError as e:
                return self._error(f"Не могу прочитать папку: {e}")

            print(f"[load_folder] subdirs={subdirs}")

            found_files: dict[str, str] = {}

            # Шаг 1: сканируем подпапки по алиасам
            for stage_key, aliases in ALIASES.items():
                for alias in aliases:
                    matched = next(
                        (d for d in subdirs if d.lower() == alias.lower()
                         or d.lower().startswith(alias.lower() + "_")
                         or d.lower().startswith(alias.lower() + "-")),
                        None
                    )
                    if matched:
                        sub = os.path.join(target_dir, matched)
                        vids = sorted(
                            [os.path.join(sub, f) for f in os.listdir(sub)
                             if f.lower().endswith(VIDEO_EXT)]
                        )
                        if vids:
                            found_files[stage_key] = vids[0]
                            print(f"[load_folder] {stage_key} → {vids[0]}")
                            break

            # Шаг 2: файлы прямо в папке с префиксами (hooks_01.mp4 и т.д.)
            if not found_files:
                for stage_key, aliases in ALIASES.items():
                    if stage_key in found_files:
                        continue
                    for f in sorted(os.listdir(target_dir)):
                        if not f.lower().endswith(VIDEO_EXT):
                            continue
                        fn = f.lower().replace(" ", "_").replace("-", "_")
                        if any(fn.startswith(a.lower()) for a in aliases):
                            found_files[stage_key] = os.path.join(target_dir, f)
                            print(f"[load_folder] {stage_key} → {f} (prefix)")
                            break

            # Шаг 3: fallback — первые 5 видеофайлов прямо в папке по порядку
            if not found_files:
                vids = sorted(
                    [os.path.join(target_dir, f) for f in os.listdir(target_dir)
                     if f.lower().endswith(VIDEO_EXT)]
                )
                for idx, k in enumerate(STAGE_KEYS):
                    if idx < len(vids):
                        found_files[k] = vids[idx]
                        print(f"[load_folder] {k} → {vids[idx]} (fallback)")

            print(f"[load_folder] found_files={list(found_files.keys())}")

            if not found_files:
                return self._error(
                    f"В папке {target_dir} нет видеофайлов. "
                    f"Подпапки: {subdirs}"
                )

            work = job_dir(job)
            loaded = {}
            for stage, src_path in found_files.items():
                try:
                    ext = os.path.splitext(src_path)[1].lower() or ".mp4"
                    dst = os.path.join(work, f"{stage}{ext}")
                    shutil.copy2(src_path, dst)
                    info = probe(dst)
                    make_thumb(dst, os.path.join(work, f"thumb_{stage}.jpg"),
                               min(0.5, info.duration / 3))
                    get_job(job)["files"][stage] = dst
                    loaded[stage] = {
                        "stage": stage, "name": os.path.basename(src_path),
                        "duration": round(info.duration, 2),
                        "width": info.width, "height": info.height,
                        "fps": info.fps, "audio": info.has_audio,
                    }
                except Exception as e:
                    print(f"[load_folder] ERROR processing {stage}: {e}")

            return self._json({"ok": True, "folder": target_dir,
                               "loaded": loaded, "count": len(loaded)})

        return self._error("Не найдено", 404)


def _collect_sysinfo() -> dict:
    """Сканирует рабочие папки проекта и возвращает конфигурацию для UI."""
    # Корень проекта — на два уровня выше clipforge/server.py
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")
    IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")

    def _scan_dir(rel: str, exts: tuple | None = None) -> dict:
        """Сканирует папку, возвращает список файлов с размерами."""
        abs_path = os.path.join(project_root, rel)
        result: dict = {"path": abs_path, "exists": os.path.isdir(abs_path), "files": [], "subdirs": []}
        if not result["exists"]:
            return result
        try:
            for entry in sorted(os.listdir(abs_path)):
                full = os.path.join(abs_path, entry)
                if os.path.isdir(full):
                    # Считаем файлы в подпапке
                    sub_files = []
                    try:
                        for sf in os.listdir(full):
                            sf_path = os.path.join(full, sf)
                            if os.path.isfile(sf_path):
                                if exts is None or sf.lower().endswith(exts):
                                    sub_files.append({"name": sf, "size": os.path.getsize(sf_path)})
                    except OSError:
                        pass
                    result["subdirs"].append({"name": entry, "files": sub_files, "count": len(sub_files)})
                elif os.path.isfile(full):
                    if exts is None or entry.lower().endswith(exts):
                        result["files"].append({"name": entry, "size": os.path.getsize(full)})
        except OSError:
            pass
        return result

    def _read_texts(rel: str) -> list[str]:
        path = os.path.join(project_root, rel)
        if not os.path.isfile(path):
            return []
        try:
            with open(path, encoding="utf-8") as f:
                return [line.strip() for line in f if line.strip()]
        except Exception:
            return []

    def _read_credentials(rel: str) -> list[dict]:
        path = os.path.join(project_root, rel)
        if not os.path.isfile(path):
            return []
        creds = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if ":" in line:
                        email, pwd = line.split(":", 1)
                        creds.append({"email": email, "has_pwd": bool(pwd.strip())})
        except Exception:
            pass
        return creds

    sections = []

    # 1. hook_refs — исходные видео для хуков
    hr = _scan_dir("hook_refs", VIDEO_EXT)
    sections.append({
        "id": "hook_refs", "icon": "📹", "title": "Видео-исходники (hook_refs)",
        "path": hr["path"], "exists": hr["exists"],
        "status": "ok" if hr["files"] else ("empty" if hr["exists"] else "missing"),
        "count": len(hr["files"]),
        "files": hr["files"],
    })

    # 2. models — фото моделей
    md = _scan_dir("models", IMAGE_EXT)
    sections.append({
        "id": "models", "icon": "🧑", "title": "Фото моделей (models)",
        "path": md["path"], "exists": md["exists"],
        "status": "ok" if md["files"] else ("empty" if md["exists"] else "missing"),
        "count": len(md["files"]),
        "files": md["files"],
    })

    # 3. texts.txt — тексты для хуков
    texts = _read_texts("texts.txt")
    texts_path = os.path.join(project_root, "texts.txt")
    sections.append({
        "id": "texts", "icon": "📝", "title": "Тексты (texts.txt)",
        "path": texts_path, "exists": os.path.isfile(texts_path),
        "status": "ok" if texts else ("empty" if os.path.isfile(texts_path) else "missing"),
        "count": len(texts),
        "items": texts[:20],  # первые 20
    })

    # 4. hooks_out/credentials.txt — аккаунты
    creds = _read_credentials("hooks_out/credentials.txt")
    creds_path = os.path.join(project_root, "hooks_out", "credentials.txt")
    sections.append({
        "id": "credentials", "icon": "🔑", "title": "Аккаунты (hooks_out/credentials.txt)",
        "path": creds_path, "exists": os.path.isfile(creds_path),
        "status": "ok" if creds else ("empty" if os.path.isfile(creds_path) else "missing"),
        "count": len(creds),
        "items": [c["email"] for c in creds[:30]],
    })

    # 5. raw_batch — батчи с результатами генерации
    rb = _scan_dir("raw_batch")
    batch_info = []
    for sd in rb.get("subdirs", []):
        # В подпапках ищем видео и скриншоты
        videos = [f for f in sd["files"] if f["name"].lower().endswith(VIDEO_EXT)]
        pngs = [f for f in sd["files"] if f["name"].lower().endswith((".png",))]
        creds_in = [f for f in sd["files"] if f["name"] == "credentials.txt"]
        batch_info.append({
            "name": sd["name"], "videos": len(videos),
            "screenshots": len(pngs), "has_creds": bool(creds_in),
            "total": sd["count"],
        })
    sections.append({
        "id": "raw_batch", "icon": "📦", "title": "Батчи генерации (raw_batch)",
        "path": rb["path"], "exists": rb["exists"],
        "status": "ok" if batch_info else ("empty" if rb["exists"] else "missing"),
        "count": len(batch_info),
        "batches": batch_info,
    })

    # 6. generated_hooks — финальные сгенерированные хуки
    gh = _scan_dir("generated_hooks", VIDEO_EXT + IMAGE_EXT)
    sections.append({
        "id": "generated_hooks", "icon": "✅", "title": "Готовые хуки (generated_hooks)",
        "path": gh["path"], "exists": gh["exists"],
        "status": "ok" if gh["files"] else ("empty" if gh["exists"] else "missing"),
        "count": len(gh["files"]),
        "files": gh["files"],
    })

    # 7. output — финальные смонтированные ролики
    out = _scan_dir("output", VIDEO_EXT)
    sections.append({
        "id": "output", "icon": "🎬", "title": "Смонтированные ролики (output)",
        "path": out["path"], "exists": out["exists"],
        "status": "ok" if out["files"] else ("empty" if out["exists"] else "missing"),
        "count": len(out["files"]),
        "files": out["files"],
    })

    return {"sections": sections, "project_root": project_root}


def serve(host: str = "127.0.0.1", port: int = 8420, open_browser: bool = True) -> None:
    os.makedirs(ROOT, exist_ok=True)
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"ClipForge UI: {url}\nCtrl+C — остановить")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        httpd.server_close()


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(prog="clipforge-ui", description="Веб-интерфейс ClipForge")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8420)
    p.add_argument("--no-browser", action="store_true")
    a = p.parse_args()
    serve(a.host, a.port, not a.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
