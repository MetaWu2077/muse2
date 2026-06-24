#!/usr/bin/env python3
"""
Generate a summary analysis report from a recorded EEG CSV file.
No NeuroSkill connection needed — works standalone.

Usage:
    python analyze_recording.py recordings/20260618_101352_work.csv
"""

import argparse
import csv
import sys
from pathlib import Path

import numpy as np
from scipy.signal import welch, find_peaks, butter, filtfilt

BANDS = {
    "Delta": (0.5, 4),
    "Theta": (4, 8),
    "Alpha": (8, 12),
    "Beta":  (12, 30),
    "Gamma": (30, 45),
}
CHANNELS = ["TP9", "AF7", "AF8", "TP10"]
SFREQ = 256.0


def compute_band_power(data, sfreq):
    """Compute absolute and relative band power for each channel."""
    results = {}
    nperseg = int(sfreq * 2)
    for band, (lo, hi) in BANDS.items():
        results[band] = {"abs": np.zeros(data.shape[1]), "rel": np.zeros(data.shape[1])}

    freqs, psd = welch(data, sfreq, nperseg=nperseg, axis=0)

    total_power = np.zeros(data.shape[1])
    for band, (lo, hi) in BANDS.items():
        mask = (freqs >= lo) & (freqs <= hi)
        band_power = np.trapezoid(psd[mask], freqs[mask], axis=0)
        results[band]["abs"] = band_power
        total_power += band_power

    for band in BANDS:
        results[band]["rel"] = results[band]["abs"] / total_power

    return results


def compute_heart_rate(ppg_signal, sfreq=64.0):
    """Compute average heart rate from PPG IR signal."""
    if len(ppg_signal) < int(sfreq * 3):
        return None
    ppg = ppg_signal - np.mean(ppg_signal)
    nyq = sfreq / 2
    b, a = butter(2, [0.7/nyq, 4.0/nyq], btype="band")
    filtered = filtfilt(b, a, ppg)
    peaks, _ = find_peaks(filtered, distance=int(sfreq * 0.4),
                          height=0.1 * np.std(filtered))
    if len(peaks) >= 2:
        intervals = np.diff(peaks) / sfreq
        bpm = 60.0 / np.median(intervals)
        if 40 <= bpm <= 180:
            return bpm
    return None


def main():
    parser = argparse.ArgumentParser(description="EEG Recording Analysis Report")
    parser.add_argument("csv_file", help="Path to recorded CSV")
    args = parser.parse_args()

    csv_path = Path(args.csv_file)
    if not csv_path.exists():
        print(f"File not found: {csv_path}")
        sys.exit(1)

    # Read all data columns
    timestamps = []
    eeg_data = {ch: [] for ch in CHANNELS}
    ppg_cols = ["ambient", "IR", "Red"]
    ppg_data = {k: [] for k in ppg_cols}
    acc_data = {k: [] for k in ["X", "Y", "Z"]}
    gyro_data = {k: [] for k in ["X", "Y", "Z"]}
    hr_values = []
    has_ppg = has_acc = has_gyro = has_hr = False

    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        has_ppg = "PPG_IR" in fieldnames or "PPG_ambient" in fieldnames
        has_acc = "ACC_X" in fieldnames
        has_gyro = "GYRO_X" in fieldnames
        has_hr = "HR" in fieldnames
        for row in reader:
            timestamps.append(float(row["timestamp"]))
            for ch in CHANNELS:
                eeg_data[ch].append(float(row.get(ch, 0)))
            if has_ppg:
                for k in ppg_cols:
                    ppg_data[k].append(float(row.get(f"PPG_{k}", 0)))
            if has_acc:
                for k in ["X", "Y", "Z"]:
                    acc_data[k].append(float(row.get(f"ACC_{k}", 0)))
            if has_gyro:
                for k in ["X", "Y", "Z"]:
                    gyro_data[k].append(float(row.get(f"GYRO_{k}", 0)))
            if has_hr:
                hr_val = row.get("HR", "").strip()
                hr_values.append(float(hr_val) if hr_val else None)

    if not timestamps:
        print("Empty file")
        sys.exit(1)

    duration = timestamps[-1] - timestamps[0]
    n_samples = len(timestamps)
    eeg_array = np.array([eeg_data[ch] for ch in CHANNELS]).T
    ppg_array = np.array([ppg_data[k] for k in ppg_cols]).T if has_ppg else None
    acc_array = np.array([acc_data[k] for k in ["X", "Y", "Z"]]).T if has_acc else None
    gyro_array = np.array([gyro_data[k] for k in ["X", "Y", "Z"]]).T if has_gyro else None
    hr_clean = [h for h in hr_values if h is not None and 30 < h < 220]

    # Compute band power
    bp = compute_band_power(eeg_array, SFREQ)

    # Heart rate from PPG IR signal
    hr_ppg = compute_heart_rate(ppg_array[:, 1]) if has_ppg and ppg_array.shape[0] > 0 else None
    # Use HR column values if available, fall back to PPG-derived
    hr = np.mean(hr_clean) if hr_clean else hr_ppg

    # HRV from PPG (RMSSD, SDNN)
    hrv_rmssd = hrv_sdnn = None
    if has_ppg and ppg_array.shape[0] > int(64 * 3):
        from scipy.signal import find_peaks
        ppg_ir = ppg_array[:, 1] - np.mean(ppg_array[:, 1])
        nyq = 32
        b, a = butter(2, [0.7/nyq, 4.0/nyq], btype="band")
        ppg_filt = filtfilt(b, a, ppg_ir)
        peaks, _ = find_peaks(ppg_filt, distance=int(64 * 0.4), height=0.1*np.std(ppg_filt))
        if len(peaks) >= 3:
            ibi = np.diff(peaks) / 64 * 1000  # ms
            hrv_sdnn = np.std(ibi)
            ibi_diff = np.diff(ibi)
            hrv_rmssd = np.sqrt(np.mean(ibi_diff ** 2)) if len(ibi_diff) > 0 else None

    # Signal stats
    eeg_std = np.std(eeg_array, axis=0)
    eeg_range = np.ptp(eeg_array, axis=0)

    # ACC stats
    acc_magnitude = np.sqrt(np.sum(acc_array ** 2, axis=1)) if has_acc else None
    acc_mean_mag = np.mean(acc_magnitude) if acc_magnitude is not None else None
    acc_peak = np.max(acc_magnitude) if acc_magnitude is not None else None
    movement_pct = np.sum(acc_magnitude > 0.15) / len(acc_magnitude) * 100 if acc_magnitude is not None else None
    dominant_orient = None
    if has_acc and acc_array.shape[0] > 0:
        avg_acc = np.mean(acc_array, axis=0)
        dominant_orient = "X" if abs(avg_acc[0]) > abs(avg_acc[1]) and abs(avg_acc[0]) > abs(avg_acc[2]) else \
                         "Y" if abs(avg_acc[1]) > abs(avg_acc[2]) else "Z"

    # GYRO stats
    gyro_magnitude = np.sqrt(np.sum(gyro_array ** 2, axis=1)) if has_gyro else None
    gyro_mean = np.mean(np.abs(gyro_array), axis=0) if has_gyro else None
    rotation_pct = np.sum(gyro_magnitude > 10) / len(gyro_magnitude) * 100 if gyro_magnitude is not None else None

    # ==================================================================
    # Report
    # ==================================================================
    lines = []
    def p(s=""):
        lines.append(s)
        print(s)

    p()
    p("=" * 60)
    p("  脑电记录综合分析报告  EEG Recording Analysis Report")
    p("=" * 60)
    p()
    p(f"  文件 File:          {csv_path.name}")
    p(f"  时长 Duration:      {duration:.0f}s ({duration/60:.1f} min)")
    p(f"  样本数 Samples:     {n_samples}")

    # ---- 1. EEG 脑电分析 ----
    p()
    p("  ════ 1. 脑电 EEG ════")
    p("  ── 频段功率 Band Power (相对值 relative) ──")
    band_cn = {"Delta": "δ  Delta", "Theta": "θ  Theta", "Alpha": "α  Alpha",
               "Beta": "β  Beta", "Gamma": "γ  Gamma"}
    header = f"  {'频段 Band':14s}"
    for ch in CHANNELS:
        header += f" {ch:>8s}"
    p(header)
    p("  " + "-" * 48)
    for band in BANDS:
        line = f"  {band_cn[band]:14s}"
        for i, ch in enumerate(CHANNELS):
            line += f" {bp[band]['rel'][i]:8.4f}"
        p(line)
    p()

    alpha_avg = np.mean([bp["Alpha"]["rel"][i] for i in range(4)])
    theta_avg = np.mean([bp["Theta"]["rel"][i] for i in range(4)])
    beta_avg = np.mean([bp["Beta"]["rel"][i] for i in range(4)])
    delta_avg = np.mean([bp["Delta"]["rel"][i] for i in range(4)])
    gamma_avg = np.mean([bp["Gamma"]["rel"][i] for i in range(4)])
    theta_beta_ratio = theta_avg / beta_avg if beta_avg > 0 else 0

    p("  ── 关键指标 Key Indicators ──")
    p(f"  α  Alpha (放松 relaxation):     {alpha_avg:.4f}  {'▲ 偏高' if alpha_avg > 0.3 else '▼ 偏低'}")
    p(f"  θ  Theta (冥想 meditation):     {theta_avg:.4f}")
    p(f"  β  Beta  (活跃 active):         {beta_avg:.4f}")
    p(f"  δ  Delta (深度睡眠 deep sleep): {delta_avg:.4f}")
    p(f"  γ  Gamma (高级认知 high-level):  {gamma_avg:.4f}")
    p(f"  θ/β Theta/Beta (专注 focus):    {theta_beta_ratio:.4f}  " +
      ("▲ 偏放松" if theta_beta_ratio > 1.5 else "▼ 偏专注" if theta_beta_ratio < 0.8 else "─ 中性"))
    p()

    p("  ── 通道信号质量 Channel Quality ──")
    for i, ch in enumerate(CHANNELS):
        q_cn = "良好" if eeg_std[i] > 5 else "偏弱" if eeg_std[i] > 1 else "噪声"
        quality = "good" if eeg_std[i] > 5 else "low" if eeg_std[i] > 1 else "noise"
        p(f"  {ch:6s}  标准差 std={eeg_std[i]:7.2f} uV  峰峰 range={eeg_range[i]:7.2f} uV  [{q_cn}/{quality}]")
    p()

    # ---- 2. Heart Rate & HRV 心率 ----
    if hr or has_ppg:
        p("  ════ 2. 心率与心率变异性 Heart Rate & HRV ════")
        if hr:
            if hr < 60:
                hr_cn = "低静息心率 low resting (放松/睡眠)"
            elif hr < 80:
                hr_cn = "正常静息心率 normal resting"
            else:
                hr_cn = "偏高 elevated (活动/紧张)"
            p(f"  平均心率 Avg HR:    {hr:.0f} BPM  [{hr_cn}]")
        if hr_clean:
            p(f"  心率范围 HR range:   {min(hr_clean):.0f} - {max(hr_clean):.0f} BPM")
            if len(hr_clean) > 1:
                p(f"  心率标准差 HR std:   {np.std(hr_clean):.1f} BPM")
        if hrv_rmssd:
            p(f"  RMSSD (短期心率变异性 short-term HRV): {hrv_rmssd:.0f} ms")
        if hrv_sdnn:
            p(f"  SDNN (整体心率变异性 overall HRV):     {hrv_sdnn:.0f} ms")
            if hrv_sdnn > 50:
                hrv_cn = "高 high — 恢复良好/放松"
            elif hrv_sdnn > 20:
                hrv_cn = "正常 normal"
            else:
                hrv_cn = "低 low — 压力/疲劳"
            p(f"  ── {hrv_cn}")
        p()

    # ---- 3. Movement 运动 (ACC) ----
    if has_acc:
        p("  ════ 3. 头部运动 Accelerometer ════")
        p(f"  平均加速度 Avg magnitude:  {acc_mean_mag:.3f} g")
        p(f"  峰值运动 Peak movement:   {acc_peak:.3f} g")
        if movement_pct > 30:
            mov_cn = "▲ 活跃 active (较多头部运动)"
        elif movement_pct > 10:
            mov_cn = "─ 适中 moderate"
        else:
            mov_cn = "▼ 静止 still (信号质量好)"
        p(f"  运动时间占比 Movement:      {movement_pct:.1f}%  {mov_cn}")
        for axis in ["X", "Y", "Z"]:
            idx = ["X", "Y", "Z"].index(axis)
            cn = {"X": "左右", "Y": "前后", "Z": "上下"}[axis]
            p(f"    {axis} ({cn}):  均值 mean={np.mean(acc_array[:, idx]):+.4f}g  标准差 std={np.std(acc_array[:, idx]):.4f}g")
        p()

    # ---- 4. Rotation 旋转 (GYRO) ----
    if has_gyro:
        p("  ════ 4. 头部旋转 Gyroscope ════")
        if rotation_pct > 30:
            rot_cn = "▲ 活跃 active (频繁转头)"
        elif rotation_pct > 10:
            rot_cn = "─ 适中 moderate"
        else:
            rot_cn = "▼ 静止 still"
        p(f"  旋转时间占比 Rotation:      {rotation_pct:.1f}%  {rot_cn}")
        for axis in ["X", "Y", "Z"]:
            idx = ["X", "Y", "Z"].index(axis)
            cn = {"X": "转头 yaw", "Y": "侧倾 roll", "Z": "点头 pitch"}[axis]
            p(f"    {axis} ({cn}):  均值 mean={np.mean(gyro_array[:, idx]):+7.2f} deg/s  标准差 std={np.std(gyro_array[:, idx]):.2f} deg/s")
        p()

    # ---- 5. Overall Assessment 综合评估 ----
    p("  ════ 5. 综合评估 Assessment ════")
    if alpha_avg > 0.25:
        p("  脑电 EEG: 放松/静息状态 Relaxed/resting (α 波 Alpha 较高)")
    elif beta_avg > 0.3:
        p("  脑电 EEG: 活跃/专注 Active/focused (β 波 Beta 较高)")
    elif theta_beta_ratio > 1.5:
        p("  脑电 EEG: 冥想/昏沉 Meditative/drowsy (θ/β 比值 Theta/Beta 偏高)")
    else:
        p("  脑电 EEG: 中性混合状态 Neutral/mixed")

    if hr:
        if hr < 60:
            p(f"  心率 HR: 低静息 ({hr:.0f} BPM) — 平静/睡眠状态")
        elif hr < 75:
            p(f"  心率 HR: 正常静息 ({hr:.0f} BPM) — 放松状态")
        elif hr < 90:
            p(f"  心率 HR: 略偏高 ({hr:.0f} BPM) — 轻度活动")
        else:
            p(f"  心率 HR: 偏高 ({hr:.0f} BPM) — 活跃/紧张")

    if hrv_rmssd:
        if hrv_rmssd > 40:
            p(f"  HRV: 高 ({hrv_rmssd:.0f} ms RMSSD) — 恢复良好/放松")
        elif hrv_rmssd > 20:
            p(f"  HRV: 正常 ({hrv_rmssd:.0f} ms RMSSD)")
        else:
            p(f"  HRV: 偏低 ({hrv_rmssd:.0f} ms RMSSD) — 压力/疲劳")

    if movement_pct is not None:
        if movement_pct < 5:
            p(f"  运动 Motion: 极少 ({movement_pct:.1f}%) — 信号质量良好")
        elif movement_pct < 20:
            p(f"  运动 Motion: 轻微 ({movement_pct:.1f}%) — 可能有少量伪迹")
        else:
            p(f"  运动 Motion: 较多 ({movement_pct:.1f}%) — 脑电可能受运动伪迹干扰")

    p()
    p("=" * 60)

    # Write report file
    report_path = csv_path.with_suffix(".report.txt")
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport saved to: {report_path}")


if __name__ == "__main__":
    main()
