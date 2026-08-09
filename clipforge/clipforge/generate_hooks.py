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
import json
import os
import random
import re
import shutil
import string
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
from selenium.webdriver.common.action_chains import ActionChains
import urllib.request
import uuid
from typing import Optional

MOTION_URL = "https://higgsfield.ai/ai/video/motion?rp=%2Fai%2Fvideo%2Fmotion"
HOME_URL = "https://higgsfield.ai/"


# ---------------------------------------------------------------- AnyMessage

class AnyMessageClient:
    """REST-client AnyMessage (anymessage.org)."""

    def __init__(self, api_key: str, base_url: str = "https://api.anymessage.shop",
                 site: str = "higgsfield.ai", domain: str = "outlook.com"):
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
        req = urllib.request.Request(url, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"AnyMessage HTTP {exc.code}: "
                f"{exc.read().decode('utf-8', 'replace')[:300]}"
            )
        return json.loads(body or "{}")

    def get_temp_email(self, domain: str = "") -> str:
        res = self._req("GET", "/email/order",
                        {"site": self.site, "domain": domain or self.domain})
        if res.get("status") != "success":
            raise RuntimeError(f"AnyMessage error: {res}")
        email = res.get("email") or ""
        self._email = email
        self._email_id = str(res.get("id") or email)
        if not email:
            raise RuntimeError(f"AnyMessage no email: {res}")
        return email

    def get_otp_code(self, timeout_sec: int = 180, poll: float = 5.0) -> Optional[str]:
        """Poll AnyMessage for verification code from Clerk/Higgsfield."""
        if not self._email_id:
            raise RuntimeError("Call get_temp_email() first")
        # Give Clerk a moment to actually send the email
        time.sleep(3)
        start = time.time()
        while time.time() - start < timeout_sec:
            try:
                res = self._req("GET", "/email/getmessage",
                                {"id": self._email_id, "preview": 0})
            except RuntimeError:
                time.sleep(poll)
                continue

            # Extract text from response
            message = res.get("message") or ""
            value = res.get("value")
            if not message and isinstance(value, dict):
                message = value.get("message") or ""
            html = res.get("html") or ""

            # Skip if no actual email content yet (just "wait message" etc.)
            if (not message and not html) or "wait" in str(res.get("value", "")).lower():
                time.sleep(poll)
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

            time.sleep(poll)
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

    def get_otp_code(self, timeout_sec: int = 180, poll: float = 5.0) -> Optional[str]:
        """Poll inbox for 6-digit OTP."""
        time.sleep(3)
        start = time.time()
        seen: set = set()
        while time.time() - start < timeout_sec:
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
            time.sleep(poll)
        return None

# ---------------------------------------------------------------- helpers

def _list_files(folder: str, exts: tuple[str, ...]) -> list[str]:
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
        headless: bool = True,
        timeout_gen: int = 420,
        guerrillamail_sid: str = "",
        model_photo: str = "",
    ):
        self.site_url = site_url
        self.videos = _list_files(videos_dir, (".mp4", ".mov", ".webm", ".m4v"))
        all_models = _list_files(models_dir, (".png", ".jpg", ".jpeg", ".webp"))
        if model_photo:
            matched = [m for m in all_models if os.path.basename(m).lower() == model_photo.lower()]
            self.models = matched if matched else all_models
        else:
            self.models = all_models
        self.output_dir = output_dir
        self.guerrillamail_sid = guerrillamail_sid
        self.anymessage = AnyMessageClient(api_key=anymessage_key) if anymessage_key else None
        self.headless = headless
        self.timeout_gen = timeout_gen
        self._file_lock = threading.Lock()

    def _make_email_client(self):
        """Return the configured email client."""
        if self.guerrillamail_sid:
            return GuerrillaMailClient(self.guerrillamail_sid)
        if self.anymessage:
            return self.anymessage
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
        time.sleep(2)

        # Click "Login" button in nav
        for attempt in range(3):
            sb.execute_script("""
                var items = document.querySelectorAll('button, a');
                for (var el of items) {
                    var txt = el.textContent.trim();
                    if ((txt === 'Login' || txt === 'Log in' || txt === 'Sign in')
                        && el.offsetParent !== null) {
                        el.click();
                        return;
                    }
                }
            """)
            time.sleep(2)
            # Check if Clerk modal appeared
            try:
                has_modal = sb.execute_script("""
                    var txt = document.body.innerText || '';
                    return txt.includes('Welcome') || txt.includes('Continue with Email')
                        || txt.includes('Continue with Google') || txt.includes('Sign in');
                """)
                if has_modal:
                    print(f"  ✓ Clerk login modal appeared (attempt {attempt + 1})")
                    break
            except Exception:
                pass
        else:
            print("  ⚠ Login modal did not appear")
            return False

        # Click "Continue with Email"
        time.sleep(0.5)
        try:
            sb.execute_script("""return (function(){
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
        time.sleep(1)

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
                sb.execute_script(f"""
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
            sb.execute_script("""return (function(){
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
        time.sleep(2)

        # Fill password (Clerk shows password field after email submit)
        pw_filled = False
        for pw_wait in range(10):
            try:
                has_pw = sb.execute_script("""
                    var pw = document.querySelector('input[type="password"]');
                    return pw && pw.offsetHeight > 0;
                """)
                if has_pw:
                    break
            except Exception:
                pass
            time.sleep(1)

        try:
            pw_elem = sb.find_element("input[type='password']")
            pw_elem.click()
            time.sleep(0.1)
            pw_elem.clear()
            pw_elem.send_keys(password)
            time.sleep(0.2)
            # Fire events
            sb.execute_script("""
                var inp = document.querySelector('input[type="password"]');
                if (inp) {
                    ['input','change','blur','keyup'].forEach(function(e){
                        inp.dispatchEvent(new Event(e, {bubbles: true}));
                    });
                }
            """)
            pw_filled = True
            print("  ✓ Filled password")
        except Exception as e:
            print(f"  ❌ Password fill failed: {e}")
            return False

        # Submit login form
        try:
            sb.execute_script("""return (function(){
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
        time.sleep(2)

        # Verify login succeeded — wait up to 15s for redirect
        for check in range(15):
            try:
                body = sb.execute_script(
                    "return (document.body.innerText||'').substring(0,500)") or ""
                # Still on login/signup modal = not logged in yet
                if "Continue with Google" in body or "Continue with Email" in body:
                    time.sleep(1)
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
            time.sleep(1)

        # Final check
        try:
            final_txt = sb.execute_script(
                "return (document.body.innerText||'').substring(0,300)") or ""
            if "Welcome to Higgsfield" in final_txt or "Continue with" in final_txt:
                print("  ❌ Login failed — still on auth screen")
                return False
            print("  ✅ Login appears successful")
            return True
        except Exception:
            return False

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

        # Navigate to Higgsfield with UC anti-detection
        sb.uc_open_with_reconnect(HOME_URL, reconnect_time=4)
        time.sleep(1)

        # Step 1: Click "Sign up" in nav bar and wait for Clerk modal
        # Retry if modal doesn't appear (Clerk loads async)
        for attempt in range(3):
            # JS click: find visible nav-area "Sign up" button/link
            sb.execute_script("""
                var items = document.querySelectorAll('button, a');
                for (var el of items) {
                    if (el.textContent.trim() === 'Sign up'
                        && el.offsetParent !== null
                        && el.getBoundingClientRect().top < 100) {
                        el.click();
                        return;
                    }
                }
                // Fallback: any exact "Sign up"
                for (var el of items) {
                    if (el.textContent.trim() === 'Sign up' && el.offsetParent !== null) {
                        el.click();
                        return;
                    }
                }
            """)
            print(f"  ✓ Clicked Sign up (attempt {attempt + 1})")

            # Wait for Clerk modal — check both main document and iframes
            modal_ok = False
            for wait_i in range(12):
                time.sleep(1)
                try:
                    # Standard text check
                    if (sb.is_text_visible("Continue with Email") or
                        sb.is_text_visible("Continue with Google") or
                        sb.is_text_visible("Welcome to Higgsfield") or
                        sb.is_text_visible("Create an account")):
                        modal_ok = True
                        break
                except Exception:
                    pass
                # JS fallback — check body text including iframes
                try:
                    found = sb.execute_script("""
                        var txt = document.body.innerText || '';
                        if (txt.includes('Continue with Email') || txt.includes('Welcome to Higgsfield'))
                            return true;
                        // Check iframes
                        var frames = document.querySelectorAll('iframe');
                        for (var f of frames) {
                            try {
                                var ftxt = f.contentDocument.body.innerText || '';
                                if (ftxt.includes('Continue with Email')) return true;
                            } catch(e) {}
                        }
                        return false;
                    """)
                    if found:
                        modal_ok = True
                        break
                except Exception:
                    pass
            if modal_ok:
                print("  ✓ Clerk modal appeared")
                break
            print("  ⚠ Modal not detected, retrying...")
        else:
            raise RuntimeError("Clerk modal did not appear after 3 attempts")

        # Step 2-3: The signup form may show email+password fields directly
        # (new UI) or require clicking "Continue with Email" first (old UI).
        # Detect which variant we have.

        # Check if email input is already visible using JS (more reliable than Selenium)
        time.sleep(0.5)
        email_input_visible = False
        try:
            email_input_visible = sb.execute_script("""
                var inputs = document.querySelectorAll('input');
                for (var i = 0; i < inputs.length; i++) {
                    var inp = inputs[i];
                    var r = inp.getBoundingClientRect();
                    if (r.width > 10 && r.height > 10 && inp.type !== 'hidden'
                        && (inp.type === 'email' || inp.placeholder.toLowerCase().includes('email')
                            || inp.name === 'emailAddress' || inp.autocomplete === 'email')) {
                        return true;
                    }
                }
                return false;
            """) or False
        except Exception:
            pass

        # Also check if "Continue with Email" button is already visible as a submit button
        # (new Higgsfield UI shows the full form with email+password directly)
        if not email_input_visible:
            try:
                has_submit = sb.execute_script("""
                    var btns = document.querySelectorAll('button');
                    for (var b of btns) {
                        var txt = b.textContent.trim();
                        if (txt.includes('Continue with Email') && b.offsetParent) {
                            // Check if there's also an email input nearby (form already open)
                            var form = b.closest('form') || document.querySelector('form');
                            if (form) {
                                var inp = form.querySelector('input[type="email"], input[placeholder*="email" i]');
                                if (inp) return 'form-with-submit';
                            }
                            return 'submit-btn-only';
                        }
                    }
                    return null;
                """)
                if has_submit == 'form-with-submit':
                    email_input_visible = True
                    print("  ✓ Email form already open (detected by submit button + input)")
            except Exception:
                pass

        if not email_input_visible:
            # Old UI: need to click "Continue with Email" to reveal fields
            email_btn_clicked = False
            try:
                result = sb.execute_script("""return (function(){
                    // Clerk uses input[type=submit] NOT button!
                    var submits = document.querySelectorAll('input[type="submit"]');
                    for (var s of submits) {
                        if (s.value && s.value.includes('Continue with Email')) {
                            s.click();
                            return 'input-submit';
                        }
                    }
                    var btns = document.querySelectorAll('button, a, [role="button"]');
                    for (var b of btns) {
                        if (b.offsetHeight < 10) continue;
                        var txt = b.textContent.trim();
                        if (txt.includes('Continue with Email') || txt.includes('Continue with email')) {
                            b.scrollIntoView({block: 'center'});
                            b.click();
                            return 'clicked';
                        }
                    }
                    for (var b of btns) {
                        if (b.offsetHeight < 10) continue;
                        var txt = b.textContent.trim();
                        if (txt === 'Email' || txt.includes('with Email')) {
                            b.scrollIntoView({block: 'center'});
                            b.click();
                            return 'clicked-fallback';
                        }
                    }
                    return null;
                })()""")
                if result:
                    email_btn_clicked = True
                    print(f"  ✓ Clicked Continue with Email ({result})")
            except Exception as e:
                print(f"  ⚠ JS email btn error: {e}")

            if not email_btn_clicked:
                for sel in [
                    "input[type='submit'][value*='Continue']",
                    '//button[contains(text(),"Continue with Email")]',
                    '//button[contains(text(),"Email")]',
                    '//*[contains(text(),"Continue with Email")]',
                ]:
                    try:
                        sb.click(sel, timeout=5)
                        email_btn_clicked = True
                        print("  ✓ Clicked Continue with Email (XPath)")
                        break
                    except Exception:
                        continue

            if not email_btn_clicked:
                # Last resort: maybe the form IS open but email_input_visible check failed
                # Try to fill email anyway and see what happens
                print("  ⚠ Could not find 'Continue with Email' button — attempting to fill form directly")
                email_btn_clicked = True  # proceed optimistically

            time.sleep(1)
        else:
            print("  ✓ Email input already visible (unified form)")

        # Step 3: Fill email address
        email_sels = [
            "input[name='emailAddress']",
            "input[type='email']",
            "input[name='email']",
            "input[placeholder*='mail' i]",
            "input[placeholder*='email' i]",
            "input[autocomplete='email']",
        ]
        filled = False
        for sel in email_sels:
            try:
                sb.wait_for_element_visible(sel, timeout=8)
                sb.type(sel, email)
                filled = True
                print(f"  ✓ Filled email via: {sel}")
                break
            except Exception:
                continue

        # JS fallback
        if not filled:
            try:
                sb.execute_script(f"""
                    var inputs = document.querySelectorAll('input');
                    for (var i = 0; i < inputs.length; i++) {{
                        var inp = inputs[i];
                        if (inp.offsetParent !== null && inp.type !== 'hidden'
                            && inp.type !== 'password' && inp.type !== 'file') {{
                            inp.focus();
                            inp.value = '{email}';
                            inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                            inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                            break;
                        }}
                    }}
                """)
                filled = True
                print("  ✓ Filled email via JS fallback")
            except Exception:
                pass

        if not filled:
            raise RuntimeError("Email input not found in Clerk form")

        # Step 3b: Fill password (Clerk requires it on Higgsfield)
        base_chars = (random.choices(string.ascii_lowercase, k=8) +
                      random.choices(string.ascii_uppercase, k=3) +
                      random.choices(string.digits, k=3))
        random.shuffle(base_chars)
        password = "Cf!" + "".join(base_chars)
        pw_filled = False
        # Wait for password field explicitly
        for pw_wait in range(8):
            try:
                has_pw = sb.execute_script("""
                    var pw = document.querySelector('input[type="password"]');
                    return pw && pw.offsetHeight > 0;
                """)
                if has_pw:
                    break
            except Exception:
                pass
            time.sleep(1)

        # Method 1: React-compatible nativeInputValueSetter
        try:
            result = sb.execute_script(f"""
                var inp = document.querySelector('input[type="password"]');
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

        # Method 1: Type via Selenium / ActionChains to fire all React handlers
        try:
            pw_elem = sb.find_element("input[type='password']")
            pw_elem.click()
            time.sleep(0.1)
            pw_elem.clear()
            pw_elem.send_keys(password)
            time.sleep(0.2)
            sb.execute_script("""
                var inp = document.querySelector('input[type="password"]');
                if (inp) {
                    ['input','change','blur','keyup'].forEach(function(e){
                        inp.dispatchEvent(new Event(e, {bubbles: true}));
                    });
                }
            """)
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
                btn = sb.execute_script("""return (function(){
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
                    time.sleep(0.2)
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
            sb.execute_script("document.querySelector('form')?.submit()")
            print("  ✓ Submitted via JS")

        time.sleep(1)

        # Step 4b: Handle CAPTCHA if it appeared after submit
        # Clerk/Higgsfield may show Turnstile/hCaptcha after form submission
        for captcha_attempt in range(3):
            try:
                has_captcha = sb.execute_script("""return (function(){
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
                                sb.execute_script("""
                                    var iframes = document.querySelectorAll('iframe');
                                    for (var f of iframes) {
                                        var src = f.src || '';
                                        if (src.includes('turnstile') || src.includes('hcaptcha') || src.includes('captcha')) {
                                            f.click();
                                            break;
                                        }
                                    }
                                """)
                            except Exception:
                                pass
                    time.sleep(1.5)
                    # Re-submit the form after solving captcha
                    try:
                        sb.execute_script("""return (function(){
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
                    time.sleep(1)
                else:
                    break  # No captcha, continue
            except Exception:
                break

        # Check for error messages from Clerk (e.g., blocked email domain)
        try:
            error_text = sb.execute_script("""
                var errs = document.querySelectorAll(
                    '.cl-formFieldErrorText, [data-localization-key*="error"], .cl-alert__text'
                );
                var texts = [];
                errs.forEach(function(e) {
                    if (e.textContent.trim()) texts.push(e.textContent.trim());
                });
                return texts.join(' | ');
            """)
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
            time.sleep(1)
        else:
            print("  ⚠ Verification screen not detected, trying OTP anyway")

        # Step 5-6: Get OTP, enter it, retry if incorrect
        max_otp_attempts = 3
        for otp_attempt in range(1, max_otp_attempts + 1):
            # Before fetching OTP, check if we already passed verification
            # (e.g. Clerk auto-verified from a previous attempt)
            try:
                body_pre = sb.execute_script("return document.body.innerText || ''") or ""
                if "How do you plan" in body_pre or "flagship studios" in body_pre:
                    print("  ✅ Already past verification (onboarding detected)!")
                    return email
            except Exception:
                pass

            # Check if OTP input still exists; if modal reset, re-open signup
            try:
                has_otp_field = sb.execute_script("""
                    return !!document.querySelector(
                        'input[name="code"], input[inputmode="numeric"], input[autocomplete="one-time-code"]'
                    );
                """)
                if not has_otp_field:
                    # Modal may have reset — check for "Welcome to Higgsfield"
                    body_check = sb.execute_script("return document.body.innerText || ''") or ""
                    if "Welcome to Higgsfield" in body_check or "Continue with Email" in body_check:
                        print("  ⚠ Clerk modal reset to login screen, aborting OTP retry")
                        raise RuntimeError("Clerk modal reset — registration session expired")
            except RuntimeError:
                raise
            except Exception:
                pass

            print(f"  ⏳ Waiting for OTP code (attempt {otp_attempt}/{max_otp_attempts})...")
            otp = email_client.get_otp_code(timeout_sec=180)
            if not otp:
                raise RuntimeError("OTP not received in 180 sec")
            print(f"  ✓ OTP: {otp}")

            # Enter OTP code using nativeInputValueSetter for React compatibility
            otp_entered = False

            # Method 1: nativeInputValueSetter (React-compatible, triggers state update)
            for sel in ["input[name='code']", "input[inputmode='numeric']",
                        "input[autocomplete='one-time-code']"]:
                try:
                    result = sb.execute_script(f"""
                        var inp = document.querySelector("{sel}");
                        if (!inp || inp.offsetHeight === 0) return 'not-found';
                        inp.focus();
                        var nativeSet = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value').set;
                        nativeSet.call(inp, '{otp}');
                        inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                        inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                        inp.dispatchEvent(new KeyboardEvent('keyup', {{bubbles: true}}));
                        return 'ok';
                    """)
                    if result == 'ok':
                        otp_entered = True
                        print(f"  ✓ Entered OTP via nativeSet: {sel}")
                        break
                except Exception:
                    continue

            # Method 2: Selenium send_keys fallback
            if not otp_entered:
                for sel in ["input[name='code']", "input[inputmode='numeric']",
                            "input[type='tel']", "input[type='number']"]:
                    try:
                        el = sb.find_element(sel)
                        el.click()
                        time.sleep(0.1)
                        el.clear()
                        for ch in otp:
                            el.send_keys(ch)
                            time.sleep(0.05)
                        otp_entered = True
                        print(f"  ✓ Entered OTP via send_keys: {sel}")
                        break
                    except Exception:
                        continue

            # Method 3: individual digit inputs
            if not otp_entered:
                try:
                    digit_inputs = sb.find_elements("input[data-input-otp='true']")
                    if not digit_inputs:
                        digit_inputs = sb.find_elements(
                            ".cl-otpCodeFieldInput, input[inputmode='numeric']"
                        )
                    if digit_inputs and len(digit_inputs) >= len(otp):
                        for idx, digit in enumerate(otp):
                            digit_inputs[idx].send_keys(digit)
                        otp_entered = True
                        print("  ✓ Entered OTP digits individually")
                except Exception:
                    pass

            if not otp_entered:
                raise RuntimeError("OTP input field not found")

            # Wait for auto-verification (Clerk auto-submits after 6 digits)
            print("  ⏳ Waiting for OTP auto-verification...")
            verified = False
            for _wait in range(12):
                time.sleep(1)
                try:
                    body_now = sb.execute_script("return document.body.innerText || ''") or ""
                    # Check if OTP input is still in DOM & visible
                    otp_still_visible = sb.execute_script("""return (function(){
                        var inp = document.querySelector('input[name="code"], input[inputmode="numeric"], input[autocomplete="one-time-code"]');
                        return inp && (inp.offsetWidth > 0 || inp.offsetHeight > 0);
                    })()""")
                    
                    if not otp_still_visible:
                        verified = True
                        print("  ✓ OTP input field disappeared — auto-verified!")
                        break
                    
                    if ("How do you plan" in body_now or "flagship studios" in body_now
                            or "Personalizing" in body_now or "For personal use" in body_now
                            or "Motion Control" in body_now or "Create Video" in body_now):
                        verified = True
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
                    time.sleep(1.5)
                    break
                except Exception:
                    continue

            # Re-check after manual button click
            try:
                otp_still_visible = sb.execute_script("""return (function(){
                    var inp = document.querySelector('input[name="code"], input[inputmode="numeric"], input[autocomplete="one-time-code"]');
                    return inp && (inp.offsetWidth > 0 || inp.offsetHeight > 0);
                })()""")
                if not otp_still_visible:
                    print("  ✅ Registration complete!")
                    return email
            except Exception:
                pass

            # Check for error messages
            try:
                has_error = sb.execute_script("""
                    var el = document.querySelector('.cl-formFieldErrorText');
                    if (!el) {
                        var all = document.querySelectorAll('[class*="error"], [class*="Error"]');
                        for (var i = 0; i < all.length; i++) {
                            if (all[i].textContent.toLowerCase().includes('incorrect')) return all[i].textContent;
                        }
                    }
                    return el ? el.textContent : null;
                """)
                if has_error and "incorrect" in str(has_error).lower():
                    print(f"  ⚠ Incorrect code! Error: {has_error}")
                    if otp_attempt < max_otp_attempts:
                        try:
                            sb.click('//button[contains(text(),"Resend")]', timeout=3)
                            print("  ↻ Clicked Resend, waiting for new code...")
                            time.sleep(3)
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
        """Skip onboarding by navigating directly to Motion page."""
        time.sleep(1)
        try:
            body = sb.execute_script("return (document.body.innerText||'').substring(0,500)") or ""
        except Exception:
            body = ""

        if any(m in body for m in ["How do you plan", "1 of", "Personalizing",
                                    "For personal use", "flagship studios"]):
            print("  ⏭ Onboarding detected — skipping via reload")
        else:
            print("  ℹ No onboarding")
            return

        # Skip by navigating to Motion URL directly
        sb.uc_open_with_reconnect(MOTION_URL, reconnect_time=4)
        time.sleep(1)
        # Dismiss any remaining overlays
        try:
            sb.execute_script("""
                document.dispatchEvent(new KeyboardEvent('keydown',
                    {key:'Escape',code:'Escape',bubbles:true}));
                document.querySelectorAll(
                    '[class*="modal"],[class*="overlay"],[class*="onboard"],[class*="wizard"]'
                ).forEach(function(o){ if(o.style) o.style.display='none'; });
            """)
        except Exception:
            pass
        self._close_popups(sb)
        print("  ✓ Onboarding skipped")
        time.sleep(0.5)

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
                result = sb.execute_script("""return (function(){
                    // --- 1. Promo/discount overlays (highest priority) ---
                    var bodyText = document.body.innerText || '';
                    if (bodyText.includes('OFF offer') || bodyText.includes('Claim Discount')
                        || bodyText.includes('special offer') || bodyText.includes('EXTRA DISCOUNT')
                        || bodyText.includes('Get Unlimited') || bodyText.includes('premium plan')) {
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
                    time.sleep(1)
            except Exception:
                pass

            if not closed:
                break
            time.sleep(0.5)

    def _setup_generation(self, sb, video_path: str, photo_path: str) -> None:
        """Navigate to Motion page and set up generation parameters."""
        # Check if we're already on the Motion page (from onboarding or cookie restore)
        already_on_motion = False
        try:
            cur_url = sb.execute_script("return window.location.href") or ""
            if "motion" in cur_url.lower():
                already_on_motion = True
                print("  ✓ Already on Motion page")
        except Exception:
            pass

        if not already_on_motion:
            # Navigate to Motion page — retry on network errors
            for nav_attempt in range(3):
                try:
                    sb.uc_open_with_reconnect(self.site_url, reconnect_time=4)
                    time.sleep(2)
                    # Check for network errors
                    page_text = sb.execute_script(
                        "return document.body ? document.body.innerText.substring(0, 300) : ''") or ""
                    if any(err in page_text for err in [
                        "can't be reached", "ERR_", "unexpectedly closed",
                        "INTERNET_DISCONNECTED", "CONNECTION_CLOSED",
                        "Press space to play"
                    ]):
                        print(f"  ⚠ Network error (attempt {nav_attempt + 1}/3), retrying in 10s...")
                        time.sleep(5)
                        continue
                    break
                except Exception as e:
                    print(f"  ⚠ Navigation error: {e}")
                    time.sleep(5)
            else:
                raise RuntimeError("Could not reach higgsfield.ai after 3 attempts")

        # Close popups ("ORGANIZE. SHARE. CREATE TOGETHER" etc.)
        self._close_popups(sb)

        # Wait for file inputs (the reliable check for generation UI ready)
        # Ensure we're on Motion page
        try:
            cur_url = sb.execute_script("return window.location.href") or ""
            if "motion" not in cur_url.lower():
                sb.open(MOTION_URL)
                time.sleep(2)
                self._close_popups(sb)
        except Exception:
            pass

        file_inputs = []
        for retry in range(3):
            try:
                sb.execute_script("""
                    document.dispatchEvent(new KeyboardEvent('keydown',
                        {key:'Escape',code:'Escape',bubbles:true}));
                    document.querySelectorAll('[class*="onboard"],[class*="overlay"],[class*="wizard"]').forEach(function(o) {
                        var r = o.getBoundingClientRect();
                        if (r.width > 300 && r.height > 200) o.style.display = 'none';
                    });
                """)
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
            time.sleep(2)
            if retry == 1:
                try:
                    sb.open(MOTION_URL)
                    time.sleep(3)
                    self._close_popups(sb)
                except Exception:
                    pass

        # Model picker — select "Kling Motion Control" (free, 5 credits), NOT "Kling 3.0 Motion Control" (paid, 7 credits)
        # First check what model is currently selected
        try:
            current_model = sb.execute_script("""return (function(){
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
                sb.execute_script("""return (function(){
                    var btns = document.querySelectorAll('button');
                    for (var b of btns) {
                        var txt = b.textContent.trim();
                        if ((txt.includes('Model') || txt.includes('Kling') || txt.includes('Motion'))
                            && b.offsetWidth > 0 && b.getBoundingClientRect().left < 400) {
                            b.click(); return 'clicked';
                        }
                    }
                })()""")
                time.sleep(0.8)

                # Select the FREE model (without "3.0")
                model_result = sb.execute_script("""return (function(){
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

        time.sleep(0.5)

        # Resolution: prefer 1080p
        try:
            sb.click('//button[contains(text(),"720")]', timeout=2)
            sb.click('//*[contains(text(),"1080")]', timeout=2)
            print("  ✓ Resolution: 1080p")
        except Exception:
            pass

        # Background type: switch to Video (not Image) using the toggle
        # The toggle is inside the sidebar, NOT the "Video" tab in the top navbar
        try:
            toggle_result = sb.execute_script("""return (function(){
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
                    r = sb_ref.execute_script("""return (function(){
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
                        time.sleep(1)
                        return True
                    # If we need to debug, we can print r here, but it might be spammy
                except Exception:
                    pass
                time.sleep(0.5)
            return False

        def dismiss_file_errors(sb_ref):
            """Close 'Maximum file count' and other error toasts."""
            try:
                sb_ref.execute_script("""
                    document.querySelectorAll('[class*="toast"],[class*="alert"],[class*="notification"]')
                        .forEach(function(el){
                            var btn = el.querySelector('button, [class*="close"]');
                            if (btn) btn.click();
                        });
                """)
            except Exception:
                pass

        def _unhide_inputs(sb_ref):
            """Make file inputs visible and interactable using original proven CSS."""
            sb_ref.execute_script("""
                document.querySelectorAll('input[type="file"]').forEach(function(inp) {
                    inp.style.cssText = 'display:block!important;opacity:1!important;'+
                        'visibility:visible!important;position:fixed!important;'+
                        'top:0;left:0;width:200px;height:40px;z-index:999999';
                    inp.removeAttribute('hidden');
                    inp.classList.remove('sr-only');
                });
            """)

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
            time.sleep(0.3)

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

            time.sleep(0.5)

            # Dispatch change & input events for React
            # For CDP uploads, we must also re-get the element and trigger
            # events so React's synthetic event system picks it up
            sb.execute_script(f"""(function(){{
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

            time.sleep(1)
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

        # --- Upload PHOTO ---
        time.sleep(1)
        photo_ok = do_upload("photo", photo_path, "image")
        if photo_ok:
            print(f"  🧑 Model photo: {os.path.basename(photo_path)}")
        else:
            print(f"  ⚠ Photo upload failed: {os.path.basename(photo_path)}")

        dismiss_file_errors(sb)

        if not video_ok and not photo_ok:
            raise RuntimeError("Both video and photo uploads failed — skipping generation")

        # Wait for Generate button to become active (media processing done)
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.common.by import By

        print("  ⏳ Waiting for Generate button to become active...")
        dismiss_file_errors(sb)
        accept_agreement(sb)
        try:
            WebDriverWait(sb.driver, 100).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit']"))
            )
            print("  ✓ Generate button is active!")
        except Exception as e:
            print(f"  ⚠ Timeout waiting for Generate button: {e}")


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
                sb.execute_script("""
                    document.dispatchEvent(new KeyboardEvent('keydown',
                        {key:'Escape', code:'Escape', bubbles:true}));
                """)
                # 2. Try clicking close buttons
                sb.execute_script("""return (function(){
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
                return set(sb.execute_script(r"""return (function(){
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
                return sb.execute_script("""return (function(){
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
        time.sleep(0.5)

        # Try JS click first to bypass any remaining overlay mouse traps
        clicked_gen = False
        try:
            clicked_gen = sb.execute_script("""return (function(){
                var btn = document.querySelector('button[type="submit"]');
                if (btn) {
                    btn.disabled = false;
                    btn.removeAttribute('disabled');
                    btn.scrollIntoView({block: 'center'});
                    btn.click();
                    return true;
                }
                return false;
            })()""")
        except Exception:
            pass

        # ActionChains click fallback
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

        time.sleep(2)

        # 2. Dismiss paywall / upgrade overlays
        time.sleep(0.5)
        dismiss_upgrade_overlay()
        self._close_popups(sb)
        print("  ✓ Dismissed post-generate overlays")

        # 3. Switch to History tab
        time.sleep(2)
        dismiss_upgrade_overlay()

        def ensure_history_tab():
            try:
                sb.execute_script("""return (function(){
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
        time.sleep(2)

        # 4. Wait for generation to complete
        print("  ⏳ Waiting for generation to complete...")
        start = time.time()
        last_log = time.time()
        generation_done = False
        new_video_url = None

        while time.time() - start < self.timeout_gen:
            time.sleep(1.5)
            dismiss_upgrade_overlay()
            
            elapsed = int(time.time() - start)

            # Periodically reload page (every 90s) to force React UI to sync completed job status
            if elapsed > 60 and elapsed % 90 == 0:
                print(f"  ↻ Refreshing page to sync job status ({elapsed}s)...")
                try:
                    sb.reload()
                    time.sleep(3)
                    dismiss_upgrade_overlay()
                    ensure_history_tab()
                except Exception:
                    pass

            if time.time() - last_log >= 20:
                print(f"  ⏱ Generating: {elapsed}s elapsed...")
                last_log = time.time()

            try:
                # Signal 0: Check strictly for completed job state (data-job-status="completed")
                folder_signal = sb.execute_script("""return (function(){
                    var completed = document.querySelector('[data-job-status="completed"]');
                    if (completed) return 'data-job-status-completed';
                    var html = document.documentElement.innerHTML || '';
                    if (html.includes('data-job-status="completed"')) return 'innerHTML-job-completed';
                    return null;
                })()""")
                if folder_signal:
                    print(f"  ✅ Completion element detected ({folder_signal}) at {elapsed}s! Stopping wait loop...")
                    generation_done = True
                    time.sleep(1)
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
                        time.sleep(2)
                        break

                # Signal 2: Check data-job-status="completed"
                current_completed_count = _get_completed_jobs_count()
                if current_completed_count > initial_completed_count:
                    print(f"  ✅ Job completed detected via data-job-status! ({initial_completed_count} → {current_completed_count}, {elapsed}s)")
                    generation_done = True
                    time.sleep(2)
                    break

                # Signal 3: Check if Generate button is active again (after 45s)
                if elapsed > 45:
                    gen_state = sb.execute_script("""return (function(){
                        var btn = document.querySelector('button[type="submit"]');
                        if (!btn) return null;
                        return {
                            busy: btn.getAttribute('aria-busy'),
                            disabled: btn.disabled
                        };
                    })()""")
                    if gen_state and gen_state.get('busy') == 'false' and not gen_state.get('disabled'):
                        # Re-check media URLs
                        time.sleep(2)
                        current_urls2 = _get_user_media_urls()
                        new_urls2 = current_urls2 - initial_urls
                        vid_urls = [u for u in new_urls2 if '.mp4' in u]
                        if vid_urls:
                            new_video_url = vid_urls[0]
                            print(f"  ✅ Generation done (button ready + new URL, {elapsed}s)")
                            generation_done = True
                            time.sleep(2)
                            break
                        elif current_completed_count > 0:
                            print(f"  ✅ Generation done (button ready + completed card, {elapsed}s)")
                            generation_done = True
                            time.sleep(2)
                            break
            except Exception as e:
                if elapsed > 120 and elapsed % 60 < 10:
                    print(f"  ⚠ Poll error: {e}")

        if not generation_done:
            print(f"  ⚠ Generation timeout ({self.timeout_gen}s)")

        # 5. Click on the completed result element directly on current page
        print("  🖱 Clicking completed result element on current page...")
        time.sleep(1)

        try:
            clicked_result = sb.execute_script("""return (function(){
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

        time.sleep(1.5)

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

        for dl_attempt in range(30):
            try:
                candidate_urls = sb.execute_script(EXTRACT_ALL_MP4_JS) or []
                # Filter out initial input URLs (the uploaded video/photo)
                new_candidate_urls = [u for u in candidate_urls if u.split('?')[0] not in initial_urls]

                target_url = None
                if new_candidate_urls:
                    target_url = new_candidate_urls[0]
                elif len(candidate_urls) >= 2:
                    # Pick the second URL (the generated result, skipping the 1st input video)
                    target_url = candidate_urls[1]
                elif candidate_urls and dl_attempt > 5:
                    target_url = candidate_urls[0]

                if target_url:
                    print(f"  ⬇ Found target video URL (attempt {dl_attempt+1}): {target_url[:120]}")
                    try:
                        urllib.request.urlretrieve(target_url, dst)
                        if os.path.exists(dst) and os.path.getsize(dst) > 1000:
                            print(f"  ✅ Video saved! ({os.path.getsize(dst)} bytes)")
                            return True
                        else:
                            print(f"  ⚠ File too small ({os.path.getsize(dst) if os.path.exists(dst) else 0}b), retrying...")
                    except Exception as e:
                        print(f"  ⚠ Download error: {e}")
            except Exception as e:
                if dl_attempt % 5 == 0:
                    print(f"  ⚠ Attempt {dl_attempt}: {e}")
            time.sleep(2)

        print("  ❌ Could not find/download video")
        return False

    # ---------------------------------------------------------- main loop

    def run(self, count: int = 10, workers: int = 1, generations_per_model: int = 1) -> int:
        from seleniumbase import SB
        from concurrent.futures import ThreadPoolExecutor, as_completed

        if not self.videos:
            print("Error: no videos in --videos-dir.", file=sys.stderr)
            return 1
        if not self.models:
            print("Error: no model photos in --models-dir.", file=sys.stderr)
            return 1
        os.makedirs(self.output_dir, exist_ok=True)

        # Build task list: for each model photo, generate N (generations_per_model) hooks,
        # selecting a random video hook from self.videos for each generation.
        raw_tasks = []
        for model in self.models:
            for g in range(generations_per_model):
                video = random.choice(self.videos)
                raw_tasks.append((video, model))

        if not raw_tasks:
            print("Error: task list is empty.", file=sys.stderr)
            return 1

        # Use raw_tasks length if count is default or auto, otherwise slice/extend raw_tasks to count
        if count > 0 and (count != 10 or len(raw_tasks) == 10):
            if len(raw_tasks) < count:
                multiplier = (count // len(raw_tasks)) + 1
                tasks = (raw_tasks * multiplier)[:count]
            else:
                tasks = raw_tasks[:count]
        else:
            tasks = raw_tasks

        # Determine starting index based on existing hook_XXX.mp4 files in output_dir
        existing_indices = []
        for fname in os.listdir(self.output_dir):
            m = re.match(r"^hook_(\d+)\.mp4$", fname)
            if m:
                existing_indices.append(int(m.group(1)))
        start_idx = max(existing_indices) if existing_indices else 0

        target_count = len(tasks)
        effective_workers = min(workers, target_count)
        print(f"Videos: {len(self.videos)}, photos: {len(self.models)}, "
              f"gen/model: {generations_per_model}, workers: {effective_workers}, "
              f"target: {target_count} hooks (starting index: {start_idx + 1})")

        done_counter = [0]  # mutable container for thread-safe increment

        def _execute_hook_task(task_info: tuple[int, int, str, str]) -> bool:
            """Execute a single hook generation in its own isolated browser instance."""
            worker_id, idx, video, photo = task_info
            tag = f"[W{worker_id}|H{idx}]"
            dst = os.path.join(self.output_dir, f"hook_{idx:03d}.mp4")
            v_name = os.path.basename(video)
            p_name = os.path.basename(photo)
            _tprint(f"\n{'='*50}\n  {tag} Hook {idx} [Video: {v_name} | Model: {p_name}]\n{'='*50}")

            # Ensure unique profile dir and debugging port per worker to launch dedicated, independent Chrome processes
            worker_profile_dir = os.path.join(tempfile.gettempdir(), f"sb_profile_w{worker_id}_{uuid.uuid4().hex[:6]}")
            worker_port = 9222 + (worker_id * 13 + idx) % 500

            try:
                with SB(uc=True, headless2=self.headless,
                        locale="en", disable_csp=True,
                        user_data_dir=worker_profile_dir, port=worker_port) as sb:
                    _tprint(f"  {tag} 📝 Registering new account via OTP...")
                    self._register(sb)
                    self._complete_onboarding(sb)
                    self._setup_generation(sb, video, photo)
                    if self._generate_and_download(sb, dst):
                        with self._file_lock:
                            done_counter[0] += 1
                        _tprint(f"  {tag} ✅ Saved: {dst} (total done: {done_counter[0]})")
                        return True
            except Exception as exc:
                _tprint(f"  {tag} ❌ FAIL iteration {idx}: {exc}")
            finally:
                shutil.rmtree(worker_profile_dir, ignore_errors=True)

            return False

        indexed_tasks = [(start_idx + i + 1, v, p) for i, (v, p) in enumerate(tasks)]

        if effective_workers > 1:
            print(f"🚀 Multi-threaded mode: {effective_workers} parallel browser workers")
            # Assign worker IDs round-robin
            worker_tasks = [(i % effective_workers + 1, idx, v, p)
                            for i, (idx, v, p) in enumerate(indexed_tasks)]
            with ThreadPoolExecutor(max_workers=effective_workers) as executor:
                futures = []
                for i, task in enumerate(worker_tasks):
                    futures.append(executor.submit(_execute_hook_task, task))
                    # Stagger thread starts by 3 seconds to avoid OTP API collisions
                    if i < effective_workers - 1:
                        time.sleep(3)
                for f in as_completed(futures):
                    try:
                        f.result()  # counter already incremented inside
                    except Exception as exc:
                        _tprint(f"  ⚠ Thread exception: {exc}")
        else:
            for idx, v, p in indexed_tasks:
                _execute_hook_task((1, idx, v, p))

        done = done_counter[0]
        print(f"\n{'='*50}")
        print(f"  Done: {done}/{target_count} hooks -> {self.output_dir}")
        print(f"{'='*50}")
        return 0 if done else 1


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
    parser.add_argument("--timeout-gen", type=int, default=900)
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
    )
    return gen.run(count=args.count, workers=args.workers, generations_per_model=args.generations_per_model)


if __name__ == "__main__":
    raise SystemExit(main())

