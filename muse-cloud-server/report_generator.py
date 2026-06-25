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

# ── Epoch settings ─────────────────────────────────────────────────────
EPOCH_SECONDS = 10    # Analysis window size (seconds)
EPOCH_STEP = 5        # Step between windows (seconds), 50% overlap
SMOOTH_POINTS = 3     # Moving average window for chart smoothing

# ── Noise rejection threshold ──
# If (beta + gamma) relative power exceeds this, the epoch is dominated
# by EMG / white noise rather than real EEG → reject it.
NOISE_BG_RATIO = 0.65  # beta+gamma > 65% of total = noise


def moving_average(values, window=3):
    """Simple centered moving average. Returns same-length array (edges use available points)."""
    if len(values) < window:
        return values
    result = np.zeros(len(values))
    half = window // 2
    for i in range(len(values)):
        start = max(0, i - half)
        end = min(len(values), i + half + 1)
        result[i] = np.nanmean(values[start:end])
    return result


def compute_band_power_chunk(data, sfreq):
    """Compute absolute band power in dB (10*log10(μV²)) per channel.

    Returns:
        dict with keys 'db' (dict band→dB array), 'rel' (dict band→relative array),
        'noise' (bool array per channel), 'total_db' (array of total power in dB)
        or None if data insufficient.
    """
    if data.shape[0] < int(sfreq * 0.5):
        return None
    nperseg = int(sfreq * 1)
    try:
        freqs, psd = welch(data, sfreq, nperseg=nperseg, axis=0)
    except Exception:
        return None

    n_chan = data.shape[1]
    abs_power = {}  # band → μV² array
    total = np.zeros(n_chan)

    for band, (lo, hi) in BANDS.items():
        mask = (freqs >= lo) & (freqs <= hi)
        bp = np.trapezoid(psd[mask], freqs[mask], axis=0)  # μV²
        abs_power[band] = np.maximum(bp, 1e-12)  # floor to avoid log(0)
        total += bp

    # Relative power (for noise detection)
    total_safe = np.maximum(total, 1e-12)
    rel = {}
    for band in BANDS:
        rel[band] = abs_power[band] / total_safe

    # Noise flag: beta+gamma relative > threshold → EMG/white noise dominated
    bg_rel = rel["Beta"] + rel["Gamma"]
    is_noise = bg_rel > NOISE_BG_RATIO

    # Absolute dB: 10*log10(μV²)  (reference 1 μV²)
    db = {}
    for band in BANDS:
        db[band] = 10.0 * np.log10(abs_power[band])

    total_db = 10.0 * np.log10(total_safe)

    return {"db": db, "rel": rel, "noise": is_noise, "total_db": total_db}


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


def classify_state(alpha, theta, beta, delta, theta_beta_ratio, hr):
    """Classify brain state from band powers in dB. All band inputs are in dB."""
    if alpha is not None and theta is not None and beta is not None:
        if alpha > theta and alpha > beta and alpha > 5:
            return "放松 Relaxed"
        if theta_beta_ratio is not None and theta_beta_ratio > 2.0:
            return "冥想 Meditative"
        if beta is not None and beta > 5 and beta > alpha:
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
    epoch_step = EPOCH_STEP
    epoch_width = EPOCH_SECONDS
    n_epochs = max(1, int((duration - epoch_width) / epoch_step) + 1)

    # ── Per-epoch analysis (sliding windows with overlap) ──
    rows = []
    noise_count = 0
    has_ppg = len(ppg_ir_arr) > 10
    has_hr = False

    for ep in range(n_epochs):
        t_start = t0 + ep * epoch_step
        t_end = t_start + epoch_width
        mask = (timestamps >= t_start) & (timestamps < t_end)

        row = {"minute": round((t_start - t0) / 60, 1)}

        # EEG band power
        eeg_chunk = eeg_arr[mask]
        bp = compute_band_power_chunk(eeg_chunk, SFREQ) if eeg_chunk.shape[0] > 10 else None
        if bp and all(bp["db"][b].shape[0] == 4 for b in BANDS):
            # Check noise: reject if ALL channels are noisy
            all_noisy = np.all(bp["noise"])
            row["noise"] = all_noisy
            if all_noisy:
                noise_count += 1

            # Store dB values (average across channels)
            row["alpha_db"] = float(np.mean(bp["db"]["Alpha"]))
            row["theta_db"] = float(np.mean(bp["db"]["Theta"]))
            row["beta_db"]  = float(np.mean(bp["db"]["Beta"]))
            row["delta_db"] = float(np.mean(bp["db"]["Delta"]))
            row["gamma_db"] = float(np.mean(bp["db"]["Gamma"]))
            row["total_db"] = float(np.mean(bp["total_db"]))

            # Theta/Beta ratio (linear ratio from dB)
            tb_linear = 10 ** ((row["theta_db"] - row["beta_db"]) / 10.0) if row["beta_db"] > -100 else 0
            row["theta_beta"] = tb_linear

            # Relative values (for reference)
            row["alpha_rel"] = float(np.mean(bp["rel"]["Alpha"]))
            row["beta_rel"]  = float(np.mean(bp["rel"]["Beta"]))
            row["bg_rel"]    = float(np.mean(bp["rel"]["Beta"] + bp["rel"]["Gamma"]))
        else:
            row["alpha_db"] = row["theta_db"] = row["beta_db"] = row["delta_db"] = row["gamma_db"] = None
            row["theta_beta"] = row["total_db"] = row["noise"] = None
            row["alpha_rel"] = row["beta_rel"] = row["bg_rel"] = None

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

        # State (skip noisy epochs)
        if not row.get("noise"):
            row["state"] = classify_state(row["alpha_db"], row["theta_db"],
                                           row["beta_db"], row["delta_db"],
                                           row["theta_beta"], row["hr"])
        else:
            row["state"] = "噪声 Noise"
        rows.append(row)

    # ── Apply moving average smoothing to band powers ──
    for band_key in ["delta_db", "theta_db", "alpha_db", "beta_db", "gamma_db", "theta_beta"]:
        values = np.array([r.get(band_key, np.nan) for r in rows], dtype=float)
        smoothed = moving_average(values, SMOOTH_POINTS)
        for i, r in enumerate(rows):
            r[band_key + "_smooth"] = float(smoothed[i]) if not np.isnan(smoothed[i]) else None

    # ── Build charts (use smoothed values) ──
    minutes = [r["minute"] for r in rows]

    def n(val):
        return val if val is not None else np.nan

    # Use smoothed dB values for charts
    colors = {"delta": "#888888", "theta": "#ffe66d", "alpha": "#4ecdc4",
              "beta": "#ff6b6b", "gamma": "#a37eba",
              "theta_beta": "#ff8c42", "hr": "#ff4444",
              "acc": "#44aaff", "gyro": "#44ff44"}

    state_colors = {"放松 Relaxed": "#4ecdc4", "中性 Neutral": "#888888",
                    "专注 Focused": "#ff6b6b", "冥想 Meditative": "#ffe66d",
                    "活跃 Active": "#ff8c42", "昏沉 Drowsy": "#a37eba",
                    "噪声 Noise": "#ff4444"}

    clean_rows = [r for r in rows if not r.get("noise")]

    # ── Chart 1: EEG Band Power (ABSOLUTE dB) ──
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

    vals = {k: [n(r[k + "_db_smooth"]) for r in rows] for k in ["delta", "theta", "alpha", "beta", "gamma"]}
    for band, label in [("delta", "δ Delta"), ("theta", "θ Theta"),
                         ("alpha", "α Alpha"), ("beta", "β Beta"),
                         ("gamma", "γ Gamma")]:
        ax1.plot(minutes, vals[band], color=colors[band], linewidth=1.5,
                 marker="o", markersize=4, label=label)
    ax1.set_ylabel("Power (dB re 1 μV²)", color="#ccc")
    ax1.set_title("EEG Absolute Band Power (dB)", color="#ccc", fontweight="bold")
    ax1.legend(loc="upper right", fontsize=7, facecolor="#1a1a2e",
               edgecolor="#333", labelcolor="#ccc")

    # Shade noisy minutes in red
    for r in rows:
        if r.get("noise"):
            ax1.axvspan(r["minute"] - 0.5, r["minute"] + 0.5,
                        facecolor="#ff0000", alpha=0.08)

    # Theta/Beta ratio (linear, smoothed)
    tb = [n(r["theta_beta_smooth"]) for r in rows]
    ax2.fill_between(minutes, 0, tb, color=colors["theta_beta"], alpha=0.3)
    ax2.plot(minutes, tb, color=colors["theta_beta"], linewidth=2, marker="s", markersize=5)
    ax2.axhline(y=1.0, color="#666", linewidth=0.8, linestyle="--")
    ax2.axhline(y=2.0, color="#666", linewidth=0.8, linestyle="--")
    ax2.set_ylabel("θ/β Ratio", color="#ccc")
    ax2.set_xlabel("Time (min)", color="#ccc")
    ax2.set_title("Theta/Beta Ratio", color="#ccc", fontweight="bold")
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
    ax3.set_ylim(0, 1); ax3.set_xlim(0, n_epochs)
    ax3.set_yticks([])
    ax3.set_xlabel("Time (epoch)", color="#ccc")
    ax3.set_title("Brain State Timeline (red = noise rejected)", color="#ccc", fontweight="bold")
    ax3.tick_params(colors="#888888")
    for s in ax3.spines.values(): s.set_visible(False)

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
               edgecolor="#333", labelcolor="#ccc", ncol=4)
    chart3 = fig_to_b64(fig3)
    plt.close(fig3)

    # ── Summary stats (from clean epochs only, fallback to all if none clean) ──
    src = clean_rows if clean_rows else rows
    avg_delta = np.nanmean([r["delta_db"] for r in src if r["delta_db"] is not None]) if src else 0
    avg_alpha = np.nanmean([r["alpha_db"] for r in src if r["alpha_db"] is not None]) if src else 0
    avg_beta  = np.nanmean([r["beta_db"]  for r in src if r["beta_db"] is not None]) if src else 0
    avg_theta = np.nanmean([r["theta_db"] for r in src if r["theta_db"] is not None]) if src else 0
    avg_tb = np.nanmean([r["theta_beta"] for r in src if r["theta_beta"] is not None]) if src else 0
    avg_hr = np.nanmean([r["hr"] for r in rows if r["hr"] is not None]) if any(r["hr"] is not None for r in rows) else float('nan')
    clean_pct = 100 * (n_epochs - noise_count) / max(n_epochs, 1)

    # ── Per-epoch table ──
    table_rows = ""
    for r in rows:
        def vd(val):
            return f"{val:.1f} dB" if val is not None else "-"
        def vr(val):
            return f"{val:.2f}" if val is not None else "-"
        hr_str = f"{r['hr']:.0f}" if r["hr"] else "-"
        acc_str = f"{r['acc_mag']:.3f}" if r["acc_mag"] is not None else "-"
        sc = state_colors.get(r["state"], "#888")
        noise_mark = " ⚠" if r.get("noise") else ""
        time_label = f"{r['minute']:.1f}m"
        table_rows += f"""<tr>
            <td>{time_label}{noise_mark}</td>
            <td>{vd(r.get('delta_db_smooth', r.get('delta_db')))}</td>
            <td>{vd(r.get('theta_db_smooth', r.get('theta_db')))}</td>
            <td>{vd(r.get('alpha_db_smooth', r.get('alpha_db')))}</td>
            <td>{vd(r.get('beta_db_smooth', r.get('beta_db')))}</td>
            <td>{vd(r.get('gamma_db_smooth', r.get('gamma_db')))}</td>
            <td>{vr(r.get('theta_beta_smooth', r.get('theta_beta')))}</td>
            <td>{hr_str}</td><td>{acc_str}</td>
            <td style="color:{sc};font-weight:bold;">{r['state']}</td>
        </tr>"""

    session_name = Path(bin_path).stem

    # ── Noise summary ──
    noise_html = ""
    if noise_count > 0:
        noise_html = f"""<div class="summary">
    <div style="border:1px solid #ef5350;"><span class="label">⚠ 噪声 epoch</span><br>
        <span class="value" style="color:#ef5350;">{noise_count}/{n_epochs}</span></div>
    <div><span class="label">干净数据占比</span><br><span class="value">{clean_pct:.0f}%</span></div>
    <div><span class="label">Epoch</span><br><span class="value">{EPOCH_SECONDS}s</span></div>
</div>"""

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

{noise_html}

<div class="summary">
    <div><span class="label">Duration</span><br><span class="value">{duration/60:.1f} min</span></div>
    <div><span class="label">Avg Delta</span><br><span class="value">{avg_delta:.1f} dB</span></div>
    <div><span class="label">Avg Alpha</span><br><span class="value">{avg_alpha:.1f} dB</span></div>
    <div><span class="label">Avg Theta/Beta</span><br><span class="value">{avg_tb:.2f}</span></div>
    <div><span class="label">Avg HR</span><br><span class="value">{avg_hr:.0f} BPM</span></div>
</div>

<h2>1. EEG Absolute Band Power (dB re 1 μV²)</h2>
<img src="data:image/png;base64,{chart1}">

<h2>2. Heart Rate &amp; Motion</h2>
{chart2_html if chart2_html else '<p style="color:#888;">(No PPG/Motion data)</p>'}

<h2>3. Brain State Timeline</h2>
<img src="data:image/png;base64,{chart3}">

<h2>4. Per-Minute Data</h2>
<table>
<tr><th>Time</th><th>Delta</th><th>Theta</th><th>Alpha</th><th>Beta</th><th>Gamma</th><th>TB-Ratio</th><th>HR</th><th>ACC</th><th>State</th></tr>
{table_rows}
</table>

<p class="note">
    Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}<br>
    Epoch: {EPOCH_SECONDS}s window, {EPOCH_STEP}s step, {SMOOTH_POINTS}-point moving average<br>
    Y-axis: dB = 10×log<sub>10</sub>(μV²) — absolute power referenced to 1 μV²<br>
    Bands: Delta 0.5-4Hz | Theta 4-8Hz | Alpha 8-12Hz | Beta 12-30Hz | Gamma 30-45Hz<br>
    Noise: epochs where Beta+Gamma &gt; {NOISE_BG_RATIO*100:.0f}% of total power are marked ⚠ and excluded from averages
</p>
</body></html>"""

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    Path(output_path).write_text(html, encoding="utf-8")
    return output_path
