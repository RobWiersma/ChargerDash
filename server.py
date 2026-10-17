"""Live dashboard for SkyRC iMAX B6AC v2 chargers over USB HID. Supports several at once.

Run:  python server.py [--port 8765] [--host 0.0.0.0]
Then open http://localhost:8765 (or http://<this-pc-ip>:8765 from a phone).

The chargers report no USB serial number, so each one is identified by the USB port
it's plugged into. Keep each charger on the same port and it keeps its name.
"""
import argparse
import csv
import hashlib
import io
import json
import os
import queue
import re
import secrets
import socket
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import hid
import segno

VID, PID = 0x0000, 0x0001
CMD_STATUS, CMD_SYSTEM, CMD_PROGRAM, CMD_MACHINE_ID, CMD_STOP = 0x55, 0x5A, 0x5F, 0x57, 0xFE
STOP = [0x0F, 0x03, CMD_STOP, 0x00, CMD_STOP, 0xFF, 0xFF]
ACK = [0xF0, 0xFF, 0xFF]

# Names as ChargeMaster shows them. Machine id "100130" swaps LiFe and LiIon.
BATT_TYPES = ["LiPo", "LiIon", "LiFe", "LiHV", "NiMH", "NiCd", "Pb"]
BATT_TYPES_100130 = ["LiPo", "LiFe", "LiIon", "LiHV", "NiMH", "NiCd", "Pb"]
# Program byte in the 0x5F reply, as confirmed on the real charger (screen vs. reply).
LI_PROGRAMS_SEEN = {0: "Storage", 4: "Balance charge"}

# Status mode 4 = stopped with an error; the code sits where mAh normally is.
ERRORS = {
    256: "Internal temperature too high", 512: "External temperature too high",
    768: "DC input too low", 1024: "DC input too high", 1280: "Over time limit",
    1536: "Over charge capacity limit", 1792: "Reverse polarity", 2048: "Control fail",
    2304: "Breakdown", 4096: "Input fail", 11: "Connection break", 12: "Cell voltage invalid",
    13: "Balance connection error", 14: "No battery", 15: "Cell number incorrect",
    16: "Connection error, check main port", 17: "Battery was full", 18: "Doesn't need charging",
    19: "Cell voltage too high", 20: "Connection break", 21: "Connection break", 22: "Connection break",
}

# Start-command values, from SkyRC ChargeMaster (DealtransferData / ConfigData).
LI_MODES = {"charge": 0, "discharge": 1, "storage": 2, "fast": 3, "balance": 4}
# Battery type code and ChargeMaster's per-cell min/max defaults. Storage target is set by firmware.
# LiIon's code depends on the machine id (see BATT_TYPES_100130); LiPo and LiHV are the same in both.
BATTERIES = {
    "lipo": {"name": "LiPo", "code": 0, "min_mv": 3200, "max_mv": 4200},
    "lion": {"name": "LiIon", "code": 1, "code_100130": 2, "min_mv": 3100, "max_mv": 4100},
    "lihv": {"name": "LiHV", "code": 3, "min_mv": 3000, "max_mv": 4350},
}
MAX_CHARGE_A, MAX_DISCHARGE_A = 6.0, 2.0  # B6AC v2 limits

PEAK_SKIP_SECS = 10   # ignore the first readings of a program when tracking peak current
MAX_FINISHES = 30     # finished programs remembered for learning the cut-off current
# Current a program ends at, as a share of its peak, until enough programs have finished to
# learn it. Storage observed on this charger: ended near 0.33 A after a 2.0 A start.
DEFAULT_CUTOFF_RATIOS = {"storage": 0.16, "full": 0.10}

POLL_SECONDS = 1.0
SCAN_SECONDS = 2.0
MAX_HISTORY = 6 * 3600  # one sample per second, 6 hours

def documents_dir():
    """The user's Documents folder, following OneDrive or other redirection."""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:  # CSIDL_PERSONAL
            return buf.value
    except (AttributeError, OSError):
        pass
    return os.path.join(os.path.expanduser("~"), "Documents")


# Bundled files (the page) sit next to this script, or in PyInstaller's unpack folder when
# frozen. Names, settings and logs live in Documents\ChargerDash so they survive app updates
# and are shared between the exe and running from source. CHARGERDASH_DATA overrides it.
HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("CHARGERDASH_DATA") or os.path.join(documents_dir(), "ChargerDash")
LOG_DIR = os.path.join(DATA_DIR, "logs")
NAMES_FILE = os.path.join(DATA_DIR, "chargers.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
DEFAULT_PREFS = {"battery": "lipo", "mode": "balance", "current_a": 1.0}
INDEX_HTML = os.path.join(HERE, "static", "index.html")


def u16(p, i):
    return (p[i] << 8) | p[i + 1]


def query_packet(cmd):
    return [0x0F, 0x03, cmd, 0x00, cmd, 0xFF, 0xFF]


def valid_reply(p, cmd):
    """0F <len> <cmd> 00 ... <checksum> FF FF, checksum = sum(bytes 2..len) & 0xFF."""
    if len(p) < 6 or p[0] != 0x0F or p[2] != cmd or p[1] + 3 > len(p):
        return False
    n = p[1]
    return (sum(p[2:n + 1]) & 0xFF) == p[n + 1]


def decode(p):
    """Decode a 0x55 status reply. Returns None if the frame isn't one or the checksum fails.

    Running (mode 1), finished (mode 3) or error (mode 4, error code in place of mAh):
      0F 22 55 00 | mode | mAh | secs | Vpack | I | temp | ? | 6 cells ...
    Idle (mode 2) has no live readings here, only leftover bytes from earlier replies, so
    pack and cell voltages come back as None; read them from the 0x5A reply instead.
    """
    if len(p) < 38 or p[1] != 0x22 or not valid_reply(p, CMD_STATUS):
        return None
    mode = p[4]
    if mode in (1, 3, 4):
        error = ERRORS.get(u16(p, 5), f"Error code {u16(p, 5)}") if mode == 4 else None
        return {
            "running": mode == 1,
            "done": mode == 3,
            "error": error,
            "mode": mode,
            "mah": None if error else u16(p, 5),
            "secs": u16(p, 7),
            "pack_v": u16(p, 9) / 1000,
            "current_a": u16(p, 11) / 1000,
            "temp_c": u16(p, 13),
            "cells_v": [u16(p, 17 + 2 * i) / 1000 for i in range(6)],
        }
    return {
        "running": False,
        "done": False,
        "error": None,
        "mode": mode,
        "mah": None,
        "secs": None,
        "pack_v": None,
        "current_a": 0.0,
        "temp_c": None,
        "cells_v": None,
    }


def decode_idle_voltages(p):
    """(Input voltage, cell voltages) from a 0x5A (system) reply, which stays live when idle.

    0F 25 5A 00 | settings (14 bytes) | V at 18 | 6 cells from 20 | ...
    The value at 18 stays at 15-16 V whatever pack is connected (4S or 6S), so it's most likely the
    charger's DC input, not the main lead. The main lead isn't reported while idle.
    """
    if len(p) < 32 or p[1] != 0x25 or not valid_reply(p, CMD_SYSTEM):
        return None
    return u16(p, 18) / 1000, [u16(p, 20 + 2 * i) / 1000 for i in range(6)]


# The chargers have no serial number, so the user gives each one an ID by setting its "Waste time"
# (rest between NiMH/NiCd cycle steps, 1-60 min; unused for lithium) to a different value. It's the
# first byte of the 0x5A settings. The factory default means "no ID set".
DEFAULT_WASTE_TIME = 10


def decode_tag(p):
    """The charger's ID (its waste-time setting) from a 0x5A reply, or None if not set or unreadable."""
    if not p or len(p) < 32 or p[1] != 0x25 or not valid_reply(p, CMD_SYSTEM):
        return None
    return p[4] if p[4] != DEFAULT_WASTE_TIME else None


def decode_program(p, machine_id):
    """Decode a 0x5F reply into the program the charger is set to, e.g. LiPo / Storage / 6S.

    Payload: state | batt type | cells | program | max charge A x10 | max discharge A x10 | ...
    The program byte doesn't use ChargeMaster's numbering (its Storage is 2, the charger reports
    0), so only values seen on the real charger are named; anything else shows as "Program n".
    """
    if not valid_reply(p, CMD_PROGRAM) or p[1] < 12:
        return None
    types = BATT_TYPES_100130 if machine_id == "100130" else BATT_TYPES
    t, cells, m = p[5], p[6], p[7]
    if t >= len(types):
        return None
    known = LI_PROGRAMS_SEEN if t < 4 else {}
    return {
        "type": types[t],
        "cells": cells,
        "mode": known.get(m, f"Program {m}"),
    }


def build_start(battery, cells, mode, charge_ma, discharge_ma, machine_id=""):
    """Lithium start packet, laid out like ChargeMaster's DealtransferData.sendData(5, ...)."""
    b = BATTERIES[battery]
    code = b.get("code_100130", b["code"]) if machine_id == "100130" else b["code"]
    body = [code, cells, LI_MODES[mode],
            charge_ma >> 8, charge_ma & 0xFF, discharge_ma >> 8, discharge_ma & 0xFF,
            b["min_mv"] >> 8, b["min_mv"] & 0xFF, b["max_mv"] >> 8, b["max_mv"] & 0xFF,
            0, 0,  # NiMH re-peak / cycle fields
            0, 0,  # trickle current
            0, 0, 0, 0]
    head = [0x05, 0x00]
    return [0x0F, 3 + len(body)] + head + body + [sum(head + body) & 0xFF, 0xFF, 0xFF]


IR_REST_SAMPLES = 3    # resting readings averaged before current starts (at least 2 needed)
IR_SETTLE_SECS = 15    # wait this long after current starts before measuring
IR_MIN_A = 0.5         # below this the voltage step is too small to measure
IR_REST_A = 0.05      # at or below this the pack is resting; above it current has started
IR_START_WITHIN_SECS = 30  # current starts this soon into a program (after the charger's ~10 s check)
IR_HOLD_DROP = 0.7    # current falling below this × its peak right away = charger holding full voltage
IR_NOTE_LATE = "program started before the app was open"


def internal_resistance(history):
    """Per-cell DC internal resistance from the step when a program's current starts.

    The charger checks the pack for ~10 s at 0 A first. Each cell's voltage change from rest to the
    first reading at 90% of the settled current, divided by that current, is its resistance. It's
    read through the balance lead about a second after the step, so it includes plug contact and a
    little polarisation: good for comparing cells and packs over time, not lab-grade.
    Returns {"amps", "cells_mohm", "pack_mohm"}; {"note"} saying why it can't be measured; None when
    there are no resting readings (the app joined mid-program); or False if it's too early to tell.
    """
    start = next((i for i, r in enumerate(history) if r["current_a"] > IR_REST_A), None)
    if start is None or history[-1]["secs"] < history[start]["secs"] + IR_SETTLE_SECS:
        return False
    # The step must be the program's own start (in its first seconds), not a dip in a tapering
    # current partway through, which is what joining mid-program can look like.
    if history[start]["secs"] > IR_START_WITHIN_SECS:
        return None
    rest = [r for r in history[max(0, start - IR_REST_SAMPLES):start] if r["cells_v"]]
    if len(rest) < 2:
        return {"note": "no resting readings before current started"}
    settled = [r for r in history[start:] if r["secs"] <= history[start]["secs"] + IR_SETTLE_SECS]
    peak = max(r["current_a"] for r in settled)
    load = next(r for r in settled if r["current_a"] >= 0.9 * peak)
    n = sum(1 for v in rest[-1]["cells_v"] if v > 1.0)
    if n == 0:
        return {"note": "no balance lead"}
    if settled[-1]["current_a"] < IR_HOLD_DROP * peak:
        # A pack (nearly) at the program's target (full, or storage voltage): the charger goes
        # straight to holding that voltage, so the step shows the charger's limit, not the pack.
        return {"note": "pack already at its target voltage, can't measure"}
    if load["current_a"] < IR_MIN_A:
        return {"note": f"current under {IR_MIN_A:.1f} A, too low to measure"}
    cells = [round(abs(load["cells_v"][k] - sum(r["cells_v"][k] for r in rest) / len(rest))
                   / load["current_a"] * 1000, 1) for k in range(n)]
    return {"amps": load["current_a"], "cells_mohm": cells, "pack_mohm": round(sum(cells), 1)}


def ir_from_logs(session_start):
    """Internal resistance from the CSV log that recorded this program's start, if any.

    Used when the app joined a program partway through (e.g. it was restarted): the log written
    by the earlier run still has the resting readings from before current started.
    """
    try:   # newest first; the program's own logs are among the last few written
        paths = sorted((os.path.join(LOG_DIR, n) for n in os.listdir(LOG_DIR) if n.endswith(".csv")),
                       key=os.path.getmtime, reverse=True)[:40]
    except OSError:
        return None
    for path in paths:
        try:
            with open(path, newline="") as f:
                rows = []
                for r in csv.DictReader(f):
                    rows.append({"secs": int(r["secs"]), "current_a": float(r["current_a"]),
                                 "cells_v": [float(r[f"cell{i}"]) for i in range(1, 7)],
                                 "t": datetime.fromisoformat(r["time"]).timestamp()})
                    if len(rows) >= 60:
                        break
        except (OSError, KeyError, ValueError):
            continue
        if rows and abs(rows[0]["t"] - rows[0]["secs"] - session_start) < 15:
            ir = internal_resistance(rows)
            if ir:
                return ir   # else it's a log that also joined mid-program; keep looking
    return None


def detect_cells(s, battery=None):
    """Work out a lithium pack's cell count from the balance lead.

    Returns (cells, None) or (None, reason). A balance lead is required. With a battery type, cells
    above that type's full voltage are refused; without one, anything up to a full LiHV passes.
    The main lead can't be checked here: the charger doesn't report it while idle. It checks the
    cell count against the main lead itself when a program starts (error 15, cell number
    incorrect), and the dashboard warns if the two disagree while running.
    """
    if not s:
        return None, "No reading from the charger yet."
    taps = s["cells_v"]
    n = sum(1 for v in taps if v > 1.0)
    if n == 0:
        return None, "No balance lead detected. Plug the pack's balance lead into the charger."
    if any(v <= 1.0 for v in taps[:n]):
        return None, "Balance lead readings have a gap. Check the balance plug is fully seated."
    low, high = min(taps[:n]), max(taps[:n])
    if low < 3.0:
        return None, f"Lowest cell is {low:.3f} V, below 3.00 V. The pack is over-discharged and may be damaged."
    b = BATTERIES[battery] if battery else BATTERIES["lihv"]
    limit = b["max_mv"] / 1000 + 0.05
    if high > limit:
        fits = [o["name"] for o in BATTERIES.values() if high <= o["max_mv"] / 1000 + 0.05]
        hint = f" If this pack really is {fits[0]}, choose {fits[0]}." if battery and fits else ""
        return None, f"Highest cell is {high:.3f} V, above a {b['name']}'s {b['max_mv'] / 1000:.2f} V full charge.{hint}"
    return n, None


TAPER_RATIO = 0.85  # current below this share of its peak = the top-off phase has started
PACK_MATCH_V = 0.03  # resting cells within this of a charger's last readings = the same pack
TAG_CHECK_POLLS = 10  # re-read the ID setting this often while charging (it's read every poll when idle)


def taper_profile(history, peak_a, end_secs):
    """Where a finished program's current started to fall, and how long the top-off took.

    Only reported when the history covers the steady-current phase before the taper;
    otherwise the app joined part-way and the numbers would be wrong.
    """
    rows = [r for r in history if r["secs"] >= PEAK_SKIP_SECS]
    saw_steady = any(r["current_a"] >= 0.95 * peak_a for r in rows)
    taper = next((r for r in rows if r["current_a"] < TAPER_RATIO * peak_a), None)
    if not saw_steady or not taper:
        return {}
    cells = [v for v in taper["cells_v"] if v > 1]
    return {"taper_v": round(sum(cells) / len(cells), 4), "tail_secs": end_secs - taper["secs"]}


def port_id(path):
    """Short stable id for a HID path. The path encodes the USB port on Windows."""
    return hashlib.sha1(path).hexdigest()[:8]


MODEL_NAMES = {"100008": "B6AC v2"}


def decode_device_info(r):
    """Model and firmware from a 0x57 reply. These chargers have no unique serial number: two of
    the same model and firmware report identical bytes (ChargeMaster's "SN" is just these)."""
    if not r or len(r) < 20:
        return {}
    code = bytes(r[5:11]).decode("ascii", "replace")
    return {"machine_id": code, "model": MODEL_NAMES.get(code, f"SkyRC {code}"),
            "firmware": f"{r[16]}.{r[17]:02d}", "hardware": r[18]}


class Connection:
    """One charger plugged in over USB: polls it on a background thread and passes readings to
    whichever Charger it's currently assigned to. Ends when the device is unplugged."""

    def __init__(self, path):
        self.path = path
        self.port = port_id(path)
        self.charger = None
        self.info = {}
        self.tag = None
        self._commands = queue.Queue()
        self._polls = 0

    @property
    def machine_id(self):
        return self.info.get("machine_id", "")

    def run(self):
        try:
            dev = hid.device()
            dev.open_path(self.path)
            dev.set_nonblocking(True)
        except OSError:
            fleet.connection_lost(self)
            return
        try:
            self.info = decode_device_info(self._query(dev, CMD_MACHINE_ID))
            self.tag = decode_tag(self._query(dev, CMD_SYSTEM))
            self.info["tag"] = self.tag
            first = None
            for _ in range(5):
                first = self._read_sample(dev)
                if first:
                    break
            fleet.attach(self, first)
            if first:
                self.charger._add(first)
            self._poll(dev)
        except OSError:
            pass   # unplugged
        except Exception:
            traceback.print_exc()   # a bug, not an unplug: say so instead of silently reconnecting
        finally:
            dev.close()
            fleet.connection_lost(self)

    def send(self, packet):
        """Queue a packet for the poll thread and wait for the charger's F0 FF FF ack."""
        done, result = threading.Event(), {"ok": False}
        self._commands.put((packet, done, result))
        done.wait(5)
        return result["ok"]

    def _run_commands(self, dev):
        while not self._commands.empty():
            packet, done, result = self._commands.get()
            dev.write([0x00] + packet + [0x00] * (64 - len(packet)))
            t0 = time.time()
            while time.time() - t0 < 1.5:
                r = dev.read(64)
                if r and list(r[:3]) == ACK:
                    result["ok"] = True
                    break
                time.sleep(0.01)
            done.set()

    def _query(self, dev, cmd, timeout=0.4):
        """Send a read-only query and return the first valid reply to it, or None."""
        dev.write([0x00] + query_packet(cmd) + [0x00] * 57)
        t0 = time.time()
        while time.time() - t0 < timeout:
            r = dev.read(64)
            if r and valid_reply(r, cmd):
                return r
            time.sleep(0.01)
        return None

    def _read_sample(self, dev):
        self._polls += 1
        r = self._query(dev, CMD_STATUS)
        sample = decode(r) if r else None
        # Not running (idle, finished or stopped with an error): the status reply's voltages are left
        # over from the program, so read the live cell voltages from the system reply instead.
        resting = sample and not sample["running"]
        if sample and (resting or self._polls % TAG_CHECK_POLLS == 0):
            r = self._query(dev, CMD_SYSTEM)
            if r and r[1] == 0x25 and valid_reply(r, CMD_SYSTEM):
                self._update_tag(decode_tag(r))
        if resting:
            volts = decode_idle_voltages(r) if r else None
            if not volts and sample["pack_v"] is None:
                return None  # no trustworthy voltages this round; keep the last good reading
        if resting and volts:
            # No main-lead reading while idle: the pack voltage is the balance-lead total, if any.
            sample["input_v"], sample["cells_v"] = volts
            taps = [v for v in sample["cells_v"] if v > 1.0]
            sample["pack_v"] = round(sum(taps), 3) if taps else None
        if sample:
            r = self._query(dev, CMD_PROGRAM)
            sample["program"] = decode_program(r, self.machine_id) if r else None
        return sample

    def _update_tag(self, tag):
        """The user changed the ID setting on the charger's front panel: tie it to the charger this
        connection is shown as right now."""
        if tag == self.tag:
            return
        self.tag = self.info["tag"] = tag
        c = self.charger
        if c:
            with c.lock:
                c.device = dict(self.info)
            if tag is not None:
                fleet.bind_tag(tag, c.id)

    def _poll(self, dev):
        while True:
            self._run_commands(dev)
            t0 = time.time()
            sample = self._read_sample(dev)
            if sample and self.charger:
                self.charger._add(sample)
            time.sleep(max(0.0, POLL_SECONDS - (time.time() - t0)))


class Charger:
    """One physical charger as the app knows it: name, settings and the current program's history.
    It outlives its USB connection, so an unplugged charger keeps its tab and readings."""

    def __init__(self, cid, name):
        self.id = cid
        self.name = name
        self.lock = threading.Lock()
        self.conn = None
        self.device = {}
        self.needs_confirm = False   # identity was assumed, not proven; ask the user
        self.last_seen = None
        self.latest = None
        self.history = []
        self.session_id = 0
        self.log_path = None
        self._log_file = None
        self._log = None
        self.logging = True
        self.prefs = dict(DEFAULT_PREFS)
        self.peak_a = 0.0          # highest current in the running program, for the time-left estimate
        self.session_start = None  # wall-clock time the running program started
        self.ir = False            # internal resistance of the running program's pack (see internal_resistance)

    @property
    def connected(self):
        return self.conn is not None

    def save_state(self):
        with self.lock:
            self._saved = (self.latest, list(self.history), self.session_id, self.session_start,
                           self.peak_a, self.last_seen, self.ir)

    def restore_state(self):
        """Put back the state from before a guessed connection. Caller holds self.lock."""
        saved = getattr(self, "_saved", None)
        if saved:
            (self.latest, self.history, self.session_id, self.session_start,
             self.peak_a, self.last_seen, self.ir) = saved
            self.session_id += 1   # tell pages to reload this charger's history
            self._saved = None

    def snapshot(self, since):
        with self.lock:
            cells, reason = detect_cells(self.latest) if self.connected else (None, "Charger not connected.")
            return {
                "peak_a": self.peak_a,
                "detected_cells": cells,
                "detect_error": reason,
                "id": self.id,
                "name": self.name,
                "prefs": self.prefs,
                "connected": self.connected,
                "device": self.device,
                "needs_confirm": self.needs_confirm and self.connected,
                "last_seen": self.last_seen,
                "error": None if self.connected else "Disconnected",
                "latest": self.latest,
                "session": self.session_id,
                "log": os.path.relpath(self.log_path, DATA_DIR) if self.log_path else None,
                "ir": self.ir or None,
                "next": len(self.history),
                "history": self.history[since:] if 0 <= since <= len(self.history) else self.history,
            }

    def start(self, battery, mode, current_a, expected_cells=None):
        """Start a lithium program with the cell count detected from the pack. Returns (ok, message).

        expected_cells is the count the user picked; it must match the detected count.
        """
        if battery not in BATTERIES:
            return False, f"Unsupported battery type: {battery}"
        if mode not in ("storage", "balance"):
            return False, f"Unsupported mode: {mode}"
        with self.lock:
            s, conn = self.latest, self.conn
            if not conn or not s or time.time() - s["t"] > 5:
                return False, "No fresh reading from the charger."
            if s["running"]:
                return False, "A program is already running. Stop it first."
            finished = s["done"] or bool(s["error"])
            cells, reason = detect_cells(s, battery)
        if not cells:
            return False, reason
        if expected_cells and expected_cells != cells:
            return False, (f"You picked {expected_cells}S but the balance lead shows {cells}S. "
                           "Check the pack's label and that the balance plug is fully seated.")
        if not 0.1 <= current_a <= MAX_CHARGE_A:
            return False, f"Current must be between 0.1 and {MAX_CHARGE_A:.1f} A."
        charge_ma = round(current_a * 10) * 100
        discharge_ma = min(charge_ma, int(MAX_DISCHARGE_A * 1000))
        # Like ChargeMaster: clear a finished or errored program before starting the next one.
        if finished and not conn.send(STOP):
            return False, "The charger didn't acknowledge clearing the finished program."
        if "code_100130" in BATTERIES[battery] and not conn.machine_id:
            return False, "Couldn't read the charger's machine id, which LiIon's type code depends on. Reconnect and try again."
        if not conn.send(build_start(battery, cells, mode, charge_ma, discharge_ma, conn.machine_id)):
            return False, "The charger didn't acknowledge the start command."
        name = BATTERIES[battery]["name"]
        label = "balance charge" if mode == "balance" else mode
        return True, f"Started {name} {label} on {cells}S at {charge_ma / 1000:.1f} A."

    def stop(self):
        conn = self.conn
        if not conn or not conn.send(STOP):
            return False, "The charger didn't acknowledge the stop command."
        return True, "Stopped."

    def _add(self, s):
        s["t"] = round(time.time(), 1)
        with self.lock:
            prev = self.latest
            new_session = s["running"] and (
                not prev or not prev["running"] or s["secs"] < prev["secs"]
                or abs((s["t"] - s["secs"]) - (self.session_start or 0)) > 15
            )
            if new_session:
                self._close_log()
                self.history = []
                self.session_id += 1
                self.session_start = s["t"] - s["secs"]
                # If the app restarted mid-program, pick up the peak current seen before.
                self.peak_a = fleet.saved_peak(self.id, self.session_start)
                saved = fleet.saved_ir(self.id, self.session_start)   # notes are worked out again
                self.ir = saved if saved and saved.get("cells_mohm") else False
            if s["running"]:
                self.history.append(s)
                del self.history[:-MAX_HISTORY]
                if self.ir is False:
                    self.ir = internal_resistance(self.history)
                    if self.ir is None:   # joined mid-program: the start may be in an earlier log
                        self.ir = ir_from_logs(self.session_start) or {"note": IR_NOTE_LATE}
                    if self.ir:
                        fleet.save_ir(self.id, self.session_start, self.ir)
                if s["secs"] >= PEAK_SKIP_SECS and s["current_a"] > self.peak_a + 0.02:
                    self.peak_a = s["current_a"]
                    fleet.save_peak(self.id, self.session_start, self.peak_a)
                if self.logging and not self._log:
                    self._open_log()
                if self._log:
                    self._log.writerow([datetime.now().isoformat(timespec="seconds"), s["secs"], s["mah"],
                                        s["pack_v"], s["current_a"], s["temp_c"], *s["cells_v"]])
                    self._log_file.flush()
            elif prev and prev["running"]:
                self._close_log()  # program finished or stopped
                if s["done"] and prev.get("program") and self.peak_a > 0.2:
                    # Learn how this charger actually ends a program, for the time-left estimate.
                    tail = sorted(r["current_a"] for r in self.history[-10:])
                    fleet.record_finish(prev["program"], tail[len(tail) // 2], self.peak_a,
                                        **taper_profile(self.history, self.peak_a, prev["secs"]))
            self.latest = s

    def set_logging(self, on):
        with self.lock:
            self.logging = on
            if not on:
                self._close_log()
                self.log_path = None

    def _close_log(self):
        if self._log_file:
            self._log_file.close()
        self._log_file = self._log = None

    def _open_log(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-") or self.id
        self.log_path = os.path.join(LOG_DIR, f"{slug}-{stamp}.csv")
        self._log_file = open(self.log_path, "w", newline="")
        self._log = csv.writer(self._log_file)
        self._log.writerow(["time", "secs", "mah", "pack_v", "current_a", "temp_c",
                            "cell1", "cell2", "cell3", "cell4", "cell5", "cell6"])


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def clean_prefs(p):
    """Validate saved Start-form settings, falling back to defaults for anything invalid."""
    out = dict(DEFAULT_PREFS)
    if not isinstance(p, dict):
        return out
    if p.get("battery") in BATTERIES:
        out["battery"] = p["battery"]
    if p.get("mode") in ("storage", "balance"):
        out["mode"] = p["mode"]
    try:
        amps = round(float(p.get("current_a")), 1)
        if 0.1 <= amps <= MAX_CHARGE_A:
            out["current_a"] = amps
    except (TypeError, ValueError):
        pass
    return out


class Fleet:
    """Finds chargers as they're plugged in and works out which known charger each one is.

    The chargers have no serial number, so identity is worked out per connection:
      1. a charger mid-program is matched by when that program started (its elapsed-time counter);
      2. otherwise it's assumed to be the charger last used on that USB port, and the user is asked
         to confirm if another known charger could be it;
      3. otherwise it's a new charger.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.chargers = {}   # charger id -> Charger shown in the app (this run)
        self.conns = {}      # HID path -> Connection
        self.names = load_json(NAMES_FILE)
        # Per-charger Start settings keyed by charger id, plus app-wide ones under "app".
        self.settings = load_json(SETTINGS_FILE)
        app = self.settings.get("app")
        self.logging = app.get("logging", True) is not False if isinstance(app, dict) else True

    def scan_forever(self):
        while True:
            for d in hid.enumerate(VID, PID):
                with self.lock:
                    if d["path"] in self.conns:
                        continue
                    conn = self.conns[d["path"]] = Connection(d["path"])
                threading.Thread(target=conn.run, daemon=True).start()
            time.sleep(SCAN_SECONDS)

    # ----- identity -----

    def _known_disconnected(self, exclude=None):
        """Known charger ids (from saved names) that aren't currently plugged in."""
        return [cid for cid in self.names
                if cid != exclude and not (cid in self.chargers and self.chargers[cid].connected)]

    def _get_charger(self, cid):
        c = self.chargers.get(cid)
        if not c:
            if cid not in self.names:
                self.names[cid] = self._next_name()
                self._save_names()
            c = self.chargers[cid] = Charger(cid, self.names[cid])
            c.prefs = clean_prefs(self.settings.get(cid))
            c.logging = self.logging
        return c

    def _next_name(self):
        n = len(self.names) + 1
        while f"Charger {n}" in self.names.values():
            n += 1
        return f"Charger {n}"

    def attach(self, conn, sample):
        """Decide which charger a new connection is, and link them."""
        with self.lock:
            free = self._known_disconnected()
            cid, proven = None, False
            tagged = self._app().get("tags", {}).get(str(conn.tag)) if conn.tag is not None else None
            if tagged in free:
                # The charger's ID setting says exactly which one it is.
                cid, proven = tagged, True
            elif sample and sample["running"]:
                start = time.time() - sample["secs"]
                for k in free:
                    c = self.chargers.get(k)
                    known_start = c.session_start if c and c.session_start else \
                        (self._app().get("peaks", {}).get(k) or {}).get("start")
                    if known_start and abs(known_start - start) < 15:
                        cid, proven = k, True
                        break
            elif sample:
                # An idle or finished charger with a pack on it: the pack's cell voltages barely move
                # at rest, so they identify which unplugged charger this is.
                matches = [k for k in free if self._same_pack(k, sample)]
                if len(matches) == 1:
                    cid, proven = matches[0], True
            ruled_out = unsure = False
            if not cid:
                last = self._app().get("ports", {}).get(conn.port)
                if not last and conn.port in self.names:   # ids used to be the port itself
                    last = conn.port
                if last in free:
                    pack = self._pack_relation(last, sample)
                    if self._contradicts(self.chargers.get(last), sample) or pack == "different":
                        ruled_out = True
                    else:
                        cid, unsure = last, pack == "unknown"
            if not cid:
                cid = secrets.token_hex(4)
            c = self._get_charger(cid)
            # Ask if this was a guess with real doubt, or another known charger could be the one plugged in.
            c.needs_confirm = not proven and (ruled_out or unsure or bool(self._known_disconnected(exclude=cid)))
            if c.needs_confirm:
                c.save_state()   # so a wrong guess can be undone without losing this charger's history
            elif conn.tag is not None and not tagged:
                self._bind_tag(conn.tag, cid)   # identity is certain: remember its ID from now on
            self._link(conn, c)

    def _pack_relation(self, cid, sample):
        """Compare the pack in `sample` with the one charger `cid` had when it was unplugged (kept
        in settings, so this works after an app restart too): "same", "different", "unknown"
        (only one of them has a pack), or "none" (neither does)."""
        c = self.chargers.get(cid)
        if c and c.latest and c.latest["running"]:
            return "unknown"   # it was charging, so its voltages have moved on
        old = [v for v in self._app().get("last_cells", {}).get(cid, []) if v > 1]
        new = [v for v in (sample or {}).get("cells_v") or [] if v > 1]
        if not old and not new:
            return "none"
        if not old or not new:
            return "unknown"
        if len(old) == len(new) and max(abs(a - b) for a, b in zip(old, new)) <= PACK_MATCH_V:
            return "same"
        return "different"

    def _same_pack(self, cid, sample):
        return self._pack_relation(cid, sample) == "same"

    @staticmethod
    def _contradicts(c, sample):
        """True if the readings show this can't be charger `c`: it was mid-program when unplugged,
        and the device now is idle, or running a program that started at a different time."""
        if not c or not c.latest or not c.latest["running"] or not sample:
            return False
        if sample["running"]:
            return abs((time.time() - sample["secs"]) - (c.session_start or 0)) > 15
        return not sample["done"]   # finished could be the same charger; idle can't

    def _link(self, conn, c):
        conn.charger = c
        with c.lock:
            c.conn = conn
            c.device = dict(conn.info)
        self._app().setdefault("ports", {})[conn.port] = c.id
        save_json(SETTINGS_FILE, self.settings)

    def connection_lost(self, conn):
        with self.lock:
            self.conns.pop(conn.path, None)
            c = conn.charger
        if c and c.conn is conn:
            with c.lock:
                c.conn = None
                c.last_seen = round(time.time())
                c._close_log()
                cells = c.latest["cells_v"] if c.latest else None
            with self.lock:
                # Remember the pack it had, to recognise this charger when it's plugged back in.
                self._app().setdefault("last_cells", {})[c.id] = cells or []
                save_json(SETTINGS_FILE, self.settings)

    def reassign(self, cid, target):
        """The user says the charger shown as `cid` is really `target` (a known id, or "new")."""
        with self.lock:
            c = self.chargers.get(cid)
            if not c or not c.conn:
                return False, "That charger isn't plugged in."
            if target == cid:
                c.needs_confirm = False
                if c.conn.tag is not None:
                    self._bind_tag(c.conn.tag, cid)
                return True, "Confirmed."
            if target != "new" and target not in self._known_disconnected(exclude=cid):
                return False, "That charger is already plugged in."
            conn = c.conn
            disposable = self._disposable(c, ignore_port=conn.port)
            t = self._get_charger(secrets.token_hex(4) if target == "new" else target)
            with c.lock:
                c.conn = None
                c.last_seen = round(time.time())
                c.needs_confirm = False
                c._close_log()
                c.restore_state()   # undo the readings the wrong guess gave it
            t.needs_confirm = False
            self._link(conn, t)
            if conn.tag is not None:
                self._bind_tag(conn.tag, t.id)
            if disposable:
                # It only existed because of the wrong guess: drop it so it isn't offered again.
                self.chargers.pop(c.id, None)
                self.names.pop(c.id, None)
                self._save_names()
            return True, f"Now showing it as {t.name}."

    def bind_tag(self, tag, cid):
        """The user set an ID on the charger shown as `cid`: that also confirms which charger it is."""
        with self.lock:
            self._bind_tag(tag, cid)
            c = self.chargers.get(cid)
            if c:
                c.needs_confirm = False
                c._saved = None

    def _bind_tag(self, tag, cid):
        """Tie an ID setting value to a charger (one ID per charger). Caller holds self.lock."""
        tags = self._app().setdefault("tags", {})
        for k in [k for k, v in tags.items() if v == cid]:
            del tags[k]
        tags[str(tag)] = cid
        save_json(SETTINGS_FILE, self.settings)

    def _disposable(self, c, ignore_port):
        """A charger the app created by guessing, with nothing saved for it: no program run,
        no settings, never unplugged with a pack, and not the last charger on any other port."""
        app = self._app()
        ports = {p: k for p, k in app.get("ports", {}).items() if p != ignore_port}
        return (not c.history and c.session_id == 0 and c.id not in self.settings
                and not app.get("last_cells", {}).get(c.id) and c.id not in app.get("peaks", {})
                and c.id not in ports.values())

    def forget(self, cid):
        """Close an unplugged charger's tab. Its name and settings are kept for next time."""
        with self.lock:
            c = self.chargers.get(cid)
            if not c or c.connected:
                return False
            del self.chargers[cid]
            return True

    def snapshot(self, since):
        with self.lock:
            chargers = sorted(self.chargers.values(), key=lambda c: c.name.lower())
            finishes = [f for f in self._app().get("finishes", []) if "tail_secs" in f]
            known = [{"id": k, "name": self.names[k]} for k in self._known_disconnected()]
        return {"logging": self.logging, "cutoff_ratios": self.cutoff_ratios(), "finishes": finishes,
                "known": sorted(known, key=lambda k: k["name"].lower()),
                "chargers": [c.snapshot(since.get(c.id, 0)) for c in chargers]}

    def _app(self):
        app = self.settings.get("app")
        if not isinstance(app, dict):
            app = self.settings["app"] = {}
        return app

    def saved_peak(self, cid, start):
        """Peak current saved for this charger's program, if it's the same program (same start)."""
        with self.lock:
            p = self._app().get("peaks", {}).get(cid)
            return p["peak_a"] if p and abs(p.get("start", 0) - start) < 15 else 0.0

    def save_peak(self, cid, start, peak_a):
        with self.lock:
            p = self._app().setdefault("peaks", {}).get(cid) or {}
            keep = {"ir": p["ir"]} if "ir" in p and abs(p.get("start", 0) - start) < 15 else {}
            self._app()["peaks"][cid] = {"start": round(start), "peak_a": round(peak_a, 3), **keep}
            save_json(SETTINGS_FILE, self.settings)

    def saved_ir(self, cid, start):
        """Internal resistance saved for this charger's program, if it's the same program (same start)."""
        with self.lock:
            p = self._app().get("peaks", {}).get(cid)
            return p.get("ir") if p and abs(p.get("start", 0) - start) < 15 else None

    def save_ir(self, cid, start, ir):
        with self.lock:
            p = self._app().setdefault("peaks", {}).get(cid) or {}
            if abs(p.get("start", 0) - start) >= 15:
                p = {"start": round(start), "peak_a": 0.0}
            p["ir"] = ir
            self._app()["peaks"][cid] = p
            save_json(SETTINGS_FILE, self.settings)

    def record_finish(self, program, end_a, peak_a, taper_v=None, tail_secs=None):
        with self.lock:
            finishes = self._app().setdefault("finishes", [])
            f = {"mode": program.get("mode"), "type": program.get("type"), "cells": program.get("cells"),
                 "end_a": round(end_a, 3), "peak_a": round(peak_a, 3), "at": round(time.time())}
            if taper_v is not None:
                f.update(taper_v=taper_v, tail_secs=tail_secs)
            finishes.append(f)
            del finishes[:-MAX_FINISHES]
            save_json(SETTINGS_FILE, self.settings)

    def cutoff_ratios(self):
        """Current a program ends at, as a share of its peak: learned from finished programs."""
        with self.lock:
            finishes = list(self._app().get("finishes", []))
        kinds = {"storage": ("Storage",), "full": ("Charge", "Balance charge", "Fast charge")}
        out = dict(DEFAULT_CUTOFF_RATIOS)
        for kind, modes in kinds.items():
            ratios = sorted(f["end_a"] / f["peak_a"] for f in finishes
                            if f.get("peak_a", 0) > 0.2 and f.get("mode") in modes)
            if ratios:
                out[kind] = ratios[len(ratios) // 2]
        return out

    def set_logging(self, on):
        with self.lock:
            self.logging = on
            self._app()["logging"] = on
            save_json(SETTINGS_FILE, self.settings)
            chargers = list(self.chargers.values())
        for c in chargers:
            c.set_logging(on)

    def get(self, cid):
        with self.lock:
            return self.chargers.get(cid)

    def rename(self, cid, name):
        name = name.strip()[:40]
        with self.lock:
            c = self.chargers.get(cid)
            if not c or not name:
                return False
            self.names[cid] = name
            self._save_names()
        with c.lock:
            c.name = name
        return True

    def set_prefs(self, cid, prefs):
        with self.lock:
            c = self.chargers.get(cid)
            if not c:
                return None
            clean = clean_prefs(prefs)
            self.settings[cid] = clean
            save_json(SETTINGS_FILE, self.settings)
        with c.lock:
            c.prefs = clean
        return clean

    def _save_names(self):
        save_json(NAMES_FILE, self.names)


fleet = Fleet()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            with open(INDEX_HTML, "rb") as f:
                self._send(200, "text/html; charset=utf-8", f.read())
        elif url.path == "/api/state":
            # since=<id>:<n>,<id>:<n> -> only history samples after n for each charger
            since = {}
            for part in parse_qs(url.query).get("since", [""])[0].split(","):
                cid, _, n = part.partition(":")
                if n.isdigit():
                    since[cid] = int(n)
            self._json(fleet.snapshot(since))
        elif url.path == "/api/info":
            self._json({"local": self._is_local(), "phone_url": self._phone_url(),
                        "data_dir": DATA_DIR if self._is_local() else None})
        elif url.path == "/api/phone-qr.svg" and self._is_local() and self._phone_url():
            # The phone address as a QR code, so a phone's camera can open it. Black on white
            # whatever the theme: that's what phone cameras read most reliably.
            buf = io.BytesIO()
            segno.make(self._phone_url(), error="m").save(buf, kind="svg", scale=8, border=3,
                                                          dark="#000", light="#fff", xmldecl=False)
            self._send(200, "image/svg+xml", buf.getvalue())
        else:
            self._send(404, "text/plain", b"not found")

    def _phone_url(self):
        """Address for phones on the same network, or None when only listening on this PC."""
        host, port = self.server.server_address[:2]
        return f"http://{lan_ip()}:{port}" if host == "0.0.0.0" else None

    def _is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/logging":
            try:
                on = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))["on"]
            except (ValueError, KeyError, TypeError):
                return self._json({"ok": False}, 400)
            fleet.set_logging(bool(on))
            return self._json({"ok": True, "logging": fleet.logging})
        if path == "/api/open-logs":
            # Only from this PC, so a phone on the network can't pop windows open here.
            if not self._is_local() or not hasattr(os, "startfile"):
                return self._json({"ok": False}, 403)
            os.makedirs(LOG_DIR, exist_ok=True)
            os.startfile(LOG_DIR)
            return self._json({"ok": True})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            cid = str(body["id"])
        except (ValueError, KeyError):
            return self._json({"ok": False, "message": "Bad request."}, 400)
        if path == "/api/prefs":
            saved = fleet.set_prefs(cid, body)
            return self._json({"ok": saved is not None, "prefs": saved}, 200 if saved else 404)
        if path == "/api/rename":
            ok = fleet.rename(cid, str(body.get("name", "")))
            return self._json({"ok": ok}, 200 if ok else 400)
        if path == "/api/assign":
            ok, msg = fleet.reassign(cid, str(body.get("to", "")))
            return self._json({"ok": ok, "message": msg}, 200 if ok else 400)
        if path == "/api/forget":
            ok = fleet.forget(cid)
            return self._json({"ok": ok}, 200 if ok else 400)
        charger = fleet.get(cid)
        if path in ("/api/start", "/api/stop") and not charger:
            return self._json({"ok": False, "message": "Unknown charger."}, 404)
        if path == "/api/start":
            try:
                current = float(body.get("current_a", 0))
            except (TypeError, ValueError):
                current = 0
            try:
                cells = int(body.get("cells") or 0)
            except (TypeError, ValueError):
                cells = 0
            ok, msg = charger.start(str(body.get("battery", "lipo")), str(body.get("mode", "")), current, cells)
            return self._json({"ok": ok, "message": msg}, 200 if ok else 400)
        if path == "/api/stop":
            ok, msg = charger.stop()
            return self._json({"ok": ok, "message": msg}, 200 if ok else 400)
        self._send(404, "text/plain", b"not found")

    def _json(self, obj, code=200):
        self._send(code, "application/json", json.dumps(obj).encode())

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def lan_ip():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def start_server(host="0.0.0.0", port=8765):
    """Start charger scanning and the web server on background threads. Returns the server."""
    threading.Thread(target=fleet.scan_forever, daemon=True).start()
    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    server = start_server(args.host, args.port)
    print(f"Charger dashboard: http://localhost:{args.port}")
    if args.host == "0.0.0.0":
        print(f"On your phone:     http://{lan_ip()}:{args.port}")
    print(f"Data folder:       {DATA_DIR}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
