#!/usr/bin/env python3
"""
Adds to Home Base (Nov 2026):
  * the "Fix image for publishing" button on every pass card
  * an "On sat.dahabra.com now" list with a Take down button for every posted pass, however it
    was posted (phone question or Home Base); pass cards also recognise phone-posted passes

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


# ---------------------------------------------------------------- take down from Home Base (2nd feature)
SITE_MARK = "take down from Home Base (Nov 2026)"


def site_passflow_edits():
    with open(os.path.join(HERE, "fixbutton", "passflow_site.py")) as fh:
        fn = fh.read()
    return [
        ("# ------------------------------------------------------------------ 6 publish",
         fn.strip("\n") + "\n\n", "before"),
        ('on_site = bool(other) and other.get("id") == sid',
         'on_site = bool(other) and same_pass(other.get("id"), sid)', "replace"),
        ('r = _publisher("--take-down", meta.get("site_id") or site_id(meta))',
         'r = _publisher("--take-down", posted_id(meta))', "replace"),
        ('"home": at_home(), "results": results()}',
         '"home": at_home(), "results": results(), "site": site_list()}', "replace"),
        ('    if p in ("/api/passflow/publish", "/api/passflow/unpublish"):',
         '    if p == "/api/passflow/takedown":          # take any posted pass off the website\n'
         '        sid = str(body.get("id", ""))\n'
         '        if not re.fullmatch(r"METEOR-M2-[0-9x]-\\d{8}-\\d{6}", sid) or sid not in [e.get("id") for e in site_posts().values()]:\n'
         '            return 400, {"error": "That pass isn\'t on the website"}\n'
         '        if not at_home():\n'
         '            return 400, {"error": "Only on home Wi-Fi: the Pi never publishes over the phone\'s data"}\n'
         '        busy = jload(TAKEDOWN, {})\n'
         '        if busy.get(sid) and time.time() - busy[sid] < 900:\n'
         '            return 409, {"error": "Already taking that one down"}\n'
         '        busy[sid] = int(time.time())\n'
         '        jsave(TAKEDOWN, busy)\n'
         '        spawn("takedown", sid)\n'
         '        return 200, {"ok": True}\n', "before"),
        ('    if a[0] == "flip" and len(a) > 1:',
         '    if a[0] == "takedown" and len(a) > 1:\n'
         '        ok, msg = take_down(a[1])\n'
         '        st = jload(STATUS, {"log": []})\n'
         '        st.setdefault("log", []).append(time.strftime("%H:%M") + "  %s: %s" % (a[1], msg))\n'
         '        jsave(STATUS, st)\n'
         '        print(msg)\n'
         '        sys.exit(0 if ok else 1)\n', "before"),
    ]


SITE_PAGE_EDITS = [
    ('  <section style="display:flex;flex-direction:column;gap:10px">\n    <h2>Passes</h2>',
     '  <section style="display:flex;flex-direction:column;gap:10px">\n'
     '    <style>.sites{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px}'
     '.site{display:grid;grid-template-columns:64px 1fr;gap:8px 10px;align-items:center;background:var(--card);border:1px solid #2c3a48;border-radius:12px;padding:8px}'
     '.site img{width:64px;height:64px;object-fit:cover;border-radius:8px;background:#0b1520}'
     '.site .t{min-width:0;font-size:14px}.site .btn{grid-column:1/-1}</style>\n'
     '    <h2>On sat.dahabra.com now</h2>\n'
     '    <div class="muted" style="font-size:13px">Every pass on the website, however it got there. Take one down, fix it on its card below, then Publish it again if you like it.</div>\n'
     '    <div class="sites" id="onsite"><div class="muted">Loading…</div></div>\n'
     '  </section>\n', "before"),
    ("  $('log').textContent=",
     "  const ons=S.site||[];$('onsite').innerHTML=ons.length?ons.map(siteCard).join(''):'<div class=\"muted\">Nothing on the website right now.</div>';\n",
     "before"),
    ("async function saveRules(){",
     "function siteCard(e){\n"
     "  const b=e.busy?`<button class=\"btn\" disabled>Taking down…</button>`:!S.home?`<button class=\"btn\" disabled title=\"Only on home Wi-Fi\">Home Wi-Fi only</button>`:\n"
     "    `<button class=\"btn red\" data-take=\"${esc(e.id)}\" data-when=\"${esc(e.day+' '+e.start_local)}\">Take down</button>`;\n"
     "  return `<div class=\"site\"><img src=\"${esc(e.thumb)}\" alt=\"\" loading=\"lazy\"><div class=\"t\"><b>Meteor ${esc(e.sat)}</b><br>${esc(new Date(e.day+'T12:00:00').toLocaleDateString('en-GB',{day:'numeric',month:'short'}))} · ${esc(e.start_local)}\n"
     "    <div class=\"muted\" style=\"font-size:12px\">${e.fixed?'Fixed · ':''}${e.home_base?'from Home Base':'from the phone question'}</div></div>${b}</div>`;\n"
     "}\n"
     "$('onsite').onclick=async e=>{const b=e.target.closest('[data-take]');if(!b)return;\n"
     "  if(!b.classList.contains('sure')){clearTimeout(timer);b.classList.add('sure');b.textContent='Tap again to take it down';timer=setTimeout(load,6000);return}\n"
     "  b.disabled=true;b.textContent='Taking down…';\n"
     "  try{await post('/api/passflow/takedown',{id:b.dataset.take});setTimeout(load,4000);setTimeout(load,30000)}catch(err){msg(err.message);load()}};\n",
     "before"),
]


def apply(text, edits):
    for anchor, new, where in edits:
        if text.count(anchor) != 1:
            return None, anchor
        if where == "replace":
            text = text.replace(anchor, new)
        else:
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
    features = [("Fix image for publishing", MARK, 'data-act="fix"', passflow_edits, PAGE_EDITS),
                ("take down from the website", SITE_MARK, 'id="onsite"', site_passflow_edits, SITE_PAGE_EDITS)]
    added = []
    new_pf, new_pg = pf, pg
    for name, pf_mark, pg_mark, pf_edits, pg_edits in features:
        if pf_mark in new_pf and pg_mark in new_pg:
            continue
        a, miss = apply(new_pf, pf_edits()) if pf_mark not in new_pf else (new_pf, None)
        if miss:
            return False, f"Home Base's passflow.py has changed (couldn't find: {miss[:60]})"
        b, miss = apply(new_pg, pg_edits) if pg_mark not in new_pg else (new_pg, None)
        if miss:
            return False, f"Home Base's page has changed (couldn't find: {miss[:60]})"
        new_pf, new_pg = a, b
        added.append(name)
    if not added:
        return True, "already in"
    test = "/tmp/azpi_passflow_fixtest.py"
    with open(test, "w") as fh:
        fh.write(new_pf)
    try:
        py_compile.compile(test, doraise=True)
    except py_compile.PyCompileError as e:
        return False, f"the new Home Base code didn't check out ({e.msg[-80:]}), nothing changed"
    finally:
        os.remove(test)
    what = " and ".join(added)
    if dry:
        return True, f"would add: {what} (dry run)"
    tag = ".bak-fixbutton" if "Fix image for publishing" in added else ".bak-takedown"
    for path, text, old_text in ((PASSFLOW, new_pf, pf), (PAGE, new_pg, pg)):
        if text == old_text:
            continue
        if not os.path.exists(path + tag):
            shutil.copy2(path, path + tag)
        tmp = path + ".tmp-fixbutton"
        with open(tmp, "w") as fh:
            fh.write(text)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    r = subprocess.run(["sudo", "-n", "systemctl", "restart", "azpi-portal"], capture_output=True, text=True)
    if r.returncode:
        return True, f"added {what}; it shows after the Pi's next restart (couldn't restart Home Base by itself)"
    return True, f"added {what} and restarted Home Base"


def run(log=print, dry=False):
    ok, msg = install(dry=dry, log=log)
    if msg == "already in":
        return ok, msg
    log(f"[Home Base update] {msg}")
    if not dry:
        if ok:
            tell("Home Base update: " + msg + ". \"Fix image for publishing\" is on every pass card, and the new "
                 "\"On sat.dahabra.com now\" list at the top lets you take any posted pass down. "
                 "Refresh the Home Base page to see it.", True)
        else:
            tell("A Home Base update couldn't be added: " + msg + ". Nothing was changed.", False)
    return ok, msg


if __name__ == "__main__":
    ok, msg = run(dry="--dry-run" in sys.argv)
    print(msg)
    sys.exit(0 if ok else 1)
