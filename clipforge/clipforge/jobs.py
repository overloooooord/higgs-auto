"""Единый менеджер задач ClipForge.

Заменяет глобальные `HOOK_GEN_JOB` + `_cancel_event` из server.py.

Зачем:
  * Один слот активной задачи с атомарным захватом — двойной старт невозможен.
  * `cancel_event` живёт внутри объекта задачи и никогда не переприсваивается,
    поэтому «Стоп» не теряется и новый запуск не снимает отмену со старого.
  * `finalize()` всегда освобождает слот и чистит прогресс → после любого
    завершения (успех / отмена / ошибка / таймаут) состояние «Готов к новой
    задаче» без перезапуска программы.
  * Watchdog-поток добивает задачу, которая перестала присылать heartbeat,
    поэтому зависший воркер не может держать UI занятым бесконечно.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

# ------------------------------------------------------------------ состояния

IDLE = "idle"
RUNNING = "running"
CANCELLING = "cancelling"
CANCELLED = "cancelled"
DONE = "done"
ERROR = "error"

TERMINAL_STATES = (DONE, ERROR, CANCELLED, IDLE)

LOG_LIMIT = 600
#: Профили браузера, которые создаёт генератор хуков (для очистки при reset).
PROFILE_PREFIX = "cf_w"


@dataclass
class Job:
    """Одна активная задача (генерация хуков или сборка монтажа)."""

    kind: str
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: str = RUNNING
    phase: str = ""
    percent: float = 0.0
    done: int = 0
    failed: int = 0
    total: int = 0
    error: Optional[str] = None
    log: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)
    heartbeat: float = field(default_factory=time.monotonic)
    finished_at: Optional[float] = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    #: Колбэки принудительной остановки (hard-kill драйверов/процессов).
    killers: list[Callable[[], None]] = field(default_factory=list)

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()


class JobManager:
    """Потокобезопасный держатель единственного активного задания."""

    def __init__(self, stall_timeout: float = 120.0) -> None:
        self._lock = threading.RLock()
        self._job: Optional[Job] = None
        #: Снимок последнего завершённого задания — чтобы UI успел увидеть итог.
        self._last: dict = {
            "state": IDLE, "phase": "", "percent": 0.0, "done": 0, "failed": 0,
            "total": 0, "error": None, "log": [], "kind": "", "job_id": "",
        }
        self.stall_timeout = stall_timeout
        self._watchdog: Optional[threading.Thread] = None
        self._stop_watchdog = threading.Event()

    # ---------------------------------------------------------------- запуск

    def try_start(self, kind: str, total: int = 0) -> Optional[Job]:
        """Атомарно захватывает слот. Возвращает None, если он уже занят."""
        with self._lock:
            if self._job is not None:
                return None
            job = Job(kind=kind, total=total)
            self._job = job
            self._ensure_watchdog()
            return job

    def busy(self) -> bool:
        with self._lock:
            return self._job is not None

    def current(self) -> Optional[Job]:
        with self._lock:
            return self._job

    # ------------------------------------------------------------- обновления

    def beat(self, job: Job, phase: str = "") -> None:
        """Отмечает, что задача жива. Вызывать на каждом заметном шаге."""
        with self._lock:
            job.heartbeat = time.monotonic()
            if phase:
                job.phase = phase

    def log(self, job: Optional[Job], message: str) -> None:
        if not message:
            return
        with self._lock:
            target = job or self._job
            if target is None:
                self._last.setdefault("log", []).append(message)
                del self._last["log"][:-LOG_LIMIT]
                return
            target.log.append(message)
            del target.log[:-LOG_LIMIT]
            target.heartbeat = time.monotonic()

    def progress(self, job: Job, done: int, total: int, failed: Optional[int] = None,
                 phase: str = "") -> None:
        with self._lock:
            job.done = done
            job.total = max(total, 0)
            if failed is not None:
                job.failed = failed
            if phase:
                job.phase = phase
            job.percent = round(done / max(1, total) * 100, 1)
            job.heartbeat = time.monotonic()

    # ---------------------------------------------------------------- отмена

    def register_killer(self, job: Job, killer: Callable[[], None]) -> None:
        """Регистрирует колбэк принудительного завершения (например driver.quit)."""
        with self._lock:
            job.killers.append(killer)

    def unregister_killer(self, job: Job, killer: Callable[[], None]) -> None:
        with self._lock:
            try:
                job.killers.remove(killer)
            except ValueError:
                pass

    def request_cancel(self) -> bool:
        """Просит текущую задачу остановиться. Слот НЕ освобождается сразу:
        его освободит `finalize()` рабочего потока или watchdog."""
        with self._lock:
            job = self._job
            if job is None:
                return False
            job.cancel_event.set()
            job.state = CANCELLING
            job.phase = "отмена"
            job.log.append("⏹ Отмена запрошена — останавливаю воркеры…")
            killers = list(job.killers)
        for kill in killers:
            _safe_call(kill)
        return True

    # -------------------------------------------------------------- финализация

    def finalize(self, job: Job, state: str, error: Optional[str] = None,
                 message: str = "") -> None:
        """Всегда вызывать в `finally` рабочего потока.

        Освобождает слот и оставляет снимок для UI. После этого приложение
        снова в состоянии «Готов к новой задаче».
        """
        with self._lock:
            if self._job is not job:
                return                       # уже финализировано watchdog'ом
            if job.cancelled and state != ERROR:
                state = CANCELLED
            job.state = state
            job.error = error
            job.finished_at = time.monotonic()
            if message:
                job.log.append(message)
            job.log.append("🔄 Готов к новой задаче.")
            self._last = {
                "state": state,
                "phase": job.phase,
                "percent": 100.0 if state == DONE else job.percent,
                "done": job.done,
                "failed": job.failed,
                "total": job.total,
                "error": error,
                "log": list(job.log),
                "kind": job.kind,
                "job_id": job.job_id,
                "elapsed": round(job.finished_at - job.started_at, 1),
            }
            job.killers.clear()
            self._job = None

    def reset(self, clean_profiles: bool = True) -> dict:
        """Принудительный возврат в «Готов к новой задаче».

        Используется кнопкой «Сбросить состояние», когда что-то всё же зависло.
        """
        with self._lock:
            job = self._job
            killers = list(job.killers) if job else []
            if job is not None:
                job.cancel_event.set()
        for kill in killers:
            _safe_call(kill)
        with self._lock:
            if self._job is not None:
                self._job.killers.clear()
                self._job = None
            self._last = {
                "state": IDLE, "phase": "", "percent": 0.0, "done": 0, "failed": 0,
                "total": 0, "error": None, "kind": "", "job_id": "",
                "log": ["🧹 Состояние сброшено. Готов к новой задаче."],
            }
        removed = kill_orphan_browsers() if clean_profiles else 0
        return {"ok": True, "profiles_removed": removed}

    # ---------------------------------------------------------------- снимок

    def snapshot(self) -> dict:
        with self._lock:
            job = self._job
            if job is None:
                snap = dict(self._last)
                snap.setdefault("heartbeat_age", 0.0)
                snap["busy"] = False
                return snap
            return {
                "state": job.state,
                "kind": job.kind,
                "job_id": job.job_id,
                "phase": job.phase,
                "percent": job.percent,
                "done": job.done,
                "failed": job.failed,
                "total": job.total,
                "error": job.error,
                "log": list(job.log),
                "elapsed": round(time.monotonic() - job.started_at, 1),
                "heartbeat_age": round(time.monotonic() - job.heartbeat, 1),
                "busy": True,
            }

    # -------------------------------------------------------------- watchdog

    def _ensure_watchdog(self) -> None:
        if self._watchdog and self._watchdog.is_alive():
            return
        self._stop_watchdog.clear()
        self._watchdog = threading.Thread(target=self._watch, daemon=True,
                                          name="JobWatchdog")
        self._watchdog.start()

    def _watch(self) -> None:
        while not self._stop_watchdog.wait(5.0):
            with self._lock:
                job = self._job
                if job is None:
                    continue
                age = time.monotonic() - job.heartbeat
                stalled = age > self.stall_timeout
                killers = list(job.killers) if stalled else []
            if not stalled:
                continue
            for kill in killers:
                _safe_call(kill)
            with self._lock:
                job = self._job
                if job is None:
                    continue
                job.log.append(
                    f"⛔ Задача не отвечает {int(age)}с — принудительное завершение."
                )
                self.finalize(
                    job, ERROR,
                    error=f"Задача зависла (нет активности {int(age)}с) и была снята",
                )

    def shutdown(self) -> None:
        self._stop_watchdog.set()


# ------------------------------------------------------------------ утилиты

def _safe_call(fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception:                                   # noqa: BLE001
        pass


def kill_orphan_browsers() -> int:
    """Убивает осиротевшие Chrome/chromedriver и чистит временные профили.

    Возвращает число удалённых каталогов профилей. Работает «best effort»:
    любая ошибка игнорируется, чтобы сброс состояния никогда не падал.
    """
    pattern = os.path.join(tempfile.gettempdir(), PROFILE_PREFIX + "*")
    profiles = glob.glob(pattern)

    for profile in profiles:
        _kill_by_profile(profile)

    removed = 0
    for profile in profiles:
        try:
            shutil.rmtree(profile, ignore_errors=True)
            removed += 1
        except OSError:
            pass
    return removed


def _kill_by_profile(profile: str) -> None:
    """Завершает процессы браузера, запущенные с указанным user-data-dir.

    Только процессы, принадлежащие нашим временным профилям `cf_w*`, — чужие
    окна Chrome пользователя не затрагиваются.
    """
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["wmic", "process", "where",
                 f"CommandLine like '%{os.path.basename(profile)}%'", "delete"],
                capture_output=True, timeout=20,
            )
        else:
            subprocess.run(["pkill", "-f", os.path.basename(profile)],
                           capture_output=True, timeout=20)
    except Exception:                                   # noqa: BLE001
        pass


#: Общий на процесс менеджер — используется server.py.
MANAGER = JobManager()
