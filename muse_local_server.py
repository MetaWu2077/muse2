#!/usr/bin/env python3
"""
Muse Local Server — Real-time raw BLE receiver + EEG analyzer

Receives raw Muse BLE payloads from the Android app (UDP framed),
decodes with the same pipeline as the cloud server, displays waveforms.

Usage:
    python muse_local_server.py                 # listen on 0.0.0.0:5000
    python muse_local_server.py --port 5000     # custom port
    python muse_local_server.py --simulate      # demo with synthetic data
"""

import os
import sys
import struct
import socket
import threading
import time
import tkinter as tk
from tkinter import ttk
from datetime import datetime
from pathlib import Path
from collections import deque

import numpy as np

# Matplotlib imports (needed at module level for Figure)
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

# ── Paths ─────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AMUSED_DIR = os.path.join(SCRIPT_DIR, "amused-src")
CLOUD_DIR = os.path.join(SCRIPT_DIR, "muse-cloud-server")
REPORT_DIR = os.path.join(SCRIPT_DIR, "report")
os.makedirs(REPORT_DIR, exist_ok=True)
sys.path.insert(0, AMUSED_DIR)
sys.path.insert(0, CLOUD_DIR)

import muse_metrics as mm

# Lazy imports
_report_imports_done = False
_decoder_imports_done = False
MuseRealtimeDecoder = None


def _ensure_decoder():
    global _decoder_imports_done, MuseRealtimeDecoder
    if _decoder_imports_done:
        return MuseRealtimeDecoder is not None
    try:
        from muse_realtime_decoder import MuseRealtimeDecoder as _MRD
        MuseRealtimeDecoder = _MRD
        _decoder_imports_done = True
        return True
    except ImportError as e:
        print(f"Decoder import failed: {e}")
        return False


def _ensure_imports():
    global _report_imports_done
    if _report_imports_done:
        return True
    try:
        from scipy.signal import welch, butter, filtfilt, find_peaks
        global report_generator
        import report_generator
        _report_imports_done = True
        return True
    except ImportError as e:
        print(f"Missing dependency: {e}")
        return False


# ── OSC Decoder ────────────────────────────────────────────────────────────

def decode_osc(data: bytes):
    """Decode OSC packet. Returns (path, [float, ...]) or None."""
    try:
        # 1. Path: null-terminated string, padded to 4-byte boundary
        path_end = data.find(b'\x00')
        if path_end < 0:
            return None
        path = data[:path_end].decode('utf-8', errors='replace')

        # Align to next 4-byte boundary after path
        offset = path_end + 1
        offset = ((offset + 3) // 4) * 4

        # 2. Type tag: ",f..." null-terminated, padded to 4-byte boundary
        type_end = data.find(b'\x00', offset)
        if type_end < 0:
            return None
        type_tag = data[offset:type_end].decode('utf-8', errors='replace')
        if not type_tag.startswith(','):
            return None

        # Align past type tag
        offset = type_end + 1
        offset = ((offset + 3) // 4) * 4

        # 3. Values: each float is 4 bytes big-endian, no padding needed between them
        types = type_tag[1:]
        values = []
        for t in types:
            if offset + 4 > len(data):
                break
            if t == 'f':
                val = struct.unpack('>f', data[offset:offset + 4])[0]
                values.append(val)
                offset += 4
            elif t == 'i':
                val = struct.unpack('>i', data[offset:offset + 4])[0]
                values.append(float(val))
                offset += 4

        return (path, values)
    except Exception:
        return None


RAW_MAGIC = b'\x4d\x01'
_ATHENA_TAGS = frozenset({0x11, 0x12, 0x34, 0x35, 0x36, 0x47, 0x88, 0x98})


def decode_raw_frame(data: bytes):
    """Decode Android RawUdpSender frame: 0x4D 0x01 | seq u32 | len u16 | payload."""
    if len(data) >= 8 and data[0:2] == RAW_MAGIC:
        length = struct.unpack('>H', data[6:8])[0]
        end = 8 + length
        if end <= len(data):
            return data[8:end]
    return None


def decode_incoming_udp(data: bytes):
    """Return Muse BLE payload from framed UDP, bare BLE, or None."""
    payload = decode_raw_frame(data)
    if payload is not None:
        return payload
    # Bare Athena notification (no UDP wrapper)
    if len(data) >= 15 and (data[9] & 0xFF) in _ATHENA_TAGS:
        return data
    return None


# ── UDP Receiver ───────────────────────────────────────────────────────────

class UdpReceiver:
    """Background thread receiving raw BLE frames (and legacy OSC) over UDP."""

    def __init__(self, host="0.0.0.0", port=5000):
        self.host = host
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.settimeout(1.0)
        self.running = False
        self.thread = None
        self.callbacks = {}  # path → callback (legacy OSC)
        self.raw_callback = None
        self.packet_count = 0
        self.byte_count = 0
        self.raw_count = 0  # Total UDP datagrams received (including malformed)
        self.last_error = None
        self.start_time = None
        self.last_addr = None

    def on(self, path, callback):
        self.callbacks[path] = callback

    def on_raw(self, callback):
        self.raw_callback = callback

    def start(self):
        self.sock.bind((self.host, self.port))
        self.running = True
        self.start_time = time.time()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)
        try:
            self.sock.close()
        except Exception:
            pass

    def _run(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(65536)
                self.raw_count += 1
                self.last_addr = addr
                self.byte_count += len(data)
                payload = decode_incoming_udp(data)
                if payload is not None and self.raw_callback:
                    self.raw_callback(payload, addr)
                    self.packet_count += 1
                    continue
                result = decode_osc(data)
                if result:
                    path, values = result
                    if path in self.callbacks:
                        self.callbacks[path](values, addr)
                        self.packet_count += 1
                else:
                    # Malformed packet — still count it for diagnostics
                    pass
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    self.last_error = str(e)
                    continue


# ── Data Buffer ────────────────────────────────────────────────────────────

CHANNELS = ["TP9", "AF7", "AF8", "TP10"]
N_CHANNELS = 4
EEG_SAMPLES_PER_PACKET = 4  # Android sends 4 samples per BLE packet
SFREQ = 256.0
DISP_WINDOW_SEC = 5  # Display window in seconds
DISP_SAMPLES = int(SFREQ * DISP_WINDOW_SEC)  # 1280 samples

# For band power (10 s window, update every 2 s; needs 8 s before first estimate)
BP_EPOCH_SEC = 10
BP_EPOCH_SAMPLES = int(SFREQ * BP_EPOCH_SEC)  # 2560 samples
BP_MIN_SAMPLES = int(SFREQ * 8)  # match report_generator.BP_MIN_EPOCH_SEC

# Map report_generator band names → bp_history keys
_BAND_KEY = {"Delta": "delta", "Theta": "theta", "Alpha": "alpha",
             "Beta": "beta", "Gamma": "gamma"}

# PPG / IMU — shared with cloud (muse_metrics)
PPG_SFREQ = mm.PPG_SFREQ
PPG_DISP_SEC = 5
PPG_DISP_SAMPLES = int(PPG_SFREQ * PPG_DISP_SEC)
PPG_ANALYSIS_SEC = 30
PPG_ANALYSIS_SAMPLES = int(PPG_SFREQ * PPG_ANALYSIS_SEC)
OPTICS_BASELINE_SAMPLES = mm.OPTICS_BASELINE_SAMPLES
OPTICS_CHANNELS_8 = mm.OPTICS_CHANNELS_8
OPTICS_CHANNELS_4 = mm.OPTICS_CHANNELS_4
PPG_HR_CHANNEL = mm.PPG_HR_CHANNEL
IMU_SFREQ = mm.IMU_SFREQ
IMU_DISP_SEC = 5
IMU_DISP_SAMPLES = int(IMU_SFREQ * IMU_DISP_SEC)


def _optics_channel_names(n_ch: int) -> list:
    if n_ch == 8:
        return OPTICS_CHANNELS_8
    if n_ch == 4:
        return OPTICS_CHANNELS_4
    return [f"opt{i}" for i in range(n_ch)]


class DataBuffer:
    """Thread-safe ring buffer for EEG, PPG, and IMU data."""

    def __init__(self):
        self.lock = threading.Lock()
        self.eeg = {ch: deque(maxlen=DISP_SAMPLES) for ch in CHANNELS}
        self.eeg_all = {ch: [] for ch in CHANNELS}  # Unlimited for .bin saving
        self.optics = {name: deque(maxlen=PPG_ANALYSIS_SAMPLES) for name in OPTICS_CHANNELS_8}
        self.optics_n_ch = 0
        self.optics_baseline = {}
        self.optics_baseline_ready = False
        self.ppg_ir = deque(maxlen=PPG_DISP_SAMPLES)  # display: LO_NIR
        self.ppg_ir_analysis = deque(maxlen=PPG_ANALYSIS_SAMPLES)
        self.acc_x = deque(maxlen=IMU_DISP_SAMPLES)
        self.acc_y = deque(maxlen=IMU_DISP_SAMPLES)
        self.acc_z = deque(maxlen=IMU_DISP_SAMPLES)
        self.gyro_x = deque(maxlen=IMU_DISP_SAMPLES)
        self.gyro_y = deque(maxlen=IMU_DISP_SAMPLES)
        self.gyro_z = deque(maxlen=IMU_DISP_SAMPLES)
        self.latest_vitals = {
            "hr": None, "rmssd": None, "sdnn": None, "pnn50": None,
            "motion": "---", "pitch": None, "roll": None,
            "acc_std": None, "gyro_rms": None,
            "tsi": None, "d_hbo2": None, "d_hbr": None,
            "spo2": None, "optics_ch": 0,
        }
        self.last_vitals_time = 0
        self.sample_count = 0
        self.session_start = None
        self.last_bp_time = 0
        self.bp_history = {"delta": [], "theta": [], "alpha": [], "beta": [], "gamma": [], "time": []}
        self.latest_bp = {}  # band → latest dB (for real-time display)
        self.latest_state = ""
        self.raw_packets = []  # For .bin saving
        self.total_bytes = 0

    def add_eeg(self, samples: list):
        """samples: flat list of ns*5 floats (ns samples × 5 channels)"""
        with self.lock:
            if not self.session_start:
                self.session_start = time.time()

            ns = len(samples) // 5
            for s in range(ns):
                offset = s * 5
                for i, ch in enumerate(CHANNELS):
                    val = samples[offset + i] if offset + i < len(samples) else 0.0
                    self.eeg[ch].append(val)
                    self.eeg_all[ch].append(val)
                self.sample_count += 1

    def process_raw_packet(self, data: bytes, decoder):
        """Ingest one Muse BLE payload via MuseRealtimeDecoder (same as cloud)."""
        decoded = decoder.decode(data, datetime.now())
        with self.lock:
            if not self.session_start:
                self.session_start = time.time()
            if decoded.eeg:
                for ch in CHANNELS:
                    if ch in decoded.eeg:
                        for v in decoded.eeg[ch]:
                            self.eeg[ch].append(float(v))
                            self.eeg_all[ch].append(float(v))
                            self.sample_count += 1
            if decoded.ppg:
                self.optics_n_ch = max(self.optics_n_ch, len(decoded.ppg))
                for name, samples in decoded.ppg.items():
                    for v in samples:
                        fv = float(v)
                        if name in self.optics:
                            self.optics[name].append(fv)
                        if name == PPG_HR_CHANNEL:
                            self.ppg_ir.append(fv)
                            self.ppg_ir_analysis.append(fv)
            if decoded.imu:
                for row in decoded.imu.get("accel") or []:
                    if len(row) >= 3:
                        self.acc_x.append(float(row[0]))
                        self.acc_y.append(float(row[1]))
                        self.acc_z.append(float(row[2]))
                for row in decoded.imu.get("gyro") or []:
                    if len(row) >= 3:
                        self.gyro_x.append(float(row[0]))
                        self.gyro_y.append(float(row[1]))
                        self.gyro_z.append(float(row[2]))

    def add_ppg(self, values: list):
        """Append all optics channels from flat ns×nChannels batch."""
        with self.lock:
            if not values:
                return
            n = len(values)
            for n_ch in (8, 4, 16, 3, 2, 1):
                if n % n_ch != 0:
                    continue
                ns = n // n_ch
                names = _optics_channel_names(n_ch)
                self.optics_n_ch = max(self.optics_n_ch, n_ch)
                for s in range(ns):
                    for c in range(n_ch):
                        name = names[c] if c < len(names) else f"opt{c}"
                        v = float(values[s * n_ch + c])
                        if name in self.optics:
                            self.optics[name].append(v)
                        if name == PPG_HR_CHANNEL:
                            self.ppg_ir.append(v)
                            self.ppg_ir_analysis.append(v)
                return
            v = float(sum(values) / n)
            self.ppg_ir.append(v)
            self.ppg_ir_analysis.append(v)
            if PPG_HR_CHANNEL in self.optics:
                self.optics[PPG_HR_CHANNEL].append(v)

    def _update_optics_baseline(self):
        """Capture per-channel median baseline from first ~10 s."""
        if self.optics_baseline_ready:
            return
        needed = ["LO_NIR", "LO_IR"]
        if self.optics_n_ch >= 8:
            needed += ["LI_NIR", "LI_IR"]
        for name in needed:
            if len(self.optics.get(name, ())) < OPTICS_BASELINE_SAMPLES:
                return
        for name in needed:
            self.optics_baseline[name] = float(
                np.median(list(self.optics[name])[:OPTICS_BASELINE_SAMPLES]))
        self.optics_baseline_ready = True

    def get_optics_arrays(self):
        with self.lock:
            out = {}
            for name, buf in self.optics.items():
                arr = list(buf)
                if arr:
                    out[name] = np.array(arr)
            return out, self.optics_n_ch

    def add_acc(self, values: list):
        with self.lock:
            if len(values) >= 3:
                self.acc_x.append(float(values[0]))
                self.acc_y.append(float(values[1]))
                self.acc_z.append(float(values[2]))

    def add_gyro(self, values: list):
        with self.lock:
            if len(values) >= 3:
                self.gyro_x.append(float(values[0]))
                self.gyro_y.append(float(values[1]))
                self.gyro_z.append(float(values[2]))

    def get_eeg_arrays(self):
        with self.lock:
            result = {}
            for ch in CHANNELS:
                arr = list(self.eeg[ch])
                result[ch] = np.array(arr) if arr else np.zeros(0)
            return result

    def get_bp_history(self):
        with self.lock:
            return dict(self.bp_history)

    def duration_seconds(self):
        if self.session_start:
            return time.time() - self.session_start
        return 0

    def get_latest_bp(self):
        with self.lock:
            return dict(self.latest_bp), self.latest_state

    def get_ppg_array(self):
        with self.lock:
            arr = list(self.ppg_ir)
            return np.array(arr) if arr else np.zeros(0)

    def get_acc_array(self):
        """Return N×3 accelerometer array for motion analysis."""
        with self.lock:
            if not self.acc_x:
                return np.zeros((0, 3))
            return np.column_stack([
                list(self.acc_x), list(self.acc_y), list(self.acc_z)])

    def get_acc_arrays(self):
        with self.lock:
            if not self.acc_x:
                return np.zeros(0), np.zeros(0), np.zeros(0)
            return np.array(self.acc_x), np.array(self.acc_y), np.array(self.acc_z)

    def get_gyro_arrays(self):
        with self.lock:
            if not self.gyro_x:
                return np.zeros(0), np.zeros(0), np.zeros(0)
            return np.array(self.gyro_x), np.array(self.gyro_y), np.array(self.gyro_z)

    def get_vitals(self):
        with self.lock:
            return dict(self.latest_vitals)

    def compute_vitals(self):
        """Update HR/HRV, fNIRS/SpO₂ prototype, and head motion."""
        with self.lock:
            self._update_optics_baseline()
            optics = {k: np.array(list(v)) for k, v in self.optics.items() if v}
            baseline = dict(self.optics_baseline)
            baseline_ready = self.optics_baseline_ready
            optics_n_ch = self.optics_n_ch
            ppg = np.array(self.ppg_ir_analysis) if self.ppg_ir_analysis else np.zeros(0)
            if len(self.acc_x) >= 5:
                acc = np.column_stack([
                    list(self.acc_x), list(self.acc_y), list(self.acc_z)])
            else:
                acc = np.zeros((0, 3))
            if len(self.gyro_x) >= 5:
                gyro = np.column_stack([
                    list(self.gyro_x), list(self.gyro_y), list(self.gyro_z)])
            else:
                gyro = None

        if len(ppg) >= int(PPG_SFREQ * 3):
            ppg_m = mm.analyze_ppg(ppg)
            if ppg_m:
                with self.lock:
                    self.latest_vitals["hr"] = ppg_m["hr"]
                    self.latest_vitals["rmssd"] = ppg_m["rmssd"]
                    self.latest_vitals["sdnn"] = ppg_m["sdnn"]
                    self.latest_vitals["pnn50"] = ppg_m["pnn50"]

        if baseline_ready:
            lo_nir = optics.get("LO_NIR")
            lo_ir = optics.get("LO_IR")
            if lo_nir is not None and lo_ir is not None:
                fn = mm.analyze_fnirs(lo_nir, lo_ir, baseline)
                if fn:
                    with self.lock:
                        self.latest_vitals["tsi"] = fn["tsi"]
                        self.latest_vitals["d_hbo2"] = fn["d_hbo2"]
                        self.latest_vitals["d_hbr"] = fn["d_hbr"]

            li_nir = optics.get("LI_NIR")
            li_ir = optics.get("LI_IR")
            if li_nir is not None and li_ir is not None:
                ox = mm.estimate_spo2(li_nir, li_ir)
                if ox:
                    with self.lock:
                        self.latest_vitals["spo2"] = ox["spo2"]

        with self.lock:
            self.latest_vitals["optics_ch"] = optics_n_ch

        if len(acc) >= 5:
            mot = mm.analyze_motion(acc, gyro)
            if mot:
                with self.lock:
                    self.latest_vitals["motion"] = mot["status"]
                    self.latest_vitals["pitch"] = mot["pitch"]
                    self.latest_vitals["roll"] = mot["roll"]
                    self.latest_vitals["acc_std"] = mot["acc_std"]
                    self.latest_vitals["gyro_rms"] = mot["gyro_rms"]

    def bp_ready_fraction(self):
        """0..1 progress toward first band-power estimate."""
        with self.lock:
            min_len = min((len(self.eeg_all[ch]) for ch in CHANNELS), default=0)
        return min(1.0, min_len / max(BP_EPOCH_SAMPLES, 1))

    def save_bin(self):
        """Save EEG data as .npz and generate HTML report. Returns (data_path, report_path) or None."""
        if not self.eeg_all[CHANNELS[0]]:
            return None

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        timestamp = datetime.now().isoformat()

        # Build numpy array: (n_samples, 4)
        n = min(len(self.eeg_all[ch]) for ch in CHANNELS)
        if n == 0:
            return None
        eeg_arr = np.zeros((n, N_CHANNELS))
        for i, ch in enumerate(CHANNELS):
            eeg_arr[:n, i] = self.eeg_all[ch][:n]

        # Save raw data
        data_path = os.path.join(REPORT_DIR, f"local_{ts}.npz")
        meta = {
            "timestamp": timestamp,
            "sfreq": SFREQ,
            "channels": CHANNELS,
            "samples": self.sample_count,
            "duration": self.duration_seconds(),
        }
        np.savez(data_path, eeg=eeg_arr, meta=meta)
        print(f"Data saved: {data_path}")

        report_path = os.path.join(REPORT_DIR, f"local_{ts}.report.html")
        if not _ensure_imports():
            print("Report skipped: missing scipy/report_generator dependencies")
            return data_path, None

        try:
            result = _generate_report_from_array(eeg_arr, SFREQ, ts, report_path)
            if not result:
                print(f"Report generation returned no output for {report_path}")
                return data_path, None
        except Exception as e:
            print(f"Report generation failed: {e}")
            return data_path, None

        return data_path, report_path

    def compute_band_power(self):
        """Run band power analysis on latest epoch if enough data."""
        if not _ensure_imports():
            return

        with self.lock:
            min_len = min(len(self.eeg_all[ch]) for ch in CHANNELS)
            epoch_samples = min(min_len, BP_EPOCH_SAMPLES)
            if epoch_samples < BP_MIN_SAMPLES:
                return
            if time.time() - self.last_bp_time < 2.0:
                return

            self.last_bp_time = time.time()

            data = np.zeros((epoch_samples, N_CHANNELS))
            for i, ch in enumerate(CHANNELS):
                chunk = self.eeg_all[ch][-epoch_samples:]
                data[:len(chunk), i] = chunk

        try:
            bp = report_generator.compute_band_power_chunk(data, SFREQ)
            if not bp:
                return
            if not all(bp["db"][b].shape[0] == N_CHANNELS for b in report_generator.BANDS):
                return

            means = {band: float(np.mean(bp["db"][band])) for band in report_generator.BANDS}
            alpha = means["Alpha"]
            theta = means["Theta"]
            beta = means["Beta"]
            delta = means["Delta"]
            tb = 10 ** ((theta - beta) / 10.0) if beta > -100 else 0
            noisy = bool(np.all(bp["noise"]))
            state = "噪声 Noise" if noisy else report_generator.classify_state(
                alpha, theta, beta, delta, tb, None)

            with self.lock:
                now = time.time() - (self.session_start or 0)
                self.bp_history["time"].append(now / 60)
                for band, val in means.items():
                    key = _BAND_KEY[band]
                    self.bp_history[key].append(val)
                    self.latest_bp[key] = val
                self.latest_state = state
                max_bp = 120
                if len(self.bp_history["time"]) > max_bp:
                    for k in self.bp_history:
                        self.bp_history[k] = self.bp_history[k][-max_bp:]
        except Exception as e:
            print(f"Band power compute error: {e}")


# ── GUI ─────────────────────────────────────────────────────────────────────

def _generate_report_from_array(eeg_arr, sfreq, session_name, output_path):
    """Generate an HTML report directly from a numpy EEG array, bypassing .bin files."""
    import report_generator as rg
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from base64 import b64encode
    import io
    from pathlib import Path

    n_total = eeg_arr.shape[0]
    n_chan = eeg_arr.shape[1]
    duration = n_total / sfreq

    epoch_step = rg.EPOCH_STEP
    epoch_width = rg.EPOCH_SECONDS
    n_epochs = max(1, int((duration - epoch_width) / epoch_step) + 1)

    rows = []
    noise_count = 0

    for ep in range(n_epochs):
        t_start = int(ep * epoch_step * sfreq)
        t_end = int(t_start + epoch_width * sfreq)
        chunk = eeg_arr[t_start:t_end, :]
        if chunk.shape[0] < 10:
            continue

        row = {"minute": round(ep * epoch_step / 60, 1)}

        bp = rg.compute_band_power_chunk(chunk, sfreq)
        if bp and all(bp["db"][b].shape[0] == n_chan for b in rg.BANDS):
            all_noisy = np.all(bp["noise"])
            row["noise"] = all_noisy
            if all_noisy:
                noise_count += 1
            row["alpha_db"] = float(np.mean(bp["db"]["Alpha"]))
            row["theta_db"] = float(np.mean(bp["db"]["Theta"]))
            row["beta_db"]  = float(np.mean(bp["db"]["Beta"]))
            row["delta_db"] = float(np.mean(bp["db"]["Delta"]))
            row["gamma_db"] = float(np.mean(bp["db"]["Gamma"]))
            tb = 10 ** ((row["theta_db"] - row["beta_db"]) / 10.0) if row["beta_db"] > -100 else 0
            row["theta_beta"] = tb
            row["state"] = "噪声 Noise" if all_noisy else rg.classify_state(
                row["alpha_db"], row["theta_db"], row["beta_db"],
                row["delta_db"], row["theta_beta"], None)
        else:
            for k in ["alpha_db","theta_db","beta_db","delta_db","gamma_db","theta_beta","noise","state"]:
                row[k] = None
        rows.append(row)

    if not rows:
        return None

    # Smooth
    for key in ["delta_db", "theta_db", "alpha_db", "beta_db", "gamma_db", "theta_beta"]:
        vals = np.array([r.get(key, np.nan) for r in rows], dtype=float)
        smoothed = rg.moving_average(vals, rg.SMOOTH_POINTS)
        for i, r in enumerate(rows):
            r[key + "_smooth"] = float(smoothed[i]) if not np.isnan(smoothed[i]) else None

    # Charts
    minutes = [r["minute"] for r in rows]
    colors = {"delta":"#888","theta":"#ffe66d","alpha":"#4ecdc4","beta":"#ff6b6b","gamma":"#a37eba","tb":"#ff8c42"}
    sc = {"放松 Relaxed":"#4ecdc4","中性 Neutral":"#888","专注 Focused":"#ff6b6b",
          "冥想 Meditative":"#ffe66d","活跃 Active":"#ff8c42","昏沉 Drowsy":"#a37eba","噪声 Noise":"#f44"}

    def n(v): return v if v is not None else np.nan

    fig1, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7))
    fig1.patch.set_facecolor("#0f0f11")
    for ax in [ax1, ax2]:
        ax.set_facecolor("#1a1a2e"); ax.tick_params(colors="#888")
        for s in ax.spines.values(): s.set_color("#333")
        ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
        ax.grid(True, color="#222", linewidth=0.3)

    for b, l in [("delta","δ Delta"),("theta","θ Theta"),("alpha","α Alpha"),("beta","β Beta"),("gamma","γ Gamma")]:
        ax1.plot(minutes, [n(r.get(b+"_db_smooth")) for r in rows], color=colors[b], lw=1.5, marker="o", ms=3, label=l)
    ax1.set_ylabel("Power (dB re 1 μV²)", color="#ccc")
    ax1.set_title("EEG Absolute Band Power", color="#ccc", fontweight="bold")
    ax1.legend(fontsize=7, facecolor="#1a1a2e", edgecolor="#333", labelcolor="#ccc")

    tb_vals = [n(r.get("theta_beta_smooth")) for r in rows]
    ax2.fill_between(minutes, 0, tb_vals, color=colors["tb"], alpha=0.3)
    ax2.plot(minutes, tb_vals, color=colors["tb"], lw=2, marker="s", ms=4)
    ax2.set_ylabel("θ/β Ratio", color="#ccc"); ax2.set_xlabel("Time (min)", color="#ccc")
    ax2.set_title("Theta/Beta Ratio", color="#ccc", fontweight="bold"); ax2.set_ylim(bottom=0)
    chart1 = rg.fig_to_b64(fig1); plt.close(fig1)

    fig3, ax3 = plt.subplots(figsize=(10, 2))
    fig3.patch.set_facecolor("#0f0f11"); ax3.set_facecolor("#0f0f11")
    ax3.set_ylim(0,1); ax3.set_xlim(0, n_epochs); ax3.set_yticks([])
    ax3.set_xlabel("Time (epoch)", color="#ccc")
    ax3.set_title("Brain State Timeline", color="#ccc", fontweight="bold")
    ax3.tick_params(colors="#888")
    for s in ax3.spines.values(): s.set_visible(False)
    for i, r in enumerate(rows):
        state = r.get("state","?") or "?"
        ax3.axvspan(i, i+1, facecolor=sc.get(state,"#888"), alpha=0.6)
    chart3 = rg.fig_to_b64(fig3); plt.close(fig3)

    clean_rows = [r for r in rows if not r.get("noise")]
    src = clean_rows if clean_rows else rows
    avg_delta = np.nanmean([r["delta_db"] for r in src if r.get("delta_db") is not None]) if src else 0
    avg_alpha = np.nanmean([r["alpha_db"] for r in src if r.get("alpha_db") is not None]) if src else 0
    avg_tb    = np.nanmean([r["theta_beta"] for r in src if r.get("theta_beta") is not None]) if src else 0
    clean_pct = 100 * (n_epochs - noise_count) / max(n_epochs, 1)

    noise_html = ""
    if noise_count > 0:
        noise_html = f"""<div class="summary">
    <div style="border:1px solid #ef5350;"><span class="label">Noise epochs</span><br>
        <span class="value" style="color:#ef5350;">{noise_count}/{n_epochs}</span></div>
    <div><span class="label">Clean data</span><br><span class="value">{clean_pct:.0f}%</span></div>
</div>"""

    table_rows = ""
    for r in rows:
        def vd(v): return f"{v:.1f}" if v is not None else "-"
        nm = " ⚠" if r.get("noise") else ""
        table_rows += f"<tr><td>{r['minute']}{nm}</td><td>{vd(r.get('delta_db_smooth'))}</td><td>{vd(r.get('theta_db_smooth'))}</td><td>{vd(r.get('alpha_db_smooth'))}</td><td>{vd(r.get('beta_db_smooth'))}</td><td>{vd(r.get('gamma_db_smooth'))}</td><td style='color:{sc.get(r.get('state',''),'#888')}'>{r.get('state','-')}</td></tr>"

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>{session_name}</title>
<style>body{{background:#0f0f11;color:#ccc;font-family:system-ui,sans-serif;max-width:1000px;margin:0 auto;padding:20px}}
h1{{color:#fff;border-bottom:2px solid #333}}h2{{color:#ddd;margin-top:24px}}
table{{border-collapse:collapse;width:100%;margin:12px 0;font-size:12px}}
th{{background:#1a1a2e;color:#fff;padding:6px 8px}}td{{padding:5px 8px;border-bottom:1px solid #222;text-align:right}}
.summary{{display:flex;gap:16px;flex-wrap:wrap;margin:12px 0}}
.summary div{{background:#1a1a2e;padding:10px 16px;border-radius:6px;min-width:100px}}
.summary .value{{font-size:22px;font-weight:bold;color:#4ecdc4}}.summary .label{{font-size:11px;color:#888}}
.note{{font-size:11px;color:#888;margin-top:24px;border-top:1px solid #333;padding-top:8px}}
img{{max-width:100%;border:1px solid #333;border-radius:4px}}</style></head><body>
<h1>EEG Report<br><small>{session_name}</small></h1>
{noise_html}
<div class="summary">
<div><span class="label">Duration</span><br><span class="value">{duration/60:.1f} min</span></div>
<div><span class="label">Avg Delta</span><br><span class="value">{avg_delta:.1f} dB</span></div>
<div><span class="label">Avg Alpha</span><br><span class="value">{avg_alpha:.1f} dB</span></div>
<div><span class="label">Avg T/B Ratio</span><br><span class="value">{avg_tb:.2f}</span></div>
</div>
<h2>1. EEG Band Power (dB)</h2><img src="data:image/png;base64,{chart1}">
<h2>2. Brain State Timeline</h2><img src="data:image/png;base64,{chart3}">
<h2>3. Per-Epoch Data</h2>
<table><tr><th>Time</th><th>Delta</th><th>Theta</th><th>Alpha</th><th>Beta</th><th>Gamma</th><th>State</th></tr>{table_rows}</table>
<p class="note">Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}<br>
Epoch: {epoch_width}s, step: {epoch_step}s | dB = 10×log<sub>10</sub>(μV²) | Noise: β+γ &gt; {rg.NOISE_BG_RATIO*100:.0f}%</p>
</body></html>"""

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    Path(output_path).write_text(html, encoding="utf-8")
    return output_path


class MuseLocalServer(tk.Tk):
    def __init__(self, simulate=False, port=5000):
        super().__init__()
        self.title("Muse Local Analyzer")
        self.geometry("1200x920")
        self.minsize(900, 700)
        self.configure(bg="#0f1923")

        self.simulate = simulate
        self.port = port
        self.running = False
        self.buffer = DataBuffer()
        self.receiver = None
        self.decoder = None
        self._anim_id = None
        self._last_vitals_ui = 0

        _ensure_imports()
        self._build_ui()

    def _build_ui(self):
        # ── Header ──
        header = tk.Frame(self, bg="#1a2d3d", padx=12, pady=8)
        header.pack(fill=tk.X)

        tk.Label(header, text="🧠 Muse Local Analyzer", fg="#4fc3f7", bg="#1a2d3d",
                 font=("", 14, "bold")).pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value="Stopped")
        self.status_label = tk.Label(header, textvariable=self.status_var,
                                      fg="#78909c", bg="#1a2d3d", font=("", 10))
        self.status_label.pack(side=tk.LEFT, padx=(20, 0))

        # Show current local IP prominently
        local_ip = self._get_local_ip()
        self.ip_label = tk.Label(header, text=f"🖥 {local_ip}:{self.port}",
                                  fg="#ffb74d", bg="#1a2d3d", font=("Consolas", 11, "bold"))
        self.ip_label.pack(side=tk.LEFT, padx=(20, 0))

        self.pkt_var = tk.StringVar(value="BLE: 0")
        tk.Label(header, textvariable=self.pkt_var, fg="#546e7a",
                 bg="#1a2d3d", font=("", 9)).pack(side=tk.LEFT, padx=(20, 0))

        self.raw_var = tk.StringVar(value="UDP: 0")
        tk.Label(header, textvariable=self.raw_var, fg="#546e7a",
                 bg="#1a2d3d", font=("", 9)).pack(side=tk.LEFT, padx=(10, 0))

        self.dur_var = tk.StringVar(value="00:00")
        tk.Label(header, textvariable=self.dur_var, fg="#546e7a",
                 bg="#1a2d3d", font=("", 9)).pack(side=tk.LEFT, padx=(10, 0))

        # Buttons
        btn_frame = tk.Frame(header, bg="#1a2d3d")
        btn_frame.pack(side=tk.RIGHT)

        self.btn_start = tk.Button(btn_frame, text="▶ Start", command=self._toggle,
                                    bg="#2d5f8a", fg="#fff", font=("", 10),
                                    relief=tk.FLAT, padx=14, pady=3, cursor="hand2")
        self.btn_start.pack(side=tk.LEFT, padx=4)

        self.btn_test = tk.Button(btn_frame, text="🔍 Self Test", command=self._self_test,
                                   bg="#37474f", fg="#ccc", font=("", 10),
                                   relief=tk.FLAT, padx=14, pady=3, cursor="hand2")
        self.btn_test.pack(side=tk.LEFT, padx=4)

        self.btn_save = tk.Button(btn_frame, text="💾 Save & Report", command=self._save_and_report,
                                   bg="#37474f", fg="#ccc", font=("", 10),
                                   relief=tk.FLAT, padx=14, pady=3, cursor="hand2",
                                   state=tk.DISABLED)
        self.btn_save.pack(side=tk.LEFT, padx=4)

        # ── Main content ──
        main = tk.PanedWindow(self, orient=tk.HORIZONTAL, bg="#0f1923", sashwidth=2)
        main.pack(fill=tk.BOTH, expand=True)

        # Left: EEG waveform
        left = tk.Frame(main, bg="#0f1923")
        main.add(left)

        self.fig = Figure(figsize=(8, 9), facecolor="#0f1923", dpi=100)
        gs = self.fig.add_gridspec(4, 1, height_ratios=[3, 1.8, 1.2, 1.2], hspace=0.45)

        # Row 0: 4-channel EEG
        self.ax_eeg = self.fig.add_subplot(gs[0])
        self.ax_eeg.set_facecolor("#15232e")
        self.ax_eeg.set_title("EEG Waveform (5s window)", color="#ccc", fontsize=10)
        self.ax_eeg.set_ylabel("μV", color="#888")
        self.ax_eeg.tick_params(colors="#888", labelsize=8)
        self.ax_eeg.set_ylim(-100, 100)
        self.eeg_lines = {}
        colors = ["#4fc3f7", "#81c784", "#ffb74d", "#e57373"]
        for ch, c in zip(CHANNELS, colors):
            line, = self.ax_eeg.plot([], [], color=c, linewidth=0.6, label=ch)
            self.eeg_lines[ch] = line
        self.ax_eeg.legend(loc="upper right", fontsize=7, facecolor="#15232e",
                           edgecolor="#333", labelcolor="#ccc", ncol=4)

        # Row 1: Band power bars
        self.ax_bp = self.fig.add_subplot(gs[1])
        self.ax_bp.set_facecolor("#15232e")
        self.ax_bp.set_title("Band Power (dB re 1 μV²)", color="#ccc", fontsize=10)
        self.ax_bp.set_ylabel("dB", color="#888")
        self.ax_bp.tick_params(colors="#888", labelsize=8)
        self.ax_bp.set_ylim(-10, 40)
        band_labels = ["Delta", "Theta", "Alpha", "Beta", "Gamma"]
        band_colors = ["#888888", "#ffe66d", "#4ecdc4", "#ff6b6b", "#a37eba"]
        x = np.arange(len(band_labels))
        self.bp_bars = self.ax_bp.bar(x, [0]*5, color=band_colors, width=0.6)
        self.ax_bp.set_xticks(x)
        self.ax_bp.set_xticklabels(band_labels, fontsize=9, color="#ccc")

        # Row 2: PPG (IR)
        self.ax_ppg = self.fig.add_subplot(gs[2])
        self.ax_ppg.set_facecolor("#15232e")
        self.ax_ppg.set_title("PPG LO_NIR (5s)", color="#ccc", fontsize=9)
        self.ax_ppg.set_ylabel("a.u.", color="#888", fontsize=8)
        self.ax_ppg.tick_params(colors="#888", labelsize=7)
        self.ppg_line, = self.ax_ppg.plot([], [], color="#ef5350", linewidth=0.7)

        # Row 3: Accelerometer (head motion)
        self.ax_acc = self.fig.add_subplot(gs[3])
        self.ax_acc.set_facecolor("#15232e")
        self.ax_acc.set_title("Accelerometer (5s)", color="#ccc", fontsize=9)
        self.ax_acc.set_ylabel("g", color="#888", fontsize=8)
        self.ax_acc.set_xlabel("Time (s)", color="#888", fontsize=8)
        self.ax_acc.tick_params(colors="#888", labelsize=7)
        acc_colors = ["#4fc3f7", "#81c784", "#ffb74d"]
        self.acc_lines = {}
        for lbl, c in zip(["X", "Y", "Z"], acc_colors):
            self.acc_lines[lbl], = self.ax_acc.plot([], [], color=c, linewidth=0.6, label=lbl)
        self.ax_acc.legend(loc="upper right", fontsize=6, facecolor="#15232e",
                           edgecolor="#333", labelcolor="#ccc", ncol=3)

        self.canvas = FigureCanvasTkAgg(self.fig, master=left)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # Right: Info panel
        right = tk.Frame(main, bg="#15232e", width=280)
        main.add(right)

        # Signal panel
        info = tk.Frame(right, bg="#15232e", padx=16, pady=12)
        info.pack(fill=tk.X)

        tk.Label(info, text="Signal Channels", fg="#78909c", bg="#15232e",
                 font=("", 10, "bold")).pack(anchor=tk.W)

        self.ch_labels = {}
        for ch in CHANNELS:
            f = tk.Frame(info, bg="#15232e")
            f.pack(fill=tk.X, pady=2)
            c = tk.Canvas(f, width=10, height=10, bg="#ff4444", highlightthickness=0)
            c.pack(side=tk.LEFT, padx=(0, 8))
            l = tk.Label(f, text=f"{ch}: ---", fg="#ccc", bg="#15232e", font=("Consolas", 9))
            l.pack(side=tk.LEFT)
            self.ch_labels[ch] = (c, l)

        tk.Label(right, text="", bg="#15232e").pack()  # spacer

        # BP values
        bp_info = tk.Frame(right, bg="#15232e", padx=16, pady=12)
        bp_info.pack(fill=tk.X)
        tk.Label(bp_info, text="Latest Band Power", fg="#78909c", bg="#15232e",
                 font=("", 10, "bold")).pack(anchor=tk.W)

        self.bp_labels = {}
        for band in ["Delta", "Theta", "Alpha", "Beta", "Gamma"]:
            f = tk.Frame(bp_info, bg="#15232e")
            f.pack(fill=tk.X, pady=1)
            tk.Label(f, text=f"{band:6s}", fg="#ccc", bg="#15232e",
                     font=("Consolas", 9)).pack(side=tk.LEFT)
            l = tk.Label(f, text="--- dB", fg="#888", bg="#15232e", font=("Consolas", 9))
            l.pack(side=tk.RIGHT)
            self.bp_labels[band] = l

        # Brain state
        state_frame = tk.Frame(right, bg="#15232e", padx=16, pady=8)
        state_frame.pack(fill=tk.X)
        tk.Label(state_frame, text="Brain State", fg="#78909c", bg="#15232e",
                 font=("", 10, "bold")).pack(anchor=tk.W)
        self.state_label = tk.Label(state_frame, text="---", fg="#4fc3f7", bg="#15232e",
                                    font=("", 12, "bold"), wraplength=220, justify=tk.LEFT)
        self.state_label.pack(anchor=tk.W, pady=(4, 0))
        self.bp_progress_label = tk.Label(state_frame, text="Band power: waiting for data",
                                          fg="#546e7a", bg="#15232e", font=("", 8))
        self.bp_progress_label.pack(anchor=tk.W, pady=(2, 0))

        # ── PPG / HRV ──
        ppg_frame = tk.Frame(right, bg="#15232e", padx=16, pady=8)
        ppg_frame.pack(fill=tk.X)
        tk.Label(ppg_frame, text="PPG / HRV", fg="#78909c", bg="#15232e",
                 font=("", 10, "bold")).pack(anchor=tk.W)

        hr_row = tk.Frame(ppg_frame, bg="#15232e")
        hr_row.pack(fill=tk.X, pady=(4, 0))
        tk.Label(hr_row, text="Heart Rate", fg="#ccc", bg="#15232e",
                 font=("Consolas", 9)).pack(side=tk.LEFT)
        self.hr_label = tk.Label(hr_row, text="-- BPM", fg="#ef5350", bg="#15232e",
                                 font=("", 13, "bold"))
        self.hr_label.pack(side=tk.RIGHT)

        self.hrv_labels = {}
        for key, lbl in [("rmssd", "RMSSD"), ("sdnn", "SDNN"), ("pnn50", "pNN50")]:
            f = tk.Frame(ppg_frame, bg="#15232e")
            f.pack(fill=tk.X, pady=1)
            tk.Label(f, text=lbl, fg="#ccc", bg="#15232e",
                     font=("Consolas", 9)).pack(side=tk.LEFT)
            l = tk.Label(f, text="---", fg="#888", bg="#15232e", font=("Consolas", 9))
            l.pack(side=tk.RIGHT)
            self.hrv_labels[key] = l

        # ── fNIRS / SpO₂ (prototype) ──
        o2_frame = tk.Frame(right, bg="#15232e", padx=16, pady=8)
        o2_frame.pack(fill=tk.X)
        tk.Label(o2_frame, text="fNIRS / O₂ (prototype)", fg="#78909c", bg="#15232e",
                 font=("", 10, "bold")).pack(anchor=tk.W)
        tk.Label(o2_frame, text="Uncalibrated — research use only",
                 fg="#546e7a", bg="#15232e", font=("", 7)).pack(anchor=tk.W)

        self.o2_labels = {}
        for key, lbl in [
            ("tsi", "TSI (fNIRS)"), ("d_hbo2", "ΔHbO₂"), ("d_hbr", "ΔHbR"),
            ("spo2", "SpO₂ est."), ("optics_ch", "Optics ch"),
        ]:
            f = tk.Frame(o2_frame, bg="#15232e")
            f.pack(fill=tk.X, pady=1)
            tk.Label(f, text=lbl, fg="#ccc", bg="#15232e",
                     font=("Consolas", 9)).pack(side=tk.LEFT)
            l = tk.Label(f, text="---", fg="#888", bg="#15232e", font=("Consolas", 9))
            l.pack(side=tk.RIGHT)
            self.o2_labels[key] = l

        # ── Head motion (ACC/GYRO) ──
        mot_frame = tk.Frame(right, bg="#15232e", padx=16, pady=8)
        mot_frame.pack(fill=tk.X)
        tk.Label(mot_frame, text="Head Motion", fg="#78909c", bg="#15232e",
                 font=("", 10, "bold")).pack(anchor=tk.W)

        self.motion_label = tk.Label(mot_frame, text="---", fg="#4fc3f7", bg="#15232e",
                                     font=("", 11, "bold"))
        self.motion_label.pack(anchor=tk.W, pady=(4, 0))

        self.motion_detail_labels = {}
        for key, lbl in [
            ("pitch", "Pitch"), ("roll", "Roll"),
            ("acc_std", "Acc jitter"), ("gyro_rms", "Gyro"),
        ]:
            f = tk.Frame(mot_frame, bg="#15232e")
            f.pack(fill=tk.X, pady=1)
            tk.Label(f, text=lbl, fg="#ccc", bg="#15232e",
                     font=("Consolas", 9)).pack(side=tk.LEFT)
            l = tk.Label(f, text="---", fg="#888", bg="#15232e", font=("Consolas", 9))
            l.pack(side=tk.RIGHT)
            self.motion_detail_labels[key] = l

        # Bottom status
        self.bottom_label = tk.Label(self, text="Ready — press Start to listen on port 5000",
                                      fg="#78909c", bg="#1a2d3d", font=("", 9),
                                      padx=12, pady=4, anchor=tk.W)
        self.bottom_label.pack(fill=tk.X, side=tk.BOTTOM)

    def _toggle(self):
        if self.running:
            self._stop()
        else:
            self._start()

    def _start(self):
        self.running = True
        self.buffer = DataBuffer()

        if self.simulate:
            self._start_simulate()
        else:
            if not _ensure_decoder():
                from tkinter import messagebox
                messagebox.showerror("Error", "Cannot load MuseRealtimeDecoder (check amused-src)")
                self.running = False
                return
            self.decoder = MuseRealtimeDecoder()
            self.receiver = UdpReceiver("0.0.0.0", self.port)
            self.receiver.on_raw(self._on_raw)
            # Legacy OSC fallback (older APK or test tools)
            self.receiver.on("/muse/eeg", self._on_eeg)
            self.receiver.on("/muse/acc", self._on_acc)
            self.receiver.on("/muse/gyro", self._on_gyro)
            self.receiver.on("/muse/ppg", self._on_ppg)
            self.receiver.start()

            local_ip = self._get_local_ip()
            self.ip_label.config(text=f"🖥 {local_ip}:{self.port}")
            self.bottom_label.config(
                text=f"Listening raw BLE UDP 0.0.0.0:{self.port} | Local IP: {local_ip} | "
                     f"Android: Local Mode ON, Target {local_ip}:{self.port}, press GO")

        self.btn_start.config(text="⏹ Stop", bg="#b71c1c")
        self.btn_save.config(state=tk.NORMAL)
        self.status_var.set("Recording")
        self.status_label.config(fg="#81c784")
        self._animate()

    @staticmethod
    def _get_local_ip():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "?.?.?.?"

    def _stop(self):
        self.running = False
        if self.receiver:
            self.receiver.stop()
            self.receiver = None
        self.decoder = None
        self._sim_running = False
        self.btn_start.config(text="▶ Start", bg="#2d5f8a")
        self.status_var.set("Stopped")
        self.status_label.config(fg="#78909c")
        self.bottom_label.config(text="Stopped — press Save & Report to save data")

    def _on_raw(self, data, addr):
        if not self.decoder:
            return
        try:
            self.buffer.process_raw_packet(data, self.decoder)
        except Exception as e:
            print(f"Raw decode error: {e}")

    def _on_eeg(self, values, addr):
        self.buffer.add_eeg(values)

    def _on_acc(self, values, addr):
        self.buffer.add_acc(values)

    def _on_gyro(self, values, addr):
        self.buffer.add_gyro(values)

    def _on_ppg(self, values, addr):
        self.buffer.add_ppg(values)

    def _self_test(self):
        """Send a test raw frame to localhost and verify reception."""
        if not self.running:
            from tkinter import messagebox
            messagebox.showinfo("Self Test", "Press Start first, then Self Test.")
            return

        before = self.receiver.packet_count if self.receiver else 0
        try:
            payload = bytes([0] * 14 + [0x11] + [0] * 13)
            frame = RAW_MAGIC + struct.pack('>I', 0) + struct.pack('>H', len(payload)) + payload
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(frame, ('127.0.0.1', self.port))
            s.close()
            time.sleep(0.5)
            after = self.receiver.packet_count if self.receiver else 0

            local_ip = self._get_local_ip()
            if after > before:
                self.bottom_label.config(
                    text=f"✅ Self test PASSED | Local IP: {local_ip} | "
                         f"Phone should be on same WiFi SSID, IP in same subnet")
            else:
                self.bottom_label.config(text="❌ Self test FAILED — check Windows Firewall for UDP port 5000")
        except Exception as e:
            self.bottom_label.config(text=f"❌ Self test error: {e}")

    def _save_and_report(self):
        result = self.buffer.save_bin()
        if not result:
            from tkinter import messagebox
            messagebox.showwarning("No Data", "No EEG data recorded yet.")
            return

        if isinstance(result, tuple):
            filepath, report_path = result
        else:
            filepath = result
            report_path = filepath.replace(".npz", ".report.html")

        self.bottom_label.config(text=f"Saved: {os.path.basename(filepath)}")

        if report_path and os.path.exists(report_path):
            import webbrowser
            webbrowser.open(f"file:///{report_path}")
            self.bottom_label.config(text=f"Report: {os.path.basename(report_path)}")
        else:
            self.bottom_label.config(text="Saved data, report generation failed")

    def _animate(self):
        if not self.running:
            return

        try:
            eeg = self.buffer.get_eeg_arrays()

            # Update EEG waveforms
            has_data = any(len(arr) > 0 for arr in eeg.values())
            if has_data:
                self.buffer.compute_band_power()
                latest_bp, latest_state = self.buffer.get_latest_bp()

                t = np.arange(DISP_SAMPLES) / SFREQ
                for ch in CHANNELS:
                    arr = eeg[ch]
                    if len(arr) > 0:
                        y = np.zeros(DISP_SAMPLES)
                        n = min(len(arr), DISP_SAMPLES)
                        y[-n:] = arr[-n:]
                        self.eeg_lines[ch].set_data(t, y)
                    else:
                        self.eeg_lines[ch].set_data([], [])
                self.ax_eeg.relim()
                self.ax_eeg.autoscale_view(scaley=False)

                # Update channel RMS labels
                for ch in CHANNELS:
                    c, l = self.ch_labels[ch]
                    arr = eeg[ch]
                    if len(arr) > 50:
                        rms = np.std(arr[-256:]) if len(arr) >= 256 else np.std(arr)
                        if rms > 0:
                            l.config(text=f"{ch}: {rms:.1f} μV")
                            # Color: green if good signal (RMS > 5 μV), red if weak
                            color = "#4caf50" if rms > 5 else "#ff9800" if rms > 2 else "#f44336"
                            c.config(bg=color)
                        else:
                            l.config(text=f"{ch}: 0.0 μV")
                    else:
                        l.config(text=f"{ch}: ---")
                        c.config(bg="#f44336")

            # Update band power bars
            latest_bp, latest_state = self.buffer.get_latest_bp()
            if latest_bp:
                bands = ["delta", "theta", "alpha", "beta", "gamma"]
                vals = [latest_bp.get(b, 0.0) for b in bands]
                if len(vals) == 5:
                    ymin = min(min(vals) - 5, -5)
                    ymax = max(max(vals) + 5, 20)
                    self.ax_bp.set_ylim(ymin, ymax)
                    for bar, val in zip(self.bp_bars, vals):
                        bar.set_height(val)
                    self.ax_bp.relim()
                    self.ax_bp.autoscale_view(scaley=False)
                    display_names = ["Delta", "Theta", "Alpha", "Beta", "Gamma"]
                    for band, name in zip(bands, display_names):
                        if band in latest_bp:
                            self.bp_labels[name].config(text=f"{latest_bp[band]:.1f} dB")
                if latest_state:
                    self.state_label.config(text=latest_state)
                self.bp_progress_label.config(text="Band power: live")
            elif has_data:
                pct = int(self.buffer.bp_ready_fraction() * 100)
                self.bp_progress_label.config(
                    text=f"Band power: collecting {pct}% ({BP_EPOCH_SEC}s window)")
            else:
                self.bp_progress_label.config(text="Band power: waiting for data")

            # PPG / ACC waveforms + vitals (every ~2 s)
            now = time.time()
            if now - self._last_vitals_ui >= 2.0:
                self._last_vitals_ui = now
                self.buffer.compute_vitals()

            ppg = self.buffer.get_ppg_array()
            if len(ppg) > 10:
                t_ppg = np.arange(len(ppg)) / PPG_SFREQ
                t_ppg = t_ppg - t_ppg[-1]
                centered = ppg - np.mean(ppg)
                scale = max(np.std(centered), 1e-6)
                self.ppg_line.set_data(t_ppg, centered / scale)
                self.ax_ppg.relim()
                self.ax_ppg.autoscale_view()

            ax_, ay_, az_ = self.buffer.get_acc_arrays()
            if len(ax_) > 5:
                t_imu = np.arange(len(ax_)) / IMU_SFREQ
                t_imu = t_imu - t_imu[-1]
                for lbl, arr in zip(["X", "Y", "Z"], [ax_, ay_, az_]):
                    self.acc_lines[lbl].set_data(t_imu, arr)
                self.ax_acc.relim()
                self.ax_acc.autoscale_view()

            vit = self.buffer.get_vitals()
            if vit.get("hr") is not None:
                self.hr_label.config(text=f"{vit['hr']:.0f} BPM")
            if vit.get("rmssd") is not None:
                self.hrv_labels["rmssd"].config(text=f"{vit['rmssd']:.0f} ms")
            if vit.get("sdnn") is not None:
                self.hrv_labels["sdnn"].config(text=f"{vit['sdnn']:.0f} ms")
            if vit.get("pnn50") is not None:
                self.hrv_labels["pnn50"].config(text=f"{vit['pnn50']:.0f} %")

            if vit.get("tsi") is not None:
                self.o2_labels["tsi"].config(text=f"{vit['tsi']:.1f} %")
            if vit.get("d_hbo2") is not None:
                self.o2_labels["d_hbo2"].config(text=f"{vit['d_hbo2']:+.2f} μM")
            if vit.get("d_hbr") is not None:
                self.o2_labels["d_hbr"].config(text=f"{vit['d_hbr']:+.2f} μM")
            if vit.get("spo2") is not None:
                self.o2_labels["spo2"].config(text=f"{vit['spo2']:.0f} %")
            och = vit.get("optics_ch", 0)
            self.o2_labels["optics_ch"].config(
                text=str(och) if och else "---")

            motion = vit.get("motion", "---")
            mot_colors = {"Still": "#4caf50", "Slight motion": "#ff9800", "Moving": "#ef5350"}
            self.motion_label.config(
                text=motion,
                fg=mot_colors.get(motion, "#4fc3f7"))
            if vit.get("pitch") is not None:
                self.motion_detail_labels["pitch"].config(text=f"{vit['pitch']:.0f}°")
            if vit.get("roll") is not None:
                self.motion_detail_labels["roll"].config(text=f"{vit['roll']:.0f}°")
            if vit.get("acc_std") is not None:
                self.motion_detail_labels["acc_std"].config(text=f"{vit['acc_std']:.3f} g")
            if vit.get("gyro_rms") is not None:
                self.motion_detail_labels["gyro_rms"].config(text=f"{vit['gyro_rms']:.0f} °/s")

            # Update packet count and duration
            dur = self.buffer.duration_seconds()
            self.dur_var.set(f"{int(dur//60):02d}:{int(dur%60):02d}")
            if self.receiver:
                pkt = self.receiver.packet_count
                raw = self.receiver.raw_count
                self.pkt_var.set(f"BLE: {pkt}")
                self.raw_var.set(f"UDP: {raw}")
                if raw > 0 and pkt == 0:
                    self.status_var.set("⚠ Bad format")
                    self.status_label.config(fg="#ff9800")
                    addr = self.receiver.last_addr
                    if addr:
                        self.bottom_label.config(
                            text=f"UDP from {addr[0]}:{addr[1]} — no valid BLE/OSC payload")
                elif raw > 0 and self.receiver.last_addr:
                    addr = self.receiver.last_addr
                    self.bottom_label.config(
                        text=f"Receiving from {addr[0]}:{addr[1]} | BLE:{pkt} UDP:{raw}")

            self.canvas.draw_idle()

        except Exception:
            pass

        self._anim_id = self.after(100, self._animate)  # 10 Hz refresh

    # ── Simulation mode ──
    _sim_running = False
    _sim_thread = None

    def _start_simulate(self):
        self._sim_running = True
        self._sim_thread = threading.Thread(target=self._sim_loop, daemon=True)
        self._sim_thread.start()
        self.bottom_label.config(text="Simulation mode — generating synthetic 1/f EEG...")
        self.status_label.config(fg="#ffb74d")

    def _sim_loop(self):
        import random
        # Generate 1/f noise
        n_total = 256 * 60  # 1 minute buffer
        freqs = np.fft.rfftfreq(n_total, 1 / 256)
        psd_1f = 1.0 / (freqs + 0.1)
        psd_1f[0] = 0
        noise = np.random.randn(n_total, 4)
        fft_scaled = np.fft.rfft(noise, axis=0) * np.sqrt(psd_1f[:, np.newaxis])
        eeg_sim = np.fft.irfft(fft_scaled, n=n_total, axis=0)
        eeg_sim *= 30 / np.std(eeg_sim)  # 30 μV RMS

        idx = 0
        ppg_phase = 0.0
        while self._sim_running:
            # Send 4 samples at a time (simulating BLE packet)
            if idx + 4 >= n_total:
                idx = 0
            batch = []
            for s in range(4):
                for ch in range(4):
                    batch.append(eeg_sim[idx + s, ch])
                batch.append(0.0)  # 5th channel padding
            self.buffer.add_eeg(batch)
            idx += 4

            # Synthetic 8-ch optics (~72 BPM) + still head ACC
            ppg_phase += 2 * np.pi * (72 / 60) / PPG_SFREQ
            pulse = 0.08 * np.sin(ppg_phase)
            # 8ch: outer fNIRS + inner PPG paths (LO_NIR, RO_NIR, LO_IR, RO_IR, LI_NIR, ...)
            base = [0.55, 0.52, 0.48, 0.45, 0.60, 0.58, 0.50, 0.47]
            ppg8 = []
            for s in range(2):
                for i, b in enumerate(base):
                    ppg8.append(b + pulse * (1.0 + 0.1 * (i % 2)))
            self.buffer.add_ppg(ppg8)

            t_sim = time.time()
            wobble = 0.008 * np.sin(t_sim * 0.7)
            self.buffer.add_acc([wobble, 0.02, 1.0 + wobble])
            self.buffer.add_gyro([0.5, -0.3, 0.2])

            time.sleep(1.0 / 64)  # 64 packets/sec


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Muse Local Server")
    parser.add_argument("--port", type=int, default=5000, help="UDP port (default: 5000)")
    parser.add_argument("--simulate", action="store_true", help="Demo with synthetic EEG")
    args = parser.parse_args()

    if not args.simulate:
        print("=" * 56)
        print("  Muse Local Server")
        print("  Listening: UDP 0.0.0.0:" + str(args.port))
        print("  Waiting for Android raw BLE frames (Local Mode + GO)")
        print()
        print("  Android setup:")
        print("    1. Rebuild and install APK")
        print("    2. Turn ON 'Local Mode'")
        print("    3. Set Target IP to this PC")
        print("    4. Connect Muse S, press GO")
        print()
        print("  If no data: check Windows firewall allows UDP port " + str(args.port))
        print("=" * 56)

    app = MuseLocalServer(simulate=args.simulate, port=args.port)
    app.mainloop()
