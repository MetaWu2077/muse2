#!/usr/bin/env python3
"""
BrainVision EEG Timeline Report.
Handles 31-channel data from parse_brainvision.py output CSV.

Usage:
    python brainvision_report.py BP_practice_data/EEG_0001.csv
"""

import argparse
import csv
import io
import sys
from base64 import b64encode
from datetime import datetime
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.signal import welch

plt.rcParams["font.family"] = "sans-serif"
plt.rcParams["axes.unicode_minus"] = False

BANDS = {"Delta (0.5-4Hz)": (0.5, 4), "Theta (4-8Hz)": (4, 8),
         "Alpha (8-12Hz)": (8, 12), "Beta (12-30Hz)": (12, 30),
         "Gamma (30-45Hz)": (30, 45)}
WINDOW_SEC = 60  # Per-minute analysis


def compute_band_power(data, sfreq):
    """Band power for a data chunk. data: (samples, channels)"""
    if data.shape[0] < int(sfreq * 0.5):
        return None
    nperseg = min(int(sfreq * 1), data.shape[0] // 4)
    if nperseg < 32:
        return None
    nperseg = max(nperseg, 32)  # Ensure minimum segment size
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
        rel[band] = result[band] / np.where(total > 0, total, 1)
    return rel


def classify_state(alpha_avg, theta_avg, beta_avg, tb_avg):
    if alpha_avg and alpha_avg > 0.25:
        return u"放松 Relaxed (α↑)"
    if tb_avg and tb_avg > 1.5:
        return u"冥想 Meditative (θ/β↑)"
    if beta_avg and beta_avg > 0.30:
        return u"专注 Focused (β↑)"
    return u"中性 Neutral"


def fig_to_b64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                facecolor="#0f0f11", edgecolor="none")
    buf.seek(0)
    return b64encode(buf.read()).decode()


def n(val):
    return val if val is not None else np.nan


def main():
    parser = argparse.ArgumentParser(description="BrainVision Timeline Report")
    parser.add_argument("csv_file", help="CSV from parse_brainvision.py")
    args = parser.parse_args()

    csv_path = Path(args.csv_file)
    if not csv_path.exists():
        print(f"Not found: {csv_path}")
        sys.exit(1)

    # ── Read CSV ──
    timestamps = []
    markers = []
    all_data = []
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            print("Empty CSV")
            sys.exit(1)
        # Detect channel columns (anything that's not timestamp/marker)
        skip_cols = {"timestamp", "marker"}
        ch_names = [c for c in reader.fieldnames if c not in skip_cols]
        n_ch = len(ch_names)
        print(f"Detected {n_ch} channels: {', '.join(ch_names[:8])}..." if n_ch > 8 else f"Detected {n_ch} channels: {', '.join(ch_names)}")

        for row in reader:
            timestamps.append(float(row["timestamp"]))
            markers.append(row.get("marker", ""))
            all_data.append([float(row.get(ch, 0)) for ch in ch_names])

    timestamps = np.array(timestamps)
    data_arr = np.array(all_data)
    duration = timestamps[-1] - timestamps[0]
    sfreq = len(timestamps) / duration

    # Find marker events
    events = []
    for i, m in enumerate(markers):
        if m and m.strip():
            t = timestamps[i]
            events.append({"time": t, "minute": int(t / 60) + 1, "label": m})

    n_minutes = max(1, int(np.ceil(duration / 60)))

    # ── Per-minute analysis ──
    rows = []
    t0 = timestamps[0]
    for minute in range(n_minutes):
        t_start = t0 + minute * 60
        t_end = t0 + (minute + 1) * 60
        mask = (timestamps >= t_start) & (timestamps < t_end)
        chunk = data_arr[mask]

        # Events in this minute
        min_events = [e for e in events if t_start <= e["time"] < t_end]

        row = {"minute": minute + 1, "events": min_events}

        if chunk.shape[0] > 10:
            bp = compute_band_power(chunk, sfreq)
            if bp:
                row["alpha"] = float(np.mean(bp["Alpha (8-12Hz)"]))
                row["theta"] = float(np.mean(bp["Theta (4-8Hz)"]))
                row["beta"] = float(np.mean(bp["Beta (12-30Hz)"]))
                row["delta"] = float(np.mean(bp["Delta (0.5-4Hz)"]))
                row["gamma"] = float(np.mean(bp["Gamma (30-45Hz)"]))
                row["theta_beta"] = row["theta"] / row["beta"] if row["beta"] > 0 else 0

                # Top-3 dominant channels for Alpha
                alpha_by_ch = bp["Alpha (8-12Hz)"]
                top3_idx = np.argsort(alpha_by_ch)[-3:][::-1]
                row["alpha_top3"] = ", ".join(f"{ch_names[i]}" for i in top3_idx)
            else:
                row["alpha"] = row["theta"] = row["beta"] = row["delta"] = row["gamma"] = row["theta_beta"] = None
                row["alpha_top3"] = ""
        else:
            row["alpha"] = row["theta"] = row["beta"] = row["delta"] = row["gamma"] = row["theta_beta"] = None
            row["alpha_top3"] = ""

        # Signal quality
        if chunk.shape[0] > 10:
            row["sig_std"] = float(np.mean(np.std(chunk, axis=0)))
            row["sig_range"] = float(np.mean(np.ptp(chunk, axis=0)))
        else:
            row["sig_std"] = row["sig_range"] = None

        # State
        row["state"] = classify_state(row["alpha"], row["theta"], row["beta"], row["theta_beta"])
        rows.append(row)

    # ── Pre-compute channel spectral topography ──
    bp_full = compute_band_power(data_arr, sfreq) if data_arr.shape[0] > 100 else None

    # ── Parse coordinates from source .vhdr ──
    vhdr_path = csv_path.with_suffix(".vhdr")
    ch_coords = {}  # name -> (x, y) for topo plot
    if vhdr_path.exists():
        section = None
        with open(vhdr_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("["):
                    section = line.strip("[]").strip()
                    continue
                if section == "Coordinates" and line.startswith("Ch"):
                    parts = line.split(",")
                    ch_num = int(parts[0].split("=")[0][2:])
                    radius = float(parts[0].split("=")[1] if "=" in parts[0] else parts[1])
                    theta = float(parts[1])  # azimuth
                    phi = float(parts[2])    # latitude
                    # Convert spherical to 2D topo (azimuthal equidistant projection)
                    # Normalize: theta 0=right, phi 0=front
                    r = (90 - abs(phi)) / 90  # 0=top, 1=bottom
                    x = r * np.cos(np.radians(theta))
                    y = r * np.sin(np.radians(theta))
                    if ch_num - 1 < len(ch_names):
                        ch_coords[ch_names[ch_num - 1]] = (x, y)

    # ── Figure: Topographic Maps ──
    if bp_full and len(ch_coords) >= 8:
        fig_topo, axes_topo = plt.subplots(1, 5, figsize=(14, 3.5))
        fig_topo.patch.set_facecolor("#0f0f11")

        for idx, (band_name, (lo, hi)) in enumerate(BANDS.items()):
            ax = axes_topo[idx]
            ax.set_facecolor("#0f0f11")
            ax.set_aspect("equal")
            ax.axis("off")

            values = bp_full[band_name]
            # Global normalize across all bands for comparison
            all_vals = np.concatenate([bp_full[b] for b in BANDS])
            vmin, vmax = np.percentile(all_vals, 5), np.percentile(all_vals, 95)
            if vmax <= vmin:
                vmax = vmin + 0.001

            # Plot each electrode
            for i, ch_name in enumerate(ch_names):
                if ch_name in ch_coords and i < len(values):
                    x, y = ch_coords[ch_name]
                    val = values[i]
                    t = (val - vmin) / (vmax - vmin)
                    t = max(0, min(1, t))
                    color = plt.cm.viridis(t)
                    ax.add_patch(plt.Circle((x, y), 0.06, facecolor=color, edgecolor="#333", linewidth=0.5))

            # Head outline + nose
            ax.add_patch(plt.Circle((0, 0), 1.02, fill=False, color="#555", linewidth=1))
            ax.plot(0, 1.05, marker="v", color="#555", markersize=6)
            ax.set_xlim(-1.15, 1.15); ax.set_ylim(-1.15, 1.2)
            short_name = band_name.split(" ")[0]
            ax.set_title(f"{short_name} {lo}-{hi}Hz", color="#ccc", fontsize=9)

        # Add colorbar
        sm = plt.cm.ScalarMappable(cmap="viridis", norm=plt.Normalize(vmin, vmax))
        cbar = fig_topo.colorbar(sm, ax=axes_topo, orientation="horizontal", fraction=0.05, pad=0.12,
                                  shrink=0.6, aspect=30)
        cbar.set_label("相对功率 Relative Power (低→高)", color="#ccc", fontsize=8)
        cbar.ax.tick_params(colors="#888", labelsize=7)

        chart_topo = fig_to_b64(fig_topo)
        plt.close(fig_topo)
    else:
        chart_topo = None

    # ── Figure: Regional Analysis (bar chart) ──
    if bp_full and len(ch_coords) >= 8:
        # Define regions
        regions = {
            "前额 Frontal": ["Fp1", "Fp2", "F3", "F4", "F7", "F8", "Fz"],
            "中央 Central": ["C3", "C4", "Cz", "FC1", "FC2", "FC5", "FC6", "CP1", "CP2", "CP5", "CP6"],
            "颞叶 Temporal": ["T7", "T8", "FT9", "FT10", "TP9", "TP10"],
            "顶叶 Parietal": ["P3", "P4", "P7", "P8", "Pz"],
            "枕叶 Occipital": ["O1", "O2", "Oz"],
        }
        region_data = {}
        for region, region_chs in regions.items():
            ch_indices = [i for i, ch in enumerate(ch_names) if ch in region_chs]
            if ch_indices:
                region_vals = {}
                for band_name in BANDS:
                    region_vals[band_name] = float(np.mean(bp_full[band_name][ch_indices]))
                region_data[region] = region_vals

        fig_reg, ax_reg = plt.subplots(figsize=(12, 4))
        fig_reg.patch.set_facecolor("#0f0f11")
        ax_reg.set_facecolor("#1a1a2e")
        ax_reg.tick_params(colors="#888")
        for s in ax_reg.spines.values(): s.set_color("#333")
        ax_reg.spines["top"].set_visible(False); ax_reg.spines["right"].set_visible(False)
        ax_reg.grid(True, color="#222", linewidth=0.3, axis="y")

        x = np.arange(len(regions))
        width = 0.15
        band_colors_reg = {"Delta (0.5-4Hz)": "#888888", "Theta (4-8Hz)": "#ffe66d",
                           "Alpha (8-12Hz)": "#4ecdc4", "Beta (12-30Hz)": "#ff6b6b",
                           "Gamma (30-45Hz)": "#a37eba"}
        for i, (band_name, color) in enumerate(band_colors_reg.items()):
            vals = [region_data[r][band_name] for r in regions]
            ax_reg.bar(x + i * width, vals, width, color=color, alpha=0.8, label=band_name.split(" ")[0])

        ax_reg.set_xticks(x + width * 2)
        ax_reg.set_xticklabels(regions.keys(), color="#ccc")
        ax_reg.set_ylabel("相对功率 Relative Power", color="#ccc")
        ax_reg.set_title("脑区频段分布 Regional Band Power", color="#ccc", fontweight="bold")
        ax_reg.legend(loc="upper right", fontsize=7, facecolor="#1a1a2e", edgecolor="#333", labelcolor="#ccc")

        chart_reg = fig_to_b64(fig_reg)
        plt.close(fig_reg)

        # Left vs Right asymmetry (key indicator for emotion/approach-avoidance)
        left_chs = [ch for ch in ch_names if ch[-1].isdigit() and int(ch[-1]) % 2 == 1]  # odd = left
        right_chs = [ch for ch in ch_names if ch[-1].isdigit() and int(ch[-1]) % 2 == 0]  # even = right
        left_idx = [i for i, ch in enumerate(ch_names) if ch in left_chs]
        right_idx = [i for i, ch in enumerate(ch_names) if ch in right_chs]

        if left_idx and right_idx:
            left_alpha = float(np.mean(bp_full["Alpha (8-12Hz)"][left_idx]))
            right_alpha = float(np.mean(bp_full["Alpha (8-12Hz)"][right_idx]))
            asymmetry_score = (right_alpha - left_alpha) / (right_alpha + left_alpha) if (right_alpha + left_alpha) > 0 else 0
            if asymmetry_score > 0.05:
                asym_label = "左脑偏活跃 Left-dominant (趋近倾向 approach)"
            elif asymmetry_score < -0.05:
                asym_label = "右脑偏活跃 Right-dominant (回避倾向 withdrawal)"
            else:
                asym_label = "左右均衡 Balanced"
        else:
            left_alpha = right_alpha = asymmetry_score = None
            asym_label = ""
    else:
        chart_reg = None
        left_alpha = right_alpha = asymmetry_score = None
        asym_label = ""

    # ── Charts ──
    minutes = list(range(1, n_minutes + 1))
    colors = {"alpha": "#4ecdc4", "beta": "#ff6b6b", "theta": "#ffe66d",
              "delta": "#888888", "gamma": "#a37eba", "theta_beta": "#ff8c42"}

    # Figure 1: Band Power + Theta/Beta
    fig1, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7))
    fig1.patch.set_facecolor("#0f0f11")
    for ax in [ax1, ax2]:
        ax.set_facecolor("#1a1a2e"); ax.tick_params(colors="#888")
        ax.grid(True, color="#222", linewidth=0.3)
        for s in ax.spines.values(): s.set_color("#333")
        ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)

    for band, key, label in [
        ("Delta (0.5-4Hz)", "delta", u"δ Delta (深度睡眠)"),
        ("Theta (4-8Hz)", "theta", u"θ Theta (冥想)"),
        ("Alpha (8-12Hz)", "alpha", u"α Alpha (放松)"),
        ("Beta (12-30Hz)", "beta", u"β Beta (活跃)"),
        ("Gamma (30-45Hz)", "gamma", u"γ Gamma (高级认知)")]:
        y = [n(r[key]) for r in rows]
        ax1.plot(minutes, y, color=colors[key], linewidth=1.5, marker="o", markersize=4, label=label)
    ax1.set_ylabel("相对功率 Relative Power", color="#ccc")
    ax1.set_title(f"脑电频段趋势 Band Power ({n_ch}ch, {sfreq:.0f}Hz) — {csv_path.name}", color="#ccc", fontweight="bold")
    ax1.legend(loc="upper right", fontsize=6, facecolor="#1a1a2e", edgecolor="#333", labelcolor="#ccc", ncol=2)

    tb = [n(r["theta_beta"]) for r in rows]
    ax2.fill_between(minutes, 0, tb, color=colors["theta_beta"], alpha=0.3)
    ax2.plot(minutes, tb, color=colors["theta_beta"], linewidth=2, marker="s", markersize=5)
    ax2.axhline(y=0.8, color="#666", linewidth=0.8, linestyle="--")
    ax2.axhline(y=1.5, color="#666", linewidth=0.8, linestyle="--")
    ax2.text(minutes[-1], 0.82, "专注 Focused", color="#888", fontsize=7, va="bottom")
    ax2.text(minutes[-1], 1.52, "放松 Relaxed", color="#888", fontsize=7, va="bottom")
    ax2.set_ylabel("θ/β 比值 Ratio", color="#ccc")
    ax2.set_xlabel("分钟 Minute", color="#ccc")
    ax2.set_title("θ/β 比值趋势 (专注度 Focus Indicator)", color="#ccc", fontweight="bold")
    ax2.set_ylim(bottom=0)

    chart1 = fig_to_b64(fig1)
    plt.close(fig1)

    # Figure 2: Signal Quality
    fig2, ax3 = plt.subplots(figsize=(12, 3))
    fig2.patch.set_facecolor("#0f0f11")
    ax3.set_facecolor("#1a1a2e"); ax3.tick_params(colors="#888")
    ax3.grid(True, color="#222", linewidth=0.3)
    for s in ax3.spines.values(): s.set_color("#333")
    ax3.spines["top"].set_visible(False); ax3.spines["right"].set_visible(False)
    sig_std = [n(r["sig_std"]) for r in rows]
    ax3.fill_between(minutes, 0, sig_std, color="#4ecdc4", alpha=0.3)
    ax3.plot(minutes, sig_std, color="#4ecdc4", linewidth=1.5, marker="o", markersize=4)
    ax3.set_ylabel("σ (µV)", color="#ccc")
    ax3.set_xlabel("分钟 Minute", color="#ccc")
    ax3.set_title("信号强度趋势 Signal Amplitude (标准差 averaged std)", color="#ccc", fontweight="bold")
    chart2 = fig_to_b64(fig2)
    plt.close(fig2)

    # Figure 3: State Timeline
    state_colors = {u"放松 Relaxed (α↑)": "#4ecdc4", u"中性 Neutral": "#888888",
                    u"专注 Focused (β↑)": "#ff6b6b", u"冥想 Meditative (θ/β↑)": "#ffe66d"}
    fig3, ax4 = plt.subplots(figsize=(12, 2.5))
    fig3.patch.set_facecolor("#0f0f11")
    ax4.set_facecolor("#0f0f11")
    ax4.set_ylim(0, 1); ax4.set_xlim(0, n_minutes); ax4.set_yticks([])
    ax4.set_xlabel("分钟 Minute", color="#ccc")
    ax4.set_title("脑状态时间线 Brain State Timeline", color="#ccc", fontweight="bold")
    ax4.tick_params(colors="#888")
    for s in ax4.spines.values(): s.set_visible(False)

    prev_state = None
    for i, r in enumerate(rows):
        state = r["state"]
        color = state_colors.get(state, "#888")
        ax4.axvspan(i, i + 1, facecolor=color, alpha=0.6)
        if state != prev_state:
            ax4.text(i + 0.5, 0.5, state, ha="center", va="center",
                    color="#fff", fontsize=8, fontweight="bold")
        prev_state = state

    # Legend + event markers
    for st, clr in state_colors.items():
        if any(r["state"] == st for r in rows):
            ax4.plot([], [], color=clr, linewidth=6, label=st)
    # Mark events
    for e in events:
        x_pos = e["minute"] - 0.4 + (e["time"] % 60) / 60
        ax4.axvline(x=x_pos, color="#fff", linewidth=1, linestyle=":", alpha=0.7)
        ax4.text(x_pos, 0.9, e["label"], color="#fff", fontsize=6, rotation=90, va="top")
    ax4.legend(loc="upper right", fontsize=7, facecolor="#1a1a2e", edgecolor="#333",
               labelcolor="#ccc", ncol=2)

    chart3 = fig_to_b64(fig3)
    plt.close(fig3)

    # ── HTML Report ──
    table_rows = ""
    for r in rows:
        def v(val, fmt=".4f"):
            return f"{val:{fmt}}" if val is not None else "-"
        sc = state_colors.get(r["state"], "#888")
        evt = ", ".join(e["label"] for e in r["events"]) if r["events"] else ""
        table_rows += f"""<tr>
            <td>{r['minute']}</td><td>{v(r['delta'])}</td><td>{v(r['theta'])}</td>
            <td>{v(r['alpha'])}</td><td>{v(r['beta'])}</td><td>{v(r['gamma'])}</td>
            <td>{v(r['theta_beta'])}</td>
            <td>{v(r['sig_std'], '.1f')}</td>
            <td style="font-size:10px;">{r.get('alpha_top3', '')}</td>
            <td style="color:{sc};font-weight:bold;">{r['state']}</td>
            <td style="font-size:10px;">{evt}</td>
        </tr>"""

    alpha_vals = [r["alpha"] for r in rows if r["alpha"] is not None]
    tb_vals = [r["theta_beta"] for r in rows if r["theta_beta"] is not None]

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{csv_path.stem} - BrainVision Report</title>
<style>
body {{ background:#0f0f11; color:#ccc; font-family:system-ui,sans-serif; max-width:1200px; margin:0 auto; padding:20px; }}
h1 {{ color:#fff; border-bottom:2px solid #333; padding-bottom:10px; }}
h2 {{ color:#ddd; margin-top:30px; }}
table {{ border-collapse:collapse; width:100%; margin:15px 0; font-size:13px; }}
th {{ background:#1a1a2e; color:#fff; padding:8px 10px; text-align:right; }}
td {{ padding:6px 10px; text-align:right; border-bottom:1px solid #222; }}
th:first-child, td:first-child {{ text-align:center; }}
td:nth-child(8), td:nth-child(9) {{ text-align:center; }}
img {{ max-width:100%; border:1px solid #333; border-radius:4px; }}
.summary {{ display:flex; gap:20px; flex-wrap:wrap; margin:15px 0; }}
.summary div {{ background:#1a1a2e; padding:12px 18px; border-radius:6px; min-width:120px; }}
.summary .value {{ font-size:24px; font-weight:bold; color:#4ecdc4; }}
.summary .label {{ font-size:12px; color:#888; }}
.note {{ font-size:12px; color:#888; margin:30px 0 10px 0; border-top:1px solid #333; padding-top:10px; }}
</style>
</head>
<body>

<h1>BrainVision 脑电时间线分析报告<br>
<small>EEG Timeline Analysis — {csv_path.name}</small></h1>

<div class="summary">
    <div><span class="label">通道 Channels</span><br><span class="value">{n_ch}</span></div>
    <div><span class="label">采样率 SFreq</span><br><span class="value">{sfreq:.0f} Hz</span></div>
    <div><span class="label">时长 Duration</span><br><span class="value">{duration/60:.1f} min</span></div>
    <div><span class="label">平均 α Avg Alpha</span><br><span class="value">{np.nanmean(alpha_vals):.3f}</span></div>
    <div><span class="label">平均 θ/β Avg TB</span><br><span class="value">{np.nanmean(tb_vals):.3f}</span></div>
    <div><span class="label">标记 Events</span><br><span class="value">{len(events)}</span></div>
</div>

<h2>1. 头皮地形图 Scalp Topography (全脑频段分布)</h2>
{ f'<img src="data:image/png;base64,{chart_topo}">' if chart_topo else '<p style="color:#888;">无法生成地形图（坐标数据缺失）</p>' }

<h2>2. 脑区频段分布 Regional Analysis</h2>
{ f'<img src="data:image/png;base64,{chart_reg}">' if chart_reg else '<p style="color:#888;">无法生成区域分析（坐标数据缺失）</p>' }
{"".join([
    f'<div class="summary" style="margin-top:10px;">',
    f'<div><span class="label">左脑 α Left Alpha</span><br><span class="value">{left_alpha:.3f}</span></div>' if left_alpha is not None else '',
    f'<div><span class="label">右脑 α Right Alpha</span><br><span class="value">{right_alpha:.3f}</span></div>' if right_alpha is not None else '',
    f'<div><span class="label">不对称指数 Asymmetry</span><br><span class="value">{asymmetry_score:+.3f}</span><br><span class="label">{asym_label}</span></div>' if asymmetry_score is not None else '',
    f'</div>',
]) if asymmetry_score is not None else ""}

<h2>3. 脑电频段趋势 EEG Band Power & Focus</h2>
<img src="data:image/png;base64,{chart1}">

<h2>4. 信号强度 Signal Amplitude</h2>
<img src="data:image/png;base64,{chart2}">

<h2>5. 脑状态时间线 Brain State Timeline</h2>
<img src="data:image/png;base64,{chart3}">

<h2>6. 逐分钟明细 Per-Minute Data</h2>
<table>
<tr>
    <th>分钟 Min</th><th>δ Delta</th><th>θ Theta</th><th>α Alpha</th>
    <th>β Beta</th><th>γ Gamma</th><th>θ/β TB</th>
    <th>信号 σ(µV)</th><th>Alpha最强通道</th>
    <th>状态 State</th><th>标记 Events</th>
</tr>
{table_rows}
</table>

<h2>7. 标记事件 Event Markers</h2>
<table>
<tr><th>时间 Time</th><th>分钟 Min</th><th>标记 Marker</th></tr>
{"".join(f'<tr><td>{e["time"]:.1f}s</td><td>{e["minute"]}</td><td>{e["label"]}</td></tr>' for e in events) if events else '<tr><td colspan="3" style="text-align:center;color:#888;">无标记 No markers</td></tr>'}
</table>

<p class="note">
    报告生成 Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}<br>
    通道: {', '.join(ch_names)}<br>
    频段 Bands: δ=0.5-4Hz | θ=4-8Hz | α=8-12Hz | β=12-30Hz | γ=30-45Hz
</p>

</body></html>"""

    report_path = csv_path.with_suffix(".brainvision.html")
    report_path.write_text(html, encoding="utf-8")
    print(f"Report saved: {report_path}")


if __name__ == "__main__":
    main()
