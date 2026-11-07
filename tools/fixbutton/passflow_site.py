
# ------------------------------------------------------------------ take down from Home Base (Nov 2026)
TAKEDOWN = os.path.join(HERE, "takedown_busy.json")

def same_pass(a, b):
    """Two website ids for the same pass? The phone-posted ones are named from the picture file,
    Home Base ones from the recording start, so they can differ by a few seconds."""
    if not a or not b:
        return False
    if a == b:
        return True
    try:
        if a[:-15] != b[:-15]:
            return False
        ta = calendar.timegm(time.strptime(a[-15:], "%Y%m%d-%H%M%S"))
        tb = calendar.timegm(time.strptime(b[-15:], "%Y%m%d-%H%M%S"))
        return abs(ta - tb) < 180
    except Exception:
        return False

def posted_id(meta):
    """The id the website uses for this pass folder's pass (falls back to the folder's own)."""
    sid = meta.get("site_id") or site_id(meta)
    day = time.strftime("%Y-%m-%d", time.gmtime(int(meta.get("start") or 0) + 4 * 3600))
    other = site_posts().get(day)
    return other["id"] if other and same_pass(other.get("id"), sid) else sid

def site_list():
    """Everything on sat.dahabra.com now, newest first, for the 'On the website' list."""
    busy = jload(TAKEDOWN, {})
    out = []
    for day, e in sorted(site_posts().items(), reverse=True):
        t = busy.get(e.get("id"))
        out.append({"id": e.get("id"), "sat": (e.get("sat") or "").replace("Meteor-M N", "M"), "day": day,
                     "start_local": e.get("start_local"), "fixed": bool(e.get("fixed")),
                     "home_base": e.get("from") == "home-base",
                     "thumb": "https://sat.dahabra.com/" + (e.get("thumb") or ""),
                     "busy": bool(t and time.time() - t < 900)})
    return out

def take_down(sid):
    busy = jload(TAKEDOWN, {})
    busy[sid] = int(time.time())
    jsave(TAKEDOWN, busy)
    try:
        r = _publisher("--take-down", sid)
    finally:
        busy = jload(TAKEDOWN, {})
        busy.pop(sid, None)
        jsave(TAKEDOWN, busy)
    if r.get("ok"):
        for dest in glob.glob(os.path.join(PASSES, "*")):        # its pass folder no longer shows as published
            meta = jload(os.path.join(dest, "pass.json"), {})
            if meta.get("start") and same_pass(meta.get("site_id") or site_id(meta), sid):
                _mark(os.path.basename(dest), published=None, site_url=None, taken_down=int(time.time()),
                      pub_msg=None, pub_error=None)
    return bool(r.get("ok")), r.get("msg", "")
