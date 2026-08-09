#!/usr/bin/env python3
"""Login with existing account → generate hooks on Higgsfield Motion page.

DOM findings (from live inspection):
- Switch "Log in"/"Sign up" = <button class="text-font-primary underline">
- Submit buttons = <input type="submit" value="Continue with Email"|"Log in">
- NOT <button> or <a> tags!
"""
import os
import sys
import time
import urllib.request

MOTION_URL = "https://higgsfield.ai/ai/video/motion?rp=%2Fai%2Fvideo%2Fmotion"
HOME_URL = "https://higgsfield.ai/"
OUTPUT = "./generated_hooks"
CREDS_FILE = "./hooks_out/credentials.txt"
VIDEOS_DIR = "./hook_refs"
MODELS_DIR = "./models"
COUNT = 1  # testing




def load_creds():
    creds = []
    for path in [CREDS_FILE, "./raw_batch/1/credentials.txt"]:
        if os.path.isfile(path):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if ":" in line:
                        email, pwd = line.split(":", 1)
                        creds.append((email, pwd))
    seen = set()
    unique = []
    for c in creds:
        if c[0] not in seen:
            seen.add(c[0])
            unique.append(c)
    return unique


def slow_type_field(sb, selector, text, clear=True):
    """Type text character by character for React compatibility."""
    try:
        el = sb.find_element(selector)
        if clear:
            el.clear()
            time.sleep(0.3)
        el.click()
        time.sleep(0.3)
        for ch in text:
            el.send_keys(ch)
            time.sleep(0.05)
        return True
    except Exception:
        return False


def login(sb, email, password):
    """Login via Clerk modal with correct DOM selectors."""
    sb.uc_open_with_reconnect(HOME_URL, reconnect_time=3)
    time.sleep(1.5)

    # Click Sign up in navbar (opens Clerk modal reliably)
    sb.execute_script("""
        var items = document.querySelectorAll('button, a');
        for (var el of items) {
            if (el.textContent.trim() === 'Sign up' && el.offsetParent
                && el.getBoundingClientRect().top < 80) {
                el.click(); return;
            }
        }
        for (var el of items) {
            if (el.textContent.trim() === 'Login' && el.offsetParent) {
                el.click(); return;
            }
        }
    """)
    print("  ✓ Clicked Sign up")
    time.sleep(1.5)

    # Wait for modal
    for i in range(15):
        try:
            has = sb.execute_script("""
                return document.body.innerText.includes('Welcome to Higgsfield')
                    || document.body.innerText.includes('Log in to Higgsfield')
                    || document.body.innerText.includes('Create an account')
                    || !!document.querySelector('input[type="email"]');
            """)
            if has:
                break
        except Exception:
            pass
        time.sleep(1)


    # Detect modal type
    modal_type = sb.execute_script("""
        var txt = document.body.innerText || '';
        if (txt.includes('Log in to Higgsfield')) return 'login';
        return 'signup';
    """)
    print(f"  Modal: {modal_type}")

    if modal_type == 'signup':
        # Step 1: Click "Continue with Email" to get past OAuth buttons screen
        # The first screen shows Google/Apple/Microsoft + "Continue with Email"
        # After clicking, we get to email+password form with "Already have an account? Log in"
        sb.execute_script("""(function(){
            var btns = document.querySelectorAll('button, input[type="submit"]');
            for (var b of btns) {
                var txt = (b.textContent || b.value || '').trim();
                if (txt.includes('Continue with Email') && b.offsetParent) {
                    b.click(); return;
                }
            }
        })()""")
        print("  ✓ Clicked Continue with Email")
        time.sleep(1)


        # Step 2: Now click "Log in" link: "Already have an account? Log in"
        # This can be a <button>, <a>, or <span>
        switched = sb.execute_script("""(function(){
            var allEls = document.querySelectorAll('button, a, span, p, div');
            for (var el of allEls) {
                var txt = el.textContent.trim();
                if (txt === 'Log in' && el.offsetParent) {
                    var r = el.getBoundingClientRect();
                    if (r.height > 50 || r.width > 250) continue;
                    el.click();
                    return 'direct:' + el.tagName;
                }
            }
            var parents = document.querySelectorAll('p, div, span');
            for (var p of parents) {
                var ptxt = p.textContent || '';
                if (ptxt.includes('Already have an account')) {
                    var children = p.querySelectorAll('button, a, span, [role="link"]');
                    for (var c of children) {
                        if (c.textContent.trim().includes('Log in')) {
                            c.click();
                            return 'parent:' + c.tagName;
                        }
                    }
                }
            }
            return null;
        })()""")

        if switched:
            print(f"  ✓ Switched to Login: {switched}")
            time.sleep(1)
        else:
            print("  ⚠ Could not switch via JS, trying XPath...")

            try:
                sb.click('//*[text()="Log in"]', timeout=3)
                print("  ✓ Switched via XPath")
                time.sleep(3)
            except Exception:
                # Last attempt: click "Login" in navbar instead
                try:
                    # Close modal first
                    sb.execute_script("""
                        var x = document.querySelector('[aria-label="Close"], button:has(svg)');
                        if (x) x.click();
                    """)
                    time.sleep(1)
                    sb.click('//button[text()="Login"]', timeout=3)
                    time.sleep(3)
                    print("  ✓ Opened Login via navbar")
                except Exception:
                    return False





    # Now on Login form — fill email
    # Wait for email field
    for i in range(10):
        try:
            if sb.execute_script("return !!document.querySelector('input[type=\"email\"]')"):
                break
        except Exception:
            pass
        time.sleep(1)

    filled_email = False
    for sel in ["input[type='email']", "input[name='identifier']", "input[name='emailAddress']"]:
        try:
            sb.wait_for_element_visible(sel, timeout=5)
            # Clear first, then type
            sb.execute_script(f"""
                var inp = document.querySelector('{sel}');
                if (inp) {{
                    inp.focus();
                    inp.value = '';
                    inp.dispatchEvent(new Event('input', {{bubbles:true}}));
                }}
            """)
            time.sleep(0.3)
            sb.type(sel, email)
            filled_email = True
            print(f"  ✓ Email: {email}")
            break
        except Exception:
            continue

    if not filled_email:
        # JS fallback with React events
        sb.execute_script(f"""
            var inp = document.querySelector('input[type="email"]');
            if (inp) {{
                var nativeSet = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                nativeSet.call(inp, '{email}');
                inp.dispatchEvent(new Event('input', {{bubbles:true}}));
                inp.dispatchEvent(new Event('change', {{bubbles:true}}));
            }}
        """)
        print(f"  ✓ Email (React JS): {email}")

    time.sleep(1)

    # Fill password — CRITICAL: user reports password not being entered
    # Wait for password field explicitly
    print("  ⏳ Waiting for password field...")
    pw_visible = False
    for i in range(10):
        try:
            has_pw = sb.execute_script("""
                var pw = document.querySelector('input[type="password"]');
                return pw && pw.offsetHeight > 0;
            """)
            if has_pw:
                pw_visible = True
                break
        except Exception:
            pass
        time.sleep(1)

    if not pw_visible:
        print("  ⚠ Password field not visible, trying to advance form...")
        # Maybe need to click Continue/Next to get to password step
        sb.execute_script("""(function(){
            var submits = document.querySelectorAll('input[type="submit"]');
            for (var s of submits) {
                if (s.value && s.value.includes('Continue')) { s.click(); return; }
            }
            var btns = document.querySelectorAll('button');
            for (var b of btns) {
                if (b.textContent.trim().toLowerCase() === 'continue') { b.click(); return; }
            }
        })()""")
        time.sleep(1.5)



    # Type password using React-compatible method
    filled_pw = False

    # Method 1: Native React setter (most reliable for React forms)
    try:
        result = sb.execute_script(f"""
            var inp = document.querySelector('input[type="password"]');
            if (!inp || inp.offsetHeight === 0) return 'not-found';
            inp.focus();
            // Use React's native value setter to bypass synthetic event system
            var nativeSet = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            nativeSet.call(inp, '{password}');
            inp.dispatchEvent(new Event('input', {{bubbles: true}}));
            inp.dispatchEvent(new Event('change', {{bubbles: true}}));
            return 'ok';
        """)
        if result == 'ok':
            filled_pw = True
            print("  ✓ Password filled (React setter)")
    except Exception as e:
        print(f"  ⚠ React setter failed: {e}")

    # Method 2: Selenium type (sends real keystrokes)
    if not filled_pw:
        try:
            sb.type("input[type='password']", password)
            filled_pw = True
            print("  ✓ Password filled (sb.type)")
        except Exception:
            pass

    # Method 3: Slow character-by-character
    if not filled_pw:
        if slow_type_field(sb, "input[type='password']", password):
            filled_pw = True
            print("  ✓ Password filled (slow_type)")

    if not filled_pw:

        print("  ✗ Could not fill password!")
        return False


    time.sleep(1)

    # Submit login form — use input[type="submit"][value="Log in"]
    # NOT button! The submit is an <input>, not a <button>!
    submitted = False

    # Method 1: Click input[type=submit] directly
    try:
        result = sb.execute_script("""(function(){
            var submits = document.querySelectorAll('input[type="submit"]');
            for (var s of submits) {
                if (s.value === 'Log in' || s.value === 'Continue'
                    || s.value === 'Continue with Email') {
                    s.click();
                    return s.value;
                }
            }
            return null;
        })()""")
        if result:
            submitted = True
            print(f"  ✓ Submit via input[type=submit]: {result}")
    except Exception:
        pass

    # Method 2: uc_click on input[type=submit]
    if not submitted:
        try:
            sb.uc_click("input[type='submit']", timeout=4)
            submitted = True
            print("  ✓ Submit via uc_click input[type=submit]")
        except Exception:
            pass

    # Method 3: Find and click button with "Log in" text (but NOT the switch button)
    if not submitted:
        try:
            sb.execute_script("""(function(){
                var btns = document.querySelectorAll('button');
                for (var b of btns) {
                    var txt = b.textContent.trim();
                    if ((txt === 'Log in' || txt === 'Continue')
                        && b.offsetParent && b.offsetHeight > 35
                        && !b.className.includes('underline')) {
                        b.click(); return;
                    }
                }
            })()""")
            submitted = True
            print("  ✓ Submit via JS button fallback")
        except Exception:
            pass

    if not submitted:
        print("  ⚠ Could not find submit, pressing Enter...")
        try:
            sb.execute_script("""
                var pw = document.querySelector('input[type="password"]');
                if (pw) {
                    pw.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', code:'Enter', bubbles:true}));
                    pw.dispatchEvent(new KeyboardEvent('keypress', {key:'Enter', code:'Enter', bubbles:true}));
                    pw.dispatchEvent(new KeyboardEvent('keyup', {key:'Enter', code:'Enter', bubbles:true}));
                }
            """)
        except Exception:
            pass

    # Wait for login to complete
    print("  ⏳ Waiting for redirect...")
    for i in range(20):
        time.sleep(0.5)
        try:
            url = sb.get_current_url()
            if "/ai/" in url or "explore" in url or "cinema" in url:
                print(f"  ✓ Logged in! URL: {url}")
                return True
        except Exception:
            pass
        try:
            result = sb.execute_script("""
                var txt = document.body.innerText || '';
                var hasModal = txt.includes('Log in to Higgsfield')
                    || txt.includes('Create an account')
                    || txt.includes('Welcome to Higgsfield');
                if (hasModal) return 'modal';
                var navEls = document.querySelectorAll('nav a, nav button, header a, header button');
                for (var el of navEls) {
                    var t = el.textContent.trim();
                    if ((t === 'Login' || t === 'Sign up') && el.offsetParent) return 'not-logged';
                }
                return 'logged';
            """)
            if result == 'logged':
                print(f"  ✓ Login confirmed after {i+1}s")
                return True
        except Exception:
            pass



    # Check errors
    try:
        body = sb.execute_script("return document.body.innerText.substring(0, 1000)")
        if "incorrect" in body.lower() or "invalid" in body.lower():
            print("  ✗ Credentials rejected")
            return False
        if "Log in to Higgsfield" in body or "Create an account" in body:
            print("  ✗ Modal still visible")
            return False
    except Exception:
        pass

    print("  ⚠ Login uncertain")
    return False


def close_popups(sb):
    """Close ANY popup/overlay/modal that blocks interaction.

    Known popups on Higgsfield:
    - "Congratulations! You received a personal 61% OFF offer" (promo)
    - "ORGANIZE. SHARE. CREATE TOGETHER" (Cinema Studio promo)
    - Cookie consent banners
    - Feature announcements
    """
    for attempt in range(5):
        closed = False
        try:
            result = sb.execute_script("""(function(){
                // --- 1. Promo/discount overlays (highest priority) ---
                var bodyText = document.body.innerText || '';
                if (bodyText.includes('OFF offer') || bodyText.includes('Claim Discount')
                    || bodyText.includes('special offer') || bodyText.includes('EXTRA DISCOUNT')
                    || bodyText.includes('Get Unlimited') || bodyText.includes('premium plan')) {
                    // Find close/X buttons in any overlay container
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
                            // Close buttons: X/×, aria-label=close, small SVG button in corner
                            if (txt === '×' || txt === '✕' || txt === 'X' || txt === 'x'
                                || txt === 'Close' || ariaLabel === 'close'
                                || (hasSvg && r.width < 60 && r.height < 60)) {
                                b.click();
                                return 'closed-promo-overlay';
                            }
                        }
                    }
                }

                // --- 2. Fixed/absolute close buttons (any floating X) ---
                var allBtns = document.querySelectorAll('button, [role="button"]');
                for (var b of allBtns) {
                    if (!b.offsetParent && b.offsetHeight === 0) continue;
                    var txt = b.textContent.trim();
                    var ariaLabel = (b.getAttribute('aria-label') || '').toLowerCase();
                    // Direct X/close text
                    if (txt === '×' || txt === '✕' || txt === 'X' || txt === 'x') {
                        // Make sure it's not a tiny invisible button
                        var r = b.getBoundingClientRect();
                        if (r.width > 5 && r.height > 5) {
                            b.click();
                            return 'closed-x-btn';
                        }
                    }
                    // aria-label="close" buttons
                    if (ariaLabel === 'close' || ariaLabel === 'dismiss' || ariaLabel === 'закрыть') {
                        b.click();
                        return 'closed-aria';
                    }
                }

                // --- 3. SVG-only close buttons in modals/dialogs ---
                var containers = document.querySelectorAll(
                    '[role="dialog"], [class*="modal"], [class*="popup"], [class*="overlay"]');
                for (var c of containers) {
                    if (!c.offsetParent && c.offsetHeight === 0) continue;
                    var btns = c.querySelectorAll('button');
                    for (var b of btns) {
                        var svg = b.querySelector('svg');
                        var r = b.getBoundingClientRect();
                        if (svg && r.width < 60 && r.height < 60 && r.width > 5) {
                            b.click();
                            return 'closed-svg-modal';
                        }
                    }
                }

                // --- 4. Top banner dismiss (e.g. "SIGN UP AND GET..." bar) ---
                var banner = document.querySelector('[class*="banner"] button, [class*="Banner"] button');
                if (banner && banner.offsetParent) {
                    var txt = banner.textContent.trim();
                    var svg = banner.querySelector('svg');
                    if (txt === '×' || txt === 'X' || txt === '✕' || svg) {
                        banner.click();
                        return 'closed-banner';
                    }
                }

                return null;
            })()""")
            if result:
                closed = True
                print(f"  ✓ Popup closed: {result}")
                time.sleep(1)
        except Exception:
            pass

        if not closed:
            break
        time.sleep(0.5)


def complete_onboarding(sb):
    """Skip onboarding by navigating directly to Motion page."""
    time.sleep(1)
    try:
        body = sb.execute_script("return (document.body.innerText||'').substring(0,500)") or ""
        if any(m in body for m in ["How do you plan", "1 of", "For personal use", "Personalizing"]):
            print("  ⏭ Onboarding detected — skipping via reload")
        else:
            print("  ℹ No onboarding")
            return
    except Exception:
        pass
    # Skip by navigating to Motion URL directly
    sb.open(MOTION_URL)
    time.sleep(2)
    # Dismiss any remaining overlays
    try:
        sb.execute_script("""
            document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',code:'Escape',bubbles:true}));
            document.querySelectorAll('[class*="modal"],[class*="overlay"],[class*="onboard"],[class*="wizard"]')
                .forEach(function(o){if(o.style) o.style.display='none';});
        """)
    except Exception:
        pass
    close_popups(sb)
    print("  ✓ Onboarding skipped")


def setup_and_generate(sb, video_path, photo_path, dst):
    # Navigate to Motion page
    sb.open(MOTION_URL)
    time.sleep(3)
    try:
        txt = sb.execute_script(
            "return document.body ? document.body.innerText.substring(0,300) : ''") or ""
        if "ERR_" in txt or "can't be reached" in txt:
            time.sleep(5)
            sb.open(MOTION_URL)
            time.sleep(3)
    except Exception:
        pass

    close_popups(sb)
    time.sleep(1)


    # Wait for file inputs (max 10s)
    for i in range(10):
        try:
            if sb.execute_script("return !!document.querySelector('input[type=\"file\"]')"):
                break
        except Exception:
            pass
        time.sleep(1)

    # Select FREE model (Kling Motion Control, not 3.0)
    try:
        sb.execute_script("""(function(){
            var btns = document.querySelectorAll('button, div[role="button"]');
            for (var b of btns) {
                var txt = b.textContent.trim();
                if (txt.includes('Kling') && b.getBoundingClientRect().left < 400) {
                    b.click(); return;
                }
            }
        })()""")
        time.sleep(1)
        sb.execute_script("""(function(){
            var items = document.querySelectorAll('div, li, button, a, [role="option"]');
            for (var el of items) {
                if (!el.offsetParent && el.offsetHeight === 0) continue;
                var txt = el.textContent.trim();
                if (txt.includes('Kling Motion Control') && !txt.includes('3.0') && txt.length < 100) {
                    el.click(); return;
                }
            }
        })()""")
        print("  ✓ Model: Kling Motion Control (free)")
    except Exception as e:
        print(f"  ⚠ Model: {e}")
    time.sleep(0.5)

    # Video toggle (not Image)
    try:
        sb.execute_script("""(function(){
            var btns = document.querySelectorAll('button');
            for (var b of btns) {
                if (b.textContent.trim() === 'Video'
                    && b.getBoundingClientRect().top > 150
                    && b.getBoundingClientRect().left < 500) {
                    b.click(); return;
                }
            }
        })()""")
        print("  ✓ Switched to Video")
    except Exception:
        pass
    time.sleep(0.5)

    # --- Upload files via CDP ---
    def upload_file_cdp(file_path, label, input_index):
        abs_path = os.path.abspath(file_path)
        # Unhide all file inputs
        sb.execute_script("""
            document.querySelectorAll('input[type="file"]').forEach(function(inp) {
                inp.style.cssText = 'display:block!important;opacity:1!important;'+
                    'visibility:visible!important;position:absolute!important;'+
                    'top:0;left:0;width:1px;height:1px;z-index:99999';
                inp.removeAttribute('hidden');
                inp.classList.remove('sr-only');
            });
        """)
        time.sleep(0.3)
        inputs = sb.find_elements("input[type='file']")
        if not inputs:
            print(f"  ⚠ {label}: no file inputs")
            return False
        idx = min(input_index, len(inputs) - 1)
        inp = inputs[idx]
        # Try CDP first
        try:
            driver = sb.driver
            doc = driver.execute_cdp_cmd('DOM.getDocument', {'depth': -1})
            root_id = doc['root']['nodeId']
            qr = driver.execute_cdp_cmd('DOM.querySelectorAll', {
                'nodeId': root_id, 'selector': 'input[type="file"]'
            })
            node_ids = qr.get('nodeIds', [])
            if node_ids and idx < len(node_ids):
                driver.execute_cdp_cmd('DOM.setFileInputFiles', {
                    'files': [abs_path], 'nodeId': node_ids[idx]
                })
                time.sleep(0.5)
                sb.execute_script(f"""
                    var inp = document.querySelectorAll('input[type="file"]')[{idx}];
                    if (inp) {{
                        inp.dispatchEvent(new Event('change', {{bubbles:true}}));
                        inp.dispatchEvent(new Event('input', {{bubbles:true}}));
                    }}
                """)
                fc = sb.execute_script(f"""
                    var inp = document.querySelectorAll('input[type="file"]')[{idx}];
                    return inp ? inp.files.length : 0;
                """)
                if fc and fc > 0:
                    print(f"  ✓ {label}: uploaded via CDP")
                    return True
        except Exception as e:
            print(f"  ⚠ {label} CDP error: {e}")
        # Fallback: send_keys
        try:
            inp.send_keys(abs_path)
            time.sleep(1)
            print(f"  ✓ {label}: uploaded via send_keys")
            return True
        except Exception as e2:
            print(f"  ⚠ {label} send_keys error: {e2}")
        return False

    v_ok = upload_file_cdp(video_path, "Video", 0)
    print(f"  📹 Video: {'OK' if v_ok else 'FAIL'} — {os.path.basename(video_path)}")
    time.sleep(1.5)

    p_ok = upload_file_cdp(photo_path, "Photo", 1)
    print(f"  🧑 Photo: {'OK' if p_ok else 'FAIL'} — {os.path.basename(photo_path)}")
    time.sleep(1.5)



    # --- Click Generate ---
    gen_clicked = False
    # Method 1: JS click
    try:
        r = sb.execute_script("""(function(){
            var btns = document.querySelectorAll('button');
            for (var b of btns) {
                if (!b.offsetWidth || !b.offsetHeight) continue;
                var txt = b.textContent.trim();
                if ((txt.includes('Generate') || txt.includes('Create')) && txt.length < 50) {
                    b.scrollIntoView({block:'center'});
                    b.click();
                    return txt;
                }
            }
            return null;
        })()""")
        if r:
            gen_clicked = True
            print(f"  ✓ Clicked '{r}' via JS")
    except Exception:
        pass
    # Method 2: XPath
    if not gen_clicked:
        for btn in ["Generate", "Create"]:
            try:
                sb.click(f'//button[contains(.,"{btn}")]', timeout=4)
                gen_clicked = True
                print(f"  ✓ Clicked {btn} via XPath")
                break
            except Exception:
                continue
    if not gen_clicked:

        raise RuntimeError("Generate button not found")

    # Wait for result
    print("  ⏳ Generating...")
    start = time.time()
    while time.time() - start < 600:
        time.sleep(10)
        # Close any promo popups that appear during generation
        close_popups(sb)
        try:
            for sel in ["a[download]", '//a[contains(text(),"Download")]',
                        '//button[contains(text(),"Download")]']:
                try:
                    el = sb.find_element(sel)
                    if el.is_displayed():
                        el.click()
                        time.sleep(5)
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        try:
            src = sb.execute_script("""
                var v = document.querySelector('video[src*=".mp4"]');
                if (!v) { var s = document.querySelector('video source[src*=".mp4"]'); if (s) v = s; }
                return v ? (v.src || v.getAttribute('src')) : null;
            """)
            if src and ".mp4" in src:
                if src.startswith("/"):
                    src = "https://higgsfield.ai" + src
                urllib.request.urlretrieve(src, dst)
                print(f"  ✓ Downloaded: {dst}")
                return True
        except Exception:
            pass
        elapsed = int(time.time() - start)
        if elapsed % 60 == 0:
            print(f"  ... {elapsed}s")


    raise RuntimeError("Timeout 600s")


def main():
    from seleniumbase import SB

    creds = load_creds()
    if not creds:
        print("ERROR: No credentials", file=sys.stderr)
        return 1

    videos = sorted([os.path.join(VIDEOS_DIR, f) for f in os.listdir(VIDEOS_DIR)
                     if f.lower().endswith((".mp4", ".mov", ".webm"))])
    models = sorted([os.path.join(MODELS_DIR, f) for f in os.listdir(MODELS_DIR)
                     if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))])

    if not videos or not models:
        print("ERROR: No videos or photos", file=sys.stderr)
        return 1

    os.makedirs(OUTPUT, exist_ok=True)
    print(f"Accounts: {len(creds)}, Videos: {len(videos)}, Photos: {len(models)}")
    print(f"Target: {COUNT} hooks\n")

    done = 0
    for i in range(COUNT):
        email, pwd = creds[i % len(creds)]
        video = videos[i % len(videos)]
        photo = models[i % len(models)]
        dst = os.path.join(OUTPUT, f"hook_{i+1:03d}.mp4")

        print(f"\n{'='*50}")
        print(f"  Hook {i+1}/{COUNT} — {email}")
        print(f"{'='*50}")

        try:
            with SB(uc=True, headless2=True, locale="en", disable_csp=True) as sb:
                ok = login(sb, email, pwd)
                if not ok:
                    print("  ✗ Login failed")
                    continue
                complete_onboarding(sb)
                setup_and_generate(sb, video, photo, dst)
                done += 1
                print(f"  ✅ Saved: {dst}")
        except Exception as exc:
            print(f"  ❌ FAIL: {exc}")

    print(f"\nResult: {done}/{COUNT}")
    return 0 if done else 1


if __name__ == "__main__":
    raise SystemExit(main())
