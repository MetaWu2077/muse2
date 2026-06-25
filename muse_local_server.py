#!/usr/bin/env python3
"""
Muse Local Server — Real-time OSC receiver + EEG analyzer

Receives OSC/UDP packets from the Android Muse Bridge app,
displays real-time EEG waveforms and band power, saves .bin files.

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

# ── Paths ─────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AMUSED_DIR = os.path.join(SCRIPT_DIR, "amused-src")
CLOUD_DIR = os.path.join(SCRIPT_DIR, "muse-cloud-server")
REPORT_DIR = os.path.join(SCRIPT_DIR, "report")
os.makedirs(REPORT_DIR, exist_ok=True)
sys.path.insert(0, AMUSED_DIR)
sys.path.insert(0, CLOUD_DIR)

# Lazy imports
_report_imports_done = False


def _ensure_imports():
    global _report_imports_done
    if _report_imports_done:
        return True
    try:
        import matplotlib
        matplotlib.use("TkAgg")
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        from matplotlib.figure import Figure
        import matplotlib.pyplot as plt
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


# ── UDP Receiver ───────────────────────────────────────────────────────────

class UdpReceiver:
    """Background thread receiving OSC/UDP packets."""

    def __init__(self, host="0.0.0.0", port=5000):
        self.host = host
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.settimeout(1.0)
        self.running = False
        self.thread = None
        self.callbacks = {}  # path → callback
        self.packet_count = 0
        self.byte_count = 0
        self.start_time = None

    def on(self, path, callback):
        self.callbacks[path] = callback

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
                self.packet_count += 1
                self.byte_count += len(data)
                result = decode_osc(data)
                if result:
                    path, values = result
                    if path in self.callbacks:
                        self.callbacks[path](values, addr)
            except socket.timeout:
                continue
            except Exception:
                if self.running:
                    continue


# ── Data Buffer ────────────────────────────────────────────────────────────

CHANNELS = ["TP9", "AF7", "AF8", "TP10"]
N_CHANNELS = 4
EEG_SAMPLES_PER_PACKET = 4  # Android sends 4 samples per BLE packet
SFREQ = 256.0
DISP_WINDOW_SEC = 5  # Display window in seconds
DISP_SAMPLES = int(SFREQ * DISP_WINDOW_SEC)  # 1280 samples

# For band power
BP_EPOCH_SEC = 10
BP_EPOCH_SAMPLES = int(SFREQ * BP_EPOCH_SEC)  # 2560 samples


class DataBuffer:
    """Thread-safe ring buffer for EEG, PPG, and IMU data."""

    def __init__(self):
        self.lock = threading.Lock()
        self.eeg = {ch: deque(maxlen=DISP_SAMPLES) for ch in CHANNELS}
        self.eeg_all = {ch: [] for ch in CHANNELS}  # Unlimited for .bin saving
        self.ppg = deque(maxlen=500)
        self.hr = deque(maxlen=300)
        self.acc = deque(maxlen=500)
        self.gyro = deque(maxlen=500)
        self.sample_count = 0
        self.session_start = None
        self.last_bp_time = 0
        self.bp_history = {"delta": [], "theta": [], "alpha": [], "beta": [], "gamma": [], "time": []}
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

    def add_ppg(self, values: list):
        with self.lock:
            self.ppg.append(sum(values) / max(len(values), 1))

    def add_acc(self, values: list):
        with self.lock:
            if len(values) >= 3:
                mag = np.sqrt(values[0]**2 + values[1]**2 + values[2]**2)
                self.acc.append(mag)

    def add_gyro(self, values: list):
        with self.lock:
            if len(values) >= 3:
                mag = np.sqrt(values[0]**2 + values[1]**2 + values[2]**2)
                self.gyro.append(mag)

    def get_eeg_arrays(self):
        with self.lock:
            result = {}
            for ch in CHANNELS:
                arr = list(self.eeg[ch])
                result[ch] = np.array(arr) if arr else np.zeros(0)
            return result

    def get_latest_hr(self):
        with self.lock:
            if self.hr:
                return self.hr[-1]
            return None

    def get_bp_history(self):
        with self.lock:
            return dict(self.bp_history)

    def duration_seconds(self):
        if self.session_start:
            return time.time() - self.session_start
        return 0

    def save_bin(self):
        """Save EEG data as .npy and generate report directly."""
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
        npy_path = os.path.join(REPORT_DIR, f"local_{ts}.npy")
        meta = {
            "timestamp": timestamp,
            "sfreq": SFREQ,
            "channels": CHANNELS,
            "samples": self.sample_count,
            "duration": self.duration_seconds(),
        }
        np.savez(npy_path, eeg=eeg_arr, meta=meta)
        print(f"Data saved: {npy_path}")

        # Generate report directly from array
        _ensure_imports()
        import report_generator as rg
        from io import StringIO

        report_path = os.path.join(REPORT_DIR, f"local_{ts}.report.html")
        _generate_report_from_array(eeg_arr, SFREQ, ts, report_path)

        return npy_path

    def compute_band_power(self):
        """Run band power analysis on latest epoch if enough data."""
        with self.lock:
            min_len = min(len(self.eeg_all[ch]) for ch in CHANNELS)
            if min_len < BP_EPOCH_SAMPLES:
                return
            if time.time() - self.last_bp_time < 2:  # Compute every 2 seconds
                return

            self.last_bp_time = time.time()

            # Build array from last epoch
            data = np.zeros((BP_EPOCH_SAMPLES, N_CHANNELS))
            for i, ch in enumerate(CHANNELS):
                chunk = self.eeg_all[ch][-BP_EPOCH_SAMPLES:]
                data[:len(chunk), i] = chunk

        # Compute outside lock (uses report_generator)
        try:
            bp = report_generator.compute_band_power_chunk(data, SFREQ)
            if bp and all(bp["db"][b].shape[0] == N_CHANNELS for b in report_generator.BANDS):
                with self.lock:
                    now = time.time() - (self.session_start or 0)
                    self.bp_history["time"].append(now / 60)  # minutes
                    for band in report_generator.BANDS:
                        val = float(np.mean(bp["db"][band]))
                        self.bp_history[band].append(val)
                    # Keep last 60 points
                    max_bp = 120
                    if len(self.bp_history["time"]) > max_bp:
                        for k in self.bp_history:
                            self.bp_history[k] = self.bp_history[k][-max_bp:]
        except Exception:
            pass


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
    def __init__(self, simulate=False):
        super().__init__()
        self.title("Muse Local Analyzer")
        self.geometry("1100x700")
        self.minsize(800, 500)
        self.configure(bg="#0f1923")

        self.simulate = simulate
        self.running = False
        self.buffer = DataBuffer()
        self.receiver = None
        self._anim_id = None

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

        self.pkt_var = tk.StringVar(value="Packets: 0")
        tk.Label(header, textvariable=self.pkt_var, fg="#546e7a",
                 bg="#1a2d3d", font=("", 9)).pack(side=tk.LEFT, padx=(20, 0))

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

        self.fig = Figure(figsize=(8, 6), facecolor="#0f1923", dpi=100)
        gs = self.fig.add_gridspec(2, 1, height_ratios=[3, 2], hspace=0.35)

        # Top: 4-channel EEG
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

        # Bottom: Band power bars
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

        self.canvas = FigureCanvasTkAgg(self.fig, master=left)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # Right: Info panel
        right = tk.Frame(main, bg="#15232e", width=260)
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

        # HR
        hr_frame = tk.Frame(right, bg="#15232e", padx=16, pady=6)
        hr_frame.pack(fill=tk.X)
        tk.Label(hr_frame, text="Heart Rate", fg="#78909c", bg="#15232e",
                 font=("", 10, "bold")).pack(side=tk.LEFT)
        self.hr_label = tk.Label(hr_frame, text="-- BPM", fg="#ef5350", bg="#15232e",
                                  font=("", 14, "bold"))
        self.hr_label.pack(side=tk.RIGHT)

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
            self.receiver = UdpReceiver("0.0.0.0", 5000)
            self.receiver.on("/muse/eeg", self._on_eeg)
            self.receiver.on("/muse/acc", self._on_acc)
            self.receiver.on("/muse/gyro", self._on_gyro)
            self.receiver.on("/muse/ppg", self._on_ppg)
            self.receiver.start()
            self.bottom_label.config(text="Listening on UDP 0.0.0.0:5000 — waiting for data from Android...")

        self.btn_start.config(text="⏹ Stop", bg="#b71c1c")
        self.btn_save.config(state=tk.NORMAL)
        self.status_var.set("Recording")
        self.status_label.config(fg="#81c784")

        self._animate()

    def _stop(self):
        self.running = False
        if self.receiver:
            self.receiver.stop()
            self.receiver = None
        self._sim_running = False
        self.btn_start.config(text="▶ Start", bg="#2d5f8a")
        self.status_var.set("Stopped")
        self.status_label.config(fg="#78909c")
        self.bottom_label.config(text="Stopped — press Save & Report to save data")

    def _on_eeg(self, values, addr):
        self.buffer.add_eeg(values)

    def _on_acc(self, values, addr):
        self.buffer.add_acc(values)

    def _on_gyro(self, values, addr):
        self.buffer.add_gyro(values)

    def _on_ppg(self, values, addr):
        self.buffer.add_ppg(values)

    def _save_and_report(self):
        filepath = self.buffer.save_bin()
        if not filepath:
            from tkinter import messagebox
            messagebox.showwarning("No Data", "No EEG data recorded yet.")
            return

        # Report was already generated by save_bin()
        report_path = filepath.replace(".npz", ".report.html")
        self.bottom_label.config(text=f"Saved: {os.path.basename(filepath)}")

        if os.path.exists(report_path):
            import webbrowser
            webbrowser.open(f"file:///{report_path}")
            self.bottom_label.config(text=f"Report: {os.path.basename(report_path)}")
        else:
            self.bottom_label.config(text=f"Saved data, report generation failed")

    def _animate(self):
        if not self.running:
            return

        try:
            eeg = self.buffer.get_eeg_arrays()
            bp_hist = self.buffer.get_bp_history()

            # Update EEG waveforms
            has_data = any(len(arr) > 0 for arr in eeg.values())
            if has_data:
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
            if bp_hist["time"] and len(bp_hist["delta"]) > 0:
                bands = ["delta", "theta", "alpha", "beta", "gamma"]
                vals = [bp_hist[b][-1] for b in bands if bp_hist[b]]
                if len(vals) == 5:
                    for bar, val in zip(self.bp_bars, vals):
                        bar.set_height(max(0, val))
                    self.ax_bp.relim()
                    self.ax_bp.autoscale_view()
                    # Update labels
                    for band in bands:
                        if bp_hist[band]:
                            self.bp_labels[band.capitalize()].config(
                                text=f"{bp_hist[band][-1]:.1f} dB")

                # Update HR
                self.hr_label.config(text="-- BPM")  # HR from PPG not implemented in this version

            # Update packet count and duration
            dur = self.buffer.duration_seconds()
            self.dur_var.set(f"{int(dur//60):02d}:{int(dur%60):02d}")
            if self.receiver:
                self.pkt_var.set(f"Packets: {self.receiver.packet_count}")

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
            time.sleep(1.0 / 64)  # 64 packets/sec


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Muse Local Server")
    parser.add_argument("--port", type=int, default=5000, help="UDP port (default: 5000)")
    parser.add_argument("--simulate", action="store_true", help="Demo with synthetic EEG")
    args = parser.parse_args()

    app = MuseLocalServer(simulate=args.simulate)
    if not args.simulate:
        app.receiver = UdpReceiver("0.0.0.0", args.port)
    app.mainloop()
