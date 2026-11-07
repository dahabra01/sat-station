
# ------------------------------------------------------------------ fix image for publishing (Nov 2026)
def fix_pictures(dest):
    """Home Base 'Fix image for publishing': repair the lines in every picture of a pass
    (tools/azpi_fix_image.py in the website copy). Pressing it again puts the originals back."""
    sys.path.insert(0, os.path.join(SITE_REPO, "tools"))
    import azpi_fix_image
    mp = os.path.join(dest, "pass.json")
    meta = jload(mp, {})
    fj = os.path.join(dest, "fix.json")
    if os.path.exists(fj):                                   # undo
        info = jload(fj, {})
        azpi_fix_image.undo_folder(dest, log=lambda *_: None)
        if bool(info.get("flipped")) != bool(meta.get("flipped")):
            # turned with Rotate 180° after the fix: turn the originals the same way
            from PIL import Image
            for f in glob.glob(os.path.join(dest, "products", "**", "*.png"), recursive=True):
                if "projected" in os.path.basename(f).lower():
                    continue
                try:
                    im = Image.open(f)
                    im.load()
                    im.rotate(180).save(f)
                except Exception:
                    pass
        meta = jload(mp, {})
        meta.pop("fixed", None)
        meta.pop("fixing", None)
        jsave(mp, meta)
        return "put the original pictures back"
    info = azpi_fix_image.fix_folder(dest, log=lambda *_: None)
    info["flipped"] = bool(meta.get("flipped"))
    jsave(fj, info)
    meta = jload(mp, {})
    meta["fixed"] = int(time.time())
    meta.pop("fixing", None)
    jsave(mp, meta)
    m = info.get("main") or {}
    return "fixed %d picture(s): %s%% rebuilt from the other channels, %s%% blended, %s%% left black" % (
        len(info.get("pictures", [])), m.get("rebuilt_pct", 0), m.get("blended_pct", 0), m.get("blacked_pct", 0))
