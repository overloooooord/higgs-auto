with open('/home/caterinw/Downloads/ClipForge/clipforge/clipforge/generate_hooks.py', 'r') as f:
    code = f.read()

start = '        def upload_file(file_path: str, label: str, drop_zone_text: str) -> bool:'
end_marker = '        # First check for and accept any agreement modal'

if start not in code or end_marker not in code:
    print(f"start={'FOUND' if start in code else 'MISSING'}")
    print(f"end={'FOUND' if end_marker in code else 'MISSING'}")
    exit(1)

before = code.split(start)[0]
after = end_marker + code.split(end_marker)[1]

new_fn = r'''        def upload_file(file_path: str, label: str, drop_zone_text: str) -> bool:
            """Upload file via CDP file chooser interception (React-compatible)."""
            abs_path = os.path.abspath(file_path)
            if not os.path.exists(abs_path):
                print(f"  ❌ File not found: {abs_path}")
                return False

            driver = sb.driver

            # --- Method 1: CDP file chooser interception ---
            # This is the only reliable method for React file inputs.
            # We click the drop-zone label/container, which triggers
            # the native file chooser, then intercept it via CDP.
            try:
                driver.execute_cdp_cmd(
                    'Page.setInterceptFileChooserDialog', {'enabled': True})
            except Exception:
                pass

            # Click the correct drop-zone container via JS
            click_result = sb.execute_script(f"""(function(){{
                // For video: click inside #video-form-input
                if ('{label}' === 'video') {{
                    var vf = document.querySelector('#video-form-input');
                    if (vf) {{
                        var lbl = vf.querySelector('label') || vf;
                        lbl.click();
                        return 'clicked-video-label';
                    }}
                }}
                // For photo: click the FIRST drop-zone label that is NOT inside #video-form-input
                var labels = document.querySelectorAll('label.interactive-tap, label[class*="interactive"]');
                for (var l of labels) {{
                    if (!l.closest('#video-form-input') && l.offsetWidth > 10) {{
                        l.click();
                        return 'clicked-photo-label';
                    }}
                }}
                // Fallback: search by drop_zone_text in parent divs
                var divs = document.querySelectorAll('div, label');
                for (var d of divs) {{
                    if (!d.offsetWidth || !d.offsetHeight) continue;
                    var txt = d.textContent.trim();
                    if (txt.includes('{drop_zone_text}') && txt.length < 200) {{
                        var r = d.getBoundingClientRect();
                        if (r.width > 50 && r.height > 50) {{
                            d.click();
                            return 'clicked-dropzone-text';
                        }}
                    }}
                }}
                // Last fallback: click the border-dashed upload containers
                var containers = document.querySelectorAll('[class*="border-dashed"]');
                var idx = '{label}' === 'video' ? 1 : 0;
                if (containers.length > idx) {{
                    containers[idx].click();
                    return 'clicked-dashed-' + idx;
                }}
                return null;
            }})()\""")

            if click_result:
                print(f"  📎 {label}: {click_result}")
                time.sleep(0.3)
                accept_agreement(sb)
                try:
                    driver.execute_cdp_cmd('Page.handleFileChooser', {
                        'action': 'accept',
                        'files': [abs_path]
                    })
                    time.sleep(1.5)
                    print(f"  ✓ {label}: uploaded via CDP file chooser")
                    dismiss_file_errors(sb)
                    return True
                except Exception as e:
                    print(f"  ⚠ {label}: CDP handleFileChooser failed: {e}")
                    try:
                        driver.execute_cdp_cmd(
                            'Page.handleFileChooser', {'action': 'cancel'})
                    except Exception:
                        pass
            else:
                print(f"  ⚠ {label}: could not click any drop-zone container")

            # --- Method 2: Direct send_keys fallback ---
            # Don't reposition inputs (breaks React), just make opacity visible
            try:
                sb.execute_script("""
                    document.querySelectorAll('input[type="file"]').forEach(function(inp) {
                        inp.style.opacity = '1';
                        inp.style.visibility = 'visible';
                        inp.removeAttribute('hidden');
                    });
                """)
                time.sleep(0.3)
                inputs = sb.find_elements("input[type='file']")
                for inp in inputs:
                    try:
                        accept_attr = (inp.get_attribute('accept') or '').lower()
                        if label == 'video' and 'image' in accept_attr and 'video' not in accept_attr:
                            continue
                        if label == 'photo' and 'video' in accept_attr and 'image' not in accept_attr:
                            continue
                        inp.send_keys(abs_path)
                        time.sleep(1.0)
                        accept_agreement(sb)
                        print(f"  ✓ {label}: uploaded via send_keys fallback")
                        dismiss_file_errors(sb)
                        return True
                    except Exception as e2:
                        print(f"  ⚠ {label}: send_keys fallback error: {e2}")
                        continue
            except Exception as e:
                print(f"  ⚠ {label}: Method 2 error: {e}")

            print(f"  ❌ {label}: all upload methods failed")
            return False

'''

new_code = before + new_fn + '        ' + after
with open('/home/caterinw/Downloads/ClipForge/clipforge/clipforge/generate_hooks.py', 'w') as f:
    f.write(new_code)
print("DONE")
