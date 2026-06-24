#!/usr/bin/env python3
"""
Session report generator — adapted from timeline_report.py.
Reads a .bin file, generates an HTML report with charts and per-minute analysis.
"""

import io
import os
from base64 import b64encode
from datetime import datetime
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.signal import welch, find_peaks, butter, filtfilt

# Chinese font
plt.rcParams["font.family"] = "sans-serif"
plt.rcParams["axes.unicode_minus"] = False

BANDS = {"Delta": (0.5, 4), "Theta": (4, 8), "Alpha": (8, 12),
         "Beta": (12, 30), "Gamma": (30, 45)}
CHANNELS = ["TP9", "AF7", "AF8", "TP10"]
SFREQ = 256.0


def compute_band_power_chunk(data, sfreq):
    if data.shape[0] < int(sfreq * 0.5):
        return None
    nperseg = int(sfreq * 1)
    try:
        freqs, psd = welch(data, sfreq, nperseg=nperseg, axis=0)
    except Exception:
        return None
    result = {}
    total = np.zeros(data.shape[1])
    for band, (lo, hi) in BANDS.items():
        mask = (freqs >= lo) & (freqs <= hi)
        bp = np.trapezoid(psd[mask], freqs[mask], axis=0)
        result[band] = bp
        total += bp
    rel = {}
    for band in BANDS:
        rel[band] = result[band] / total
    return rel


def compute_hr_chunk(ppg, sfreq=64.0):
    if len(ppg) < int(sfreq * 2):
        return None
    ppg_c = ppg - np.mean(ppg)
    nyq = sfreq / 2
    b, a = butter(2, [0.7 / nyq, 4.0 / nyq], btype="band")
    filtered = filtfilt(b, a, ppg_c)
    peaks, _ = find_peaks(filtered, distance=int(sfreq * 0.35),
                          height=0.1 * np.std(filtered))
    if len(peaks) >= 2:
        return 60.0 / np.median(np.diff(peaks) / sfreq)
    return None


def classify_state(alpha, theta, beta, theta_beta, hr):
    if alpha is not None and alpha > 0.25:
        return "放松 Relaxed"
    if theta_beta is not None and theta_beta > 1.5:
        return "冥想 Meditative"
    if beta is not None and beta > 0.30:
        return "专注 Focused"
    if hr is not None and hr > 90:
        return "活跃 Active"
    if hr is not None and hr < 55:
        return "昏沉 Drowsy"
    return "中性 Neutral"


def fig_to_b64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                facecolor="#0f0f11", edgecolor="none")
    buf.seek(0)
    return b64encode(buf.read()).decode()


def generate_report(bin_path: str, output_path: str) -> str:
    """
    Read a .bin file, generate an HTML report, save to output_path.
    Returns the output path.
    """
    # Import here so server can load this module without amused-src on path
    import sys as _sys
    _amusedsrc = os.path.join(os.path.dirname(__file__), "..", "amused-src")
    if _amusedsrc not in _sys.path:
        _sys.path.insert(0, _amusedsrc)
    from muse_raw_stream import MuseRawStream

    # ── Read .bin file, extract all sensor data ──
    stream = MuseRawStream(bin_path)
    stream.open_read()

    timestamps, eeg_raw, ppg_raw, acc_raw, gyro_raw, hr_raw = [], [], [], [], [], []
    ppg_ir = []  # IR channel for HR

    for pkt in stream.read_packets():
        decoded = stream.decode_packet(pkt)
        ts = pkt.timestamp.timestamp()

        # EEG
        if "eeg" in decoded:
            timestamps.append(ts)
            row = [0.0] * 4
            for i, ch in enumerate(CHANNELS):
                if ch in decoded["eeg"]:
                    row[i] = decoded["eeg"][ch][-1]  # Last sample
            eeg_raw.append(row)
        else:
            timestamps.append(ts)
            eeg_raw.append([0.0] * 4)

        # PPG — use first available IR channel
        if "ppg" in decoded:
            ppg_data = decoded["ppg"]
            ir_keys = ["LO_IR", "RO_IR", "LI_IR", "RI_IR"]
            ir_val = None
            for k in ir_keys:
                if k in ppg_data:
                    ir_val = ppg_data[k][-1]
                    break
            if ir_val is None and ppg_data:
                ir_val = list(ppg_data.values())[0][-1]
            ppg_ir.append(ir_val if ir_val is not None else 0.0)
        elif ppg_ir:
            ppg_ir.append(ppg_ir[-1])
        else:
            ppg_ir.append(0.0)

        # IMU
        if "imu" in decoded and decoded["imu"]:
            imu = decoded["imu"]
            if "accelerometer" in imu and imu["accelerometer"]:
                acc_raw.append(imu["accelerometer"][-1][:3])
            if "gyroscope" in imu and imu["gyroscope"]:
                gyro_raw.append(imu["gyroscope"][-1][:3])
        elif acc_raw:
            acc_raw.append(acc_raw[-1])
            gyro_raw.append(gyro_raw[-1])
        else:
            acc_raw.append([0, 0, 0])
            gyro_raw.append([0, 0, 0])

    stream.close()

    if not timestamps:
        return None

    timestamps = np.array(timestamps)
    eeg_arr = np.array(eeg_raw) if eeg_raw else np.zeros((len(timestamps), 4))
    ppg_ir_arr = np.array(ppg_ir) if ppg_ir else np.array([])
    acc_arr = np.array(acc_raw) if acc_raw else np.zeros((len(timestamps), 3))
    gyro_arr = np.array(gyro_raw) if gyro_raw else np.zeros((len(timestamps), 3))

    t0 = timestamps[0]
    tend = timestamps[-1]
    duration = tend - t0
    n_minutes = max(1, int(np.ceil(duration / 60)))

    # ── Per-minute analysis ──
    rows = []
    has_ppg = len(ppg_ir_arr) > 10
    has_hr = False  # Will be set if HR extracted

    for m in range(n_minutes):
        t_start = t0 + m * 60
        t_end = t0 + (m + 1) * 60
        mask = (timestamps >= t_start) & (timestamps < t_end)

        row = {"minute": m + 1}

        # EEG band power
        eeg_chunk = eeg_arr[mask]
        bp = compute_band_power_chunk(eeg_chunk, SFREQ) if eeg_chunk.shape[0] > 10 else None
        if bp and all(bp[b].shape[0] == 4 for b in bp):
            row["alpha"] = float(np.mean(bp["Alpha"]))
            row["theta"] = float(np.mean(bp["Theta"]))
            row["beta"] = float(np.mean(bp["Beta"]))
            row["delta"] = float(np.mean(bp["Delta"]))
            row["gamma"] = float(np.mean(bp["Gamma"]))
            row["theta_beta"] = row["theta"] / row["beta"] if row["beta"] > 0 else 0
        else:
            row["alpha"] = row["theta"] = row["beta"] = row["delta"] = row["gamma"] = row["theta_beta"] = None

        # HR from PPG
        if has_ppg:
            ppg_chunk = ppg_ir_arr[mask]
            hr_val = compute_hr_chunk(ppg_chunk) if len(ppg_chunk) > 10 else None
            row["hr"] = hr_val
            if hr_val:
                has_hr = True
        else:
            row["hr"] = None

        # Movement
        if acc_arr.shape[0] > 0:
            acc_chunk = acc_arr[mask]
            if acc_chunk.shape[0] > 0:
                mag = np.sqrt(np.sum(acc_chunk ** 2, axis=1))
                row["acc_mag"] = float(np.mean(mag))
                row["acc_max"] = float(np.max(mag))
            else:
                row["acc_mag"] = row["acc_max"] = None
        else:
            row["acc_mag"] = row["acc_max"] = None

        # Rotation
        if gyro_arr.shape[0] > 0:
            gyro_chunk = gyro_arr[mask]
            if gyro_chunk.shape[0] > 0:
                mag = np.sqrt(np.sum(gyro_chunk ** 2, axis=1))
                row["gyro_mag"] = float(np.mean(mag))
            else:
                row["gyro_mag"] = None
        else:
            row["gyro_mag"] = None

        # State
        row["state"] = classify_state(row["alpha"], row["theta"], row["beta"],
                                       row["theta_beta"], row["hr"])
        rows.append(row)

    # ── Build charts ──
    minutes = [r["minute"] for r in rows]

    def n(val):
        return val if val is not None else np.nan

    colors = {"alpha": "#4ecdc4", "beta": "#ff6b6b", "theta": "#ffe66d",
              "delta": "#888888", "gamma": "#a37eba", "theta_beta": "#ff8c42",
              "hr": "#ff4444", "acc": "#44aaff", "gyro": "#44ff44"}

    state_colors = {"放松 Relaxed": "#4ecdc4", "中性 Neutral": "#888888",
                    "专注 Focused": "#ff6b6b", "冥想 Meditative": "#ffe66d",
                    "活跃 Active": "#ff8c42", "昏沉 Drowsy": "#a37eba"}

    # ── Chart 1: EEG Band Power ──
    fig1, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7))
    fig1.patch.set_facecolor("#0f0f11")
    for ax in [ax1, ax2]:
        ax.set_facecolor("#1a1a2e")
        ax.tick_params(colors="#888888")
        ax.spines["bottom"].set_color("#333")
        ax.spines["left"].set_color("#333")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.grid(True, color="#222", linewidth=0.3)

    vals = {k: [n(r[k]) for r in rows] for k in ["alpha", "beta", "theta", "delta", "gamma"]}
    for band, label in [("alpha", "α Alpha"), ("beta", "β Beta"),
                         ("theta", "θ Theta"), ("delta", "δ Delta"),
                         ("gamma", "γ Gamma")]:
        ax1.plot(minutes, vals[band], color=colors[band], linewidth=1.5,
                 marker="o", markersize=4, label=label)
    ax1.set_ylabel("Relative Power", color="#ccc")
    ax1.set_title("EEG Band Power Trends", color="#ccc", fontweight="bold")
    ax1.legend(loc="upper right", fontsize=7, facecolor="#1a1a2e",
               edgecolor="#333", labelcolor="#ccc")

    tb = [n(r["theta_beta"]) for r in rows]
    ax2.fill_between(minutes, 0, tb, color=colors["theta_beta"], alpha=0.3)
    ax2.plot(minutes, tb, color=colors["theta_beta"], linewidth=2, marker="s", markersize=5)
    ax2.axhline(y=0.8, color="#666", linewidth=0.8, linestyle="--")
    ax2.axhline(y=1.5, color="#666", linewidth=0.8, linestyle="--")
    ax2.set_ylabel("θ/β Ratio", color="#ccc")
    ax2.set_xlabel("Minute", color="#ccc")
    ax2.set_title("Theta/Beta Ratio (Focus Index)", color="#ccc", fontweight="bold")
    ax2.set_ylim(bottom=0)
    chart1 = fig_to_b64(fig1)
    plt.close(fig1)

    # ── Chart 2: HR + Motion ──
    has_hr_data = any(r["hr"] is not None for r in rows)
    has_motion = any(r["acc_mag"] is not None and r["acc_mag"] > 0.001 for r in rows)
    chart2_html = ""

    if has_hr_data or has_motion:
        n_plots = (1 if has_hr_data else 0) + (1 if has_motion else 0)
        fig2, axes = plt.subplots(n_plots, 1, figsize=(10, 3 * n_plots))
        fig2.patch.set_facecolor("#0f0f11")
        if n_plots == 1:
            axes = [axes]

        plot_idx = 0
        if has_hr_data:
            ax = axes[plot_idx]; plot_idx += 1
            ax.set_facecolor("#1a1a2e"); ax.tick_params(colors="#888888")
            for s in ax.spines.values(): s.set_color("#333")
            ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
            ax.grid(True, color="#222", linewidth=0.3)
            hr_vals = [n(r["hr"]) for r in rows]
            ax.fill_between(minutes, 50, hr_vals, color=colors["hr"], alpha=0.15)
            ax.plot(minutes, hr_vals, color=colors["hr"], linewidth=2, marker="o", markersize=5)
            ax.set_ylabel("BPM", color="#ccc")
            ax.set_title("Heart Rate", color="#ccc", fontweight="bold")
            ax.set_ylim(40, 120)

        if has_motion:
            ax = axes[plot_idx]
            ax.set_facecolor("#1a1a2e"); ax.tick_params(colors="#888888")
            for s in ax.spines.values(): s.set_color("#333")
            ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
            ax.grid(True, color="#222", linewidth=0.3)
            acc_vals = [n(r["acc_mag"]) for r in rows]
            gyro_vals = [n(r["gyro_mag"]) for r in rows]
            ax2b = ax.twinx()
            ax.fill_between(minutes, 0, acc_vals, color=colors["acc"], alpha=0.2, label="ACC")
            ax.plot(minutes, acc_vals, color=colors["acc"], linewidth=1.5, marker="o", markersize=4)
            ax2b.plot(minutes, gyro_vals, color=colors["gyro"], linewidth=1.5, marker="s", markersize=4, label="GYRO")
            ax.set_ylabel("ACC (g)", color=colors["acc"])
            ax2b.set_ylabel("GYRO (deg/s)", color=colors["gyro"])
            ax.set_xlabel("Minute", color="#ccc")
            ax.set_title("Movement & Rotation", color="#ccc", fontweight="bold")

        chart2 = fig_to_b64(fig2)
        plt.close(fig2)
        chart2_html = f'<img src="data:image/png;base64,{chart2}" style="width:100%; margin:10px 0;">'

    # ── Chart 3: Brain State ──
    fig3, ax3 = plt.subplots(figsize=(10, 2.5))
    fig3.patch.set_facecolor("#0f0f11")
    ax3.set_facecolor("#0f0f11")
    ax3.set_ylim(0, 1); ax3.set_xlim(0, n_minutes)
    ax3.set_yticks([])
    ax3.set_xlabel("Minute", color="#ccc")
    ax3.set_title("Brain State Timeline", color="#ccc", fontweight="bold")
    ax3.tick_params(colors="#888888")
    for s in ax3.spines.values(): s.set_visible(False)

    states_order = ["放松 Relaxed", "中性 Neutral", "专注 Focused",
                    "冥想 Meditative", "活跃 Active", "昏沉 Drowsy"]

    prev_state = None
    for i, r in enumerate(rows):
        state = r["state"]
        color = state_colors.get(state, "#888")
        ax3.axvspan(i, i + 1, facecolor=color, alpha=0.6)
        if state != prev_state:
            ax3.text(i + 0.5, 0.5, state, ha="center", va="center",
                    color="#fff", fontsize=8, fontweight="bold")
        prev_state = state

    for state, color in state_colors.items():
        if any(r["state"] == state for r in rows):
            ax3.plot([], [], color=color, linewidth=6, label=state)
    ax3.legend(loc="upper right", fontsize=7, facecolor="#1a1a2e",
               edgecolor="#333", labelcolor="#ccc", ncol=3)
    chart3 = fig_to_b64(fig3)
    plt.close(fig3)

    # ── Summary stats ──
    avg_alpha = np.nanmean([r["alpha"] for r in rows if r["alpha"] is not None])
    avg_tb = np.nanmean([r["theta_beta"] for r in rows if r["theta_beta"] is not None])
    avg_hr = np.nanmean([r["hr"] for r in rows if r["hr"] is not None])

    # ── Per-minute table ──
    table_rows = ""
    for r in rows:
        def v(val, fmt=".4f"):
            return f"{val:{fmt}}" if val is not None else "-"
        hr_str = f"{r['hr']:.0f}" if r["hr"] else "-"
        acc_str = f"{r['acc_mag']:.3f}" if r["acc_mag"] is not None else "-"
        sc = state_colors.get(r["state"], "#888")
        table_rows += f"""<tr>
            <td>{r['minute']}</td>
            <td>{v(r['alpha'])}</td><td>{v(r['beta'])}</td><td>{v(r['theta'])}</td>
            <td>{v(r['theta_beta'])}</td><td>{hr_str}</td><td>{acc_str}</td>
            <td style="color:{sc};font-weight:bold;">{r['state']}</td>
        </tr>"""

    session_name = Path(bin_path).stem

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{session_name} - Report</title>
<style>
body {{ background:#0f0f11; color:#ccc; font-family:system-ui,sans-serif; max-width:1100px; margin:0 auto; padding:20px; }}
h1 {{ color:#fff; border-bottom:2px solid #333; padding-bottom:10px; }}
h2 {{ color:#ddd; margin-top:30px; }}
table {{ border-collapse:collapse; width:100%; margin:15px 0; font-size:13px; }}
th {{ background:#1a1a2e; color:#fff; padding:8px 10px; text-align:right; }}
td {{ padding:6px 10px; text-align:right; border-bottom:1px solid #222; }}
th:first-child, td:first-child {{ text-align:center; }}
td:last-child {{ text-align:center; }}
img {{ max-width:100%; border:1px solid #333; border-radius:4px; }}
.summary {{ display:flex; gap:20px; flex-wrap:wrap; margin:15px 0; }}
.summary div {{ background:#1a1a2e; padding:12px 18px; border-radius:6px; min-width:120px; }}
.summary .value {{ font-size:24px; font-weight:bold; color:#4ecdc4; }}
.summary .label {{ font-size:12px; color:#888; }}
.note {{ font-size:12px; color:#888; margin:30px 0 10px; border-top:1px solid #333; padding-top:10px; }}
</style>
</head>
<body>

<h1>EEG Recording Report<br>
<small>{session_name}</small></h1>

<div class="summary">
    <div><span class="label">Duration</span><br><span class="value">{duration/60:.1f} min</span></div>
    <div><span class="label">Avg Alpha</span><br><span class="value">{avg_alpha:.3f}</span></div>
    <div><span class="label">Avg TB-Ratio</span><br><span class="value">{avg_tb:.3f}</span></div>
    <div><span class="label">Avg HR</span><br><span class="value">{avg_hr:.0f} BPM</span></div>
</div>

<h2>1. EEG Band Power Trends</h2>
<img src="data:image/png;base64,{chart1}">

<h2>2. Heart Rate &amp; Motion</h2>
{chart2_html if chart2_html else '<p style="color:#888;">(No PPG/Motion data)</p>'}

<h2>3. Brain State Timeline</h2>
<img src="data:image/png;base64,{chart3}">

<h2>4. Per-Minute Data</h2>
<table>
<tr><th>Min</th><th>Alpha</th><th>Beta</th><th>Theta</th><th>TB-Ratio</th><th>HR</th><th>ACC</th><th>State</th></tr>
{table_rows}
</table>

<p class="note">
    Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}<br>
    Bands: Delta 0.5-4Hz | Theta 4-8Hz | Alpha 8-12Hz | Beta 12-30Hz | Gamma 30-45Hz
</p>
</body></html>"""

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    Path(output_path).write_text(html, encoding="utf-8")
    return output_path
