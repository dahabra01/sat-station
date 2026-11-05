#!/usr/bin/env python3
"""
Draws country borders and city names onto pass pictures that were posted without them.

New passes get their borders and names from SatDump itself (see azpi_satdump_overlays.py).
This is for the ones posted before that was switched on: it works out where every pixel of
the picture sits on the Earth (same orbit + coastline fit as the Footprint map) and draws
the borders and names from footprint/overlay.json onto every picture type of the pass.

Before drawing it keeps a clean copy of the natural-colour picture (plain_221.jpg) so the
Footprint map can still be stitched from a picture with nothing drawn on it.

Usage: azpi_label_pictures.py [--site DIR] [--tle FILE] [--force] [--only ID] [--dry-run]
"""
import json
import os
import shutil
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import azpi_footprint as F  # noqa: E402

ARGS = sys.argv[1:]
FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
         "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"]
BORDER = np.array([232, 180, 92], float)      # the site's sand colour
RES = 0.02                                    # degrees per cell of the border drawing
JPEG_Q = 76


def arg(flag):
    return ARGS[ARGS.index(flag) + 1] if flag in ARGS and ARGS.index(flag) + 1 < len(ARGS) else None


def row_map(ref, img):
    """Picture types of one pass come out a little different in height. Find
    row_in_221 = a * row + b by lining up the brightness pattern down the picture."""
    from scipy.ndimage import uniform_filter1d

    def prof(a):
        g = a.astype(float).mean(2)
        P = np.stack([g[:, c].mean(1) for c in np.array_split(np.arange(g.shape[1]), 8)], 1)
        P = P - uniform_filter1d(P, 31, axis=0)
        return P / (P.std(0) + 1e-6)

    R, I = prof(ref), prof(img)
    ri = np.arange(len(I))
    best = (-9.0, 1.0, 0.0)
    for a in np.arange(0.975, 1.0251, 0.0015):
        for b in np.arange(-30, 30.1, 1.0):
            m = a * ri + b
            ok = (m >= 0) & (m <= len(R) - 1)
            if ok.sum() < len(I) * 0.6:
                continue
            c = float((R[np.round(m[ok]).astype(int)] * I[ok]).mean())
            if c > best[0]:
                best = (c, a, b)
    return best[1], best[2], best[0]


def border_raster(overlay, lat, lon):
    """Borders drawn onto a lat/lon grid covering the picture."""
    la0, la1 = float(np.nanmin(lat)) - 1, float(np.nanmax(lat)) + 1
    lo0, lo1 = float(np.nanmin(lon)) - 1, float(np.nanmax(lon)) + 1
    w, h = int((lo1 - lo0) / RES) + 1, int((la1 - la0) / RES) + 1
    im = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(im)
    for line in overlay["borders"]:
        pts = [((x - lo0) / RES, (la1 - y) / RES) for x, y in line]
        d.line(pts, fill=255, width=2)
    return np.asarray(im) > 0, (la1, lo0)


def label(entry, overlay, tles, site, dry):
    folder = os.path.join(site, "captures", entry["id"])
    src221 = next((i for i in entry["images"] if i["kind"] == "221" and i["proj"] == "spread"), None)
    if not src221:
        return "no plain 221 picture"
    plain_rel = entry.get("plain") or f"captures/{entry['id']}/plain_221.jpg"
    plain = os.path.join(site, plain_rel)
    clean_src = plain if os.path.exists(plain) else os.path.join(site, src221["file"])
    ref = np.asarray(Image.open(clean_src).convert("RGB"))
    W = ref.shape[1]
    band = int(round(W * 0.055))                      # watermark along the bottom: never draw there
    res, why = F.locate(entry, ref[:-band], tles)
    if res is None:
        return why
    sat, step, direction, t_top, placed = res
    if not placed:
        return "couldn't line the picture up with the coastline"
    times = F.row_times(len(ref), step, direction, t_top, placed)
    u = (np.arange(W) + 0.5) / W * 2 - 1
    lat, lon = F.grid_latlon(sat, times, u, F.HALF_SWATH_KM)
    mask, (la1, lo0) = border_raster(overlay, lat, lon)
    bad221 = F.bad_rows(ref[:-band])

    try:
        font = ImageFont.truetype(next(f for f in FONTS if os.path.exists(f)), max(11, round(W * 0.017)))
    except StopIteration:
        font = ImageFont.load_default()

    if dry:
        return f"would label {len(entry['images'])} pictures"
    if not os.path.exists(plain):
        shutil.copyfile(os.path.join(site, src221["file"]), plain)
    entry["plain"] = plain_rel

    for pic in entry["images"]:
        if pic["proj"] != "spread":
            continue
        if entry.get("overlays") and pic["kind"] != "221":
            continue                                      # already labelled; only 221 has a clean copy to redo from
        path = os.path.join(site, pic["file"])
        img = ref if pic["kind"] == "221" else np.asarray(Image.open(path).convert("RGB"))
        Hp = img.shape[0]
        bandp = int(round(img.shape[1] * 0.055))
        a, b = (1.0, 0.0) if pic["kind"] == "221" else row_map(ref[:-band], img[:-bandp])[:2]
        r221 = np.clip(np.round(a * np.arange(Hp) + b).astype(int), 0, len(ref) - 1)
        La, Lo = lat[r221], lon[r221]
        out = img.astype(float).copy()
        gy = np.clip(((la1 - La) / RES).astype(int), 0, mask.shape[0] - 1)
        gx = np.clip(((Lo - lo0) / RES).astype(int), 0, mask.shape[1] - 1)
        on = mask[gy, gx]
        dark = img.astype(float).mean(2) < 8                  # leave blank gaps blank
        on &= ~dark
        on[Hp - bandp:] = False
        out[on] = out[on] * 0.15 + BORDER * 0.85
        im = Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))

        # city names: most important first, skipping any that would overlap
        d = ImageDraw.Draw(im)
        taken = []
        for c in overlay["cities"]:
            dist = (La - c["lat"]) ** 2 + ((Lo - c["lon"]) * np.cos(np.radians(c["lat"]))) ** 2
            r, k = np.unravel_index(np.argmin(dist), dist.shape)
            if dist[r, k] > 0.06 ** 2 or r >= Hp - bandp - 14 or r < 6 or k < 4 or k > img.shape[1] - 4:
                continue                                       # not in this picture
            if dark[max(0, r - 2):r + 3, max(0, k - 2):k + 3].any():
                continue
            box = d.textbbox((0, 0), c["name"], font=font, stroke_width=2)
            tw, th = box[2] - box[0], box[3] - box[1]
            x = k + 6 if k + 6 + tw < img.shape[1] - 2 else k - 6 - tw
            y = r - th // 2 - 2
            rect = (min(x, k - 4), y - 2, max(x + tw, k + 4), y + th + 4)
            if any(rect[0] < q[2] and q[0] < rect[2] and rect[1] < q[3] and q[1] < rect[3] for q in taken):
                continue
            if c["tier"] > 1 and len(taken) > 14:
                continue
            taken.append(rect)
            d.ellipse((k - 3, r - 3, k + 3, r + 3), fill=(255, 255, 255), outline=(0, 0, 0), width=1)
            d.text((x, y), c["name"], font=font, fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0))
        im.save(path, "JPEG", quality=JPEG_Q, optimize=True, progressive=True)
        if pic["kind"] == "221" and entry.get("thumb"):
            t = im.copy()
            t = t.resize((480, round(t.height * 480 / t.width)), Image.LANCZOS)
            t.save(os.path.join(site, entry["thumb"]), "JPEG", quality=JPEG_Q, optimize=True, progressive=True)
    entry["overlays"] = True
    return None


def main():
    site = arg("--site") or F.SITE
    F.SITE = site
    if arg("--tle"):
        F.TLE_CANDIDATES.insert(0, arg("--tle"))
    dry = "--dry-run" in ARGS
    man_path = os.path.join(site, "captures", "captures.json")
    manifest = json.load(open(man_path))
    overlay = json.load(open(os.path.join(site, "footprint", "overlay.json")))
    tles = F.load_tles()
    changed = []
    for e in manifest["passes"]:
        if arg("--only") and e["id"] != arg("--only"):
            continue
        if e.get("from") != "home-base":
            print(f"{e['id']}: skipped, its pictures already have borders and names from the old setup")
            continue
        if e.get("overlays") and "--force" not in ARGS:
            print(f"{e['id']}: already has borders and names")
            continue
        why = label(e, overlay, tles, site, dry)
        print(f"{e['id']}: " + (why or "borders and names added"))
        if not why:
            changed.append(e["id"])
    if changed and not dry:
        with open(man_path, "w") as fh:
            json.dump(manifest, fh, indent=1)
    return changed


if __name__ == "__main__":
    main()
