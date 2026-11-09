
# ------------------------------------------------------------------ fix older passes (Nov 2026)
def folder_for(sid):
    """The Home Base pass folder holding SatDump's original pictures of a posted pass, if any."""
    for dest in glob.glob(os.path.join(PASSES, "*")):
        meta = jload(os.path.join(dest, "pass.json"), {})
        if meta.get("start") and same_pass(meta.get("site_id") or site_id(meta), sid):
            if pick(dest, "msu_mr_rgb_221_corrected.png", "msu_mr_rgb_221.png", "MSU-MR-1.png"):
                return {"folder": os.path.basename(dest), "fixed_folder": bool(meta.get("fixed")),
                        "fixing": bool(meta.get("fixing")) and time.time() - meta["fixing"] < 1800}
    return {"folder": None, "fixed_folder": False, "fixing": False}

def on_site_now(meta):
    """Is this pass folder's pass on the website right now (however it got there)?"""
    if not meta.get("start"):
        return False
    day = time.strftime("%Y-%m-%d", time.gmtime(int(meta["start"]) + 4 * 3600))
    other = site_posts().get(day)
    return bool(other) and same_pass(other.get("id"), meta.get("site_id") or site_id(meta))
