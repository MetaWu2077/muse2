#!/usr/bin/env python3
"""
Per-minute timeline analysis with trend charts.
Generates an HTML report with embedded charts.

Usage:
    python timeline_report.py recordings/20260618_101352_work.csv
"""

import argparse
import csv
import io
import sys
from base64 import b64encode
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.signal import welch, find_peaks, butter, filtfilt

# Chinese font support
plt.rcParams["font.family"] = "sans-serif"
plt.rcParams["axes.unicode_minus"] = False

BANDS = {"Delta": (0.5, 4), "Theta": (4, 8), "Alpha": (8, 12),
         "Beta": (12, 30), "Gamma": (30, 45)}
CHANNELS = ["TP9", "AF7", "AF8", "TP10"]
SFREQ = 256.0
WINDOW_MIN = 1  # Per-minute analysis


def compute_band_power_chunk(data, sfreq):
    """Band power for a data chunk."""
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
    # Relative
    rel = {}
    for band in BANDS:
        rel[band] = result[band] / total
    return rel


def compute_hr_chunk(ppg, sfreq=64.0):
    """Heart rate from PPG chunk."""
    if len(ppg) < int(sfreq * 2):
        return None
    ppg_c = ppg - np.mean(ppg)
    nyq = sfreq / 2
    b, a = butter(2, [0.7/nyq, 4.0/nyq], btype="band")
    filtered = filtfilt(b, a, ppg_c)
    peaks, _ = find_peaks(filtered, distance=int(sfreq * 0.35),
                          height=0.1 * np.std(filtered))
    if len(peaks) >= 2:
        return 60.0 / np.median(np.diff(peaks) / sfreq)
    return None


def classify_state(alpha, theta, beta, theta_beta, hr):
    """Classify brain state from metrics."""
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
    """Convert matplotlib figure to base64 PNG."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                facecolor="#0f0f11", edgecolor="none")
    buf.seek(0)
    return b64encode(buf.read()).decode()


def main():
    parser = argparse.ArgumentParser(description="Timeline Analysis Report")
    parser.add_argument("csv_file", help="Path to recorded CSV")
    args = parser.parse_args()

    csv_path = Path(args.csv_file)
    if not csv_path.exists():
        print(f"File not found: {csv_path}")
        sys.exit(1)

    # ── Read all data ──
    timestamps, eeg_raw, ppg_raw, acc_raw, gyro_raw, hr_raw = [], [], [], [], [], []
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        has_ppg = "PPG_IR" in fields or "PPG_ambient" in fields
        has_acc = "ACC_X" in fields
        has_gyro = "GYRO_X" in fields
        has_hr = "HR" in fields
        for row in reader:
            timestamps.append(float(row["timestamp"]))
            eeg_raw.append([float(row.get(ch, 0)) for ch in CHANNELS])
            if has_ppg:
                ppg_raw.append([float(row.get(f"PPG_{k}", 0)) for k in ["ambient", "IR", "Red"]])
            if has_acc:
                acc_raw.append([float(row.get(f"ACC_{k}", 0)) for k in ["X", "Y", "Z"]])
            if has_gyro:
                gyro_raw.append([float(row.get(f"GYRO_{k}", 0)) for k in ["X", "Y", "Z"]])
            if has_hr:
                hr_raw.append(float(row["HR"]) if row.get("HR", "").strip() else None)

    timestamps = np.array(timestamps)
    eeg_arr = np.array(eeg_raw)
    ppg_arr = np.array(ppg_raw) if ppg_raw else np.zeros((0, 3))
    acc_arr = np.array(acc_raw) if acc_raw else np.zeros((0, 3))
    gyro_arr = np.array(gyro_raw) if gyro_raw else np.zeros((0, 3))
    hr_arr = np.array(hr_raw) if hr_raw else np.array([])

    t0 = timestamps[0]
    tend = timestamps[-1]
    duration = tend - t0
    n_minutes = max(1, int(np.ceil(duration / 60)))

    # ── Per-minute analysis ──
    rows = []
    for m in range(n_minutes):
        t_start = t0 + m * 60
        t_end = t0 + (m + 1) * 60
        mask = (timestamps >= t_start) & (timestamps < t_end)

        row = {"minute": m + 1, "time": datetime.now().strftime("%H:%M")}

        # EEG
        eeg_chunk = eeg_arr[mask]
        bp = compute_band_power_chunk(eeg_chunk, SFREQ) if eeg_chunk.shape[0] > 10 else None
        if bp:
            row["alpha"] = float(np.mean([bp["Alpha"][i] for i in range(4)]))
            row["theta"] = float(np.mean([bp["Theta"][i] for i in range(4)]))
            row["beta"] = float(np.mean([bp["Beta"][i] for i in range(4)]))
            row["delta"] = float(np.mean([bp["Delta"][i] for i in range(4)]))
            row["gamma"] = float(np.mean([bp["Gamma"][i] for i in range(4)]))
            row["theta_beta"] = row["theta"] / row["beta"] if row["beta"] > 0 else 0
        else:
            row["alpha"] = row["theta"] = row["beta"] = row["delta"] = row["gamma"] = row["theta_beta"] = None

        # HR
        if has_ppg and ppg_arr.shape[0] > 0:
            ppg_chunk = ppg_arr[mask, 1]  # IR channel
            hr_val = compute_hr_chunk(ppg_chunk) if len(ppg_chunk) > 10 else None
            row["hr"] = hr_val
        elif has_hr and len(hr_arr) > 0:
            hr_chunk = hr_arr[mask]
            hr_vals = [h for h in hr_chunk if h is not None and 30 < h < 220]
            row["hr"] = float(np.mean(hr_vals)) if hr_vals else None
        else:
            row["hr"] = None

        # Movement
        if has_acc and acc_arr.shape[0] > 0:
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
        if has_gyro and gyro_arr.shape[0] > 0:
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

    # Helper: replace None with np.nan for plotting
    def n(val):
        return val if val is not None else np.nan
    colors = {"alpha": "#4ecdc4", "beta": "#ff6b6b", "theta": "#ffe66d",
              "delta": "#888888", "gamma": "#a37eba", "theta_beta": "#ff8c42",
              "hr": "#ff4444", "acc": "#44aaff", "gyro": "#44ff44"}
    states_order = ["放松 Relaxed", "中性 Neutral", "专注 Focused",
                    "冥想 Meditative", "活跃 Active", "昏沉 Drowsy"]
    state_colors = {"放松 Relaxed": "#4ecdc4", "中性 Neutral": "#888888",
                    "专注 Focused": "#ff6b6b", "冥想 Meditative": "#ffe66d",
                    "活跃 Active": "#ff8c42", "昏沉 Drowsy": "#a37eba"}

    # ── Figure 1: EEG Band Power Trends + Theta/Beta ──
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

    # Band power
    vals = {k: [n(r[k]) for r in rows] for k in ["alpha", "beta", "theta", "delta", "gamma"]}
    for band, label in [("alpha", u"α Alpha 放松"), ("beta", u"β Beta 活跃"),
                         ("theta", u"θ Theta 冥想"), ("delta", u"δ Delta 深度"),
                         ("gamma", u"γ Gamma 认知")]:
        y = vals[band]
        ax1.plot(minutes, y, color=colors[band], linewidth=1.5, marker="o", markersize=4, label=label)
    ax1.set_ylabel("相对功率 Relative Power", color="#ccc")
    ax1.set_title("脑电频段趋势 EEG Band Power Trends", color="#ccc", fontweight="bold")
    ax1.legend(loc="upper right", fontsize=7, facecolor="#1a1a2e", edgecolor="#333",
               labelcolor="#ccc")

    # Theta/Beta ratio
    tb = [n(r["theta_beta"]) for r in rows]
    ax2.fill_between(minutes, 0, tb, color=colors["theta_beta"], alpha=0.3)
    ax2.plot(minutes, tb, color=colors["theta_beta"], linewidth=2, marker="s", markersize=5)
    ax2.axhline(y=0.8, color="#666", linewidth=0.8, linestyle="--")
    ax2.axhline(y=1.5, color="#666", linewidth=0.8, linestyle="--")
    ax2.text(minutes[-1], 0.8, "  专注 Focused", color="#888", fontsize=7, va="bottom")
    ax2.text(minutes[-1], 1.5, "  放松 Relaxed", color="#888", fontsize=7, va="bottom")
    ax2.set_ylabel("θ/β 比值 Ratio", color="#ccc")
    ax2.set_xlabel("分钟 Minute", color="#ccc")
    ax2.set_title("θ/β 比值趋势 Theta/Beta Ratio (专注度 Focus)", color="#ccc", fontweight="bold")
    ax2.set_ylim(bottom=0)

    chart1 = fig_to_b64(fig1)
    plt.close(fig1)

    # ── Figure 2: Heart Rate + Movement ──
    has_hr_data = any(r["hr"] is not None for r in rows)
    has_motion = any(r["acc_mag"] is not None and r["acc_mag"] > 0.001 for r in rows)
    charts2_html = ""

    if has_hr_data or has_motion:
        n_plots = (1 if has_hr_data else 0) + (1 if has_motion else 0)
        fig2, axes = plt.subplots(n_plots, 1, figsize=(10, 3 * n_plots))
        fig2.patch.set_facecolor("#0f0f11")
        if n_plots == 1:
            axes = [axes]

        plot_idx = 0
        if has_hr_data:
            ax = axes[plot_idx]
            ax.set_facecolor("#1a1a2e")
            ax.tick_params(colors="#888888")
            for s in ax.spines.values(): s.set_color("#333")
            ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
            ax.grid(True, color="#222", linewidth=0.3)
            hr_vals = [n(r["hr"]) for r in rows]
            ax.fill_between(minutes, 50, hr_vals, color=colors["hr"], alpha=0.15)
            ax.plot(minutes, hr_vals, color=colors["hr"], linewidth=2, marker="o", markersize=5)
            ax.axhline(y=60, color="#666", linewidth=0.8, linestyle="--", alpha=0.5)
            ax.axhline(y=80, color="#666", linewidth=0.8, linestyle="--", alpha=0.5)
            ax.set_ylabel("BPM", color="#ccc")
            ax.set_title("心率趋势 Heart Rate", color="#ccc", fontweight="bold")
            ax.set_ylim(40, 120)
            plot_idx += 1

        if has_motion:
            ax = axes[plot_idx]
            ax.set_facecolor("#1a1a2e")
            ax.tick_params(colors="#888888")
            for s in ax.spines.values(): s.set_color("#333")
            ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
            ax.grid(True, color="#222", linewidth=0.3)
            acc_vals = [n(r["acc_mag"]) for r in rows]
            gyro_vals = [n(r["gyro_mag"]) for r in rows]
            ax2b = ax.twinx()
            ax.fill_between(minutes, 0, acc_vals, color=colors["acc"], alpha=0.2, label="运动 ACC")
            ax.plot(minutes, acc_vals, color=colors["acc"], linewidth=1.5, marker="o", markersize=4)
            ax2b.plot(minutes, gyro_vals, color=colors["gyro"], linewidth=1.5, marker="s", markersize=4, label="旋转 GYRO")
            ax.set_ylabel("加速度 ACC (g)", color=colors["acc"])
            ax2b.set_ylabel("角速度 GYRO (deg/s)", color=colors["gyro"])
            ax.set_xlabel("分钟 Minute", color="#ccc")
            ax.set_title("头部运动与旋转 Movement & Rotation", color="#ccc", fontweight="bold")
            ax.legend(loc="upper left", fontsize=7, facecolor="#1a1a2e", edgecolor="#333", labelcolor="#ccc")
            ax2b.legend(loc="upper right", fontsize=7, facecolor="#1a1a2e", edgecolor="#333", labelcolor="#ccc")

        chart2 = fig_to_b64(fig2)
        plt.close(fig2)
        charts2_html = f'<img src="data:image/png;base64,{chart2}" style="width:100%; margin:10px 0;">'

    # ── Figure 3: Brain State Timeline ──
    fig3, ax3 = plt.subplots(figsize=(10, 2.5))
    fig3.patch.set_facecolor("#0f0f11")
    ax3.set_facecolor("#0f0f11")
    ax3.set_ylim(0, 1)
    ax3.set_xlim(0, n_minutes)
    ax3.set_yticks([])
    ax3.set_xlabel("分钟 Minute", color="#ccc")
    ax3.set_title("脑状态变化 Brain State Timeline", color="#ccc", fontweight="bold")
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

    # Legend
    for state, color in state_colors.items():
        if any(r["state"] == state for r in rows):
            ax3.plot([], [], color=color, linewidth=6, label=state)
    ax3.legend(loc="upper right", fontsize=7, facecolor="#1a1a2e", edgecolor="#333",
               labelcolor="#ccc", ncol=3)

    chart3 = fig_to_b64(fig3)
    plt.close(fig3)

    # ── Build HTML Report ──
    table_rows = ""
    for r in rows:
        def v(val, fmt=".4f"):
            return f"{val:{fmt}}" if val is not None else "-"
        hr_str = f"{r['hr']:.0f}" if r["hr"] else "-"
        acc_str = f"{r['acc_mag']:.3f}" if r["acc_mag"] is not None else "-"
        state_color = state_colors.get(r["state"], "#888")
        table_rows += f"""<tr>
            <td>{r['minute']}</td>
            <td>{v(r['alpha'])}</td><td>{v(r['beta'])}</td><td>{v(r['theta'])}</td>
            <td>{v(r['theta_beta'])}</td><td>{hr_str}</td><td>{acc_str}</td>
            <td style="color:{state_color};font-weight:bold;">{r['state']}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{csv_path.stem} - Timeline Report</title>
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
.note {{ font-size:12px; color:#888; margin:30px 0 10px 0; border-top:1px solid #333; padding-top:10px; }}
</style>
</head>
<body>

<h1>脑电记录时间线分析报告<br>
<small>EEG Timeline Analysis Report — {csv_path.name}</small></h1>

<div class="summary">
    <div><span class="label">时长 Duration</span><br><span class="value">{duration/60:.1f} min</span></div>
    <div><span class="label">平均 α Alpha</span><br><span class="value">{np.nanmean([r['alpha'] for r in rows if r['alpha'] is not None]):.3f}</span></div>
    <div><span class="label">平均 θ/β TB-Ratio</span><br><span class="value">{np.nanmean([r['theta_beta'] for r in rows if r['theta_beta'] is not None]):.3f}</span></div>
    <div><span class="label">平均心率 Avg HR</span><br><span class="value">{np.nanmean([r['hr'] for r in rows if r['hr'] is not None]):.0f} BPM</span></div>
</div>

<h2>1. 脑电频段趋势 EEG Band Power & Focus</h2>
<img src="data:image/png;base64,{chart1}">

<h2>2. 心率与运动 Heart Rate & Motion</h2>
{charts2_html if charts2_html else '<p style="color:#888;">(无 PPG/ACC/GYRO 数据 no data)</p>'}

<h2>3. 脑状态时间线 Brain State Timeline</h2>
<img src="data:image/png;base64,{chart3}">

<h2>4. 逐分钟明细表 Per-Minute Data</h2>
<table>
<tr>
    <th>分钟 Min</th>
    <th>α Alpha</th><th>β Beta</th><th>θ Theta</th>
    <th>θ/β Ratio</th><th>心率 HR</th><th>运动 ACC</th>
    <th>状态 State</th>
</tr>
{table_rows}
</table>

<p class="note">
    报告生成时间 Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}<br>
    频段定义 Bands: δ Delta 0.5-4Hz | θ Theta 4-8Hz | α Alpha 8-12Hz | β Beta 12-30Hz | γ Gamma 30-45Hz<br>
    状态判断: α&gt;0.25→放松 Relaxed | θ/β&gt;1.5→冥想 Meditative | β&gt;0.30→专注 Focused | HR&gt;90→活跃 Active | HR&lt;55→昏沉 Drowsy
</p>

</body></html>"""

    # Save
    report_path = csv_path.with_suffix(".timeline.html")
    report_path.write_text(html, encoding="utf-8")
    print(f"Report saved: {report_path}")


if __name__ == "__main__":
    main()
