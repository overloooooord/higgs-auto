"""Локальный веб-интерфейс. Только стандартная библиотека: python -m clipforge.server"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import threading
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .jobs import CANCELLED, DONE, ERROR, MANAGER

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_UPLOAD = 500 * 1024 * 1024  # 500 MB


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
                return self._json({"ok": True, "job": uuid.uuid4().hex[:16]})
            if url.path == "/api/sysinfo":
                return self._json(_collect_sysinfo())
            if url.path == "/api/generate_hooks_status":
                return self._json(MANAGER.snapshot())
            return self._error("Не найдено", 404)
        except Exception as exc:                    # noqa: BLE001
            return self._error(str(exc), 500)

    def do_HEAD(self) -> None:                      # noqa: N802
        self.do_GET()

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
            if not MANAGER.request_cancel():
                return self._json({"ok": True, "message": "Активных задач нет."})
            return self._json({"ok": True, "state": "cancelling",
                               "message": "Отмена запрошена, останавливаю воркеры…"})

        if url.path == "/api/reset_hooks":
            # Аварийный выход в «Готов к новой задаче»: снимаем задачу,
            # добиваем осиротевшие браузеры и чистим временные профили.
            info = MANAGER.reset()
            return self._json({"ok": True, "state": "idle", **info})

        return self._error("Не найдено", 404)


def _collect_sysinfo() -> dict:
    """Сканирует рабочие папки проекта и возвращает конфигурацию для UI."""
    project_root = PROJECT_ROOT

    VIDEO_EXT_LOCAL = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")
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
    hr = _scan_dir("hook_refs", VIDEO_EXT_LOCAL)
    sections.append({
        "id": "hook_refs", "icon": "📹", "title": "Видео-хуки (hook_refs)",
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

    # 3. hooks_out/credentials.txt — аккаунты
    creds = _read_credentials("hooks_out/credentials.txt")
    creds_path = os.path.join(project_root, "hooks_out", "credentials.txt")
    sections.append({
        "id": "credentials", "icon": "🔑", "title": "Аккаунты (hooks_out/credentials.txt)",
        "path": creds_path, "exists": os.path.isfile(creds_path),
        "status": "ok" if creds else ("empty" if os.path.isfile(creds_path) else "missing"),
        "count": len(creds),
        "items": [c["email"] for c in creds[:30]],
    })

    # 4. raw_batch — батчи с результатами генерации
    rb = _scan_dir("raw_batch")
    batch_info = []
    for sd in rb.get("subdirs", []):
        videos = [f for f in sd["files"] if f["name"].lower().endswith(VIDEO_EXT_LOCAL)]
        pngs = [f for f in sd["files"] if f["name"].lower().endswith((".png",))]
        creds_in = [f for f in sd["files"] if f["name"] == "credentials.txt"]
        batch_info.append({
            "name": sd["name"], "videos": len(videos),
            "screenshots": len(pngs), "has_creds": bool(creds_in),
            "total": sd["count"],
        })
    sections.append({
        "id": "raw_batch", "icon": "📦", "title": "Результаты генерации (raw_batch)",
        "path": rb["path"], "exists": rb["exists"],
        "status": "ok" if batch_info else ("empty" if rb["exists"] else "missing"),
        "count": len(batch_info),
        "batches": batch_info,
    })

    return {"sections": sections, "project_root": project_root}


def serve(host: str = "127.0.0.1", port: int = 8420, open_browser: bool = True) -> None:
    os.makedirs(PROJECT_ROOT, exist_ok=True)
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
