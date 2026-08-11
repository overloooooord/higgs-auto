"""Debug: inspect Sign up button details and React props."""
import sys, time
sys.path.insert(0, '.')
from seleniumbase import SB

HOME = "https://higgsfield.ai/"

with SB(uc=True, headless2=False, locale="en", disable_csp=True) as sb:
    print("[1] Opening Higgsfield...")
    sb.uc_open_with_reconnect(HOME, reconnect_time=4)
    time.sleep(3)
    
    info = sb.execute_script("""return (function(){
        var btns = document.querySelectorAll('button, a');
        var res = [];
        for (var b of btns) {
            var txt = (b.textContent || '').trim();
            if (txt === 'Sign up' || txt === 'Login') {
                var props = Object.keys(b).filter(k => k.startsWith('__react') || k.startsWith('__vue'));
                res.push({
                    text: txt,
                    tag: b.tagName,
                    outer: b.outerHTML,
                    reactKeys: props,
                    parent: b.parentElement ? b.parentElement.outerHTML.substring(0, 300) : null
                });
            }
        }
        return res;
    })()""")
    print(f"[2] Buttons info:\n{info}")

    # Now let's try dispatching synthetic click with full MouseEvent sequence
    print("\n[3] Dispatching full MouseEvent sequence (mousedown, mouseup, click)...")
    res_click = sb.execute_script("""return (function(){
        var btns = document.querySelectorAll('button, a');
        for (var b of btns) {
            if ((b.textContent || '').trim() === 'Sign up') {
                b.scrollIntoView();
                b.dispatchEvent(new MouseEvent('pointerdown', {bubbles: true, cancelable: true}));
                b.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}));
                b.dispatchEvent(new MouseEvent('pointerup', {bubbles: true, cancelable: true}));
                b.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true}));
                b.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                return 'dispatched-events';
            }
        }
        return 'not-found';
    })()""")
    print(f"    Result: {res_click}")

    time.sleep(3)
    txt = sb.execute_script("return (document.body.innerText||'').substring(0,600)")
    print(f"\n[4] Page text after full MouseEvent:\n{txt[:400]}")
