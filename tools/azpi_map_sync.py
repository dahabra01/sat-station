#!/usr/bin/env python3
"""
Big Picture sync: every recorded pass that is good enough goes on the map,
whether or not it was posted to the Captures page.

Why this exists: the Captures page only gets one pass a day, and passes that
raspberry-noaa-v2 decoded itself come with borders and city names burned in,
so the stitcher can't line them up. Those passes used to be left off the map
even when they covered ground nobody else had (6 Nov: Sudan and the Red Sea).

What it does, on every publisher run at home (azpi_publish.py calls it):
  1. Lists every Meteor pass recorded in the last MAP_DAYS days
     (raspberry-noaa-v2's pictures in /srv/images and Home Base folders in /srv/passes).
  2. Skips passes already on the map, and passes below the quality standard:
     at least MIN_LOCK_S seconds locked and MIN_PEAK_DB peak signal.
  3. Gets a clean picture for each one:
       * a SatDump folder that already has the clean pictures -> use it
       * otherwise the raw data (.cadu) kept from the pass -> SatDump makes
         them again, offline, with no borders or names on the clean copy
  4. Puts it on the map:
       * mostly clean picture (CLEAN_FOR_SATDUMP or better): SatDump's own exact
         placement on the map grid
       * patchy picture: the coastline fit, which only uses the clean stretches,
         so noisy lines never land on the map
  5. A pass rated below MIN_MAP_RATING stays off. Everything else goes in under the
     Captain's rule: it fills any empty ground, and only takes over ground already
     covered if its rating is the same or higher.

It never runs SatDump in the PASS_GUARD_S minutes before a pass, so it can't
get in the way of a recording, and it does at most MAX_PER_RUN passes per run.
What happened to each pass is kept in ~/azpi_footprint/sync.json.

Run by hand on the Pi (as pi):
  python3 ~/sat-station-site/tools/azpi_publish.py --map-sync-only --dry-run
      lists what it would add and why the others stay off, changes nothing
  python3 ~/sat-station-site/tools/azpi_publish.py --map-sync-only
      does it and sends the updated map to the website
  add --map-retry to try again passes it gave up on before
"""
import datetime as dt
import glob
import json
import os
import shutil
import subprocess
import time

import numpy as np
from PIL import Image

import azpi_footprint as F

# ---------------------------------------------------------------- settings
MAP_DAYS = 14                 # how far back to look for passes not on the map yet
MIN_LOCK_S = 60               # quality standard: at least a minute of lock...
MIN_PEAK_DB = 5.0             # ...and a peak signal good enough for pictures
MIN_MAP_RATING = 30           # a pass rated under this stays off the map
CLEAN_FOR_SATDUMP = 0.90      # share of clean picture lines to trust SatDump's own placement
MAX_PER_RUN = 2               # SatDump takes a minute or two per pass on the Pi
MAX_TRIES = 3                 # give up on a pass after this many failed attempts
PASS_GUARD_S = 25 * 60        # no SatDump work this close to the next pass
SETTLE_S = 300                # leave passes alone for 5 minutes after their files last changed
SATDUMP = shutil.which("satdump") or "/usr/bin/satdump"
PIPELINE = os.environ.get("AZPI_SATDUMP_PIPELINE", "meteor_m2-x_lrpt")
PASSES_DIR = os.environ.get("AZPI_PASSES_DIR", "/srv/passes")
RAW_DIRS = [d for d in os.environ.get("AZPI_RAW_DIRS", "").split(":") if d] or [
    "/srv/passes", "/srv/audio", "/srv/images", "/srv/meteor",
    os.path.expanduser("~/raspberry-noaa-v2/tmp")]
DUBAI = dt.timezone(dt.timedelta(hours=4))
SAME_PASS_S = 180             # two records this close together are the same pass
SYNC_VERSION = 2              # bump to redo passes this sync placed with an older method
                              # (2: thin and cloudy stretches placed by SatDump's line timing)


def _say(msg):
    print(f"[map sync] {msg}", flush=True)


say = _say


# ---------------------------------------------------------------- what's on the Pi
def folder_passes():
    """Home Base / SatDump pass folders: /srv/passes/<name>/pass.json with 'start' and 'sat'."""
    out = []
    for meta_path in glob.glob(os.path.join(PASSES_DIR, "*", "pass.json")):
        try:
            with open(meta_path) as fh:
                meta = json.load(fh)
            start = dt.datetime.fromtimestamp(int(meta["start"]), dt.timezone.utc)
        except Exception:
            continue
        sat = str(meta.get("sat") or "")
        if sat not in ("M2-3", "M2-4"):
            continue
        out.append({"id": f"METEOR-{sat}-{start.strftime('%Y%m%d-%H%M%S')}", "sat": sat,
                    "start": start, "folder": os.path.dirname(meta_path),
                    "newest": os.path.getmtime(meta_path)})
    return out


def all_recorded(image_passes):
    """Merge raspberry-noaa-v2's passes and the pass folders into one list, one row per pass."""
    rows = []
    for p in image_passes:
        sat = "M2-" + p["sat_code"].split("-")[-1]
        if sat not in ("M2-3", "M2-4"):
            continue
        rows.append({"id": p["id"], "sat": sat, "start": p["start"], "folder": None,
                     "newest": p.get("newest", 0)})
    for f in folder_passes():
        twin = next((r for r in rows if r["sat"] == f["sat"]
                     and abs((r["start"] - f["start"]).total_seconds()) < SAME_PASS_S), None)
        if twin:
            twin["folder"] = f["folder"]
            twin["newest"] = max(twin["newest"], f["newest"])
        else:
            rows.append(f)
    return sorted(rows, key=lambda r: r["start"])


def on_map(row, layers):
    for info in layers:
        if info.get("sat") != row["sat"]:
            continue
        t = dt.datetime.fromisoformat(info["start_utc"])
        if abs((t - row["start"]).total_seconds()) < SAME_PASS_S:
            return True
    return False


# ---------------------------------------------------------------- getting a clean picture
def products_in(folder):
    """(SatDump's map-grid picture, clean natural-colour picture) found under folder, or Nones."""
    if not folder or not os.path.isdir(folder):
        return None, None
    if os.path.isdir(os.path.join(folder, "products")):    # a Home Base pass: its SELECTED picture set only
        folder = os.path.join(folder, "products")           # (BRD 1.83: SatDump and MeteorRX keep separate sets)
    files = glob.glob(os.path.join(folder, "**", "*.png"), recursive=True)
    names = {f: os.path.basename(f).lower() for f in files}
    grid = next((f for f, n in names.items() if n.endswith("az_221_map_projected.png")), None)
    flat = next((f for f, n in names.items() if n.endswith("rgb_221_corrected.png")), None)
    return grid, flat


def find_raw(row):
    """The pass's raw decoded data (.cadu), wherever the station kept it."""
    if row.get("folder"):
        sel = os.path.join(row["folder"], "data.cadu")     # BRD 1.83: the selected set's data
        if os.path.isfile(sel):
            return sel
        hits = glob.glob(os.path.join(row["folder"], "**", "*.cadu"), recursive=True)
        if hits:
            return max(hits, key=os.path.getsize)
    t0 = row["start"].timestamp()
    stamp = row["start"].strftime("%Y%m%d")
    best = None
    for base in RAW_DIRS:
        for f in glob.glob(os.path.join(base, "**", "*.cadu"), recursive=True):
            try:
                m = os.path.getmtime(f)
            except OSError:
                continue
            # written during or just after the pass (up to 40 minutes after it started)
            if not (t0 - 120 <= m <= t0 + 40 * 60) and stamp not in os.path.basename(f):
                continue
            if os.path.getsize(f) < 200_000:              # a few seconds of data at most
                continue
            gap = abs(m - (t0 + 900))
            if best is None or gap < best[0]:
                best = (gap, f)
    return best[1] if best else None


def run_satdump(raw, out_dir):
    """Decode the raw data again with SatDump, offline. Returns '' if it worked, else why not."""
    if not os.path.exists(SATDUMP):
        return "SatDump isn't installed where expected"
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)
    cmd = ["nice", "-n", "15", SATDUMP, PIPELINE, "cadu", raw, out_dir]
    try:
        # run from the work folder: a settings.json in the current folder would override the Pi's own
        r = subprocess.run(cmd, cwd=F.WORK, capture_output=True, text=True, timeout=20 * 60)
    except subprocess.TimeoutExpired:
        return "SatDump took longer than 20 minutes"
    grid, flat = products_in(out_dir)
    if not flat:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or ["no output"]
        return f"SatDump made no natural-colour picture ({tail[0][-120:]})"
    return ""


def shrink_to(src, dst, width=800):
    im = Image.open(src).convert("RGB")
    if im.width > width:
        im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
    im.save(dst, quality=90)
    return np.asarray(im)


# ---------------------------------------------------------------- next pass guard
def next_pass_in(db):
    """Seconds until the next scheduled pass (raspberry-noaa-v2's database), or None."""
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        now = int(time.time())
        row = con.execute("SELECT MIN(pass_start) FROM predict_passes WHERE pass_start > ?", (now,)).fetchone()
        con.close()
        return (row[0] - now) if row and row[0] else None
    except Exception:
        return None


# ---------------------------------------------------------------- the sync
def load_state():
    try:
        with open(os.path.join(F.WORK, "sync.json")) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(st):
    os.makedirs(F.WORK, exist_ok=True)
    with open(os.path.join(F.WORK, "sync.json"), "w") as fh:
        json.dump(st, fh, indent=1)


def sync(site, image_passes, stats_for, geometry_for, db, posted_ids=(), dry=False, retry=False, log=None):
    """Add every good-enough recorded pass to the map's layers.
    Returns (added ids, report lines). The caller redraws the map and pushes it."""
    global say
    say = log or _say
    F.SITE = site
    st = load_state()
    layers = F.load_layers()
    now = time.time()
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=MAP_DAYS)
    # passes this sync placed by the coastline fit with an older method get done again
    # (the old layer stays on the map until the new one is ready, then gets overwritten)
    redo = set()
    for pid, rec in list(st.items()):
        if rec.get("how", "").startswith("coastline") and rec.get("v", 1) < SYNC_VERSION:
            redo.add(pid)
            st[pid] = {"redo": True, "tries": 0}
            say(f"{pid}: placing it again with the newer method")
    mapped = [i for i in layers if i["id"] not in redo]
    rows = [r for r in all_recorded(image_passes)
            if r["start"] >= cutoff and now - r["newest"] > SETTLE_S and not on_map(r, mapped)]
    added, report, worked = [], [], 0
    tles = None
    for row in rows:
        # a posted pass keeps the id the Captures page uses, so the map links to it
        pid = next((p for p in posted_ids if p.startswith(f"METEOR-{row['sat']}-")
                    and abs((dt.datetime.strptime(p[-15:], "%Y%m%d-%H%M%S").replace(tzinfo=dt.timezone.utc)
                             - row["start"]).total_seconds()) < SAME_PASS_S), row["id"])
        rec = st.get(pid, {})
        if rec.get("final") and not retry:
            continue
        if rec.get("tries", 0) >= MAX_TRIES and not retry:
            continue
        local = row["start"].astimezone(DUBAI)
        label = f"{row['sat']} {local.strftime('%d %b %H:%M')}"
        stats = stats_for(row["start"])
        if stats.get("lock_s", 0) < MIN_LOCK_S or stats.get("peak_snr", 0) < MIN_PEAK_DB:
            why = f"below the standard ({stats.get('lock_s', 0)} s locked, {stats.get('peak_snr', 0)} dB peak)"
            report.append(f"{label}: stays off, {why}")
            st[pid] = {"why": why, "final": True}
            continue
        if worked >= MAX_PER_RUN:
            report.append(f"{label}: next run")
            continue
        entry = {"id": pid, "sat": row["sat"], "start_utc": row["start"].isoformat(),
                 "start_local": local.strftime("%H:%M"), "day": local.strftime("%Y-%m-%d")}
        entry.update(stats)
        entry.update(geometry_for(row["start"]))

        grid, flat = products_in(row.get("folder"))
        work_dir = os.path.join(F.WORK, "reprocess", pid)
        if not flat:
            raw = find_raw(row)
            if not raw:
                why = "no clean picture and no raw data kept for it"
                rec = {"why": why, "tries": rec.get("tries", 0) + 1}
                st[pid] = rec
                report.append(f"{label}: gave up, {why}" if rec["tries"] >= MAX_TRIES
                              else f"{label}: stays off for now, {why} (look {rec['tries']} of {MAX_TRIES})")
                continue
            if dry:
                report.append(f"{label}: would decode it again from {raw} and add it")
                continue
            soon = next_pass_in(db)
            if soon is not None and soon < PASS_GUARD_S:
                report.append(f"{label}: waiting, a pass starts in {soon // 60} min")
                break
            say(f"{label}: decoding again with SatDump from {raw}")
            worked += 1
            why = run_satdump(raw, work_dir)
            if why:
                rec = {"why": why, "tries": rec.get("tries", 0) + 1}
                st[pid] = rec
                report.append(f"{label}: gave up, SatDump failed ({why})" if rec["tries"] >= MAX_TRIES
                              else f"{label}: SatDump failed ({why}), try {rec['tries']} of {MAX_TRIES}")
                continue
            grid, flat = products_in(work_dir)
        elif dry:
            report.append(f"{label}: would add it from {row['folder']}")
            continue
        else:
            worked += 1

        os.makedirs(os.path.join(F.WORK, "plain"), exist_ok=True)
        plain = os.path.join(F.WORK, "plain", pid + ".jpg")
        rgb = shrink_to(flat, plain)
        clean_share = 1.0 - float(F.bad_rows(rgb).mean())
        ok, why = False, ""
        if grid and clean_share >= CLEAN_FOR_SATDUMP:
            ok = F.layer_from_folder(entry, os.path.dirname(grid))
            how = "SatDump placement"
        if not ok:                                        # patchy picture: clean stretches only
            entry["plain"] = plain                         # absolute path: the stitcher reads it as is
            entry["no_watermark"] = True                   # our own copy: nothing to crop off the bottom
            entry["timed"] = True                          # SatDump's picture: time runs on across gaps
            tles = tles or F.load_tles()
            try:
                res, why = F.build_layer(entry, tles)
            except Exception as ex:
                res, why = None, f"{ex.__class__.__name__}: {ex}"
            if res:
                F.save_layer(*res)
                ok, how = True, "coastline fit, clean stretches only"
        shutil.rmtree(work_dir, ignore_errors=True)       # the layer keeps what the map needs
        if not ok:
            rec = {"why": why or "couldn't place it on the map", "tries": rec.get("tries", 0) + 1}
            st[pid] = rec
            report.append(f"{label}: gave up, couldn't place it ({rec['why']})" if rec["tries"] >= MAX_TRIES
                          else f"{label}: couldn't place it ({rec['why']}), try {rec['tries']} of {MAX_TRIES}")
            continue
        info = next(i for i in F.load_layers() if i["id"] == pid)
        if info["rating"] < MIN_MAP_RATING:
            F.drop_layer(pid)
            why = f"rating {info['rating']} is under {MIN_MAP_RATING}"
            st[pid] = {"why": why, "final": True}
            report.append(f"{label}: stays off, {why}")
            continue
        st[pid] = {"added": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
                   "rating": info["rating"], "how": how, "final": True, "v": SYNC_VERSION}
        added.append(pid)
        report.append(f"{label}: added, rating {info['rating']} ({how})")
    if not dry:
        save_state(st)
    return added, report
