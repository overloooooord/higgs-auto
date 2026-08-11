"""Generator AI-hooks: Higgsfield Kling Motion 2.6 via SeleniumBase UC mode.

Uses undetected Chrome to bypass Cloudflare Turnstile/CAPTCHA detection.
Higgsfield uses Clerk for auth — NO visible CAPTCHA required.

Flow:
  1. Open higgsfield.ai → click "Sign up"
  2. Clerk modal appears → click "Continue with Email"
  3. Enter temp email from AnyMessage → submit
  4. Wait for OTP code from AnyMessage → enter code
  5. Skip onboarding → navigate to Motion page
  6. Upload video + model photo → Generate → Download

Usage:
    export ANYMESSAGE_KEY="..."
    python -m clipforge.generate_hooks --count 60 \
        --videos-dir ./hook_refs --models-dir ./models --output-dir ./raw_batch/1
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import queue
import random
import re
import shutil
import string
import socket
import sys
import tempfile
import threading
import time
import traceback
import ssl
import urllib.error
import urllib.parse
import urllib.request

# Bypass SSL certificate verification errors on Windows/clean Python installs
try:
    ssl._create_default_https_context = ssl._create_unverified_context
except AttributeError:
    pass
from dataclasses import dataclass, field
from enum import Enum, auto
from selenium.webdriver.common.action_chains import ActionChains
import uuid
from typing import Callable, Optional

MOTION_URL = "https://higgsfield.ai/ai/video/motion?rp=%2Fai%2Fvideo%2Fmotion"
HOME_URL = "https://higgsfield.ai/"


# ---------------------------------------------------------------- Task types

class TaskStatus(Enum):
    PENDING = auto()
    RUNNING = auto()
    SUCCESS = auto()
    FAILED = auto()
    SKIPPED = auto()


@dataclass
class GenerationTask:
    """Single video generation task with retry/rotation state.

    Два независимых бюджета:
      * `attempt` / `max_retries` — технические сбои (сеть, таймаут, DOM).
      * `nsfw_rotations` / `max_nsfw_rotations` — отказ по контент-политике.
        NSFW не расходует повторы: берётся другой хук и генерация стартует
        заново (требование п.7).
    """
    task_id: int
    model_photo: str
    hook_video: str
    output_path: str
    status: TaskStatus = TaskStatus.PENDING
    attempt: int = 0
    max_retries: int = 2
    nsfw_rotations: int = 0
    max_nsfw_rotations: int = 3
    error: str = ""
    #: True, если задача создана как замещающая (backfill) для добора N × M.
    is_backfill: bool = False

    def rotate_hook(self, all_hooks: list[str]) -> str:
        """Гарантированно берёт ДРУГОЙ хук (если файлов больше одного).

        `random.choice` при 1–2 файлах часто возвращал тот же самый хук, из-за
        чего ротация при NSFW не имела эффекта. Здесь циклический сдвиг по
        списку — детерминированно и всегда другой файл.
        """
        if not all_hooks:
            return self.hook_video
        try:
            idx = all_hooks.index(self.hook_video)
        except ValueError:
            idx = -1
        self.hook_video = all_hooks[(idx + 1) % len(all_hooks)]
        return self.hook_video

    def budget_left(self) -> bool:
        return self.attempt <= self.max_retries


class NSFWError(Exception):
    """Higgsfield rejected content as NSFW / content policy violation."""
    pass


class AttemptTimeout(Exception):
    """Полный бюджет времени на попытку исчерпан."""
    pass


class Cancelled(Exception):
    """Пользователь запросил отмену — выходим тихо."""
    pass


# ---------------------------------------------------------------- Deadline

class Deadline:
    """Монотонный дедлайн на попытку генерации.

    Пробрасывается во все шаги (регистрация → onboarding → загрузка →
    генерация → скачивание), чтобы попытка не могла длиться дольше
    настроенного `timeout_attempt`. Раньше бюджет был только у самой
    генерации, поэтому реальная попытка растягивалась на
    `timeout_gen + OTP 180s + …`.
    """

    __slots__ = ("_end", "_budget", "label")

    def __init__(self, budget: float, label: str = "attempt"):
        self._budget = max(1.0, float(budget))
        self._end = time.monotonic() + self._budget
        self.label = label

    @property
    def budget(self) -> float:
        return self._budget

    def remaining(self) -> float:
        return self._end - time.monotonic()

    def expired(self) -> bool:
        return self.remaining() <= 0

    def elapsed(self) -> float:
        return self._budget - self.remaining()

    def check(self, phase: str = "") -> None:
        """Бросает AttemptTimeout, если время вышло."""
        if self.expired():
            where = f" на этапе «{phase}»" if phase else ""
            raise AttemptTimeout(f"Таймаут {self._budget:.0f}с исчерпан{where}")

    def sub(self, seconds: float) -> float:
        """Ограничивает вложенный таймаут остатком общего бюджета."""
        return max(0.0, min(float(seconds), self.remaining()))


def interruptible_wait(seconds: float,
                       cancel: Optional[threading.Event] = None,
                       deadline: Optional["Deadline"] = None,
                       phase: str = "") -> None:
    """Замена `time.sleep`, реагирующая на отмену и дедлайн.

    Раньше все ожидания были фиксированными `time.sleep`, из-за чего «Стоп»
    и таймаут не могли прервать паузу — воркер продолжал спать.
    """
    if seconds <= 0:
        if cancel is not None and cancel.is_set():
            raise Cancelled("Отменено пользователем")
        if deadline is not None:
            deadline.check(phase)
        return

    end = time.monotonic() + seconds
    while True:
        if cancel is not None and cancel.is_set():
            raise Cancelled("Отменено пользователем")
        if deadline is not None:
            deadline.check(phase)
        left = end - time.monotonic()
        if left <= 0:
            return
        if cancel is not None:
            if cancel.wait(min(left, 0.25)):
                raise Cancelled("Отменено пользователем")
        else:
            time.sleep(min(left, 0.25))


def free_port() -> int:
    """Свободный TCP-порт от ОС для Chrome DevTools.

    Прежняя схема `9222 + worker_id * 10 + attempt` при нескольких воркерах и
    повторах давала коллизии портов: Chrome не поднимался, а поток вставал
    внутри блокирующего вызова Selenium.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def hard_kill_driver(sb) -> None:
    """Принудительно закрывает браузер и убивает процесс chromedriver.

    Вызывается из ПОСТОРОННЕГО потока (watchdog), чтобы разморозить рабочий
    поток, залипший внутри блокирующего вызова Selenium: после смерти драйвера
    такой вызов немедленно падает исключением.
    """
    driver = getattr(sb, "driver", None) or sb
    for action in ("quit", "close"):
        try:
            fn = getattr(driver, action, None)
            if callable(fn):
                fn()
                break
        except Exception:                                   # noqa: BLE001
            pass
    for attr in ("service", "_service"):
        svc = getattr(driver, attr, None)
        proc = getattr(svc, "process", None) if svc else None
        if proc is None:
            continue
        try:
            proc.kill()
        except Exception:                                   # noqa: BLE001
            pass



# ---------------------------------------------------------------- AnyMessage

class AnyMessageClient:
    """REST-client AnyMessage (anymessage.org)."""

    def __init__(self, api_key: str, base_url: str = "https://api.anymessage.shop",
                 site: str = "higgsfield.ai", domain: str = "gmail.com"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.site = site
        self.domain = domain
        self._email: Optional[str] = None
        self._email_id: Optional[str] = None

    def _req(self, method: str, path: str, params: Optional[dict] = None) -> dict:
        url = f"{self.base_url}/{path.lstrip('/')}"
        qs = {"token": self.api_key}
        qs.update(params or {})
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urllib.parse.urlencode(qs)}"
        req = urllib.request.Request(
            url, method=method,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        try:
            import ssl
            ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"AnyMessage HTTP {exc.code}: "
                f"{exc.read().decode('utf-8', 'replace')[:300]}"
            )
        return json.loads(body or "{}")

    def get_temp_email(self, domain: str = "") -> str:
        preferred = domain or self.domain
        fallback_domains = [preferred, "gmail.com", "icloud.com", "rambler.ru", "yandex.ru", "hotmail.com", "outlook.com"]
        candidate_domains = list(dict.fromkeys([d for d in fallback_domains if d]))

        last_error = None
        for attempt in range(3):
            for d in candidate_domains:
                try:
                    res = self._req("GET", "/email/order",
                                    {"site": self.site, "domain": d})
                    if res.get("status") == "success" and res.get("email"):
                        email = res.get("email")
                        self._email = email
                        self._email_id = str(res.get("id") or email)
                        print(f"  📧 AnyMessage email ordered ({d}): {email}")
                        return email
                    else:
                        print(f"  [AnyMessage] domain {d} error: {res}")
                        last_error = res
                except Exception as exc:
                    last_error = exc
            time.sleep(2)

        raise RuntimeError(f"AnyMessage error: {last_error}")

    def get_otp_code(self, timeout_sec: int = 180, poll: float = 1.5,
                     cancel: Optional[threading.Event] = None,
                     deadline: Optional["Deadline"] = None) -> Optional[str]:
        """Poll AnyMessage for verification code from Clerk/Higgsfield.

        `cancel`/`deadline` обязательны для отзывчивости: раньше ожидание OTP
        могло длиться до 180с независимо от нажатия «Стоп» и таймаута попытки.
        """
        if not self._email_id:
            raise RuntimeError("Call get_temp_email() first")
        # Give Clerk a moment to actually send the email
        interruptible_wait(1.5, cancel, deadline, "ожидание OTP")
        start = time.time()
        while time.time() - start < timeout_sec:
            if deadline is not None and deadline.expired():
                return None
            try:
                res = self._req("GET", "/email/getmessage",
                                {"id": self._email_id, "preview": 0})
            except RuntimeError:
                interruptible_wait(poll, cancel, deadline, "ожидание OTP")
                continue

            # Extract text from response
            message = res.get("message") or ""
            value = res.get("value")
            if not message and isinstance(value, dict):
                message = value.get("message") or ""
            html = res.get("html") or ""

            # Skip if no actual email content yet (just "wait message" etc.)
            if (not message and not html) or "wait" in str(res.get("value", "")).lower():
                interruptible_wait(poll, cancel, deadline, "ожидание OTP")
                continue

            # IMPORTANT: Strip HTML tags to get plain text only.
            # Raw HTML contains numbers in styles/attributes (colors, timestamps)
            # that falsely match as OTP codes.
            plain_msg = re.sub(r"<[^>]+>", " ", message)
            plain_html = re.sub(r"<[^>]+>", " ", html)
            # Collapse whitespace
            plain_msg = re.sub(r"\s+", " ", plain_msg).strip()
            plain_html = re.sub(r"\s+", " ", plain_html).strip()
            text = f"{plain_msg}\n{plain_html}"

            # Debug: log what AnyMessage returned (plain text)
            print(f"    [AnyMessage] plain text: {text[:300]}")

            # NOTE: res.get("code") is AnyMessage's internal response code,
            # NOT the verification OTP! Do NOT use it.

            # Parse OTP from email body (plain text only)
            # Clerk sends: "Your verification code is 123456" or similar
            # Priority 1: explicit "code is X" pattern
            m = re.search(
                r"(?:verification\s+code|code\s+is|your\s+code)[:\s]+(\d{4,8})",
                text, re.IGNORECASE
            )
            if m:
                print(f"    [AnyMessage] found code via pattern: {m.group(1)}")
                return m.group(1)

            # Priority 2: code near keyword
            m = re.search(r"(?:code|код|verify)[^\d]{0,20}(\d{6})", text, re.IGNORECASE)
            if m:
                print(f"    [AnyMessage] found 6-digit near keyword: {m.group(1)}")
                return m.group(1)

            # Priority 3: standalone 6-digit number (most common OTP length)
            m = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
            if m:
                print(f"    [AnyMessage] found standalone 6-digit: {m.group(1)}")
                return m.group(1)

            # Priority 4: 4-8 digit number as last resort
            m = re.search(r"(?<!\d)(\d{4,8})(?!\d)", text)
            if m:
                print(f"    [AnyMessage] found {len(m.group(1))}-digit fallback: {m.group(1)}")
                return m.group(1)

            interruptible_wait(poll, cancel, deadline, "ожидание OTP")
        return None




# ---------------------------------------------------------------- Guerrillamail

class GuerrillaMailClient:
    """Temp-mail client using GuerillaMail (sid_token auth)."""

    BASE = "https://api.guerrillamail.com/ajax.php"

    def __init__(self, sid_token: str):
        self.sid = sid_token
        self._email: Optional[str] = None

    def _get(self, params: dict) -> dict:
        qs = urllib.parse.urlencode({"sid_token": self.sid, **params})
        req = urllib.request.Request(f"{self.BASE}?{qs}")
        req.add_header("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())

    def get_temp_email(self, domain: str = "") -> str:
        d = self._get({"f": "get_email_address", "lang": "en"})
        self.sid = d.get("sid_token", self.sid)
        self._email = d["email_addr"]
        return self._email

    def get_otp_code(self, timeout_sec: int = 180, poll: float = 1.5,
                     cancel: Optional[threading.Event] = None,
                     deadline: Optional["Deadline"] = None) -> Optional[str]:
        """Poll inbox for 6-digit OTP (прерываемо по отмене/дедлайну)."""
        interruptible_wait(1.5, cancel, deadline, "ожидание OTP")
        start = time.time()
        seen: set = set()
        while time.time() - start < timeout_sec:
            if deadline is not None and deadline.expired():
                return None
            try:
                d = self._get({"f": "get_email_list", "offset": 0})
                self.sid = d.get("sid_token", self.sid)
                for msg in d.get("list", []):
                    mid = str(msg.get("mail_id", ""))
                    if mid in seen:
                        continue
                    seen.add(mid)
                    # Try excerpt first
                    excerpt = msg.get("mail_excerpt", "")
                    m = re.search(r"(?<!\d)(\d{6})(?!\d)", excerpt)
                    if m:
                        print(f"    [GM] OTP from excerpt: {m.group(1)}")
                        return m.group(1)
                    # Fetch full body
                    try:
                        d2 = self._get({"f": "fetch_email", "email_id": mid})
                        body = re.sub(r"<[^>]+>", " ", d2.get("mail_body", ""))
                        body = re.sub(r"\s+", " ", body)
                        print(f"    [GM] mail body: {body[:200]}")
                        # Pattern: "code is X"
                        m2 = re.search(
                            r"(?:code|verification|verify)[^\d]{0,30}(\d{4,8})",
                            body, re.IGNORECASE)
                        if m2:
                            print(f"    [GM] OTP pattern: {m2.group(1)}")
                            return m2.group(1)
                        m3 = re.search(r"(?<!\d)(\d{6})(?!\d)", body)
                        if m3:
                            print(f"    [GM] OTP standalone: {m3.group(1)}")
                            return m3.group(1)
                    except Exception:
                        pass
            except Exception as e:
                print(f"    [GM] poll error: {e}")
            interruptible_wait(poll, cancel, deadline, "ожидание OTP")
        return None

# ---------------------------------------------------------------- helpers

def _resolve_dir(folder: str) -> str:
    if os.path.isdir(folder):
        return folder
    # Try parent directories relative to this file and current working dir
    base_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(base_dir)
    grandparent_dir = os.path.dirname(parent_dir)
    folder_name = os.path.basename(folder.rstrip("/\\"))
    for root in [parent_dir, base_dir, os.getcwd(), grandparent_dir]:
        candidate = os.path.join(root, folder_name)
        if os.path.isdir(candidate):
            return candidate
    return folder

def _list_files(folder: str, exts: tuple[str, ...]) -> list[str]:
    folder = _resolve_dir(folder)
    if not os.path.isdir(folder):
        return []
    files = [os.path.join(folder, f) for f in os.listdir(folder)
             if f.lower().endswith(exts)]
    files.sort(key=lambda p: [int(c) if c.isdigit() else c.lower()
                              for c in re.split(r"(\d+)", os.path.basename(p))])
    return files



# ---------------------------------------------------------------- generator

# Force UTF-8 encoding for stdout/stderr on Windows to avoid UnicodeEncodeError with emojis
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_print_lock = threading.Lock()

def _tprint(*args, **kwargs):
    """Thread-safe print with Windows unicode fallback."""
    with _print_lock:
        try:
            print(*args, **kwargs)
        except UnicodeEncodeError:
            try:
                enc = getattr(sys.stdout, 'encoding', None) or 'utf-8'
                msg = " ".join(str(a) for a in args)
                safe_msg = msg.encode(enc, errors="replace").decode(enc)
                print(safe_msg, **kwargs)
            except Exception:
                pass

class HookGenerator:
    def __init__(
        self,
        videos_dir: str,
        models_dir: str,
        output_dir: str,
        anymessage_key: str = "",
        site_url: str = MOTION_URL,
        headless: bool = False,
        timeout_gen: int = 300,
        max_retries: int = 2,
        guerrillamail_sid: str = "",
        model_photo: str = "",
        timeout_attempt: int = 900,
        nsfw_rotations: int = 3,
    ):
        self.site_url = site_url
        self.videos = _list_files(videos_dir, (".mp4", ".mov", ".webm", ".m4v"))
        all_models = _list_files(models_dir, (".png", ".jpg", ".jpeg", ".webp"))
        if model_photo:
            # Support comma-separated list of model filenames from UI
            names = [n.strip().lower() for n in model_photo.split(",") if n.strip()]
            matched = [m for m in all_models if os.path.basename(m).lower() in names]
            self.models = matched if matched else all_models
        else:
            self.models = all_models
        self.output_dir = output_dir
        self.guerrillamail_sid = guerrillamail_sid
        self.anymessage = AnyMessageClient(api_key=anymessage_key) if anymessage_key else None
        self.headless = headless
        self.timeout_gen = timeout_gen
        #: Полный бюджет одной попытки: регистрация + onboarding + загрузка +
        #: генерация + скачивание. Не может быть меньше timeout_gen.
        self.timeout_attempt = max(int(timeout_attempt), int(timeout_gen) + 60)
        self.max_retries = max_retries
        self.nsfw_rotations = max(0, int(nsfw_rotations))
        self._file_lock = threading.Lock()
        #: Контекст попытки на поток: дедлайн + событие отмены.
        #: HookGenerator один на все воркеры, поэтому контекст обязан быть
        #: thread-local, иначе воркеры перетирали бы дедлайны друг друга.
        self._ctx = threading.local()
        #: Режим выполнения JS: None — не определён, True — CDP (без
        #: верхнеуровневого return), False — классический WebDriver.
        self._js_expr_mode: Optional[bool] = None
        self._heartbeat_cb: Optional[Callable[[], None]] = None

    # ------------------------------------------------- контекст попытки

    def _bind_ctx(self, deadline: Optional[Deadline],
                  cancel: Optional[threading.Event]) -> None:
        self._ctx.deadline = deadline
        self._ctx.cancel = cancel

    @property
    def _deadline(self) -> Optional[Deadline]:
        return getattr(self._ctx, "deadline", None)

    @property
    def _cancel(self) -> Optional[threading.Event]:
        return getattr(self._ctx, "cancel", None)

    # ------------------------------------------------- ввод текста

    def _type_slow(self, sb, selector: str, text: str,
                   delay: float = 0.09) -> None:
        """Печатает текст по символам с задержкой.

        `sb.type()` вставляет строку мгновенно — Clerk иногда не успевает
        провалидировать поле и кнопка «Continue» остаётся неактивной, а
        антибот-проверка считает такой ввод машинным. Посимвольный ввод с
        небольшой паузой ведёт себя как реальный пользователь.
        """
        elem = sb.find_element(selector)
        try:
            elem.clear()
        except Exception:                                   # noqa: BLE001
            pass
        elem.click()
        for ch in text:
            self._guard("ввод email")
            elem.send_keys(ch)
            # Небольшой разброс задержки, чтобы ритм не был машинным.
            interruptible_wait(delay + random.uniform(0.0, 0.05),
                               cancel=self._cancel, deadline=self._deadline,
                               phase="ввод email")

    # ------------------------------------------------- исполнение JS

    def _js(self, sb, script: str, default=None):
        """Выполняет JS с поддержкой обоих режимов SeleniumBase.

        Классический WebDriver оборачивает скрипт в функцию, поэтому
        верхнеуровневый `return` допустим. В UC-режиме SeleniumBase часто
        уходит на CDP `Runtime.evaluate`, где такой `return` — синтаксическая
        ошибка «Illegal return statement». Из-за этого в живом прогоне молча
        падали переключатель Video/Image, выбор модели и загрузка файлов:
        сайт потом отвечал «Input Video Required».

        Здесь режим определяется один раз и запоминается, после чего скрипт
        отправляется в подходящей форме: с `return` либо как выражение.
        """
        expr = re.sub(r"^\s*return\s+", "", script, count=1)

        # Режим уже известен — сразу правильная форма,
        # но после навигации SeleniumBase может молча сменить режим
        # (WebDriver ↔ CDP), поэтому при «Illegal return» пробуем другой.
        if self._js_expr_mode is True:
            try:
                return sb.execute_script(expr)
            except Exception as exc:                        # noqa: BLE001
                if "Illegal return statement" in str(exc):
                    # Switched back to WebDriver — try with return
                    self._js_expr_mode = False
                    try:
                        return sb.execute_script(script)
                    except Exception:                       # noqa: BLE001
                        return default
                return default
        if self._js_expr_mode is False:
            try:
                return sb.execute_script(script)
            except Exception as exc:                        # noqa: BLE001
                if "Illegal return statement" in str(exc):
                    # Switched to CDP — try without return
                    self._js_expr_mode = True
                    try:
                        return sb.execute_script(expr)
                    except Exception:                       # noqa: BLE001
                        return default
                raise

        # Первый вызов: пробуем классическую форму и запоминаем результат.
        try:
            result = sb.execute_script(script)
            self._js_expr_mode = False
            return result
        except Exception as exc:                            # noqa: BLE001
            if "Illegal return statement" not in str(exc):
                raise
        self._js_expr_mode = True
        _tprint("  ℹ JS-режим: CDP (скрипты отправляются как выражения)")
        try:
            return sb.execute_script(expr)
        except Exception:                                   # noqa: BLE001
            return default

    def set_heartbeat(self, cb: Optional[Callable[[], None]]) -> None:
        """Регистрирует колбэк «я жив», вызываемый на каждом шаге сценария.

        Раньше активность отмечалась только при завершении задачи, поэтому
        детектор зависания срабатывал через `timeout_attempt + 120` от старта,
        а не от реального залипания.
        """
        self._heartbeat_cb = cb

    def _beat(self) -> None:
        cb = getattr(self, "_heartbeat_cb", None)
        if cb is not None:
            try:
                cb()
            except Exception:                               # noqa: BLE001
                pass

    def _guard(self, phase: str = "") -> None:
        """Проверяет отмену и дедлайн без ожидания."""
        cancel = self._cancel
        if cancel is not None and cancel.is_set():
            raise Cancelled("Отменено пользователем")
        deadline = self._deadline
        if deadline is not None:
            deadline.check(phase)

    def _wait(self, seconds: float, phase: str = "") -> None:
        """Прерываемая пауза вместо `time.sleep`.

        Все ожидания внутри сценария проходят через неё, поэтому «Стоп» и
        таймаут срабатывают в пределах ~0.25с, а не после окончания паузы.
        Заодно это самая частая точка для heartbeat: пока поток крутится по
        сценарию, задача считается живой.
        """
        self._beat()
        interruptible_wait(seconds, cancel=self._cancel,
                           deadline=self._deadline, phase=phase)

    def _time_left(self, default: float) -> float:
        """Остаток бюджета попытки (или `default`, если контекста нет)."""
        deadline = self._deadline
        return deadline.remaining() if deadline is not None else float(default)

    def _make_email_client(self):
        """Return a FRESH email client instance per call (thread-safe).
        
        CRITICAL: AnyMessageClient stores _email/_email_id as instance state.
        If multiple threads share one instance, thread B's get_temp_email()
        overwrites thread A's _email_id, causing OTP cross-contamination.
        Each thread MUST get its own instance.
        """
        if self.guerrillamail_sid:
            return GuerrillaMailClient(self.guerrillamail_sid)
        if self.anymessage:
            # Create a fresh instance with same config, not the shared one
            return AnyMessageClient(
                api_key=self.anymessage.api_key,
                base_url=self.anymessage.base_url,
                site=self.anymessage.site,
                domain=self.anymessage.domain,
            )
        raise RuntimeError("No email provider configured")

    # -------------------------------------------------------- login

    def _get_saved_credentials(self) -> list[tuple[str, str]]:
        """Read email:password pairs from credentials.txt (newest first)."""
        creds_path = os.path.join(self.output_dir, "credentials.txt")
        if not os.path.exists(creds_path):
            return []
        pairs = []
        with open(creds_path, "r") as f:
            for line in f:
                line = line.strip()
                if ":" in line:
                    email, password = line.split(":", 1)
                    pairs.append((email, password))
        # Return newest first
        return list(reversed(pairs))

    def _login(self, sb, email: str, password: str) -> bool:
        """Log into Higgsfield with email+password via Clerk.

        Returns True if login succeeded.
        """
        print(f"  🔑 Logging in as {email}...")
        sb.uc_open_with_reconnect(HOME_URL, reconnect_time=4)
        self._wait(2)

        # Click "Login" button in nav
        for attempt in range(3):
            self._js(sb, """return (function(){
                var items = document.querySelectorAll('button, a');
                for (var el of items) {
                    var txt = el.textContent.trim();
                    if ((txt === 'Login' || txt === 'Log in' || txt === 'Sign in')
                        && el.offsetParent !== null) {
                        el.click();
                        return;
                    }
                }
                })()""")
            self._wait(2)
            # Check if Clerk modal appeared
            try:
                has_modal = self._js(sb, """return (function(){
                    var txt = document.body.innerText || '';
                    return txt.includes('Welcome') || txt.includes('Continue with Email')
                        || txt.includes('Continue with Google') || txt.includes('Sign in');
                    })()""")
                if has_modal:
                    print(f"  ✓ Clerk login modal appeared (attempt {attempt + 1})")
                    break
            except Exception:
                pass
        else:
            print("  ⚠ Login modal did not appear")
            return False

        # Click "Continue with Email"
        self._wait(0.5)
        try:
            self._js(sb, """return (function(){
                var submits = document.querySelectorAll('input[type="submit"]');
                for (var s of submits) {
                    if (s.value && s.value.includes('Continue with Email')) {
                        s.click(); return;
                    }
                }
                var btns = document.querySelectorAll('button, a, [role="button"]');
                for (var b of btns) {
                    if (b.offsetHeight < 10) continue;
                    var txt = b.textContent.trim();
                    if (txt.includes('Continue with Email') || txt.includes('Continue with email')) {
                        b.click(); return;
                    }
                }
            })()""")
            print("  ✓ Clicked Continue with Email")
        except Exception:
            pass
        self._wait(1)

        # Fill email
        filled_email = False
        for sel in ["input[name='emailAddress']", "input[name='identifier']",
                    "input[type='email']", "input[name='email']",
                    "input[autocomplete='email']"]:
            try:
                sb.wait_for_element_visible(sel, timeout=5)
                sb.type(sel, email)
                filled_email = True
                print(f"  ✓ Filled email via: {sel}")
                break
            except Exception:
                continue
        if not filled_email:
            # JS fallback
            try:
                self._js(sb, f"""
                    var inputs = document.querySelectorAll('input');
                    for (var inp of inputs) {{
                        if (inp.offsetParent !== null && inp.type !== 'hidden'
                            && inp.type !== 'password' && inp.type !== 'file') {{
                            var nativeSet = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value').set;
                            nativeSet.call(inp, '{email}');
                            inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                            inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                            break;
                        }}
                    }}
                """)
                filled_email = True
                print("  ✓ Filled email via JS")
            except Exception:
                pass
        if not filled_email:
            print("  ❌ Could not fill email")
            return False

        # Click Continue / submit email
        try:
            self._js(sb, """return (function(){
                var btns = document.querySelectorAll('button');
                for (var b of btns) {
                    var txt = b.textContent.trim().toLowerCase();
                    if ((txt === 'continue' || txt.includes('continue with email'))
                        && b.offsetWidth > 0) {
                        b.click(); return;
                    }
                }
                // Fallback: submit via Enter
                var inp = document.querySelector('input[type="email"], input[name="emailAddress"], input[name="identifier"]');
                if (inp) {
                    inp.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter',code:'Enter',bubbles:true}));
                    inp.form && inp.form.requestSubmit && inp.form.requestSubmit();
                }
            })()""")
        except Exception:
            pass
        self._wait(2)

        # Fill password (Clerk shows password field after email submit)
        pw_filled = False
        for pw_wait in range(10):
            try:
                has_pw = self._js(sb, """return (function(){
                    var pw = document.querySelector('input[type="password"]');
                    return pw && pw.offsetHeight > 0;
                    })()""")
                if has_pw:
                    break
            except Exception:
                pass
            self._wait(1)

        try:
            pw_elem = sb.find_element("input[type='password']")
            pw_elem.click()
            self._wait(0.1)
            pw_elem.clear()
            pw_elem.send_keys(password)
            self._wait(0.2)
            # Fire events
            self._js(sb, """return (function(){
                var inp = document.querySelector('input[type="password"]');
                if (inp) {
                    ['input','change','blur','keyup'].forEach(function(e){
                        inp.dispatchEvent(new Event(e, {bubbles: true}));
                    });
                }
                })()""")
            pw_filled = True
            print("  ✓ Filled password")
        except Exception as e:
            print(f"  ❌ Password fill failed: {e}")
            return False

        # Submit login form
        try:
            self._js(sb, """return (function(){
                var btns = document.querySelectorAll('button');
                for (var b of btns) {
                    var txt = b.textContent.trim().toLowerCase();
                    if ((txt === 'continue' || txt === 'sign in' || txt === 'log in')
                        && b.offsetWidth > 0) {
                        b.click(); return;
                    }
                }
            })()""")
        except Exception:
            # Fallback: Enter on password field
            try:
                sb.send_keys("input[type='password']", "\n")
            except Exception:
                pass
        self._wait(2)

        # Verify login succeeded — wait up to 15s for redirect
        for check in range(15):
            try:
                body = self._js(sb, 
                    "return (document.body.innerText||'').substring(0,500)") or ""
                # Still on login/signup modal = not logged in yet
                if "Continue with Google" in body or "Continue with Email" in body:
                    self._wait(1)
                    continue
                # Check for wrong password errors
                if "incorrect" in body.lower() or "wrong password" in body.lower():
                    print(f"  ❌ Wrong password for {email}")
                    return False
                # Onboarding or Motion page = success
                if ("How do you plan" in body or "Motion" in body
                        or "Generate" in body or "credits" in body):
                    print("  ✅ Login successful!")
                    return True
            except Exception:
                pass
            self._wait(1)

        # Final check
        try:
            final_txt = self._js(sb, 
                "return (document.body.innerText||'').substring(0,300)") or ""
            if "Welcome to Higgsfield" in final_txt or "Continue with" in final_txt:
                print("  ❌ Login failed — still on auth screen")
                return False
            print("  ✅ Login appears successful")
            return True
        except Exception:
            return False

    # -------------------------------------------------------- registration

    # -------------------------------------------------------- registration

    def _register(self, sb) -> str:
        """Sign up on Higgsfield via Clerk email flow.

        Clerk modal flow:
        1. Click "Sign up" → modal with OAuth buttons appears
        2. Click "Continue with Email" → email input appears
        3. Enter email → click "Continue" → OTP verification screen
        4. Enter OTP code → account created
        """
        email_client = self._make_email_client()
        email = email_client.get_temp_email()
        print(f"  📧 Email: {email}")

        # Navigate to Higgsfield with UC anti-detection.
        # reconnect_time=4 gives Cloudflare Turnstile time to auto-solve.
        sb.uc_open_with_reconnect(HOME_URL, reconnect_time=4)
        self._wait(2)

        # Wait for page to be past Cloudflare (body text must contain site content)
        print("  ⏳ Waiting for Higgsfield to load past Cloudflare…")
        for _cf in range(15):
            self._guard("ожидание CF")
            try:
                body_txt = self._js(sb, "return (document.body.innerText||'').substring(0,400)") or ""
                if ("Sign up" in body_txt or "Sign in" in body_txt
                        or "Generate" in body_txt or "Higgsfield" in body_txt):
                    break
            except Exception:
                pass
            self._wait(1)

        # Step 1: Click "Sign up" — retry every second because Clerk loads async
        print("  ⏳ Waiting for Sign up button...")
        signup_clicked = False
        for _si in range(20):
            self._guard("клик Sign up")
            clicked = self._js(sb, """return (function(){
                var btns = document.querySelectorAll('button, a');
                for (var b of btns) {
                    if (b.textContent.trim().includes('Sign up') && b.offsetHeight > 0) {
                        b.click(); return true;
                    }
                }
                return false;
            })()""")
            if clicked:
                signup_clicked = True
                print("  ✓ Clicked Sign up")
                break
            self._wait(1)

        if not signup_clicked:
            # Page may already be logged in or Cloudflare is still blocking
            body_now = self._js(sb, "return (document.body.innerText||'').substring(0,400)") or ""
            if "Generate" in body_now or "Motion" in body_now:
                print("  ✓ Already logged in — skipping Sign up")
                return email
            raise RuntimeError("Sign up button not found after 20s — Cloudflare or page error")

        # Step 2: Wait for Clerk modal (up to 25 attempts, retrying click if needed)
        modal_ok = False
        for wait_i in range(25):
            self._guard("ожидание modal")
            modal_open = self._js(sb, """return (function(){
                var txt = document.body.innerText || '';
                if (txt.includes('Continue with Email') || txt.includes('Welcome to Higgsfield')
                    || txt.includes('Create an account') || txt.includes('Continue with Google'))
                    return true;
                var frames = document.querySelectorAll('iframe');
                for (var f of frames) {
                    try {
                        if (f.contentDocument.body.innerText.includes('Continue with Email')) return true;
                    } catch(e) {}
                }
                return false;
            })()""")
            if modal_open:
                modal_ok = True
                print(f"  ✓ Clerk modal appeared (wait {wait_i}s)")
                break
            # Re-click Sign up every 5s in case modal was dismissed by Cloudflare
            if wait_i > 0 and wait_i % 5 == 0:
                self._js(sb, """(function(){
                    var btns = document.querySelectorAll('button, a');
                    for (var b of btns) {
                        if (b.textContent.trim().includes('Sign up') && b.offsetHeight > 0) {
                            b.click(); return;
                        }
                    }
                })()""")
                print(f"  ↻ Re-clicked Sign up (wait {wait_i}s)")
            self._wait(1)

        if not modal_ok:
            raise RuntimeError("Clerk modal did not appear after 25 attempts")

        # Step 2: Click "Continue with Email" button → reveals email + password fields
        print("  ⏳ Clicking 'Continue with Email'...")
        clicked = self._js(sb, """return (function(){
            var btns = document.querySelectorAll('button');
            for (var b of btns) {
                if (b.offsetHeight < 1) continue;
                if (b.textContent.trim().includes('Continue with Email')) {
                    b.click();
                    return 'clicked';
                }
            }
            return null;
        })()""")
        if clicked:
            print(f"  ✓ Clicked Continue with Email ({clicked})")
        else:
            print("  ⚠ 'Continue with Email' button not found — proceeding anyway")

        # Step 3: Fill email — field has placeholder="Email"
        sb.wait_for_element_visible("input[placeholder='Email']", timeout=10)
        self._type_slow(sb, "input[placeholder='Email']", email)
        filled = True
        print("  ✓ Filled email via: input[placeholder='Email']")

        # Step 3b: Fill password — field has placeholder="Password"
        base_chars = (random.choices(string.ascii_lowercase, k=8) +
                      random.choices(string.ascii_uppercase, k=3) +
                      random.choices(string.digits, k=3))
        random.shuffle(base_chars)
        password = "Cf!" + "".join(base_chars)
        pw_filled = False

        sb.wait_for_element_visible("input[placeholder='Password']", timeout=10)

        # Method 1: React-compatible nativeInputValueSetter
        try:
            result = self._js(sb, f"""
                var inp = document.querySelector("input[placeholder='Password']");
                if (!inp || inp.offsetHeight === 0) return 'not-found';
                inp.focus();
                var nativeSet = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                nativeSet.call(inp, '{password}');
                inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                return 'ok';
            """)
            if result == 'ok':
                pw_filled = True
                print("  ✓ Filled password (React setter)")
        except Exception:
            pass

        # Method 2: Type via Selenium send_keys to fire all React handlers
        try:
            pw_elem = sb.find_element("input[placeholder='Password']")
            pw_elem.click()
            self._wait(0.04)
            pw_elem.clear()
            pw_elem.send_keys(password)
            self._wait(0.1)
            self._js(sb, """return (function(){
                var inp = document.querySelector("input[placeholder='Password']");
                if (inp) {
                    ['input','change','blur','keyup'].forEach(function(e){
                        inp.dispatchEvent(new Event(e, {bubbles: true}));
                    });
                }
                })()""")
            pw_filled = True
            print("  ✓ Filled password via send_keys & events")
        except Exception as e:
            print(f"  ⚠ Password fill fallback: {e}")

        if not pw_filled:
            print("  ℹ No password field found")

        # Save credentials to log file
        creds_path = os.path.join(self.output_dir, "credentials.txt")
        with self._file_lock:
            with open(creds_path, "a") as f:
                f.write(f"{email}:{password}\n")
        print(f"  💾 Credentials saved to {creds_path}")

        # Step 4: Submit signup form
        submitted = False

        # Method 1: Enter key on password input (most reliable for Clerk form submission)
        try:
            sb.send_keys("input[type='password']", "\n")
            submitted = True
            print("  ✓ Submitted via Enter key on password field")
        except Exception:
            pass

        # Method 2: Click submit button (button[type=submit] or input[type=submit])
        if not submitted:
            try:
                btn = self._js(sb, """return (function(){
                    var submitBtn = document.querySelector('button[type="submit"], input[type="submit"]');
                    if (submitBtn && submitBtn.offsetWidth > 0) return submitBtn;
                    var btns = document.querySelectorAll('button');
                    for (var b of btns) {
                        var txt = b.textContent.trim().toLowerCase();
                        if ((txt === 'continue' || txt === 'sign up' || txt === 'continue with email') && b.offsetWidth > 0) {
                            return b;
                        }
                    }
                    return null;
                })()""")
                if btn:
                    self._wait(0.2)
                    ActionChains(sb.driver).move_to_element(btn).click().perform()
                    submitted = True
                    print("  ✓ Clicked Submit button (AC)")
            except Exception as e:
                print(f"  ⚠ Submit button click error: {e}")

        # Method 2: uc_click on input[type=submit]
        if not submitted:
            try:
                sb.uc_click("input[type='submit']", timeout=4)
                submitted = True
                print("  ✓ Submitted via uc_click input[type=submit]")
            except Exception:
                pass

        # Method 3: XPath fallback for button tags
        if not submitted:
            for sel in [
                '//button[contains(text(),"Continue with Email")]',
                '//button[contains(text(),"Continue")]',
                '//button[@type="submit"]',
                'button[type="submit"]',
            ]:
                try:
                    sb.click(sel, timeout=4)
                    submitted = True
                    print("  ✓ Submitted email form (button fallback)")
                    break
                except Exception:
                    continue

        if not submitted:
            # Try Enter key on the email input
            try:
                sb.send_keys("input[type='email']", "\n")
                submitted = True
                print("  ✓ Submitted via Enter key")
            except Exception:
                pass
        if not submitted:
            self._js(sb, "document.querySelector('form')?.submit()")
            print("  ✓ Submitted via JS")

        self._wait(1)

        # Step 4b: Handle CAPTCHA if it appeared after submit
        # Clerk/Higgsfield may show Turnstile/hCaptcha after form submission
        for captcha_attempt in range(3):
            try:
                has_captcha = self._js(sb, """return (function(){
                    var txt = document.body.innerText || '';
                    if (txt.includes('CAPTCHA failed') || txt.includes('security check')) {
                        return 'captcha-error';
                    }
                    // Check for active Turnstile/hCaptcha iframe
                    var iframes = document.querySelectorAll('iframe');
                    for (var f of iframes) {
                        var src = f.src || '';
                        var title = f.title || '';
                        if (src.includes('turnstile') || src.includes('hcaptcha')
                            || src.includes('challenges') || title.includes('Turnstile') || title.includes('challenge')) {
                            if (f.offsetWidth > 0 || f.offsetHeight > 0) return 'captcha-iframe';
                        }
                    }
                    return null;
                })()""")
                if has_captcha:
                    print(f"  ⚠ CAPTCHA detected ({has_captcha}), attempting to handle (attempt {captcha_attempt + 1}/3)...")
                    try:
                        sb.uc_gui_handle_captcha()
                        print("  ✓ CAPTCHA handled via uc_gui_handle_captcha")
                    except Exception:
                        try:
                            sb.uc_gui_click_captcha()
                            print("  ✓ CAPTCHA handled via uc_gui_click_captcha")
                        except Exception:
                            # Try clicking the captcha checkbox directly
                            try:
                                self._js(sb, """return (function(){
                                    var iframes = document.querySelectorAll('iframe');
                                    for (var f of iframes) {
                                        var src = f.src || '';
                                        if (src.includes('turnstile') || src.includes('hcaptcha') || src.includes('captcha')) {
                                            f.click();
                                            break;
                                        }
                                    }
                                    })()""")
                            except Exception:
                                pass
                    self._wait(1.5)
                    # Re-submit the form after solving captcha
                    try:
                        self._js(sb, """return (function(){
                            var btns = document.querySelectorAll('button');
                            for (var b of btns) {
                                var txt = b.textContent.trim().toLowerCase();
                                if ((txt.includes('continue') || txt === 'sign up') && b.offsetWidth > 0) {
                                    b.click(); return;
                                }
                            }
                        })()""")
                    except Exception:
                        pass
                    self._wait(1)
                else:
                    break  # No captcha, continue
            except Exception:
                break

        # Check for error messages from Clerk (e.g., blocked email domain)
        try:
            error_text = self._js(sb, """return (function(){
                var errs = document.querySelectorAll(
                    '.cl-formFieldErrorText, [data-localization-key*="error"], .cl-alert__text'
                );
                var texts = [];
                errs.forEach(function(e) {
                    if (e.textContent.trim()) texts.push(e.textContent.trim());
                });
                return texts.join(' | ');
                })()""")
            if error_text:
                print(f"  ⚠ Clerk error: {error_text}")
                if any(w in error_text.lower() for w in
                       ["disposable", "blocked", "not allowed", "invalid"]):
                    raise RuntimeError(f"Clerk rejected email: {error_text}")
        except RuntimeError:
            raise
        except Exception:
            pass

        # Wait for "Verify Your Email" screen before polling OTP
        print("  ⏳ Waiting for verification screen...")
        for _ in range(15):
            try:
                if sb.is_text_visible("Verify Your Email") or sb.is_text_visible("verification code"):
                    print("  ✓ Verification screen appeared")
                    break
            except Exception:
                pass
            self._wait(1)
        else:
            print("  ⚠ Verification screen not detected, trying OTP anyway")

        # Step 5-6: Get OTP, enter it, retry if incorrect
        max_otp_attempts = 3
        for otp_attempt in range(1, max_otp_attempts + 1):
            # Before fetching OTP, check if we already passed verification
            # (e.g. Clerk auto-verified from a previous attempt)
            try:
                body_pre = self._js(sb, "return document.body.innerText || ''") or ""
                if "How do you plan" in body_pre or "flagship studios" in body_pre:
                    print("  ✅ Already past verification (onboarding detected)!")
                    return email
            except Exception:
                pass

            # Check if OTP input still exists; if modal reset, re-open signup
            try:
                has_otp_field = self._js(sb, """return (function(){
                    return !!document.querySelector(
                        'input[name="code"], input[inputmode="numeric"], input[autocomplete="one-time-code"]'
                    );
                    })()""")
                if not has_otp_field:
                    # Modal may have reset — check for "Welcome to Higgsfield"
                    body_check = self._js(sb, "return document.body.innerText || ''") or ""
                    if "Welcome to Higgsfield" in body_check or "Continue with Email" in body_check:
                        print("  ⚠ Clerk modal reset to login screen, aborting OTP retry")
                        raise RuntimeError("Clerk modal reset — registration session expired")
            except RuntimeError:
                raise
            except Exception:
                pass

            print(f"  ⏳ Waiting for OTP code (attempt {otp_attempt}/{max_otp_attempts})...")
            # Ожидание OTP ограничено остатком бюджета попытки: раньше оно
            # могло длиться 180с даже после нажатия «Стоп» или таймаута.
            otp = email_client.get_otp_code(
                timeout_sec=int(min(180, self._time_left(180))),
                cancel=self._cancel,
                deadline=self._deadline,
            )
            if not otp:
                raise RuntimeError("OTP not received in 180 sec")
            print(f"  ✓ OTP: {otp}")

            # Enter OTP — send digits one by one so Clerk's auto-submit fires
            sb.wait_for_element_visible("input[placeholder='Code'], input[name='code']", timeout=10)
            otp_elem = sb.find_element("input[placeholder='Code']") or sb.find_element("input[name='code']")
            otp_elem.click()
            otp_elem.clear()
            for ch in otp:
                otp_elem.send_keys(ch)
                self._wait(0.05)
            otp_entered = True
            print(f"  ✓ Entered OTP via send_keys char-by-char")

            # Wait for auto-verification and natural site redirect (Clerk auto-submits & redirects)
            print("  ⏳ Waiting for OTP verification and natural URL redirect...")
            initial_url = ""
            try:
                initial_url = sb.get_current_url()
            except Exception:
                pass

            verified = False
            for _wait in range(15):
                self._wait(1)
                try:
                    cur_url = sb.get_current_url() or ""
                    body_now = self._js(sb, "return document.body.innerText || ''") or ""

                    # Check for session cookie presence
                    has_session_cookie = False
                    try:
                        cookies = sb.get_cookies()
                        has_session_cookie = any(
                            'session' in c.get('name', '').lower() or
                            'clerk' in c.get('name', '').lower()
                            for c in cookies
                        )
                    except Exception:
                        pass

                    # 1. URL changed from initial signup page
                    url_changed = bool(initial_url and cur_url and cur_url != initial_url
                                       and "sign-up" not in cur_url.lower()
                                       and "verify" not in cur_url.lower())

                    # 2. Authenticated UI / onboarding detected in DOM
                    auth_ui_detected = any(m in body_now for m in [
                        "How do you plan", "flagship studios", "Personalizing",
                        "For personal use", "Motion Control", "Create Video",
                        "Generate", "Video generator", "Cinema Studio"
                    ])

                    # 3. OTP input field is gone AND session cookie or URL change happened
                    otp_still_visible = self._js(sb, """return (function(){
                        var inp = document.querySelector('input[name="code"], input[inputmode="numeric"], input[autocomplete="one-time-code"]');
                        return inp && (inp.offsetWidth > 0 || inp.offsetHeight > 0);
                    })()""")

                    if url_changed or auth_ui_detected or (not otp_still_visible and has_session_cookie):
                        verified = True
                        print(f"  ✓ OTP verified & redirect completed! (URL: {cur_url}, cookie={has_session_cookie})")
                        # Allow 2.5s for session cookies & local storage to fully persist
                        self._wait(2.5)
                        break
                except Exception:
                    pass

            if verified:
                print("  ✅ Registration complete!")
                return email

            # Still on verify screen — try clicking Verify/Continue button manually
            for btn_text in ["Verify", "Continue", "Submit"]:
                try:
                    sb.click(f'//button[contains(text(),"{btn_text}")]', timeout=2)
                    print(f"  ✓ Clicked {btn_text}")
                    self._wait(2)
                    break
                except Exception:
                    continue

            # Re-check after manual button click
            try:
                cur_url = sb.get_current_url() or ""
                body_now = self._js(sb, "return document.body.innerText || ''") or ""
                otp_still_visible = self._js(sb, """return (function(){
                    var inp = document.querySelector('input[name="code"], input[inputmode="numeric"], input[autocomplete="one-time-code"]');
                    return inp && (inp.offsetWidth > 0 || inp.offsetHeight > 0);
                })()""")
                if not otp_still_visible or "sign-up" not in cur_url.lower():
                    print("  ✅ Registration complete!")
                    self._wait(2)
                    return email
            except Exception:
                pass

            # Check for error messages
            try:
                has_error = self._js(sb, """return (function(){
                    var el = document.querySelector('.cl-formFieldErrorText');
                    if (!el) {
                        var all = document.querySelectorAll('[class*="error"], [class*="Error"]');
                        for (var i = 0; i < all.length; i++) {
                            if (all[i].textContent.toLowerCase().includes('incorrect')) return all[i].textContent;
                        }
                    }
                    return el ? el.textContent : null;
                    })()""")
                if has_error and "incorrect" in str(has_error).lower():
                    print(f"  ⚠ Incorrect code! Error: {has_error}")
                    if otp_attempt < max_otp_attempts:
                        try:
                            sb.click('//button[contains(text(),"Resend")]', timeout=3)
                            print("  ↻ Clicked Resend, waiting for new code...")
                            self._wait(3)
                        except Exception:
                            print("  ⚠ Resend button not found")
                        continue
                    else:
                        raise RuntimeError(f"OTP incorrect after {max_otp_attempts} attempts")
            except RuntimeError:
                raise
            except Exception:
                pass

            if otp_attempt == max_otp_attempts:
                raise RuntimeError("Still on verification screen after all OTP attempts")

        return email


    # -------------------------------------------------------- onboarding

    def _complete_onboarding(self, sb, max_steps: int = 20) -> None:
        """Skip onboarding if needed by navigating smoothly to Motion page."""
        self._wait(0.5)
        try:
            cur_url = sb.get_current_url() or ""
            if "motion" in cur_url.lower():
                print("  ✓ Already on Motion page")
                return
            body = self._js(sb, "return (document.body.innerText||'').substring(0,500)") or ""
        except Exception:
            body = ""

        if any(m in body for m in ["How do you plan", "1 of", "Personalizing",
                                    "For personal use", "flagship studios"]):
            print("  ⏭ Onboarding detected — navigating to Motion page")
        else:
            print("  ℹ No onboarding")
            return

        # Navigate using standard sb.open to preserve session cookies
        sb.open(MOTION_URL)
        self._wait(1.5)
        # Dismiss any remaining overlays
        try:
            self._js(sb, """return (function(){
                document.dispatchEvent(new KeyboardEvent('keydown',
                    {key:'Escape',code:'Escape',bubbles:true}));
                document.querySelectorAll(
                    '[class*="modal"],[class*="overlay"],[class*="onboard"],[class*="wizard"]'
                ).forEach(function(o){ if(o.style) o.style.display='none'; });
                })()""")
        except Exception:
            pass
        self._close_popups(sb)
        print("  ✓ Onboarding skipped")
        self._wait(0.5)

    # -------------------------------------------------------- generation

    def _close_popups(self, sb) -> None:
        """Close any popups/modals that appear after login or navigation.

        Known popups:
        - "Congratulations! You received a personal 61% OFF offer" (promo)
        - "ORGANIZE. SHARE. CREATE TOGETHER" (Cinema Studio promo)
        - Cookie consent banners
        - Feature announcement modals
        """
        for attempt in range(5):
            closed = False
            try:
                result = self._js(sb, """return (function(){
                    // --- 1. Promo/discount overlays (highest priority) ---
                    // Список маркеров расширен: окно «ORGANIZE. SHARE.
                    // CREATE TOGETHER» с кнопкой «Go to Cinema Studio» ранее
                    // не распознавалось и перекрывало форму загрузки, из-за
                    // чего видео не прикреплялось.
                    var bodyText = document.body.innerText || '';
                    if (bodyText.includes('OFF offer') || bodyText.includes('Claim Discount')
                        || bodyText.includes('special offer') || bodyText.includes('EXTRA DISCOUNT')
                        || bodyText.includes('Get Unlimited') || bodyText.includes('premium plan')
                        || bodyText.includes('CREATE TOGETHER') || bodyText.includes('Cinema Studio')
                        || bodyText.includes('Go to Cinema Studio')
                        || bodyText.includes('ORGANIZE') || bodyText.includes('UPGRADE PLAN')
                        || bodyText.includes("We've rebuilt how you structure your work")) {
                        var overlays = document.querySelectorAll(
                            '[role="dialog"], [class*="modal"], [class*="Modal"], [class*="popup"],'+
                            '[class*="Popup"], [class*="overlay"], [class*="Overlay"], [class*="backdrop"],'+
                            '[class*="Backdrop"], [class*="promotion"], [class*="promo"]');
                        for (var ov of overlays) {
                            if (!ov.offsetParent && ov.offsetHeight === 0) continue;
                            var cbtns = ov.querySelectorAll('button, [role="button"]');
                            for (var b of cbtns) {
                                var txt = b.textContent.trim();
                                var ariaLabel = (b.getAttribute('aria-label') || '').toLowerCase();
                                var hasSvg = !!b.querySelector('svg');
                                var r = b.getBoundingClientRect();
                                if (txt === '\u00d7' || txt === '\u2715' || txt === 'X' || txt === 'x'
                                    || txt === 'Close' || ariaLabel === 'close'
                                    || (hasSvg && r.width < 60 && r.height < 60)) {
                                    b.click();
                                    return 'closed-promo-overlay';
                                }
                            }
                        }
                    }

                    // --- 2. Any visible X/close buttons ---
                    var allBtns = document.querySelectorAll('button, [role="button"]');
                    for (var b of allBtns) {
                        if (!b.offsetParent && b.offsetHeight === 0) continue;
                        var txt = b.textContent.trim();
                        var ariaLabel = (b.getAttribute('aria-label') || '').toLowerCase();
                        if (txt === '\u00d7' || txt === '\u2715' || txt === 'X' || txt === 'x') {
                            var r = b.getBoundingClientRect();
                            if (r.width > 5 && r.height > 5) {
                                b.click();
                                return 'closed-x-btn';
                            }
                        }
                        if (ariaLabel === 'close' || ariaLabel === 'dismiss') {
                            b.click();
                            return 'closed-aria';
                        }
                    }

                    // --- 3. SVG-only close buttons in modals/dialogs ---
                    // Only click SVG buttons in the TOP-RIGHT corner of large overlays
                    // (actual close buttons), NOT small buttons on upload previews or cards
                    var containers = document.querySelectorAll(
                        '[role="dialog"], [class*="popup"], [class*="Popup"]');
                    for (var c of containers) {
                        if (!c.offsetParent && c.offsetHeight === 0) continue;
                        var cr = c.getBoundingClientRect();
                        // Only target large overlays (not small cards/previews)
                        if (cr.width < 200 || cr.height < 150) continue;
                        var btns = c.querySelectorAll('button');
                        for (var b of btns) {
                            var svg = b.querySelector('svg');
                            var r = b.getBoundingClientRect();
                            // Must be small SVG button in top-right area of container
                            if (svg && r.width < 60 && r.height < 60 && r.width > 5
                                && r.top - cr.top < 60 && cr.right - r.right < 60) {
                                b.click();
                                return 'closed-svg-modal';
                            }
                        }
                    }

                    // --- 4. Top banner dismiss ---
                    var banner = document.querySelector('[class*="banner"] button, [class*="Banner"] button');
                    if (banner && banner.offsetParent) {
                        var txt = banner.textContent.trim();
                        var svg = banner.querySelector('svg');
                        if (txt === '\u00d7' || txt === 'X' || txt === '\u2715' || svg) {
                            banner.click();
                            return 'closed-banner';
                        }
                    }

                    return null;
                })()""")
                if result:
                    closed = True
                    print(f"  \u2713 Closed popup: {result} (attempt {attempt + 1})")
                    self._wait(1)
            except Exception:
                pass

            if not closed:
                break
            self._wait(0.5)

    def _goto(self, sb, url: str, reconnect: float = 2) -> None:
        """Переходит по адресу и СРАЗУ закрывает всплывающие окна.

        Раньше попапы закрывались лишь на отдельных шагах, поэтому окно вроде
        «ORGANIZE. SHARE. CREATE TOGETHER» успевало перехватить клики и
        загрузка видео молча срывалась.
        """
        sb.uc_open_with_reconnect(url, reconnect_time=reconnect)
        self._wait(0.5)
        self._close_popups(sb)

    def _setup_generation(self, sb, video_path: str, photo_path: str) -> None:
        """Navigate to Motion page and set up generation parameters."""
        # Check if we're already on the Motion page (from onboarding or cookie restore)
        already_on_motion = False
        try:
            cur_url = self._js(sb, "return window.location.href") or ""
            if "motion" in cur_url.lower():
                already_on_motion = True
                print("  ✓ Already on Motion page")
        except Exception:
            pass

        if already_on_motion:
            # Даже если уже на нужной странице, окна могли всплыть раньше.
            self._close_popups(sb)

        if not already_on_motion:
            # Navigate to Motion page — retry on network errors
            for nav_attempt in range(3):
                try:
                    # Переход + немедленное закрытие попапов.
                    self._goto(sb, self.site_url)
                    # Check for network errors
                    page_text = self._js(sb, 
                        "return document.body ? document.body.innerText.substring(0, 300) : ''") or ""
                    if any(err in page_text for err in [
                        "can't be reached", "ERR_", "unexpectedly closed",
                        "INTERNET_DISCONNECTED", "CONNECTION_CLOSED",
                        "Press space to play"
                    ]):
                        print(f"  ⚠ Network error (attempt {nav_attempt + 1}/3), retrying in 10s...")
                        self._wait(5)
                        continue
                    break
                except Exception as e:
                    print(f"  ⚠ Navigation error: {e}")
                    self._wait(5)
            else:
                raise RuntimeError("Could not reach higgsfield.ai after 3 attempts")

        # Close popups ("ORGANIZE. SHARE. CREATE TOGETHER" etc.)
        self._close_popups(sb)

        # Wait for file inputs (the reliable check for generation UI ready)
        # Ensure we're on Motion page
        try:
            cur_url = self._js(sb, "return window.location.href") or ""
            if "motion" not in cur_url.lower():
                sb.open(MOTION_URL)
                self._wait(2)
                self._close_popups(sb)
        except Exception:
            pass

        file_inputs = []
        for retry in range(3):
            try:
                self._js(sb, """return (function(){
                    document.dispatchEvent(new KeyboardEvent('keydown',
                        {key:'Escape',code:'Escape',bubbles:true}));
                    document.querySelectorAll('[class*="onboard"],[class*="overlay"],[class*="wizard"]').forEach(function(o) {
                        var r = o.getBoundingClientRect();
                        if (r.width > 300 && r.height > 200) o.style.display = 'none';
                    });
                    })()""")
            except Exception:
                pass
            try:
                file_inputs = sb.find_elements("input[type='file']") or []
            except Exception:
                file_inputs = []
            if file_inputs:
                print(f"  ✓ File inputs found: {len(file_inputs)}")
                break
            print(f"  ⚠ No file inputs yet (attempt {retry+1}/3), waiting...")
            self._wait(2)
            if retry == 1:
                try:
                    sb.open(MOTION_URL)
                    self._wait(3)
                    self._close_popups(sb)
                except Exception:
                    pass

        # Model picker — select "
        #  Motion Control" (free, 5 credits), NOT "Kling 3.0 Motion Control" (paid, 7 credits)
        # First check what model is currently selected
        try:
            current_model = self._js(sb, """return (function(){
                var btns = document.querySelectorAll('button');
                for (var b of btns) {
                    var txt = b.textContent.trim();
                    if (txt.includes('Kling') && txt.includes('Motion') && b.offsetWidth > 0) {
                        return txt;
                    }
                }
                return null;
            })()""") or ""
            print(f"  ℹ Current model: '{current_model}'")

            needs_change = '3.0' in current_model or not current_model
            if needs_change:
                # Open the model dropdown
                self._js(sb, """return (function(){
                    var btns = document.querySelectorAll('button');
                    for (var b of btns) {
                        var txt = b.textContent.trim();
                        if ((txt.includes('Model') || txt.includes('Kling') || txt.includes('Motion'))
                            && b.offsetWidth > 0 && b.getBoundingClientRect().left < 400) {
                            b.click(); return 'clicked';
                        }
                    }
                })()""")
                self._wait(0.8)

                # Select the FREE model (without "3.0")
                model_result = self._js(sb, """return (function(){
                    var items = document.querySelectorAll('div, li, button, a, [role="option"], [role="menuitem"]');
                    // Pass 1: exact "Kling Motion Control" without 3.0
                    for (var el of items) {
                        if (!el.offsetParent && el.offsetHeight === 0) continue;
                        var txt = el.textContent.trim();
                        // Skip if it contains "3.0" — that's the paid version
                        if (txt.includes('3.0')) continue;
                        if (txt.includes('Kling Motion Control') && txt.length < 100) {
                            el.scrollIntoView({block: 'center'});
                            el.click();
                            return 'Kling Motion Control (free)';
                        }
                    }
                    // Pass 2: by description
                    for (var el of items) {
                        if (!el.offsetParent && el.offsetHeight === 0) continue;
                        var txt = el.textContent.trim();
                        if (txt.includes('3.0')) continue;
                        if (txt.includes('Control motion with video references')) {
                            el.scrollIntoView({block: 'center'});
                            el.click();
                            return 'Kling Motion Control (by desc)';
                        }
                    }
                    // Pass 3: Motion 2.6 fallback
                    for (var el of items) {
                        if (!el.offsetParent && el.offsetHeight === 0) continue;
                        var txt = el.textContent.trim();
                        if (txt.includes('Motion 2.6') && txt.length < 100) {
                            el.scrollIntoView({block: 'center'});
                            el.click();
                            return 'Motion 2.6';
                        }
                    }
                    // Pass 4: any Kling option that is NOT 3.0
                    for (var el of items) {
                        if (!el.offsetParent && el.offsetHeight === 0) continue;
                        var txt = el.textContent.trim();
                        if (txt.includes('3.0')) continue;
                        if (txt.includes('Kling') && txt.includes('Motion') && txt.length < 100) {
                            el.scrollIntoView({block: 'center'});
                            el.click();
                            return 'Kling (non-3.0): ' + txt.substring(0, 60);
                        }
                    }
                    return null;
                })()""")
                if model_result:
                    print(f"  ✓ Selected model: {model_result}")
                else:
                    print("  ⚠ Could not select Kling Motion Control from dropdown")
            else:
                print(f"  ✓ Model already correct: {current_model}")
        except Exception as e:
            print(f"  ⚠ Model picker error: {e}")

        self._wait(1)

        # Resolution: click Quality / 720p dropdown and switch to 1080p
        try:
            res_result = self._js(sb, """return (function(){
                // 1. Try finding direct 1080p button/option first
                var els = document.querySelectorAll('button, div, span, li, [role="option"]');
                for (var el of els) {
                    var t = (el.textContent || '').trim();
                    if ((t === '1080p' || t === '1080') && el.offsetWidth > 0) {
                        el.click(); return 'direct-1080';
                    }
                }
                // 2. Click Quality / 720p dropdown trigger
                var opened = false;
                for (var el of els) {
                    var t = (el.textContent || '').trim();
                    if ((t === 'Quality' || t === '720p' || t === '720') && el.offsetWidth > 0) {
                        el.click(); opened = true; break;
                    }
                }
                if (!opened) return 'trigger-not-found';
                return 'dropdown-opened';
            })()""")
            if res_result == 'dropdown-opened':
                self._wait(0.6)
                res_result = self._js(sb, """return (function(){
                    var els = document.querySelectorAll('button, div, span, li, [role="option"], [role="menuitem"]');
                    for (var el of els) {
                        var t = (el.textContent || '').trim();
                        if (t === '1080p' || t === '1080' || t.includes('1080')) {
                            el.click(); return 'clicked-1080';
                        }
                    }
                    return '1080-not-found';
                })()""")
            print(f"  ✓ Resolution: {res_result}")
        except Exception as e:
            print(f"  ⚠ Resolution picker error: {e}")

        # Background type: switch to Video (not Image) using the toggle
        # The toggle is inside the sidebar, NOT the "Video" tab in the top navbar
        try:
            toggle_result = self._js(sb, """return (function(){
                var btns = document.querySelectorAll('button');
                for (var b of btns) {
                    var txt = b.textContent.trim();
                    if (txt === 'Video' && b.offsetParent
                        && b.getBoundingClientRect().top > 200
                        && b.getBoundingClientRect().left < 400) {
                        b.click();
                        return 'clicked Video toggle';
                    }
                }
                var allBtns = Array.from(document.querySelectorAll('button'));
                for (var i = 0; i < allBtns.length; i++) {
                    var b = allBtns[i];
                    if (b.textContent.trim() === 'Video') {
                        var next = b.nextElementSibling;
                        var prev = b.previousElementSibling;
                        if ((next && next.textContent.trim() === 'Image')
                            || (prev && prev.textContent.trim() === 'Image')) {
                            b.click();
                            return 'clicked Video in toggle pair';
                        }
                    }
                }
                return null;
            })()""")
            if toggle_result:
                print(f"  ✓ Background: {toggle_result}")
            else:
                print("  ⚠ Video/Image toggle not found")
        except Exception as e:
            print(f"  ⚠ Video toggle error: {e}")

        # Upload files — click drop-zone → accept agreement → handle file chooser
        def accept_agreement(sb_ref):
            """Click 'I agree, continue' on media upload agreement modal."""
            for _ in range(4): # quick check (up to 2 seconds)
                try:
                    r = self._js(sb_ref, """return (function(){
                        var btns = document.querySelectorAll('button');
                        var btnTexts = [];
                        for (var b of btns) {
                            var t = b.textContent.trim();
                            if (t) btnTexts.push(t);
                            var tLower = t.toLowerCase();
                            if ((tLower.includes('i agree') || tLower.includes('agree, continue')
                                 || tLower.includes('accept')) && b.getBoundingClientRect().width > 0) {
                                b.click();
                                return 'agreed';
                            }
                        }
                        return btnTexts.join('|');
                    })()""")
                    if r == 'agreed':
                        print(f"  ✓ Accepted media upload agreement")
                        self._wait(1)
                        return True
                    # If we need to debug, we can print r here, but it might be spammy
                except Exception:
                    pass
                self._wait(0.5)
            return False

        def dismiss_file_errors(sb_ref):
            """Close 'Maximum file count' and other error toasts."""
            try:
                self._js(sb_ref, """return (function(){
                    document.querySelectorAll('[class*="toast"],[class*="alert"],[class*="notification"]')
                        .forEach(function(el){
                            var btn = el.querySelector('button, [class*="close"]');
                            if (btn) btn.click();
                        });
                    })()""")
            except Exception:
                pass

        def _unhide_inputs(sb_ref):
            """Make file inputs visible and interactable using original proven CSS."""
            self._js(sb_ref, """return (function(){
                document.querySelectorAll('input[type="file"]').forEach(function(inp) {
                    inp.style.cssText = 'display:block!important;opacity:1!important;'+
                        'visibility:visible!important;position:fixed!important;'+
                        'top:0;left:0;width:200px;height:40px;z-index:999999';
                    inp.removeAttribute('hidden');
                    inp.classList.remove('sr-only');
                });
                })()""")

        # Upload files — click drop-zone → accept agreement → handle file chooser
        def do_upload(label: str, file_path: str, accept_type: str) -> bool:
            """Upload file to targeted file input with choose_file (preferred) or CDP fallback.

            Key insight: CDP setFileInputFiles sets .files on the DOM element but
            the dispatched `change` Event has an empty FileList — React reads
            event.target.files and sees nothing.  Selenium choose_file triggers a
            real native file-dialog flow that React picks up correctly.
            """
            abs_path = os.path.abspath(file_path)
            if not os.path.exists(abs_path):
                print(f"  ❌ {label}: file not found {abs_path}")
                return False

            _unhide_inputs(sb)
            self._wait(0.3)

            # Find the best target input selector
            sel = f'input[type="file"][accept*="{accept_type}"]'
            try:
                found = sb.find_elements(sel)
                if not found:
                    sel = f'input[accept*="{accept_type}"]'
                    found = sb.find_elements(sel)
                if not found:
                    # Fallback to index-based
                    idx = 0 if accept_type == 'video' else 1
                    all_inputs = sb.find_elements('input[type="file"]')
                    if all_inputs and idx < len(all_inputs):
                        sel = f'input[type="file"]:nth-of-type({idx + 1})'
                    else:
                        sel = 'input[type="file"]'
            except Exception:
                sel = 'input[type="file"]'

            print(f"  📎 {label}: uploading to selector '{sel}'...")

            success = False

            # Method 1: Selenium choose_file (most reliable for React apps)
            try:
                sb.choose_file(sel, abs_path)
                success = True
                print(f"  ✓ {label}: choose_file succeeded")
            except Exception as e_cf:
                print(f"  ⚠ {label}: choose_file failed ({e_cf}), trying CDP...")

            # Method 2: CDP setFileInputFiles + proper React event dispatch
            if not success:
                try:
                    driver = sb.driver
                    doc = driver.execute_cdp_cmd('DOM.getDocument', {'depth': -1})
                    root_node_id = doc['root']['nodeId']
                    query_result = driver.execute_cdp_cmd('DOM.querySelectorAll', {
                        'nodeId': root_node_id,
                        'selector': sel
                    })
                    node_ids = query_result.get('nodeIds', [])
                    if node_ids:
                        driver.execute_cdp_cmd('DOM.setFileInputFiles', {
                            'files': [abs_path],
                            'nodeId': node_ids[0]
                        })
                        success = True
                        print(f"  ✓ {label}: setFileInputFiles via CDP")
                except Exception as e_cdp:
                    print(f"  ❌ {label}: CDP also failed: {e_cdp}")
                    return False

            self._wait(0.5)

            # Dispatch change & input events for React
            # For CDP uploads, we must also re-get the element and trigger
            # events so React's synthetic event system picks it up
            self._js(sb, f"""(function(){{
                var inps = document.querySelectorAll('{sel}');
                for (var inp of inps) {{
                    // Trigger native React onChange handler
                    var nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value');
                    var ev = new Event('change', {{bubbles: true}});
                    // React 16+ uses this internal property to track events
                    var tracker = inp._valueTracker;
                    if (tracker) {{ tracker.setValue(''); }}
                    inp.dispatchEvent(ev);
                    inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                }}
            }})()""")

            self._wait(1)
            accept_agreement(sb)
            dismiss_file_errors(sb)

            return success

        # First check for and accept any agreement modal
        accept_agreement(sb)

        # --- Upload VIDEO ---
        video_ok = do_upload("video", video_path, "video")
        if video_ok:
            print(f"  📹 Video bg: {os.path.basename(video_path)}")
        else:
            print(f"  ⚠ Video upload failed: {os.path.basename(video_path)}")

        # Wait longer for React to process the video upload before triggering photo upload
        self._wait(3)
        # Close any popups that appeared after video upload before touching photo input
        self._close_popups(sb)

        # --- Upload PHOTO ---
        photo_ok = do_upload("photo", photo_path, "image")
        if photo_ok:
            print(f"  🧑 Model photo: {os.path.basename(photo_path)}")
        else:
            print(f"  ⚠ Photo upload failed: {os.path.basename(photo_path)}")

        dismiss_file_errors(sb)

        if not video_ok and not photo_ok:
            raise RuntimeError("Both video and photo uploads failed — skipping generation")

        # Проверяем, что сайт действительно принял видео.
        # Раньше при незакрытом модальном окне видео не прикреплялось, но код
        # всё равно жал Generate — сайт отвечал «Input Video Required», задача
        # уходила в таймаут ожидания результата (~timeout_gen впустую).
        if not self._verify_video_attached(sb):
            raise RuntimeError(
                "Сайт не принял видео (Input Video Required) — повторяю попытку"
            )

        # Wait for Generate button to become active (media processing done)
        print("  ⏳ Waiting for Generate button to become active...")
        dismiss_file_errors(sb)
        accept_agreement(sb)
        btn_active = False
        for wait_i in range(45):
            self._guard("ожидание активности кнопки Generate")
            dismiss_file_errors(sb)
            accept_agreement(sb)
            is_ready = self._js(sb, """return (function(){
                var btn = document.querySelector('button[type="submit"]');
                if (!btn) return false;
                if (btn.disabled || btn.hasAttribute('disabled')) return false;
                if (btn.getAttribute('aria-disabled') === 'true') return false;
                var style = window.getComputedStyle(btn);
                if (style.display === 'none' || style.visibility === 'hidden' || style.pointerEvents === 'none') return false;
                return btn.offsetHeight > 0;
            })()""", default=False)
            if is_ready:
                btn_active = True
                print("  ✓ Generate button is active and enabled!")
                break
            self._wait(2.0)

        if not btn_active:
            print("  ⚠ Generate button stayed disabled (media might still be processing)")


    def _verify_video_attached(self, sb, attempts: int = 12) -> bool:
        """Проверяет, что видео реально прикреплено к форме генерации.

        Признаки принятого видео: превью `<video>` в сайдбаре, заполненный
        `input[type=file][accept*=video]` либо карточка загруженного файла.
        Признак отказа: текст «Input Video Required» / «Video is required».

        Нужно потому, что при незакрытом модальном окне загрузка молча
        пропускалась: код жал Generate, сайт отвечал «Input Video Required»,
        и попытка впустую ждала результат до конца `timeout_gen`.
        """
        for i in range(attempts):
            self._guard("проверка прикрепления видео")
            try:
                state = self._js(sb, """return (function(){
                    var body = (document.body.innerText || '').toLowerCase();
                    var needs = body.indexOf('input video required') !== -1
                             || body.indexOf('video is required') !== -1;

                    var filled = false;
                    var inputs = document.querySelectorAll(
                        'input[type="file"][accept*="video"]');
                    for (var inp of inputs) {
                        if (inp.files && inp.files.length > 0) { filled = true; break; }
                    }

                    // Превью загруженного видео в панели настроек
                    var preview = false;
                    var vids = document.querySelectorAll('video');
                    for (var v of vids) {
                        var r = v.getBoundingClientRect();
                        if (r.width > 40 && r.height > 40) { preview = true; break; }
                    }

                    return {needs: needs, filled: filled, preview: preview};
                })()""") or {}
            except Exception:                               # noqa: BLE001
                state = {}

            if state.get("filled") or state.get("preview"):
                if not state.get("needs"):
                    print("  ✓ Видео прикреплено (проверка пройдена)")
                    return True

            if state.get("needs"):
                # Модальное окно/тост мог перекрыть форму — закрываем и пробуем ещё.
                print(f"  ⚠ Сайт просит видео (попытка проверки {i + 1}/{attempts}), "
                      f"закрываю окна…")
                self._close_popups(sb)

            self._wait(1, "проверка прикрепления видео")

        print("  ❌ Видео так и не прикрепилось")
        return False

    def _generate_and_download(self, sb, dst: str) -> bool:
        """Click Generate, wait for completion via History tab monitoring, then download video.

        Detection strategy:
        1. Snapshot initial user media URLs (*.cloudfront.net or /user_).
        2. Click Generate, dismiss paywall overlays.
        3. Switch to History tab.
        4. Poll for [data-job-status="completed"] elements OR new user media URLs (*.cloudfront.net/user_).
        5. Click on the video/card to trigger playback & link availability.
        6. Extract mp4 URL and download.
        """
        self._close_popups(sb)

        def dismiss_upgrade_overlay():
            """Close 'UPGRADE PLAN', 'SEEDANCE 2.5', '55% OFF', or paywall/promo overlays."""
            try:
                # 1. Send Escape key
                self._js(sb, """return (function(){
                    document.dispatchEvent(new KeyboardEvent('keydown',
                        {key:'Escape', code:'Escape', bubbles:true}));
                    })()""")
                # 2. Try clicking close buttons
                self._js(sb, """return (function(){
                    var btns = document.querySelectorAll('button, [role="button"], a');
                    for (var b of btns) {
                        var txt = (b.textContent || '').trim();
                        var aria = (b.getAttribute('aria-label') || '').toLowerCase();
                        if (txt === '✕' || txt === '×' || txt === 'X' || txt === 'x'
                            || aria.includes('close') || aria.includes('dismiss')) {
                            if (b.offsetWidth > 0 && b.offsetHeight > 0) {
                                b.click();
                            }
                        }
                    }
                    // 3. Destroy any modal/dialog overlays covering the screen
                    var overlays = document.querySelectorAll(
                        '[role="dialog"], [class*="modal"], [class*="Modal"], [class*="overlay"],'+
                        '[class*="Overlay"], [class*="backdrop"], [class*="Backdrop"], [class*="paywall"],'+
                        '[class*="upgrade"], [class*="promo"], [class*="popup"], [class*="Popup"]'
                    );
                    overlays.forEach(function(m){
                        var txt = m.innerText || '';
                        if (txt.includes('UPGRADE PLAN') || txt.includes('SEEDANCE')
                            || txt.includes('UNLIMITED') || txt.includes('SPECIAL 55% OFF')
                            || txt.includes('OFF offer') || txt.includes('Claim Discount')
                            || txt.includes('Discount') || txt.includes('Discount expires')) {
                            var xBtn = m.querySelector('button, [role="button"]');
                            if (xBtn) { try { xBtn.click(); } catch(e){} }
                            m.remove();
                        }
                    });
                    document.body.style.overflow = 'auto';
                })()""")
            except Exception:
                pass

        def _get_user_media_urls():
            """Return set of all user media URLs on the page via DOM, Network, and innerHTML scanning."""
            try:
                return set(self._js(sb, r"""return (function(){
                    var urls = [];
                    document.querySelectorAll('video, img, source, a').forEach(function(el){
                        var src = el.currentSrc || el.src || el.href || el.getAttribute('src') || '';
                        if (src.indexOf('/user_') !== -1 || (src.indexOf('cloudfront.net') !== -1 && src.indexOf('preset') === -1)) {
                            urls.push(src.split('?')[0]);
                        }
                    });
                    try {
                        var entries = performance.getEntriesByType('resource');
                        for (var i = entries.length - 1; i >= 0; i--) {
                            var u = entries[i].name || '';
                            if ((u.indexOf('cloudfront.net') !== -1 || u.indexOf('/user_') !== -1)
                                && u.indexOf('preset') === -1 && u.indexOf('static.higgsfield.ai') === -1) {
                                urls.push(u.split('?')[0]);
                            }
                        }
                    } catch(e) {}
                    var html = document.documentElement.innerHTML || '';
                    var idx = 0;
                    while (true) {
                        var pos = html.indexOf('cloudfront.net/user_', idx);
                        if (pos === -1) break;
                        var start = html.lastIndexOf('http', pos);
                        if (start === -1 || pos - start > 120) { idx = pos + 20; continue; }
                        var chunk = html.substring(start, pos + 200);
                        var end = chunk.search(/["'\s?<>]/);
                        if (end === -1) end = chunk.length;
                        var url = chunk.substring(0, end).split('?')[0];
                        if (url.indexOf('preset') === -1 && url.indexOf('static.higgsfield.ai') === -1) urls.push(url);
                        idx = pos + 20;
                    }
                    return urls;
                })()""") or [])
            except Exception:
                return set()

        def _get_completed_jobs_count():
            """Check elements with data-job-status='completed' or finished asset cards."""
            try:
                return self._js(sb, """return (function(){
                    var completed = document.querySelectorAll('[data-job-status="completed"]');
                    if (completed.length > 0) return completed.length;
                    
                    var html = document.documentElement.innerHTML || '';
                    if (html.includes('data-job-status="completed"')) {
                        var m = html.match(/data-job-status="completed"/g);
                        if (m) return m.length;
                    }
                    
                    var grid = document.getElementById('assets-grid') || document.getElementById('create-page-content');
                    if (!grid) return 0;
                    var count = 0;
                    grid.querySelectorAll('video, figure, [class*="card"]').forEach(function(el){
                        var ehtml = el.outerHTML || '';
                        if (ehtml.includes('/user_') || ehtml.includes('cloudfront.net')) {
                            if (!ehtml.includes('preset') && !ehtml.includes('static.higgsfield.ai')) {
                                count++;
                            }
                        }
                    });
                    return count;
                })()""") or 0
            except Exception:
                return 0

        # Make sure popups are cleared before taking initial state
        dismiss_upgrade_overlay()
        self._close_popups(sb)

        # 0. Snapshot existing media URLs before generation
        initial_urls = _get_user_media_urls()
        initial_completed_count = _get_completed_jobs_count()
        print(f"  ℹ Initial user media URLs: {len(initial_urls)}, completed cards: {initial_completed_count}")

        # 1. Click Generate
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.by import By

        # Clean popups again directly before clicking
        dismiss_upgrade_overlay()
        self._wait(0.5)

        # Wait for Generate button to be genuinely active (not disabled)
        gen_ready = False
        for wait_gen in range(60):
            self._guard("ожидание включения кнопки Generate")
            dismiss_upgrade_overlay()
            self._close_popups(sb)
            is_enabled = self._js(sb, """return (function(){
                var btn = document.querySelector('button[type="submit"]');
                if (!btn) return false;
                if (btn.disabled || btn.hasAttribute('disabled')) return false;
                if (btn.getAttribute('aria-disabled') === 'true') return false;
                return btn.offsetHeight > 0;
            })()""", default=False)
            if is_enabled:
                gen_ready = True
                break
            self._wait(1.0)

        if not gen_ready:
            print("  ⚠ Generate button is still disabled after wait, attempting click anyway...")

        # Click Generate button
        clicked_gen = False
        try:
            clicked_gen = self._js(sb, """return (function(){
                var btn = document.querySelector('button[type="submit"]');
                if (btn) {
                    btn.scrollIntoView({block: 'center'});
                    btn.click();
                    return true;
                }
                return false;
            })()""")
        except Exception:
            pass

        # ActionChains click fallback
        if not clicked_gen:
            try:
                btn = sb.driver.find_element(By.XPATH, "//button[@type='submit']")
                ActionChains(sb.driver).move_to_element(btn).pause(0.2).click().perform()
                clicked_gen = True
            except Exception:
                pass

        if clicked_gen:
            print("  ✓ Clicked Generate")
        else:
            print("  ⚠ Could not find Generate button to click")

        self._wait(2)

        # 2. Dismiss paywall / upgrade overlays
        self._wait(0.5)
        dismiss_upgrade_overlay()
        self._close_popups(sb)
        print("  ✓ Dismissed post-generate overlays")

        # 3. Switch to History tab
        self._wait(2)
        dismiss_upgrade_overlay()

        def ensure_history_tab():
            try:
                self._js(sb, """return (function(){
                    var histTrigger = document.querySelector('[id*="trigger-History"]');
                    if (histTrigger && histTrigger.getAttribute('data-state') !== 'active') {
                        histTrigger.click(); return 'id';
                    }
                    var tabs = document.querySelectorAll('[role="tab"]');
                    for (var t of tabs) {
                        if (t.textContent.trim() === 'History' && t.getAttribute('data-state') !== 'active' && t.offsetWidth > 0) {
                            t.click(); return 'tab';
                        }
                    }
                    return null;
                })()""")
            except Exception:
                pass

        ensure_history_tab()
        print("  ✓ Switched to History tab")
        self._wait(2)

        # 4. Wait for generation to complete
        print("  ⏳ Waiting for generation to complete...")
        start = time.time()
        last_log = time.time()
        generation_done = False
        new_video_url = None

        # Лимит ожидания генерации — минимум из timeout_gen и остатка бюджета
        # попытки, чтобы шаг не мог «съесть» время сверх timeout_attempt.
        gen_limit = min(float(self.timeout_gen), self._time_left(self.timeout_gen))
        while time.time() - start < gen_limit:
            self._wait(1.5)
            dismiss_upgrade_overlay()
            
            elapsed = int(time.time() - start)

            # Periodically reload page (every 90s) to force React UI to sync completed job status
            if elapsed > 60 and elapsed % 90 == 0:
                print(f"  ↻ Refreshing page to sync job status ({elapsed}s)...")
                try:
                    sb.reload()
                    self._wait(3)
                    dismiss_upgrade_overlay()
                    ensure_history_tab()
                except Exception:
                    pass

            if time.time() - last_log >= 20:
                print(f"  ⏱ Generating: {elapsed}s elapsed...")
                last_log = time.time()

            try:
                # Signal 0: Check strictly for completed job state (data-job-status="completed")
                folder_signal = self._js(sb, """return (function(){
                    var completed = document.querySelector('[data-job-status="completed"]');
                    if (completed) return 'data-job-status-completed';
                    var html = document.documentElement.innerHTML || '';
                    if (html.includes('data-job-status="completed"')) return 'innerHTML-job-completed';
                    return null;
                })()""")
                if folder_signal:
                    print(f"  ✅ Completion element detected ({folder_signal}) at {elapsed}s! Stopping wait loop...")
                    generation_done = True
                    self._wait(1)
                    break

                # Signal 1: Check for new user media URLs (DOM + innerHTML + Network)
                current_urls = _get_user_media_urls()
                new_urls = current_urls - initial_urls
                if new_urls:
                    for u in new_urls:
                        if '/user_' in u or 'cloudfront.net' in u:
                            print(f"  ✅ New result URL detected! ({elapsed}s): {u[:80]}")
                            if '.mp4' in u:
                                new_video_url = u
                            generation_done = True
                            break
                    if generation_done:
                        self._wait(2)
                        break

                # Signal 2: Check data-job-status="completed"
                current_completed_count = _get_completed_jobs_count()
                if current_completed_count > initial_completed_count:
                    print(f"  ✅ Job completed detected via data-job-status! ({initial_completed_count} → {current_completed_count}, {elapsed}s)")
                    generation_done = True
                    self._wait(2)
                    break

                # Signal 3: Check if Generate button is active again (after 45s)
                if elapsed > 45:
                    gen_state = self._js(sb, """return (function(){
                        var btn = document.querySelector('button[type="submit"]');
                        if (!btn) return null;
                        return {
                            busy: btn.getAttribute('aria-busy'),
                            disabled: btn.disabled
                        };
                    })()""")
                    if gen_state and gen_state.get('busy') == 'false' and not gen_state.get('disabled'):
                        # Re-check media URLs
                        self._wait(2)
                        current_urls2 = _get_user_media_urls()
                        new_urls2 = current_urls2 - initial_urls
                        vid_urls = [u for u in new_urls2 if '.mp4' in u]
                        if vid_urls:
                            new_video_url = vid_urls[0]
                            print(f"  ✅ Generation done (button ready + new URL, {elapsed}s)")
                            generation_done = True
                            self._wait(2)
                            break
                        elif current_completed_count > 0:
                            print(f"  ✅ Generation done (button ready + completed card, {elapsed}s)")
                            generation_done = True
                            self._wait(2)
                            break
            except Exception as e:
                if elapsed > 120 and elapsed % 60 < 10:
                    print(f"  ⚠ Poll error: {e}")

        if not generation_done:
            print(f"  ⚠ Generation timeout ({self.timeout_gen}s)")

        # 5. Click on the completed result element directly on current page
        print("  🖱 Clicking completed result element on current page...")
        self._wait(1)

        try:
            clicked_result = self._js(sb, """return (function(){
                var completedCard = document.querySelector('[data-job-status="completed"], [data-asset-id]');
                if (completedCard) {
                    var btn = completedCard.querySelector('button') || completedCard.querySelector('figure') || completedCard;
                    btn.click();
                    return 'clicked-card-button';
                }
                var folderImg = document.querySelector('img[alt="Folder icon"]');
                if (folderImg) {
                    var item = folderImg.closest('[class*="card"], [class*="item"], figure') || folderImg;
                    item.click();
                    return 'clicked-folder-item';
                }
                var gridVid = document.querySelector('#assets-grid video, main video, video');
                if (gridVid) {
                    gridVid.click();
                    return 'clicked-video';
                }
                return false;
            })()""")
            if clicked_result:
                print(f"  ✓ Clicked result element ({clicked_result})")
        except Exception as e:
            print(f"  ⚠ Click result element error: {e}")

        self._wait(1.5)

        # 6. Extract mp4 URL and download
        print("  ⬇ Parsing mp4 URL from site...")

        EXTRACT_ALL_MP4_JS = r"""return (function(){
            var results = [];
            function isCF(s){ return s.indexOf('cloudfront.net') !== -1 && s.indexOf('/user_') !== -1; }
            function isJunk(s){ return s.indexOf('preset') !== -1 || s.indexOf('static.higgsfield.ai') !== -1; }
            function toMp4(s){
                var u = s.split('?')[0];
                if (u.match(/\.(jpg|jpeg|png|webp)$/i)) u = u.replace(/\.(jpg|jpeg|png|webp)$/i, '.mp4');
                return u;
            }
            
            // 0. Completed cards thumbnails & elements (FIRST PRIORITY)
            var cards = document.querySelectorAll('[data-job-status="completed"], [data-asset-id], [data-cinematic-cell-id]');
            for (var card of cards) {
                var img = card.querySelector('img');
                if (img) {
                    var raw = decodeURIComponent(img.src || img.getAttribute('src') || img.srcset || '');
                    if (isCF(raw) && !isJunk(raw)) results.push(toMp4(raw));
                }
            }
            
            // 1. Video elements
            var vids = document.querySelectorAll('video');
            for (var v of vids) {
                var src = v.currentSrc || v.src || v.getAttribute('src') || '';
                if (!src) { var s = v.querySelector('source'); if (s) src = s.src; }
                if (src && src.indexOf('.mp4') !== -1 && src.indexOf('http') === 0 && !isJunk(src) && src.indexOf('v2-fnf-web-kmc') === -1) {
                    results.push(src.split('?')[0]);
                }
            }
            
            // 2. Performance network entries
            try {
                var entries = performance.getEntriesByType('resource');
                for (var i = entries.length - 1; i >= 0; i--) {
                    var n = entries[i].name || '';
                    if (n.indexOf('.mp4') !== -1 && (isCF(n) || n.indexOf('/user_') !== -1) && !isJunk(n)) {
                        results.push(n.split('?')[0]);
                    }
                }
            } catch(e) {}
            
            // 3. Scan full innerHTML for cloudfront user URLs
            var html = document.documentElement.innerHTML || '';
            var idx = 0;
            while (true) {
                var pos = html.indexOf('cloudfront.net/user_', idx);
                if (pos === -1) break;
                var start = html.lastIndexOf('http', pos);
                if (start === -1 || pos - start > 120) { idx = pos + 20; continue; }
                var chunk = html.substring(start, pos + 200);
                var end = chunk.search(/["'\s?<>]/);
                if (end === -1) end = chunk.length;
                var url = chunk.substring(0, end);
                if (!isJunk(url)) results.push(toMp4(decodeURIComponent(url)));
                idx = pos + 20;
            }
            
            var unique = [];
            for (var r of results) {
                if (r && unique.indexOf(r) === -1) unique.push(r);
            }
            return unique;
        })()"""

        tried: set[str] = set()

        for dl_attempt in range(30):
            self._guard("скачивание результата")
            try:
                candidate_urls = self._js(sb, EXTRACT_ALL_MP4_JS) or []

                # URL, замеченный детектором завершения, — самый надёжный
                # кандидат. Раньше он вычислялся, но нигде не использовался,
                # и скачивание опиралось только на повторный разбор DOM.
                if new_video_url:
                    candidate_urls = [new_video_url] + [
                        u for u in candidate_urls if u != new_video_url
                    ]

                # Отбрасываем исходники (загруженные видео/фото) и уже
                # опробованные адреса.
                fresh = [u for u in candidate_urls
                         if u.split("?")[0] not in initial_urls and u not in tried]

                targets: list[str] = list(fresh)
                if not targets and dl_attempt > 5:
                    targets = [u for u in candidate_urls if u not in tried]

                for target_url in targets[:3]:
                    tried.add(target_url)
                    print(f"  ⬇ Пробую URL (попытка {dl_attempt + 1}): "
                          f"{target_url[:120]}")
                    if self._download_video(target_url, dst):
                        return True
            except Exception as e:                          # noqa: BLE001
                if dl_attempt % 5 == 0:
                    print(f"  ⚠ Попытка {dl_attempt}: {e}")
            self._wait(2, "скачивание результата")

        print("  ❌ Не удалось найти или скачать видео")
        return False

    def _download_video(self, url: str, dst: str) -> bool:
        """Скачивает mp4 с заголовками браузера.

        `urllib.request.urlretrieve` уходит с User-Agent «Python-urllib», и
        CloudFront такие запросы отклоняет — именно поэтому найденный результат
        не сохранялся. Здесь запрос выглядит как браузерный, читается потоком
        и проверяется размер файла.
        """
        request = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/131.0.0.0 Safari/537.36"),
            "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8",
            "Referer": HOME_URL,
        })
        tmp_path = dst + ".part"
        try:
            timeout = max(30.0, min(180.0, self._time_left(180.0)))
            with urllib.request.urlopen(request, timeout=timeout) as resp, \
                    open(tmp_path, "wb") as fh:
                while True:
                    self._guard("скачивание результата")
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    fh.write(chunk)
                    self._beat()

            size = os.path.getsize(tmp_path)
            if size <= 10240:                       # меньше 10 КБ — не видео
                print(f"  ⚠ Файл слишком мал ({size} Б), пробую другой URL")
                os.remove(tmp_path)
                return False

            os.replace(tmp_path, dst)
            print(f"  ✅ Видео сохранено: {os.path.basename(dst)} "
                  f"({size / 1048576:.1f} МБ)")
            return True

        except Cancelled:
            raise
        except Exception as exc:                            # noqa: BLE001
            print(f"  ⚠ Ошибка скачивания: {exc}")
            return False
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    # ---------------------------------------------------------- NSFW detection

    def _check_nsfw_error(self, sb) -> None:
        """Ищет отказ по контент-политике только в уведомлениях.

        Раньше проверялся весь `document.body.innerText` по подстрокам вроде
        `violat`, `flagged`, `rejected`, `content policy` — обычный футер с
        Terms/Policy давал ложное срабатывание, задача бесконечно
        «ротировала» хуки и сжигала бюджет повторов. Теперь:
          * смотрим только контейнеры уведомлений/диалогов;
          * ищем точные фразы, а не общие слова;
          * возвращаем полный текст уведомления для лога.
        """
        try:
            hit = self._js(sb, """return (function(){
                var PHRASES = [
                    'nsfw',
                    'not safe for work',
                    'content policy violation',
                    'violates our content policy',
                    'violates content policy',
                    'flagged as inappropriate',
                    'inappropriate content',
                    'prohibited content',
                    'content moderation',
                    'blocked by moderation',
                    'violates community guidelines',
                    'request was rejected'
                ];
                var nodes = document.querySelectorAll(
                    '[role="alert"], [role="status"], [role="alertdialog"],' +
                    '[class*="toast"], [class*="Toast"],' +
                    '[class*="snackbar"], [class*="Snackbar"],' +
                    '[role="dialog"], [class*="notification"]'
                );
                for (var n of nodes) {
                    if (!n.offsetParent && n.offsetHeight === 0) continue;
                    var txt = (n.innerText || '').trim();
                    if (!txt || txt.length > 600) continue;
                    var low = txt.toLowerCase();
                    for (var p of PHRASES) {
                        if (low.indexOf(p) !== -1) {
                            return {phrase: p, text: txt.substring(0, 300)};
                        }
                    }
                }
                return null;
            })()""")
            if hit:
                phrase = hit.get("phrase") if isinstance(hit, dict) else str(hit)
                text = hit.get("text", "") if isinstance(hit, dict) else ""
                raise NSFWError(f"[{phrase}] {text}")
        except NSFWError:
            raise
        except Exception:
            pass

    # ---------------------------------------------------------- warm-up

    def _warmup_driver(self, sb_factory, cancel: threading.Event) -> None:
        """Однократно поднимает браузер до старта пула воркеров.

        SeleniumBase при первом запуске может скачивать/патчить драйвер,
        используя общие lock-файлы. Если несколько воркеров стартуют
        одновременно, они конкурируют за эти локи, и часть потоков зависает.
        Один прогрев снимает гонку.
        """
        if cancel.is_set():
            return
        profile = tempfile.mkdtemp(prefix="cf_warmup_")
        try:
            _tprint("🔧 Прогрев драйвера (однократно, чтобы воркеры не "
                    "конкурировали за загрузку chromedriver)…")
            with sb_factory(profile, free_port(), True):
                pass
            _tprint("  ✓ Драйвер готов")
        except Exception as exc:                            # noqa: BLE001
            _tprint(f"  ⚠ Прогрев не удался ({exc}); продолжаю без него")
        finally:
            shutil.rmtree(profile, ignore_errors=True)

    # ---------------------------------------------------------- main loop

    def run(self, count: int = 10, workers: int = 1,
            generations_per_model: int = 1,
            cancel_event: Optional[threading.Event] = None,
            progress_callback: Optional[Callable] = None) -> int:
        """Многопоточная генерация хуков со строгим итогом N × M.

        Что изменилось относительно прежней версии:
          * жёсткий бюджет на попытку (`timeout_attempt`) + hard-kill драйвера
            из сторожевого потока, поэтому залипший Selenium не может держать
            воркер и весь job вечно;
          * `join(timeout)` вместо бесконечного `join()` — `run()` гарантированно
            возвращает управление;
          * квоты по моделям и замещающие (backfill) задачи: итог строго
            N × M, а не «сколько получилось»;
          * NSFW не расходует бюджет технических повторов — просто берётся
            другой хук;
          * порт DevTools и каталог профиля выделяются без коллизий.

        Returns:
            0 — собрано ровно столько, сколько запрошено; 1 — иначе.
        """
        from seleniumbase import SB

        cancel = cancel_event or threading.Event()

        if not self.videos:
            _tprint("Ошибка: в папке видео-хуков нет файлов.")
            return 1
        if not self.models:
            _tprint("Ошибка: в папке моделей нет фотографий.")
            return 1
        os.makedirs(self.output_dir, exist_ok=True)

        gen_per_model = max(1, int(generations_per_model))

        # ---- Целевое количество: строго N × M ---------------------------
        # M — число фотографий, N — генераций на фотографию.
        target_total = len(self.models) * gen_per_model
        if count and count > 0:
            # Совместимость с CLI: --count задаёт итог явно.
            target_total = int(count)

        # Квота на каждую модель: раздаём target_total по кругу, чтобы
        # распределение оставалось ровным даже при count != N × M.
        model_quota: dict[str, int] = {m: 0 for m in self.models}
        for i in range(target_total):
            model_quota[self.models[i % len(self.models)]] += 1

        # ---- Резервация индексов выходных файлов ------------------------
        existing_indices = [
            int(m.group(1))
            for fname in os.listdir(self.output_dir)
            if (m := re.match(r"^hook_(\d+)\.mp4$", fname))
        ]
        next_index = [max(existing_indices) if existing_indices else 0]
        index_lock = threading.Lock()

        def reserve_output() -> tuple[int, str]:
            """Атомарно выдаёт номер и путь файла.

            Без этого две параллельные (в т.ч. замещающие) задачи могли
            получить одно имя и перезаписать друг друга.
            """
            with index_lock:
                next_index[0] += 1
                idx = next_index[0]
            return idx, os.path.join(self.output_dir, f"hook_{idx:03d}.mp4")

        # Порядок хуков фиксируем один раз: ротация идёт по кругу и всегда
        # даёт файл, отличный от предыдущего.
        hook_pool = list(self.videos)
        random.shuffle(hook_pool)

        task_queue: queue.Queue[GenerationTask] = queue.Queue()
        all_tasks: list[GenerationTask] = []
        tasks_lock = threading.Lock()

        def make_task(model_path: str, backfill: bool = False) -> GenerationTask:
            idx, out_path = reserve_output()
            task = GenerationTask(
                task_id=idx,
                model_photo=model_path,
                hook_video=hook_pool[idx % len(hook_pool)],
                output_path=out_path,
                max_retries=self.max_retries,
                max_nsfw_rotations=self.nsfw_rotations,
                is_backfill=backfill,
            )
            with tasks_lock:
                all_tasks.append(task)
            return task

        for model_path, quota in model_quota.items():
            for _ in range(quota):
                task_queue.put(make_task(model_path))

        if target_total <= 0:
            _tprint("Ошибка: целевое количество равно нулю.")
            return 1

        # ---- Учёт результатов ------------------------------------------
        stats_lock = threading.Lock()
        produced: dict[str, int] = {m: 0 for m in self.models}   # успехи по модели
        done_count = [0]
        fail_count = [0]
        created_files: list[str] = []
        # Предохранитель: сколько всего попыток допустимо, чтобы добор не
        # превратился в бесконечный цикл при систематическом сбое.
        attempts_budget = [target_total * (1 + self.max_retries) + target_total]
        last_activity = [time.monotonic()]

        effective_workers = max(1, min(int(workers), target_total))

        _tprint(f"📋 Очередь: {target_total} задач ({len(self.models)} фото × "
                f"{gen_per_model}), воркеров: {effective_workers}")
        _tprint(f"   Таймаут генерации: {self.timeout_gen}с, таймаут попытки: "
                f"{self.timeout_attempt}с, повторов: {self.max_retries}, "
                f"ротаций при NSFW: {self.nsfw_rotations}")

        def report(message: str) -> None:
            _tprint(message)
            if progress_callback:
                with stats_lock:
                    done, failed = done_count[0], fail_count[0]
                try:
                    progress_callback(done, target_total, message)
                except Exception:                               # noqa: BLE001
                    pass

        report(f"🚀 Старт: {target_total} задач")

        # ---- Фабрика браузера ------------------------------------------
        sb_init_lock = threading.Lock()
        
        @contextlib.contextmanager
        def sb_factory(profile_dir: str, port: int, headless: bool):
            # Check cancel BEFORE waiting for the lock — if user pressed Stop
            # or Reset while we're queued, bail out immediately.
            if cancel.is_set():
                raise Cancelled("отмена перед запуском браузера")
            # Use timeout so we don't block forever if another worker hangs
            # during browser init.
            if not sb_init_lock.acquire(timeout=60):
                raise RuntimeError("Таймаут ожидания инициализации браузера")
            try:
                if cancel.is_set():
                    raise Cancelled("отмена во время ожидания блокировки")
                ctx = SB(uc=True, headless2=headless, locale="en",
                          disable_csp=True, user_data_dir=profile_dir,
                          chromium_arg=f"--remote-debugging-port={port}")
                sb = ctx.__enter__()
            finally:
                sb_init_lock.release()
            try:
                yield sb
            finally:
                ctx.__exit__(None, None, None)

        self._warmup_driver(sb_factory, cancel)

        # Активные драйверы по воркерам — для аварийного добивания.
        active: dict[int, object] = {}
        active_lock = threading.Lock()

        def kill_all_active(reason: str) -> None:
            with active_lock:
                items = list(active.items())
            if items:
                _tprint(f"  ⛔ Принудительное закрытие браузеров ({reason}): "
                        f"{len(items)} шт.")
            for _wid, sb_obj in items:
                hard_kill_driver(sb_obj)

        # ---- Одна попытка задачи ---------------------------------------
        def _run_attempt(task: GenerationTask, worker_id: int) -> None:
            """Выполняет одну попытку. Бросает исключение при неудаче."""
            profile_dir = tempfile.mkdtemp(prefix=f"cf_w{worker_id}_t{task.task_id}_")
            deadline = Deadline(self.timeout_attempt, label=f"T{task.task_id}")
            self._bind_ctx(deadline, cancel)
            # Каждый SB-инстанс может использовать другой режим JS (WebDriver
            # vs CDP), поэтому определение нужно начинать заново.
            self._js_expr_mode = None

            holder: dict[str, object] = {}
            stop_monitor = threading.Event()

            def monitor() -> None:
                """Сторож попытки: добивает браузер по таймауту или отмене.

                Это единственный надёжный способ разморозить рабочий поток,
                застрявший внутри блокирующего вызова Selenium.
                """
                while not stop_monitor.wait(0.5):
                    if cancel.is_set() or deadline.expired():
                        sb_obj = holder.get("sb")
                        if sb_obj is not None:
                            why = "отмена" if cancel.is_set() else "таймаут"
                            _tprint(f"  [W{worker_id}|T{task.task_id}] "
                                    f"⛔ hard-kill браузера ({why})")
                            hard_kill_driver(sb_obj)
                        return

            watchdog = threading.Thread(target=monitor, daemon=True,
                                        name=f"AttemptWatch-{worker_id}")
            watchdog.start()

            def beat() -> None:
                with stats_lock:
                    last_activity[0] = time.monotonic()

            self.set_heartbeat(beat)
            beat()

            try:
                with sb_factory(profile_dir, free_port(), self.headless) as sb:
                    holder["sb"] = sb
                    with active_lock:
                        active[worker_id] = sb
                    try:
                        self._guard("старт")
                        _tprint(f"  [W{worker_id}|T{task.task_id}] "
                                f"📝 Регистрация аккаунта…")
                        self._register(sb)
                        self._guard("после регистрации")

                        # Verify we're actually authenticated before proceeding
                        try:
                            body_check = self._js(sb,
                                "return (document.body.innerText||'').substring(0,500)") or ""
                            if (("Continue with Email" in body_check
                                    or "Continue with Google" in body_check
                                    or "Create an account" in body_check)
                                    and "Generate" not in body_check
                                    and "Motion" not in body_check):
                                raise RuntimeError(
                                    "Регистрация не завершена — всё ещё на экране входа")
                            _tprint(f"  [W{worker_id}|T{task.task_id}] "
                                    f"✅ Авторизация подтверждена")
                        except RuntimeError:
                            raise
                        except Exception:
                            pass  # не удалось проверить — продолжаем

                        self._complete_onboarding(sb)
                        self._guard("после onboarding")

                        self._setup_generation(sb, task.hook_video, task.model_photo)
                        self._guard("после загрузки файлов")

                        self._check_nsfw_error(sb)

                        if not self._generate_and_download(sb, task.output_path):
                            raise RuntimeError("Генерация или скачивание не удались")
                        self._check_nsfw_error(sb)
                    finally:
                        with active_lock:
                            active.pop(worker_id, None)
            finally:
                stop_monitor.set()
                self.set_heartbeat(None)
                self._bind_ctx(None, None)
                shutil.rmtree(profile_dir, ignore_errors=True)

        def _execute_with_retry(task: GenerationTask, worker_id: int) -> TaskStatus:
            """Повторы, ротация хука при NSFW, мягкая обработка любых ошибок."""
            while True:
                if cancel.is_set():
                    task.status = TaskStatus.SKIPPED
                    return task.status
                if not task.budget_left():
                    task.status = TaskStatus.FAILED
                    return task.status

                tag = (f"[W{worker_id}|T{task.task_id}|попытка {task.attempt + 1}"
                       f"/{task.max_retries + 1}]")
                _tprint(f"\n{'=' * 50}\n  {tag} "
                        f"хук={os.path.basename(task.hook_video)} "
                        f"модель={os.path.basename(task.model_photo)}\n{'=' * 50}")

                try:
                    _run_attempt(task, worker_id)
                    task.status = TaskStatus.SUCCESS
                    return task.status

                except Cancelled:
                    task.status = TaskStatus.SKIPPED
                    return task.status

                except NSFWError as exc:
                    # NSFW не расходует бюджет повторов (требование п.7):
                    # берём другой хук и стартуем генерацию заново.
                    task.error = f"NSFW: {exc}"
                    if task.nsfw_rotations >= task.max_nsfw_rotations:
                        _tprint(f"  {tag} 🚫 NSFW, ротации исчерпаны "
                                f"({task.max_nsfw_rotations}) — задача провалена")
                        task.status = TaskStatus.FAILED
                        return task.status
                    task.nsfw_rotations += 1
                    new_hook = task.rotate_hook(hook_pool)
                    report(f"🚫 {tag} NSFW ({exc}) → другой хук: "
                           f"{os.path.basename(new_hook)}")

                except AttemptTimeout as exc:
                    task.attempt += 1
                    task.error = str(exc)
                    task.rotate_hook(hook_pool)
                    report(f"⏰ {tag} таймаут попытки "
                           f"({self.timeout_attempt}с) — считаю неудачной")

                except Exception as exc:                        # noqa: BLE001
                    task.attempt += 1
                    task.error = str(exc)
                    task.rotate_hook(hook_pool)
                    report(f"❌ {tag} ошибка: {exc}")
                    if os.environ.get("CLIPFORGE_VERBOSE"):
                        traceback.print_exc()

        # ---- Воркер ----------------------------------------------------
        def worker(wid: int) -> None:
            while not cancel.is_set():
                try:
                    task = task_queue.get(timeout=1.0)
                except queue.Empty:
                    break

                # If cancelled while waiting in queue, drain and exit.
                if cancel.is_set():
                    task.status = TaskStatus.SKIPPED
                    task_queue.task_done()
                    break

                try:
                    with stats_lock:
                        if attempts_budget[0] <= 0:
                            _tprint(f"  [W{wid}] предохранитель попыток "
                                    f"исчерпан — задачи больше не берём")
                            task.status = TaskStatus.SKIPPED
                            continue
                        attempts_budget[0] -= 1

                    status = _execute_with_retry(task, wid)

                    with stats_lock:
                        last_activity[0] = time.monotonic()
                        if status == TaskStatus.SUCCESS:
                            done_count[0] += 1
                            produced[task.model_photo] = produced.get(
                                task.model_photo, 0) + 1
                            created_files.append(task.output_path)
                        elif status == TaskStatus.FAILED:
                            fail_count[0] += 1
                        done, failed = done_count[0], fail_count[0]
                        need_backfill = (
                            status == TaskStatus.FAILED
                            and not cancel.is_set()
                            and produced.get(task.model_photo, 0)
                                < model_quota.get(task.model_photo, 0)
                            and attempts_budget[0] > 0
                        )

                    icon = {"SUCCESS": "✅", "FAILED": "❌",
                            "SKIPPED": "⏭"}.get(status.name, "•")
                    report(f"{icon} [{done}/{target_total}] задача {task.task_id} "
                           f"→ {status.name} (успех={done}, провал={failed})")

                    if need_backfill:
                        extra = make_task(task.model_photo, backfill=True)
                        task_queue.put(extra)
                        report(f"♻ Добор до N×M: новая задача {extra.task_id} "
                               f"для {os.path.basename(task.model_photo)}")

                except Exception as exc:                        # noqa: BLE001
                    _tprint(f"  [W{wid}] ⚠ необработанная ошибка воркера: {exc}")
                    traceback.print_exc()
                finally:
                    task_queue.task_done()

            # Drain remaining tasks on cancel so other workers don't pick them.
            while not task_queue.empty():
                try:
                    leftover = task_queue.get_nowait()
                    leftover.status = TaskStatus.SKIPPED
                    task_queue.task_done()
                except queue.Empty:
                    break

        # ---- Запуск пула ------------------------------------------------
        # Register kill_all_active with the job manager so that reset()
        # can force-close browsers even when the run() function hasn't
        # returned yet.  This is the fix for "reset doesn't stop browsers".
        try:
            from .jobs import MANAGER
        except (ImportError, ValueError):
            try:
                from clipforge.jobs import MANAGER
            except (ImportError, ValueError):
                try:
                    from jobs import MANAGER
                except (ImportError, ValueError):
                    MANAGER = None

        if MANAGER is not None:
            current_job = MANAGER.current()
            if current_job is not None:
                MANAGER.register_killer(current_job, lambda: kill_all_active("reset"))

        threads: list[threading.Thread] = []
        for wid in range(1, effective_workers + 1):
            t = threading.Thread(target=worker, args=(wid,), daemon=True,
                                 name=f"HookWorker-{wid}")
            threads.append(t)
            t.start()
            if wid < effective_workers:
                # Разносим старты, чтобы не столкнуться на выдаче временных
                # почтовых адресов. Пауза прерываемая: «Стоп» работает сразу.
                try:
                    interruptible_wait(3, cancel)
                except Cancelled:
                    break

        # ---- Ожидание с ограничением (лечит вечный join) ----------------
        # Прежний `for t in threads: t.join()` без таймаута был главной
        # причиной зависания: один залипший воркер держал job навсегда.
        grace = self.timeout_attempt + 120
        forced = False
        killed_on_cancel = False
        while True:
            alive = [t for t in threads if t.is_alive()]
            if not alive:
                break
            idle = time.monotonic() - last_activity[0]
            if idle > grace:
                _tprint(f"  ⛔ Нет активности {int(idle)}с (> {int(grace)}с) — "
                        f"добиваю зависшие воркеры")
                kill_all_active("нет активности")
                forced = True
                for t in alive:
                    t.join(timeout=30)
                break
            if cancel.is_set() and not killed_on_cancel:
                # Добиваем один раз: сторож попытки сам закроет то, что
                # откроется позже.
                kill_all_active("отмена")
                killed_on_cancel = True
            alive[0].join(timeout=1.0)

        if forced:
            still = [t.name for t in threads if t.is_alive()]
            if still:
                _tprint(f"  ⚠ Воркеры не завершились: {', '.join(still)}. "
                        f"Они daemon-потоки и не мешают новой задаче.")

        # ---- Итог и проверка строгого равенства N × M -------------------
        success = sum(1 for t in all_tasks if t.status == TaskStatus.SUCCESS)
        failed = sum(1 for t in all_tasks if t.status == TaskStatus.FAILED)
        skipped = sum(1 for t in all_tasks if t.status == TaskStatus.SKIPPED)
        on_disk = len([f for f in created_files if os.path.isfile(f)])

        _tprint(f"\n{'=' * 50}")
        _tprint(f"  ИТОГ: {success} из {target_total} (файлов на диске: {on_disk})")
        _tprint(f"  Провалов: {failed}, пропущено: {skipped}")
        _tprint(f"  Папка: {self.output_dir}")
        for model_path, quota in model_quota.items():
            got = produced.get(model_path, 0)
            mark = "✓" if got == quota else "✗"
            _tprint(f"   {mark} {os.path.basename(model_path)}: {got}/{quota}")
        _tprint(f"{'=' * 50}")

        if cancel.is_set():
            report(f"⏹ Отменено пользователем: готово {success} из {target_total}")
            return 1
        if on_disk == target_total:
            report(f"✅ Готово ровно {target_total} видео (N × M соблюдено)")
            return 0
        report(f"⚠ Собрано {on_disk} из {target_total}: бюджет попыток или "
               f"ротаций исчерпан. Подробности выше в логе.")
        return 1



def main(argv: list[str] | None = None) -> int:
    from seleniumbase import SB  # noqa: F401 — early import check

    parser = argparse.ArgumentParser(
        description="AI hook generator: Higgsfield + email providers")
    parser.add_argument("--site-url", default=MOTION_URL)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--workers", "--threads", type=int, default=1, help="Number of parallel Chrome browser threads")
    parser.add_argument("--generations-per-model", "--gen-per-model", type=int, default=1, help="Generations per model per video hook")
    parser.add_argument("--videos-dir", default="./hook_refs")
    parser.add_argument("--models-dir", default="./models")
    parser.add_argument("--model-photo", default="", help="Specific model photo filename to use")
    parser.add_argument("--output-dir", default="./raw_batch/1")
    parser.add_argument("--anymessage-key",
                        default=os.environ.get("ANYMESSAGE_KEY", "4daS8LEc7P3n0CEx2tuR5BuNiqEdOt4H"))
    parser.add_argument("--guerrillamail-sid",
                        default=os.environ.get("GUERRILLAMAIL_SID", ""))
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--timeout-gen", type=int, default=300,
                        help="Таймаут ожидания самой генерации, сек")
    parser.add_argument("--timeout-attempt", type=int, default=900,
                        help="Полный таймаут одной попытки (регистрация + "
                             "загрузка + генерация + скачивание), сек")
    parser.add_argument("--max-retries", type=int, default=2,
                        help="Повторов на неудачную генерацию (0-3)")
    parser.add_argument("--nsfw-rotations", type=int, default=3,
                        help="Сколько раз менять хук при отказе по NSFW "
                             "(не расходует --max-retries)")
    args = parser.parse_args(argv)

    if not args.anymessage_key and not args.guerrillamail_sid:
        print("Error: set --anymessage-key or --guerrillamail-sid",
              file=sys.stderr)
        return 2

    gen = HookGenerator(
        videos_dir=args.videos_dir,
        models_dir=args.models_dir,
        model_photo=getattr(args, 'model_photo', ''),
        output_dir=args.output_dir,
        anymessage_key=args.anymessage_key,
        guerrillamail_sid=args.guerrillamail_sid,
        site_url=args.site_url,
        headless=not args.headed,
        timeout_gen=args.timeout_gen,
        timeout_attempt=args.timeout_attempt,
        max_retries=min(3, max(0, args.max_retries)),
        nsfw_rotations=max(0, args.nsfw_rotations),
    )
    return gen.run(count=args.count, workers=args.workers, generations_per_model=args.generations_per_model)


if __name__ == "__main__":
    raise SystemExit(main())

