#!/usr/bin/env python3
"""
AZPi footprint stitcher -> the Footprint page on sat.dahabra.com.

Takes every Meteor-M2 3 / M2 4 pass that has been posted to the website,
works out exactly where each picture sits on the Earth, and lays them all
into one big map of everything the station has seen.

How a picture gets pinned to the map:
  * The satellite's orbit (TLE) says where it was over the ground every second.
  * A picture row is one moment of the pass; a column is a distance left or
    right of the satellite's path. The exact start time of each clean stretch
    of picture is fine-tuned by lining its coastlines up with a real coastline
    map, so dropped lines in the middle of a pass don't throw it off.
  * Stretches that can't be lined up with confidence (too cloudy, too short)
    are left out rather than guessed.

Each pass gets a rating out of 100:
  signal 30 (peak SNR) + clean picture 40 (share of rows without noise or
  gaps) + length 20 (how long it was locked) + height 10 (max elevation).

THE RULE for the footprint, decided by the Captain:
  Where ground is already covered, a pass only takes over if its rating is
  the SAME OR HIGHER than the pass already there. Same rating: the newer one
  wins, so the map stays fresh. Lower rating: the old one stays.
  Ground nobody has covered yet is filled by any pass.

Each pass is turned into a map "layer" once and kept on the Pi
(~/azpi_footprint/layers), so the map can be rebuilt any time, keeps old
passes after the Captures page trims them for space, and drops a pass that
was taken down.

Writes into the website copy:
  footprint/mosaic.jpg    the stitched map (what visitors see)
  footprint/owners.json   which pass every map pixel came from (for clicks/filters)
  footprint/base.png      dark land/sea map for ground not covered yet
  footprint/thumbs/*.jpg  small picture of each pass in the map
  footprint/footprint.json  the passes, their ratings and the map's corners

Usage (normally called by azpi_publish.py before each push):
  azpi_footprint.py [--site DIR] [--rebuild] [--remove ID] [--tle FILE] [--compose]
  (--compose: redraw the map from the stored layers even if nothing new came in)
"""
import datetime as dt
import json
import os
import shutil
import sys
import urllib.request

import numpy as np
from PIL import Image

# ---------------------------------------------------------------- settings
SITE = os.environ.get("AZPI_SITE", os.path.expanduser("~/sat-station-site"))
WORK = os.environ.get("AZPI_FOOTPRINT_DIR", os.path.expanduser("~/azpi_footprint"))
TLE_CANDIDATES = [os.environ.get("AZPI_TLE", ""),
                  os.path.expanduser("~/raspberry-noaa-v2/tmp/orbit.tle"),
                  os.path.expanduser("~/raspberry-noaa-v2/tmp/weather.tle"),
                  os.path.join(WORK, "meteor.tle")]
TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP=weather&FORMAT=tle"
SAT_NAMES = {"M2-3": "METEOR-M2 3", "M2-4": "METEOR-M2 4"}

# map window and resolution (plate carree, x squeezed by cos 25 deg so shapes look right)
LON0, LON1, LAT0, LAT1 = 25.0, 92.0, -6.0, 54.0
DLAT = 0.035
DLON = DLAT / np.cos(np.radians(25.0))
MAP_W = int(round((LON1 - LON0) / DLON))
MAP_H = int(round((LAT1 - LAT0) / DLAT))

KIND = "221"                 # natural-colour picture used for the map
STEP_800 = 0.545             # seconds per picture row at 800 px wide (fitted on real passes)
HALF_SWATH_KM = 1430.0       # half the width of a 'spread' picture on the ground
MIN_FIT = 0.80               # how well a stretch must line up with the coastline map to be used
MIN_ROWS = 24                # shortest stretch of clean rows worth placing
RE = 6371.0
E2 = 0.00669438
DUBAI = dt.timezone(dt.timedelta(hours=4))
VERSION = 7                  # bump to rebuild every layer after a change to the method

ARGS = sys.argv[1:]
DIRTY = False          # set when a layer was added outside update(); the next update() redraws the map


def arg_value(flag):
    return ARGS[ARGS.index(flag) + 1] if flag in ARGS and ARGS.index(flag) + 1 < len(ARGS) else None


def say(msg):
    print(dt.datetime.now(DUBAI).strftime("%d-%m %H:%M"), "footprint:", msg, flush=True)


# ---------------------------------------------------------------- orbit
def load_tles():
    from sgp4.api import Satrec
    path = next((p for p in TLE_CANDIDATES if p and os.path.isfile(p)), None)
    text = open(path).read() if path else ""
    if "METEOR-M2 3" not in text or "METEOR-M2 4" not in text:
        try:
            with urllib.request.urlopen(TLE_URL, timeout=30) as r:
                text = r.read().decode()
            os.makedirs(WORK, exist_ok=True)
            with open(os.path.join(WORK, "meteor.tle"), "w") as fh:
                fh.write(text)
        except Exception as e:
            say(f"couldn't download orbit data ({e.__class__.__name__})")
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    out = {}
    for i in range(len(lines) - 2):
        if lines[i + 1].startswith("1 ") and lines[i + 2].startswith("2 "):
            out[lines[i].strip()] = Satrec.twoline2rv(lines[i + 1], lines[i + 2])
    return out


def ecef_unit(sat, t_unix):
    """Unit vectors of the point under the satellite, Earth-fixed, for unix times."""
    t_unix = np.atleast_1d(np.asarray(t_unix, float))
    jd = 2440587.5 + t_unix / 86400.0
    jd0 = np.floor(jd)
    _, r, _ = sat.sgp4_array(jd0, jd - jd0)
    r = np.asarray(r)
    d = jd - 2451545.0
    T = d / 36525
    g = np.radians((280.46061837 + 360.98564736629 * d + 0.000387933 * T * T) % 360)
    x = r[:, 0] * np.cos(g) + r[:, 1] * np.sin(g)
    y = -r[:, 0] * np.sin(g) + r[:, 1] * np.cos(g)
    p = np.stack([x, y, r[:, 2]], 1)
    return p / np.linalg.norm(p, axis=1, keepdims=True)


def to_latlon(p):
    lat_gc = np.arctan2(p[..., 2], np.hypot(p[..., 0], p[..., 1]))
    lat = np.degrees(np.arctan(np.tan(lat_gc) / (1 - E2)))
    lon = np.degrees(np.arctan2(p[..., 1], p[..., 0]))
    return lat, (lon + 180) % 360 - 180


def grid_latlon(sat, row_times, u, half_km, off_km=0.0):
    """lat/lon of every (row, column). u runs -1 (west edge) .. +1 (east edge).
    off_km shifts the whole swath sideways (the satellite flies leaning slightly to one side)."""
    P = ecef_unit(sat, row_times)
    V = ecef_unit(sat, row_times + 1.0) - ecef_unit(sat, row_times - 1.0)
    E = np.cross(V, P)
    E /= np.linalg.norm(E, axis=1, keepdims=True)
    east = np.stack([-P[:, 1], P[:, 0], np.zeros(len(P))], 1)
    E *= np.sign(np.sum(E * east, 1))[:, None]       # pictures are north-up, so columns run west->east
    a = ((np.asarray(u, float) * half_km + off_km) / RE)[None, :, None]
    return to_latlon(P[:, None, :] * np.cos(a) + E[:, None, :] * np.sin(a))


# ---------------------------------------------------------------- reading a picture
def land_mask(lat, lon):
    from global_land_mask import globe
    return globe.is_land(lat, lon)


def inland_water(lat, lon):
    """Caspian, Aral and Lake Urmia: water, but the coastline map counts them as land."""
    return (((lat > 36.5) & (lat < 47.5) & (lon > 46.5) & (lon < 55.5)) |
            ((lat > 43) & (lat < 47) & (lon > 58) & (lon < 62)) |
            ((lat > 37) & (lat < 38.5) & (lon > 44.9) & (lon < 46.1)))


def sea_or_land(rgb):
    """+1 land, -1 sea, 0 unsure (clouds, blanks). Sea is the darker, bluer of two colour
    groups, found fresh for each picture because colour balance changes pass to pass."""
    f = rgb.astype(float)
    R, G, B = f[..., 0], f[..., 1], f[..., 2]
    lum = (R + G + B) / 3
    ok = (lum > 6) & (lum < 190)
    out = np.zeros(R.shape, np.int8)
    if ok.sum() < 200:
        return out
    X = np.stack([R[ok] / np.maximum(B[ok], 1), lum[ok] / 100.0], 1)
    sub = X[:: max(1, len(X) // 20000)]
    c = np.array([np.percentile(sub, 10, 0), np.percentile(sub, 75, 0)])
    for _ in range(25):
        k = ((sub[:, None, :] - c[None]) ** 2).sum(2).argmin(1)
        for j in range(2):
            if (k == j).any():
                c[j] = sub[k == j].mean(0)
    sea_c, land_c = (c[0], c[1]) if c[0].sum() < c[1].sum() else (c[1], c[0])
    ds, dl = ((X - sea_c) ** 2).sum(1), ((X - land_c) ** 2).sum(1)
    lab = np.zeros(len(X), np.int8)
    lab[ds < 0.6 * dl] = -1
    lab[dl < 0.6 * ds] = 1
    out[ok] = lab
    return out


def fake_colour(rgb):
    """Pure yellow / pure blue pixels: decoder marker lines, never real weather."""
    f = rgb.astype(float)
    R, G, B = f[..., 0], f[..., 1], f[..., 2]
    yellow = (R > 110) & (G > 110) & (B < 0.45 * np.minimum(R, G))
    blue = (B > 140) & (R < 70) & (G < 70)
    return yellow | blue


def bad_rows(rgb):
    """Rows that are blank, noise or decoding glitches (including part-width coloured lines)."""
    f = rgb.astype(float)
    m = f.mean(axis=(1, 2))
    bad = m < 18
    up = np.abs(f[1:-1] - f[:-2]).max(2)
    dn = np.abs(f[1:-1] - f[2:]).max(2)
    odd = np.zeros(len(m))
    odd[1:-1] = ((up > 60) & (dn > 60)).mean(1)       # share of the row unlike both neighbours
    bad |= odd > 0.03
    colour = (f.max(2) - f.min(2)).mean(1)
    bad |= (colour > 60) & (m < 60)
    bad |= fake_colour(rgb).mean(1) > 0.01
    w = bad.copy()
    for k in (1, 2):
        w[k:] |= bad[:-k]
        w[:-k] |= bad[k:]
    return w


def stretches(bad):
    out, start = [], None
    for i, b in enumerate(list(bad) + [True]):
        if not b and start is None:
            start = i
        elif b and start is not None:
            if i - start >= MIN_ROWS:
                out.append((start, i))
            start = None
    return out


def has_map_lines(entry):
    """Pictures made by raspberry-noaa-v2 itself come with borders and city names drawn on,
    which can't go into the map. Home Base passes are satdump's clean pictures."""
    if os.environ.get("AZPI_FOOTPRINT_ALLOW_ALL") == "1":
        return False
    return entry.get("from") != "home-base"


def fit_score(sat, obs, rows, t_top, step, direction, hw=None, off=0.0, band=False):
    """Agreement (0.5 = chance, 1 = perfect) between the picture's land/sea and the coastline map.
    band=True scores only a strip either side of the coastline: much more sensitive to shifts of
    a few km, used to fine-tune each pass's geometry."""
    cols = obs.shape[1]
    u = (np.arange(cols) + 0.5) / cols * 2 - 1
    lat, lon = grid_latlon(sat, t_top + direction * rows * step, u, hw or HALF_SWATH_KM, off)
    pred = land_mask(lat, lon)
    o = obs[rows]
    m = (o != 0) & ~inland_water(lat, lon)
    if band:
        from scipy.ndimage import binary_dilation
        edge = np.zeros_like(pred)
        edge[1:] |= pred[1:] != pred[:-1]
        edge[:, 1:] |= pred[:, 1:] != pred[:, :-1]
        m &= binary_dilation(edge, iterations=4)
    sea, land = m & (o < 0), m & (o > 0)
    if sea.sum() < 25 or land.sum() < 25:
        return -1.0
    return float(0.5 * ((~pred[sea]).mean() + pred[land].mean()))


def place_stretch(sat, obs, r0, r1, t_guess, step, direction, span, hw=None, off=0.0):
    """Find the time of a stretch's first row by lining its coasts up with the map."""
    D = 3
    o = obs[r0:r1:D, ::D]
    rows = np.arange(o.shape[0])
    best = (-1.0, t_guess)
    for t in np.arange(t_guess - span, t_guess + span + 0.1, 4.0):
        s = fit_score(sat, o, rows, t, step * D, direction, hw, off)
        if s > best[0]:
            best = (s, t)
    for t in np.arange(best[1] - 4, best[1] + 4.1, 0.5):
        s = fit_score(sat, o, rows, t, step * D, direction, hw, off)
        if s > best[0]:
            best = (s, t)
    return best


def lock_window(entry):
    """First and last moment of signal lock, from the station log (10 s steps)."""
    tl = entry.get("timeline") or []
    t = dt.datetime.fromisoformat(entry["start_utc"]).timestamp()
    first = last = None
    for locked, secs in tl:
        if locked and first is None:
            first = t
        t += secs
        if locked:
            last = t
    return (first, last) if first is not None else None


# ---------------------------------------------------------------- rating
def rating(entry, clean_share):
    def clamp(x):
        return max(0.0, min(1.0, x))
    parts = {
        "signal": 30 * clamp(((entry.get("peak_snr") or 0) - 4) / 12),
        "clean": 40 * clamp(clean_share),
        "length": 20 * clamp((entry.get("lock_s") or 0) / 720),
        "height": 10 * clamp(((entry.get("max_elev") or 0) - 15) / 45),
    }
    parts = {k: int(round(v)) for k, v in parts.items()}
    return sum(parts.values()), parts


# ---------------------------------------------------------------- making a layer
def binary_closing_vertical(mask, gap):
    """Mark pixels with the mask both within `gap` pixels above and below."""
    up = np.zeros_like(mask)
    dn = np.zeros_like(mask)
    for k in range(1, gap + 1):
        up[k:] |= mask[:-k]
        dn[:-k] |= mask[k:]
    return mask | (up & dn)


def map_xy(lat, lon):
    return (lon - LON0) / DLON, (LAT1 - lat) / DLAT


def source_picture(entry):
    """The clean (no borders or names drawn on) natural-colour picture of a pass, or None."""
    if entry.get("plain"):                             # clean copy kept beside a labelled picture
        return entry["plain"]
    if has_map_lines(entry) or entry.get("overlays"):
        return None
    pic = next((i for i in entry.get("images", []) if i["kind"] == KIND and i["proj"] == "spread"), None)
    return pic["file"] if pic else None


def locate(entry, rgb, tles):
    """Work out when every row of a pass picture was taken.
    Returns (sat, step, direction, t_top, placed) where placed = [(row0, row1, time_of_row0, fit)],
    or (None, reason)."""
    W = rgb.shape[1]
    code = "M2-3" if "M2-3" in entry["id"] else "M2-4"
    sat = tles.get(SAT_NAMES[code])
    if sat is None:
        return None, "no orbit data"
    bad = bad_rows(rgb)
    obs = sea_or_land(rgb)
    obs[bad] = 0
    step = STEP_800 * 800 / W
    north = entry.get("direction") == "northbound"
    direction = -1 if north else 1                     # north-up picture: a northbound pass ends at the top
    t_start = dt.datetime.fromisoformat(entry["start_utc"]).timestamp()
    t_end = dt.datetime.fromisoformat(entry["pass_end"]).timestamp() if entry.get("pass_end") else t_start + 900
    H = len(rgb)

    # When was the top row taken? The station log says when the signal locked and
    # unlocked; satdump keeps time across gaps by leaving blank rows.
    win = lock_window(entry)
    if win:
        t_top = win[1] if north else win[0]
    else:                                              # no log: find it from the coastlines alone
        D = 4
        o = obs[::D, ::D]
        rows = np.arange(o.shape[0])
        lo, hi = sorted([t_start - 60 + (H * step if north else 0), t_end + 60 - (0 if north else H * step)])
        best = (-1.0, lo)
        for t in np.arange(lo, max(hi, lo + 1), 5.0):
            s = fit_score(sat, o, rows, t, step * D, direction)
            if s > best[0]:
                best = (s, t)
        t_top = best[1]

    # The picture's scale (seconds per row), its width on the ground and a small sideways lean
    # differ from pass to pass; a few percent off moves coasts by tens of km. Fit them on the
    # longest stretch, then use them for the whole pass.
    hw, off = HALF_SWATH_KM, 0.0
    runs = sorted(stretches(bad), key=lambda r: r[1] - r[0], reverse=True)
    if runs and runs[0][1] - runs[0][0] >= 120:
        r0, r1 = runs[0]
        D = 2
        o = obs[r0:r1:D, ::D]
        rows = np.arange(o.shape[0])
        mid = (r1 - r0) / 2
        # where the stretch sits now: the log's timing, nudged by a quick coastline fit
        t_start = place_stretch(sat, obs, r0, r1, t_top + direction * r0 * step, step, direction,
                                span=16 if win else 40)[1]
        t_mid = t_start + direction * mid * step                          # time of the stretch's middle row

        def score(st, dt_, w, f):
            return fit_score(sat, o, rows, t_mid + dt_ - direction * mid * st, st * D, direction, w, f, band=True)

        base = score(step, 0.0, hw, off)
        g = (base, step, 0.0, hw, off)
        for st in np.arange(step * 0.94, step * 1.0601, step * 0.01):        # scale and timing
            for dt_ in np.arange(-12, 12.1, 3.0):
                sc = score(st, dt_, hw, off)
                if sc > g[0]:
                    g = (sc, st, dt_, hw, off)
        for _ in range(2):                                                    # width and sideways lean
            for w in np.arange(g[3] - 60, g[3] + 61, 15):
                for f in np.arange(g[4] - 45, g[4] + 46, 7.5):
                    for dt_ in (g[2] - 2, g[2], g[2] + 2):
                        sc = score(g[1], dt_, w, f)
                        if sc > g[0]:
                            g = (sc, g[1], dt_, w, f)
            for st in np.arange(g[1] * 0.99, g[1] * 1.0101, g[1] * 0.0025):  # fine scale
                for dt_ in np.arange(g[2] - 2, g[2] + 2.1, 1.0):
                    sc = score(st, dt_, g[3], g[4])
                    if sc > g[0]:
                        g = (sc, st, dt_, g[3], g[4])
        if g[0] - base > 0.01:
            step, hw, off = g[1], g[3], g[4]
            t_top = t_mid + g[2] - direction * (r0 + mid) * step         # re-anchor the top row

    placed = []
    for r0, r1 in stretches(bad):
        t_pred = t_top + direction * r0 * step
        D = 3
        o = obs[r0:r1:D, ::D]
        rows = np.arange(o.shape[0])
        s_pred = fit_score(sat, o, rows, t_pred, step * D, direction, hw, off)
        s, t0 = place_stretch(sat, obs, r0, r1, t_pred, step, direction, span=16 if win else 40, hw=hw, off=off)
        if s >= MIN_FIT and s - s_pred > 0.01:
            placed.append((r0, r1, t0, s))             # coastlines line up: use the fine-tuned time
        elif win and (s_pred >= 0.5 or s_pred < 0):
            placed.append((r0, r1, t_pred, s_pred))    # too cloudy to check: trust the log's timing
        elif s >= MIN_FIT:
            placed.append((r0, r1, t0, s))
    return (sat, step, direction, t_top, placed, hw, off), None


def row_times(H, step, direction, t_top, placed):
    """Time of every picture row: fine-tuned where a stretch was placed, the log's timing elsewhere."""
    t = t_top + direction * np.arange(H) * step
    for r0, r1, t0, _ in placed:
        t[r0:r1] = t0 + direction * (np.arange(r0, r1) - r0) * step
    return t


def build_layer(entry, tles):
    """One pass -> its patch of the map (colour + which pixels it really covers)."""
    from scipy.interpolate import LinearNDInterpolator
    from scipy.ndimage import map_coordinates

    src = source_picture(entry)
    if not src:
        return None, "only pictures with map lines drawn on" if has_map_lines(entry) else f"no plain {KIND} picture"
    path = os.path.join(SITE, src)
    if not os.path.isfile(path):
        return None, "picture file missing"
    rgb = np.asarray(Image.open(path).convert("RGB"))
    W = rgb.shape[1]
    rgb = rgb[: -int(round(W * 0.055))]               # leave out the watermark along the bottom
    res, why = locate(entry, rgb, tles)
    if res is None:
        return None, why
    sat, step, direction, t_top, placed, hw, off = res
    H = len(rgb)
    code = "M2-3" if "M2-3" in entry["id"] else "M2-4"
    if not placed:
        return None, "couldn't line any part up with the coastline"

    used_rows = sum(r1 - r0 for r0, r1, _, _ in placed)
    clean_share = used_rows / H
    score, parts = rating(entry, clean_share)

    colour = np.zeros((MAP_H, MAP_W, 3), np.uint8)
    have = np.zeros((MAP_H, MAP_W), bool)
    outlines = []
    cs = np.arange(0, W, 6).tolist() + [W - 1]
    for r0, r1, t0, s in placed:
        rs = np.arange(r0, r1, 2).tolist() + [r1 - 1]
        rs = np.array(rs, float)
        u = (np.array(cs, float) + 0.5) / W * 2 - 1
        lat, lon = grid_latlon(sat, t0 + direction * (rs - r0) * step, u, hw, off)
        x, y = map_xy(lat, lon)
        rr, cc = np.meshgrid(rs, np.array(cs, float), indexing="ij")
        f = LinearNDInterpolator(np.stack([x.ravel(), y.ravel()], 1), np.stack([rr.ravel(), cc.ravel()], 1))
        x0, x1 = int(max(0, np.floor(x.min()))), int(min(MAP_W, np.ceil(x.max()) + 1))
        y0, y1 = int(max(0, np.floor(y.min()))), int(min(MAP_H, np.ceil(y.max()) + 1))
        if x1 <= x0 or y1 <= y0:
            continue
        gx, gy = np.meshgrid(np.arange(x0, x1), np.arange(y0, y1))
        rc = f(gx.ravel(), gy.ravel())
        ok = ~np.isnan(rc[:, 0])
        # the curved strip's outline isn't convex, so the interpolation also covers thin slivers
        # just outside it, where every value sits exactly on the first/last row or column: drop those
        e_ = 0.01
        ok &= (rc[:, 0] > r0 + e_) & (rc[:, 0] < r1 - 1 - e_) & (rc[:, 1] > e_) & (rc[:, 1] < W - 1 - e_)
        rc = rc[ok]
        px, py = gx.ravel()[ok], gy.ravel()[ok]
        vals = np.stack([map_coordinates(rgb[..., k].astype(float), rc.T, order=1, mode="nearest")
                         for k in range(3)], 1)
        good = vals.mean(1) > 4                         # skip black fill inside the picture
        colour[py[good], px[good]] = np.clip(vals[good], 0, 255).astype(np.uint8)
        have[py[good], px[good]] = True
        # outline of this stretch for the page (corners in lat/lon)
        ring = [(lat[0, 0], lon[0, 0]), (lat[0, -1], lon[0, -1]), (lat[-1, -1], lon[-1, -1]), (lat[-1, 0], lon[-1, 0])]
        outlines.append([[round(float(a), 3), round(float(b), 3)] for a, b in ring])

    # close thin seams (a few dropped noisy rows) by blending the picture above and below;
    # wider gaps stay open: that ground really wasn't captured by this pass
    seam = binary_closing_vertical(have, 3) & ~have
    if seam.any():
        Hm = have.shape[0]
        up_v = np.zeros(colour.shape, float); up_d = np.zeros(have.shape)
        dn_v = np.zeros(colour.shape, float); dn_d = np.zeros(have.shape)
        for k in range(1, 4):
            src = np.zeros_like(have); src[k:] = have[:-k]
            got = seam & src & (up_d == 0)
            ys, xs = np.nonzero(got); up_v[ys, xs] = colour[ys - k, xs]; up_d[ys, xs] = k
            src = np.zeros_like(have); src[:-k] = have[k:]
            got = seam & src & (dn_d == 0)
            ys, xs = np.nonzero(got); dn_v[ys, xs] = colour[ys + k, xs]; dn_d[ys, xs] = k
        both = seam & (up_d > 0) & (dn_d > 0)
        w = (dn_d / (up_d + dn_d))[..., None]
        blend = up_v * w + dn_v * (1 - w)
        colour[both] = np.clip(blend[both], 0, 255).astype(np.uint8)
        have |= both

    info = {
        "id": entry["id"], "sat": code, "start_utc": entry["start_utc"],
        "start_local": entry.get("start_local"), "day": entry.get("day"),
        "rating": score, "parts": parts, "peak_snr": entry.get("peak_snr"),
        "lock_s": entry.get("lock_s"), "max_elev": entry.get("max_elev"),
        "direction": entry.get("direction"), "clean_share": round(clean_share, 3),
        "fit": round(float(np.mean([p[3] for p in placed if p[3] >= 0] or [0])), 3),
        "stretches": len(placed), "outlines": outlines, "version": VERSION,
        "geometry": {"sec_per_row": round(step, 5), "half_swath_km": round(float(hw), 1), "sideways_km": round(float(off), 1)},
    }
    return (info, colour, have, rgb), None


def layer_from_folder(entry, folder):
    """Layer straight from SatDump's 'AZ 221 Map' picture, which SatDump already laid flat on
    this map's exact grid using its own line timestamps and control points: no coastline guessing.
    Returns True if the layer was made."""
    import glob as _g
    files = _g.glob(os.path.join(folder, "**", "*.png"), recursive=True)
    proj = next((f for f in files if os.path.basename(f).lower().endswith("az_221_map_projected.png")), None)
    flat = next((f for f in files if os.path.basename(f).lower().endswith("rgb_221_corrected.png")), None)
    if not proj:
        return False
    im = Image.open(proj)
    if im.size != (MAP_W, MAP_H):
        say(f"{entry['id']}: SatDump's map picture is {im.size}, expected {(MAP_W, MAP_H)}; using the coastline fit")
        return False
    a = np.asarray(im.convert("RGBA"))
    colour = a[..., :3].copy()
    have = (a[..., 3] > 0) & (colour.mean(2) > 4)
    if not have.any():
        return False
    # decoder marker lines (pure blue / yellow) and dropped lines (black streaks) survive in
    # SatDump's laid-flat picture: take them out and patch thin ones from the picture beside them
    from scipy.ndimage import binary_dilation, distance_transform_edt
    inside = a[..., 3] > 0
    lines = fake_colour(colour) | ((colour.mean(2) < 16) & inside)
    lines = binary_dilation(lines, iterations=1) & inside
    have &= ~lines
    dist, idx = distance_transform_edt(~have, return_indices=True)
    patch = lines & (dist <= 3)
    colour[patch] = colour[idx[0][patch], idx[1][patch]]
    have |= patch
    clean_share = 1.0
    thumb_src = None
    if flat:
        rgb = np.asarray(Image.open(flat).convert("RGB"))
        clean_share = 1.0 - float(bad_rows(rgb).mean())
        thumb_src = rgb
    score, parts = rating(entry, clean_share)
    ys, xs = np.nonzero(have)
    corners = [(ys.min(), xs.min()), (ys.min(), xs.max()), (ys.max(), xs.max()), (ys.max(), xs.min())]
    outline = [[round(LAT1 - (y + 0.5) * DLAT, 3), round(LON0 + (x + 0.5) * DLON, 3)] for y, x in corners]
    code = "M2-3" if "M2-3" in entry["id"] else "M2-4"
    info = {
        "id": entry["id"], "sat": code, "start_utc": entry["start_utc"],
        "start_local": entry.get("start_local"), "day": entry.get("day"),
        "rating": score, "parts": parts, "peak_snr": entry.get("peak_snr"),
        "lock_s": entry.get("lock_s"), "max_elev": entry.get("max_elev"),
        "direction": entry.get("direction"), "clean_share": round(clean_share, 3),
        "fit": 1.0, "stretches": 1, "outlines": [outline], "version": VERSION, "method": "satdump",
    }
    if thumb_src is None:
        thumb_src = colour
    save_layer(info, colour, have, thumb_src)
    say(f"added {entry['id']} from SatDump's own map placement: rating {score}")
    global DIRTY
    DIRTY = True                                      # the map picture needs redrawing
    return True


def save_layer(info, colour, have, rgb):
    d = os.path.join(WORK, "layers")
    os.makedirs(d, exist_ok=True)
    ys, xs = np.where(have)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    info["box"] = [int(y0), int(y1), int(x0), int(x1)]
    np.savez_compressed(os.path.join(d, info["id"] + ".npz"),
                        colour=colour[y0:y1, x0:x1], have=have[y0:y1, x0:x1])
    with open(os.path.join(d, info["id"] + ".json"), "w") as fh:
        json.dump(info, fh, indent=1)
    t = Image.fromarray(rgb)
    t.thumbnail((360, 360))
    t.save(os.path.join(d, info["id"] + ".thumb.jpg"), quality=78)


def load_layers():
    d = os.path.join(WORK, "layers")
    out = []
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if f.endswith(".json"):
            with open(os.path.join(d, f)) as fh:
                out.append(json.load(fh))
    return out


def drop_layer(pid):
    for ext in (".json", ".npz", ".thumb.jpg"):
        p = os.path.join(WORK, "layers", pid + ext)
        if os.path.exists(p):
            os.remove(p)


# ---------------------------------------------------------------- the map
def base_map():
    """Dark land/sea map with coastlines, for ground nobody has covered yet."""
    lat = LAT1 - (np.arange(MAP_H) + 0.5) * DLAT
    lon = LON0 + (np.arange(MAP_W) + 0.5) * DLON
    LON, LAT = np.meshgrid(lon, lat)
    land = land_mask(LAT, LON)
    img = np.empty((MAP_H, MAP_W, 3), np.uint8)
    img[:] = (11, 22, 36)
    img[land] = (22, 40, 63)
    edge = np.zeros_like(land)
    edge[1:] |= land[1:] != land[:-1]
    edge[:, 1:] |= land[:, 1:] != land[:, :-1]
    img[edge] = (58, 90, 124)
    for g in range(-5, 55, 5):                          # light lat/lon grid every 5 degrees
        y = int(round((LAT1 - g) / DLAT))
        if 0 <= y < MAP_H:
            img[y, ::3] = (31, 58, 87)
    for g in range(25, 95, 5):
        x = int(round((g - LON0) / DLON))
        if 0 <= x < MAP_W:
            img[::3, x] = (31, 58, 87)
    return img


def better(new, old):
    """THE RULE: does `new` take over ground already held by `old`?"""
    if new["rating"] != old["rating"]:
        return new["rating"] > old["rating"]
    return new["start_utc"] > old["start_utc"]           # same rating: the fresher one


def compose(layers):
    order = sorted(layers, key=lambda i: i["start_utc"])     # oldest first, as they came in
    mosaic = np.zeros((MAP_H, MAP_W, 3), np.uint8)
    owner = np.zeros((MAP_H, MAP_W), np.uint8)               # 0 = nobody, else index+1
    for n, info in enumerate(order, start=1):
        z = np.load(os.path.join(WORK, "layers", info["id"] + ".npz"))
        y0, y1, x0, x1 = info["box"]
        have = z["have"]
        cur = owner[y0:y1, x0:x1]
        take = have & (cur == 0)                             # empty ground: anyone fills it
        for m in np.unique(cur[have & (cur > 0)]):
            if better(info, order[m - 1]):
                take |= have & (cur == m)
        mosaic[y0:y1, x0:x1][take] = z["colour"][take]
        cur[take] = n
    return order, mosaic, owner


def write_site(order, mosaic, owner):
    out = os.path.join(SITE, "footprint")
    os.makedirs(os.path.join(out, "thumbs"), exist_ok=True)
    base = base_map()
    Image.fromarray(base).save(os.path.join(out, "base.png"), optimize=True)
    has = owner > 0
    full = base.copy()
    full[has] = mosaic[has]
    Image.fromarray(full).save(os.path.join(out, "mosaic.jpg"), quality=82, optimize=True, progressive=True)
    # which pass each pixel came from, as runs along each row: [value, count, value, count, ...]
    runs = []
    for row in owner:
        cut = np.flatnonzero(np.diff(row)) + 1
        starts = np.concatenate([[0], cut])
        lens = np.diff(np.concatenate([starts, [len(row)]]))
        runs.append([int(v) for pair in zip(row[starts], lens) for v in pair])
    with open(os.path.join(out, "owners.json"), "w") as fh:
        json.dump({"width": MAP_W, "height": MAP_H, "rows": runs}, fh, separators=(",", ":"))
    old_png = os.path.join(out, "owners.png")
    if os.path.exists(old_png):
        os.remove(old_png)

    lat = LAT1 - (np.arange(MAP_H) + 0.5) * DLAT
    area_rows = has.sum(1) * (DLON * 111.32 * np.cos(np.radians(lat))) * (DLAT * 110.57)
    passes = []
    keep = set()
    for n, info in enumerate(order, start=1):
        px = int((owner == n).sum())
        thumb = f"footprint/thumbs/{info['id']}.jpg"
        src = os.path.join(WORK, "layers", info["id"] + ".thumb.jpg")
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(SITE, thumb))
            keep.add(info["id"] + ".jpg")
        p = {k: info.get(k) for k in ("id", "sat", "start_utc", "start_local", "day", "rating", "parts",
                                      "peak_snr", "lock_s", "max_elev", "direction", "outlines")}
        p.update(index=n, thumb=thumb, share=round(px / max(1, has.sum()), 4),
                 on_map=px > 0, link=f"captures.html#{info['id']}")
        passes.append(p)
    for f in os.listdir(os.path.join(out, "thumbs")):
        if f not in keep:
            os.remove(os.path.join(out, "thumbs", f))
    latest = max(order, key=lambda i: i["start_utc"]) if order else None
    doc = {
        "updated": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "rule": "A pass only replaces ground already covered if its rating is the same or higher; "
                "same rating, the newer pass wins. Uncovered ground is filled by any pass.",
        "bounds": {"lon0": LON0, "lon1": LON1, "lat0": LAT0, "lat1": LAT1, "width": MAP_W, "height": MAP_H},
        "area_km2": int(area_rows.sum()),
        "pass_count": sum(1 for p in passes if p["on_map"]),
        "latest": latest["id"] if latest else None,
        "passes": sorted(passes, key=lambda p: p["start_utc"], reverse=True),
    }
    with open(os.path.join(out, "footprint.json"), "w") as fh:
        json.dump(doc, fh, indent=1)
    return doc


# ---------------------------------------------------------------- main
def update(site=None, force=False):
    """Bring the footprint up to date with captures/captures.json. Returns True if anything changed."""
    global SITE, DIRTY
    if site:
        SITE = site
    if "--rebuild" in ARGS and os.path.isdir(os.path.join(WORK, "layers")):
        shutil.rmtree(os.path.join(WORK, "layers"))
    remove = arg_value("--remove")
    if remove:
        drop_layer(remove)
    with open(os.path.join(SITE, "captures", "captures.json")) as fh:
        manifest = json.load(fh)
    done = {i["id"]: i for i in load_layers()}
    skipped_file = os.path.join(WORK, "skipped.json")
    skipped = json.load(open(skipped_file)) if os.path.exists(skipped_file) else {}
    todo = [e for e in manifest.get("passes", [])
            if (e["id"] not in done or (done[e["id"]].get("version") != VERSION
                                        and done[e["id"]].get("method") != "satdump"))
            and skipped.get(e["id"], {}).get("version") != VERSION]
    changed = bool(remove) or force or DIRTY or "--compose" in ARGS
    tles = load_tles() if todo else {}
    for e in sorted(todo, key=lambda e: e["start_utc"]):
        try:
            res, why = build_layer(e, tles)
        except Exception as ex:                      # one bad pass must never stop the rest
            res, why = None, f"{ex.__class__.__name__}: {ex}"
        if res is None:
            say(f"left out {e['id']}: {why}")
            skipped[e["id"]] = {"why": why, "version": VERSION}
            continue
        save_layer(*res)
        say(f"added {e['id']}: rating {res[0]['rating']}, {res[0]['stretches']} stretch(es), "
            f"coast fit {res[0]['fit']:.2f}")
        changed = True
    os.makedirs(WORK, exist_ok=True)
    with open(skipped_file, "w") as fh:
        json.dump(skipped, fh, indent=1)
    if not changed and os.path.exists(os.path.join(SITE, "footprint", "footprint.json")):
        say("nothing new")
        return False
    DIRTY = False
    order, mosaic, owner = compose(load_layers())
    doc = write_site(order, mosaic, owner)
    say(f"map written: {doc['pass_count']} passes, {doc['area_km2'] / 1e6:.1f} million km2")
    return True


if __name__ == "__main__":
    t = arg_value("--tle")
    if t:
        TLE_CANDIDATES.insert(0, t)
    update(arg_value("--site"))
