#!/usr/bin/env python3
"""
Adds the "Fix image for publishing" button to Home Base (Nov 2026).

Home Base lives on the Pi in /home/pi/azpi-portal (passflow.py + web/home.html), outside this
website repo, so it can't get new code by itself. azpi_publish.py runs this at the start of every
run. It does nothing once the button is in. Otherwise it:
  * backs up both files as *.bak-fixbutton (same habit as the other Home Base changes)
  * adds the button, its server side, and the fix/undo command (passflow.py fix <pass>)
  * checks the new passflow.py compiles; if anything doesn't match what it expects, it changes
    nothing and says so (once) on the phone
  * restarts the Home Base service so the button works straight away

The repair itself is tools/azpi_fix_image.py.

By hand on the Pi (as pi):  python3 ~/sat-station-site/tools/azpi_install_fixbutton.py [--dry-run]
"""
import json
import os
import py_compile
import shutil
import subprocess
import sys

PORTAL = os.environ.get("AZPI_PORTAL_DIR", "/home/pi/azpi-portal")
PASSFLOW = os.path.join(PORTAL, "passflow.py")
PAGE = os.path.join(PORTAL, "web", "home.html")
HERE = os.path.dirname(os.path.abspath(__file__))
MARK = "fix image for publishing (Nov 2026)"
NOTE = os.path.expanduser("~/azpi_footprint/fixbutton_install.json")

# ---------------------------------------------------------------- passflow.py edits (anchor, new text, where)
def passflow_edits():
    with open(os.path.join(HERE, "fixbutton", "passflow_fix.py")) as fh:
        fn = fh.read()
    return [
        ("# ------------------------------------------------------------------ 6 publish",
         fn.strip("\n") + "\n\n", "before"),
        ('"flipped": bool(meta.get("flipped")), "flipping": bool(meta.get("flipping")),',
         '\n                    "fixed": bool(meta.get("fixed")), "fixing": bool(meta.get("fixing")) and '
         'time.time() - meta["fixing"] < 1800,\n                    "fix": (jload(os.path.join(dest, "fix.json"), '
         '{}).get("main") if meta.get("fixed") else None),', "after"),
        ('    if p in ("/api/passflow/publish", "/api/passflow/unpublish"):',
         '    if p == "/api/passflow/fix":               # Fix image for publishing (and undo)\n'
         '        name = str(body.get("name", ""))\n'
         '        if not re.fullmatch(r"[0-9A-Za-z_-]{6,40}", name) or not os.path.isdir(os.path.join(PASSES, name)):\n'
         '            return 400, {"error": "Unknown pass"}\n'
         '        mp = os.path.join(PASSES, name, "pass.json")\n'
         '        meta = jload(mp, {})\n'
         '        if meta.get("fixing") and time.time() - meta["fixing"] < 1800:\n'
         '            return 409, {"error": "Already fixing that pass"}\n'
         '        meta["fixing"] = int(time.time())\n'
         '        jsave(mp, meta)\n'
         '        spawn("fix", name)\n'
         '        return 200, {"ok": True}\n', "before"),
        ('    if a[0] == "flip" and len(a) > 1:',
         '    if a[0] == "fix" and len(a) > 1:\n'
         '        dest = os.path.join(PASSES, a[1])\n'
         '        try:\n'
         '            what = fix_pictures(dest)\n'
         '        except Exception as e:\n'
         '            what = "fix failed: %s" % e\n'
         '            _m = jload(os.path.join(dest, "pass.json"), {})\n'
         '            _m.pop("fixing", None)\n'
         '            jsave(os.path.join(dest, "pass.json"), _m)\n'
         '        meta = jload(os.path.join(dest, "pass.json"), {})\n'
         '        st = jload(STATUS, {"log": []})\n'
         '        st.setdefault("log", []).append(time.strftime("%H:%M") + "  %s: %s" % (a[1], what))\n'
         '        jsave(STATUS, st)\n'
         '        if meta.get("published") and not what.startswith("fix failed"):   # on the website: send the new pictures\n'
         '            ok_, msg = publish(a[1])\n'
         '            st = jload(STATUS, {"log": []})\n'
         '            st.setdefault("log", []).append(time.strftime("%H:%M") + "  %s: website %s" % (a[1], "updated" if ok_ else "not updated: " + msg))\n'
         '            jsave(STATUS, st)\n'
         '        sys.exit(0)\n', "before"),
    ]


PAGE_EDITS = [
    ("Rotate 180°</button>`):''}",
     "\n        ${p.image?(p.fixing?`<button class=\"btn\" disabled>Fixing… (a few minutes)</button>`:"
     "`<button class=\"btn\" data-act=\"fix\" data-name=\"${esc(p.name)}\" title=\"${p.fixed?'Put the original pictures back':"
     "'Repair the coloured and black lines: lost colour channels are rebuilt from the ones that came through, short gaps blended, big gaps left black'}\">"
     "${p.fixed?'Undo fix':'Fix image for publishing'}</button>`):''}", "after"),
    ('<div class="path">passes/${nm}/</div>',
     "${p.fixed&&p.fix?`<div class=\"pubnote ok\">✓ Fixed for publishing: ${p.fix.rebuilt_pct}% rebuilt from the other channels, "
     "${p.fix.blended_pct}% blended, ${p.fix.blacked_pct}% left black (no data)</div>`:''}\n      ", "before"),
    ("  if(b.dataset.act==='flip'){",
     "  if(b.dataset.act==='fix'){b.disabled=true;b.textContent='Fixing…';\n"
     "    try{await post('/api/passflow/fix',{name:b.dataset.name});setTimeout(load,4000)}catch(err){msg(err.message);load()}return}\n",
     "before"),
]


def apply(text, edits):
    for anchor, new, where in edits:
        if text.count(anchor) != 1:
            return None, anchor
        text = text.replace(anchor, new + anchor if where == "before" else anchor + new)
    return text, None


def tell(msg, ok):
    """Say it once on the phone (ntfy, via the publisher's helper) and remember it."""
    try:
        os.makedirs(os.path.dirname(NOTE), exist_ok=True)
        old = json.load(open(NOTE)) if os.path.exists(NOTE) else {}
    except Exception:
        old = {}
    if old.get("msg") == msg:
        return
    try:
        sys.path.insert(0, HERE)
        import azpi_publish
        azpi_publish.ntfy_send("AZPi: Home Base " + ("has a new button" if ok else "button not added"), msg,
                               tags="white_check_mark" if ok else "warning")
    except Exception:
        pass
    with open(NOTE, "w") as fh:
        json.dump({"msg": msg, "ok": ok}, fh)


def install(dry=False, log=print):
    if not (os.path.exists(PASSFLOW) and os.path.exists(PAGE)):
        return False, "Home Base not found on this Pi"
    pf = open(PASSFLOW).read()
    pg = open(PAGE).read()
    if MARK in pf and 'data-act="fix"' in pg:
        return True, "already in"
    new_pf, miss = apply(pf, passflow_edits()) if MARK not in pf else (pf, None)
    if miss:
        return False, f"Home Base's passflow.py has changed (couldn't find: {miss[:60]})"
    new_pg, miss = apply(pg, PAGE_EDITS) if 'data-act="fix"' not in pg else (pg, None)
    if miss:
        return False, f"Home Base's page has changed (couldn't find: {miss[:60]})"
    test = "/tmp/azpi_passflow_fixtest.py"
    with open(test, "w") as fh:
        fh.write(new_pf)
    try:
        py_compile.compile(test, doraise=True)
    except py_compile.PyCompileError as e:
        return False, f"the new Home Base code didn't check out ({e.msg[-80:]}), nothing changed"
    finally:
        os.remove(test)
    if dry:
        return True, "would add the button (dry run)"
    for path, text in ((PASSFLOW, new_pf), (PAGE, new_pg)):
        if not os.path.exists(path + ".bak-fixbutton"):
            shutil.copy2(path, path + ".bak-fixbutton")
        tmp = path + ".tmp-fixbutton"
        with open(tmp, "w") as fh:
            fh.write(text)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    r = subprocess.run(["sudo", "-n", "systemctl", "restart", "azpi-portal"], capture_output=True, text=True)
    if r.returncode:
        return True, "button added; it shows after the Pi's next restart (couldn't restart Home Base by itself)"
    return True, "button added and Home Base restarted"


def run(log=print, dry=False):
    ok, msg = install(dry=dry, log=log)
    if msg == "already in":
        return ok, msg
    log(f"[fix button] {msg}")
    if not dry:
        if ok:
            tell("Home Base now has a \"Fix image for publishing\" button on every pass, next to Rotate 180°. "
                 "It repairs the coloured and black lines, keeps the originals, and tapping it again "
                 "(\"Undo fix\") puts them back. " + msg[0].upper() + msg[1:] + ".", True)
        else:
            tell("The \"Fix image for publishing\" button couldn't be added to Home Base: " + msg +
                 ". Nothing was changed.", False)
    return ok, msg


if __name__ == "__main__":
    ok, msg = run(dry="--dry-run" in sys.argv)
    print(msg)
    sys.exit(0 if ok else 1)
