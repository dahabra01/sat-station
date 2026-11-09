#!/usr/bin/env python3
"""
Switches on the Field Live waterfall (BRD 1.35) by itself, once it has proved it works.

How the waterfall works: while a pass records, satdump also serves its live signal and spectrum
on 127.0.0.1:8095, and Field Live (server.py /api/field/live) draws it. The option that turns this
on is added to the recording script by the self-heal guard (~/azpi/guard/azpi_selfheal.py), but
only when ~/azpi/guard/waterfall.on exists - "set by the waterfall installer only after a test
recording worked". That installer was never built, so the flag never appeared, satdump never served
anything, and Field Live sat on "waiting for satdump..." through every pass (seen 9 Nov 2026).

This is that installer. azpi_publish.py runs it at the start of every home run. Once the flag is
there and the recording script has the waterfall line, it does nothing. Otherwise:
  * waits for a quiet moment: no satdump / rtl_* running, no booked pass in the next 20 minutes
  * makes a 25-second test recording at 137.9 MHz with the same settings as a real pass, through
    the SDR monitor's capture wrapper (so the monitor steps aside the same way it does for a pass),
    with satdump's live server on
  * asks satdump's live server for its numbers while it runs
  * if satdump answered: sets the flag and runs the self-heal guard straight away, which adds the
    option to the recording script (checked with bash -n, known-good copy kept, as for every fix)
  * if it didn't: changes nothing, tries again on a later run (at most every 3 hours), and tells the
    Captain on the phone after the third miss, with satdump's own words
Never touches a recording in progress. Test output goes to /tmp and is deleted.

By hand on the Pi (as pi):  python3 ~/sat-station-site/tools/azpi_install_waterfall.py [--dry-run]
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HOME = os.environ.get("AZPI_HOME", "/home/pi")
GUARD = HOME + "/azpi/guard"
FLAG = GUARD + "/waterfall.on"
SELFHEAL = GUARD + "/azpi_selfheal.py"
SCRIPT = HOME + "/raspberry-noaa-v2/scripts/receive_meteor.sh"
CONF = HOME + "/.noaa-v2.conf"
WRAPPER = HOME + "/sdr-rf/capture-wrapper.py"
MARK = "AZPi waterfall (BRD 1.35)"
API = os.environ.get("AZPI_WF_API", "http://127.0.0.1:8095/api")
WF_OPTS = ["--http_server", "127.0.0.1:8095", "--fft_enable", "--fft_size", "512", "--fft_rate", "10"]
STATE = os.path.expanduser("~/azpi_footprint/waterfall_install.json")
BUSY = ("satdump", "rtl_fm", "rtl_sdr", "rtl_tcp", "rtl_power", "meteor_demod")
HERE = os.path.dirname(os.path.abspath(__file__))
RETRY_S = 3 * 3600
TEST_S = 25


def jload(p, d):
    try:
        return json.load(open(p))
    except Exception:
        return d


def jsave(p, d):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(d, fh, indent=1)
    os.replace(tmp, p)


def tell(title, msg, ok):
    try:
        sys.path.insert(0, HERE)
        import azpi_publish
        azpi_publish.ntfy_send(title, msg, tags="white_check_mark" if ok else "warning")
    except Exception:
        pass


def installed():
    try:
        return os.path.exists(FLAG) and MARK in open(SCRIPT, encoding="utf-8").read()
    except OSError:
        return False


def conf():
    """Gain, bias tee and dongle serial from the station's raspberry-noaa settings (M2-3 values)."""
    c = {"gain": "7.7", "bias": True, "serial": ""}
    try:
        t = open(CONF).read()
    except OSError:
        return c
    for key, name in (("gain", "METEOR_M2_3_GAIN"), ("serial", "METEOR_M2_3_SDR_DEVICE_ID")):
        m = re.search(r'^%s=["\']?([^"\'\s]*)' % name, t, re.M)
        if m and m.group(1):
            c[key] = m.group(1)
    m = re.search(r'^METEOR_M2_3_ENABLE_BIAS_TEE=["\']?([^"\'\s]*)', t, re.M)
    if m:
        c["bias"] = bool(m.group(1))
    return c


def busy():
    for name in BUSY:
        if subprocess.run(["pgrep", "-x", name], capture_output=True).returncode == 0:
            return f"{name} is using the dongle"
    # raspberry-noaa books each pass as an `at` job; keep clear of the next 20 minutes
    try:
        out = subprocess.run(["atq"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        out = ""
    now = time.time()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 6:
            continue
        try:
            t = time.mktime(time.strptime(" ".join(parts[1:6]), "%a %b %d %H:%M:%S %Y"))
        except ValueError:
            continue
        if -60 < t - now < 20 * 60:
            return "a pass is booked in the next 20 minutes"
    return None


def test_recording(log):
    """Short satdump recording with the live server on. Returns (ok, has_spectrum, detail)."""
    c = conf()
    out = tempfile.mkdtemp(prefix="azpi_wftest_")
    sd = os.environ.get("AZPI_SATDUMP") or shutil.which("satdump") or "/usr/bin/satdump"
    cmd = [sd, "live", "meteor_m2-x_lrpt", out, "--source", "rtlsdr", "--samplerate", "1.024e6",
           "--frequency", "137.9e6", "--gain", c["gain"], "--timeout", str(TEST_S)] + WF_OPTS
    if c["serial"]:
        cmd[8:8] = ["--source_id", c["serial"]]
    if c["bias"]:
        cmd.insert(cmd.index("--timeout"), "--bias")
    if os.path.exists(WRAPPER):
        cmd = ["/usr/bin/python3", WRAPPER] + cmd
    log("waterfall test: " + " ".join(cmd[-14:]))
    logf = open(os.path.join(out, "satdump.log"), "w")
    p = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, cwd=out)
    got, spectrum, end = None, False, time.time() + TEST_S + 20
    try:
        while time.time() < end and p.poll() is None:
            time.sleep(2)
            try:
                with urllib.request.urlopen(API, timeout=2) as r:
                    j = json.loads(r.read().decode("utf-8", "replace"))
                got = j
                if isinstance(j.get("fft_values"), list) and len(j["fft_values"]) > 16:
                    spectrum = True
                    break
            except Exception:
                continue
        if got is not None and not spectrum:
            time.sleep(6)                       # give the spectrum a moment before deciding
            try:
                with urllib.request.urlopen(API, timeout=2) as r:
                    j = json.loads(r.read().decode("utf-8", "replace"))
                spectrum = isinstance(j.get("fft_values"), list) and len(j["fft_values"]) > 16
            except Exception:
                pass
    finally:
        try:
            p.wait(timeout=TEST_S + 30)         # let it end by itself so the wrapper restores the monitor
        except subprocess.TimeoutExpired:
            p.terminate()
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
        logf.close()
        tail = ""
        try:
            lines = [l.strip() for l in open(os.path.join(out, "satdump.log"), errors="replace") if l.strip()]
            bad = [l for l in lines if re.search(r"error|unknown|invalid|could not|failed", l, re.I)]
            tail = (bad[-1] if bad else (lines[-1] if lines else ""))[-160:]
        except OSError:
            pass
        shutil.rmtree(out, ignore_errors=True)
    if got is None:
        return False, False, tail or "satdump's live server never answered"
    return True, spectrum, tail


def set_flag():
    try:
        with open(FLAG, "w") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S") + " test recording worked\n")
        return True
    except OSError:
        r = subprocess.run(["sudo", "-n", "sh", "-c", 'date "+%%Y-%%m-%%d %%H:%%M:%%S test recording worked" > %s' % FLAG],
                           capture_output=True)
        return r.returncode == 0


def run_selfheal():
    for cmd in (["sudo", "-n", "systemctl", "start", "azpi-selfheal.service"],
                ["sudo", "-n", "/usr/bin/python3", SELFHEAL]):
        if subprocess.run(cmd, capture_output=True, timeout=120).returncode == 0:
            for _ in range(15):
                if installed():
                    return True
                time.sleep(2)
    return installed()


def run(log=print, dry=False):
    if installed():
        return True, "already on"
    if not os.path.exists(SCRIPT) or not os.path.exists(SELFHEAL):
        return False, "recording script or self-heal guard not found on this Pi"
    st = jload(STATE, {})
    if os.path.exists(FLAG):
        # tested before, but the guard hasn't added the line yet (or an update removed it)
        if dry:
            return True, "flag set, would run the self-heal guard (dry run)"
        ok = run_selfheal()
        msg = "switched on" if ok else "flag set, but the self-heal guard couldn't add the waterfall line"
        log("[waterfall] " + msg)
        if not ok and not st.get("told_guard"):
            tell("AZPi: Field Live waterfall not on yet",
                 "The waterfall test worked, but the self-heal guard couldn't add it to the recording script. "
                 "Recordings are untouched. I'll keep trying.", False)
            st["told_guard"] = True
            jsave(STATE, st)
        return ok, msg
    if time.time() - st.get("last_try", 0) < RETRY_S and st.get("tries"):
        return False, "waiting before the next test"
    why = busy()
    if why:
        log(f"[waterfall] not testing now: {why}")
        return False, why
    if dry:
        return True, "would make a 25 s test recording (dry run)"
    ok, spectrum, detail = test_recording(log)
    st["last_try"] = int(time.time())
    if ok:
        if not set_flag():
            st["tries"] = st.get("tries", 0) + 1
            jsave(STATE, st)
            return False, "test worked but couldn't set the waterfall flag"
        on = run_selfheal()
        st.update(done=on, spectrum=spectrum, at=int(time.time()))
        jsave(STATE, st)
        if spectrum:
            msg = ("Field Live waterfall is on. From the next pass, the Waterfall card shows the live radio band "
                   "every second, and the signal line updates every second too.")
        else:
            msg = ("Field Live now gets satdump's live signal every second from the next pass. satdump answered "
                   "the test but sent no spectrum, so the Waterfall card may stay dark on this satdump version.")
        if not on:
            msg = "The waterfall test worked; it switches on at the self-heal guard's next check (within 30 minutes)."
        log("[waterfall] " + msg)
        tell("AZPi: Field Live waterfall " + ("on" if on else "almost on"), msg, True)
        return True, msg
    st["tries"] = st.get("tries", 0) + 1
    st["why"] = detail
    jsave(STATE, st)
    log(f"[waterfall] test {st['tries']} didn't work: {detail}")
    if st["tries"] == 3:
        tell("AZPi: Field Live waterfall not switched on",
             "Three test recordings with satdump's live server didn't work, so recordings were left exactly "
             "as they are. satdump said: " + (detail or "nothing") + ". I'll keep trying every few hours.", False)
    return False, detail


if __name__ == "__main__":
    ok, msg = run(dry="--dry-run" in sys.argv)
    print(msg)
    sys.exit(0 if ok else 1)
