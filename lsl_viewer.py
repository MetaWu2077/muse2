#!/usr/bin/env python
"""
Muse S 实时 EEG 可视化工具.

订阅 muselsl 推送的 LSL 流，实时绘制：
  - 左面板: 4 通道 EEG 波形滚动显示（5 秒窗口）
  - 右面板: Band Power 柱状图（每秒更新）

Usage:
    python lsl_viewer.py                  # 自动查找 Muse LSL 流
    python lsl_viewer.py --simulate       # 无设备模拟模式（合成 EEG 数据）
    python lsl_viewer.py --window 10      # 10 秒波形窗口
    python lsl_viewer.py --refresh 50     # 动画刷新率 50 ms
    python lsl_viewer.py --stream Muse    # 指定 LSL 流名称

依赖: 真机模式需先运行 stream_muse.py 启动 LSL 流.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections import deque
from pathlib import Path

import csv
import os
from datetime import datetime

import numpy as np
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from matplotlib.patches import Rectangle
from matplotlib.widgets import Button, TextBox
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

from band_power import (
    compute_band_power,
    BANDS,
    CHANNEL_NAMES,
)

CONFIG_PATH = Path(__file__).parent / ".muse_viewer_config.json"
HIGHLIGHT_COLOR = "#ffffff"
DEFAULT_YLIM = [-150, 150]
Y_STEP = 20   # µV per up/down press
T_STEP = 0.5  # seconds per left/right press

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
EEG_SFREQ = 256.0                # Muse S EEG sampling rate
N_EEG_CHANNELS = 5               # TP9, AF7, AF8, TP10, Right AUX
PLOT_CHANNELS = 4                # Display only the 4 main channels
LSL_SCAN_TIMEOUT = 5.0           # Seconds to wait for LSL stream discovery
DEFAULT_WINDOW = 5.0             # Waveform window in seconds
DEFAULT_REFRESH = 50             # Animation interval (ms)
BANDPOWER_INTERVAL = 2.0         # Band power recompute interval (seconds)
BP_WINDOW_SEC = 4.0              # How many seconds of data for band power
BP_SMOOTH = 0.3                  # EMA smoothing factor (0=no update, 1=no smooth)

CHANNEL_COLORS = ["#4ecdc4", "#ff6b6b", "#ffe66d", "#a37eba"]
PPG_SFREQ = 64.0
PPG_CHANNELS = 3       # ambient, IR, red (from osc_lsl_bridge)
PPG_BUFFER_SEC = 6     # how many seconds of PPG to buffer
HR_MIN_BPM = 40
HR_MAX_BPM = 180
IMU_SFREQ = 52.0
IMU_CHANNELS = 3       # X, Y, Z
IMU_BUFFER_SEC = 4     # how many seconds of IMU to buffer


# ---------------------------------------------------------------------------
# Ring Buffer (thread-safe)
# ---------------------------------------------------------------------------
class RingBuffer:
    """Thread-safe ring buffer for EEG data."""

    def __init__(self, capacity: int, n_channels: int):
        self._lock = threading.Lock()
        self._data: deque[np.ndarray] = deque(maxlen=capacity)
        self._timestamps: deque[float] = deque(maxlen=capacity)
        self._n_channels = n_channels

    def extend(self, samples: np.ndarray, timestamps: list[float]):
        """Append multiple samples. samples shape: (n_samples, n_channels)."""
        with self._lock:
            for i in range(samples.shape[0]):
                self._data.append(samples[i, :].copy())
                self._timestamps.append(timestamps[i])

    def get_window(self, n: int | None = None) -> np.ndarray:
        """Get the last n samples as (n, n_channels) array."""
        with self._lock:
            if not self._data:
                return np.zeros((0, self._n_channels))
            items = list(self._data)[-n:] if n else list(self._data)
            if not items:
                return np.zeros((0, self._n_channels))
            return np.stack(items, axis=0)

    def get_timestamps(self, n: int | None = None) -> np.ndarray:
        """Get the last n timestamps."""
        with self._lock:
            items = list(self._timestamps)[-n:] if n else list(self._timestamps)
            return np.array(items)

    @property
    def n_samples(self) -> int:
        with self._lock:
            return len(self._data)


# ---------------------------------------------------------------------------
# LSL Data Subscriber (Real Muse)
# ---------------------------------------------------------------------------
class LSLSubscriber:
    """Background thread that pulls data from an LSL stream into a buffer."""

    def __init__(self, buffer: RingBuffer, stream_name: str = "Muse",
                 stream_type: str = "EEG"):
        self.buffer = buffer
        self.stream_name = stream_name
        self.stream_type = stream_type
        self._inlet = None
        self._thread = None
        self._running = threading.Event()
        self.sfreq: float = EEG_SFREQ if stream_type == "EEG" else PPG_SFREQ
        self.n_channels: int = N_EEG_CHANNELS if stream_type == "EEG" else PPG_CHANNELS

    def connect(self, timeout: float = LSL_SCAN_TIMEOUT) -> bool:
        """Resolve and connect to the LSL stream."""
        from pylsl import resolve_byprop, StreamInlet, resolve_streams

        print(f"Looking for LSL stream: name='{self.stream_name}', type='{self.stream_type}'...")

        try:
            streams = resolve_byprop("type", self.stream_type, timeout=timeout)
        except Exception:
            streams = []

        if not streams:
            try:
                streams = resolve_streams("type", self.stream_type, timeout=timeout)
            except Exception:
                streams = []

        if not streams:
            print("No Muse EEG LSL stream found.")
            print("Make sure stream_muse.py is running in another terminal.")
            return False

        stream_info = streams[0]
        self._inlet = StreamInlet(stream_info, max_buflen=360)

        info = self._inlet.info()
        self.sfreq = info.nominal_srate()
        self.n_channels = info.channel_count()
        print(f"Connected to LSL stream:")
        print(f"  Name:       {info.name()}")
        print(f"  Type:       {info.type()}")
        print(f"  Channels:   {self.n_channels}")
        print(f"  Sample rate: {self.sfreq} Hz")
        print(f"  Source:     {info.source_id()}")
        return True

    def start(self):
        """Start the background data pull thread."""
        if self._inlet is None:
            raise RuntimeError("Not connected. Call connect() first.")
        self._running.set()
        self._thread = threading.Thread(target=self._pull_loop, daemon=True)
        self._thread.start()
        print("Data acquisition started (LSL).")

    def stop(self):
        """Stop the background thread."""
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        print("Data acquisition stopped.")

    def _pull_loop(self):
        """Continuously pull data chunks from LSL."""
        # LostError removed in pylsl 1.17+; handle generically
        while self._running.is_set():
            try:
                samples, timestamps = self._inlet.pull_chunk(
                    timeout=0.1, max_samples=256
                )
                if samples and len(samples) > 0:
                    arr = np.array(samples, dtype=np.float32)
                    self.buffer.extend(arr, timestamps)
            except RuntimeError as exc:
                if "lost" in str(exc).lower() or "timeout" in str(exc).lower():
                    print("LSL stream lost. Exiting.")
                else:
                    print(f"LSL runtime error: {exc}")
                self._running.clear()
                break
            except Exception as exc:
                print(f"LSL pull error: {exc}")
                time.sleep(0.5)


# ---------------------------------------------------------------------------
# Synthetic Data Generator (Simulate mode)
# ---------------------------------------------------------------------------
class SyntheticGenerator:
    """Generates synthetic EEG-like data for testing without hardware."""

    def __init__(self, buffer: RingBuffer, sfreq: float = EEG_SFREQ):
        self.buffer = buffer
        self.sfreq = sfreq
        self.n_channels = N_EEG_CHANNELS
        self._thread = None
        self._running = threading.Event()
        self._t0 = time.time()

    def start(self):
        """Start the synthetic data thread."""
        self._running.set()
        self._t0 = time.time()
        self._thread = threading.Thread(target=self._generate_loop, daemon=True)
        self._thread.start()
        print("Synthetic EEG generator started (simulate mode).")

    def stop(self):
        """Stop the synthetic data thread."""
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _generate_loop(self):
        rng = np.random.default_rng()
        chunk_size = 32  # samples per chunk (~8 Hz chunk rate)

        while self._running.is_set():
            t = time.time() - self._t0
            n = chunk_size
            dt = 1.0 / self.sfreq
            t_vec = t + np.arange(n) * dt

            samples = np.zeros((n, N_EEG_CHANNELS), dtype=np.float32)

            for ch in range(PLOT_CHANNELS):
                # Base noise
                noise = rng.standard_normal(n) * 8.0

                # Alpha oscillation (~10 Hz, strongest in posterior channels)
                alpha_amp = 30.0 + ch * 5.0  # vary per channel
                alpha = alpha_amp * np.sin(2 * np.pi * 10.0 * t_vec + ch * 0.3)

                # Beta (~20 Hz, weaker)
                beta = 8.0 * np.sin(2 * np.pi * 20.0 * t_vec + ch * 0.7)

                # Theta (~6 Hz)
                theta = 10.0 * np.sin(2 * np.pi * 6.0 * t_vec + ch * 0.2)

                # Delta (~2 Hz, slow drift)
                delta = 15.0 * np.sin(2 * np.pi * 2.0 * t_vec)

                # 50 Hz line noise (small)
                line_noise = 2.0 * np.sin(2 * np.pi * 50.0 * t_vec)

                # Occasional eye-blink artifact (every ~4 sec)
                blink_mask = (np.sin(2 * np.pi * 0.25 * t_vec) > 0.95).astype(float)
                blink = blink_mask * rng.standard_normal(n) * 80.0 * (1 if ch == 0 else 0.3)

                samples[:, ch] = (
                    noise + alpha + beta + theta + delta + line_noise + blink
                ).astype(np.float32)

            # Channel 5 (Right AUX) — mostly noise
            samples[:, 4] = (rng.standard_normal(n) * 10.0).astype(np.float32)

            timestamps = list(t_vec)
            self.buffer.extend(samples, timestamps)

            time.sleep(chunk_size / self.sfreq)


# ---------------------------------------------------------------------------
# Real-time Viewer
# ---------------------------------------------------------------------------
class MuseViewer:
    """Matplotlib-based real-time EEG viewer with band power display."""

    def __init__(
        self,
        buffer: RingBuffer,
        sfreq: float,
        ppg_buffer: RingBuffer | None = None,
        acc_buffer: RingBuffer | None = None,
        gyro_buffer: RingBuffer | None = None,
        window_sec: float = DEFAULT_WINDOW,
        refresh_ms: int = DEFAULT_REFRESH,
        simulate: bool = False,
    ):
        self.buffer = buffer
        self.sfreq = sfreq
        self.ppg_buffer = ppg_buffer
        self.acc_buffer = acc_buffer
        self.gyro_buffer = gyro_buffer
        self._battery_pct: float | None = None
        self.window_sec = window_sec
        self.window_samples = int(sfreq * window_sec)
        self.refresh_ms = refresh_ms
        self.simulate = simulate

        # Recording state
        self._recording = False
        self._rec_file = None
        self._rec_writer = None
        self._rec_path: Path | None = None
        self._rec_samples = 0
        self._rec_start_time = 0.0
        self._rec_description = ""
        self._rec_last_ts = 0.0  # Last written timestamp to avoid duplicates

        # Heart rate state
        self._hr_value: int | None = None
        self._hr_last_calc = 0.0

        # Band power state
        self._last_bp_time = 0.0
        self._latest_bp = None
        self._bp_smoothed = None  # EMA-smoothed bar heights
        self._bp_lock = threading.Lock()

        # Per-channel y-limits (loaded from config)
        self._ylim: list[list[float]] = [
            DEFAULT_YLIM.copy() for _ in range(PLOT_CHANNELS)
        ]
        self._ppg_ylim = [-3.0, 3.0]
        self._selected_ch: int | None = None  # >=0 = EEG ch, -1 = PPG

        # Load saved config (overrides window_sec and _ylim)
        self._load_config()
        self.window_samples = int(sfreq * self.window_sec)

        self._build_figure()
        self._init_bars()
        self._setup_events()

    def _build_figure(self):
        """Build the complete figure layout."""
        self.fig = plt.figure(
            "Muse S - Real-time EEG + Band Power"
            if not self.simulate
            else "Muse S - SIMULATE MODE",
            figsize=(14, 8),
        )
        self.fig.patch.set_facecolor("#0f0f11")

        # Left panel: stacked EEG channels
        gs_left = self.fig.add_gridspec(
            PLOT_CHANNELS, 1,
            left=0.06, right=0.56, top=0.93, bottom=0.26,
            hspace=0.12,
        )

        self.ax_eeg = []
        self.eeg_lines = []

        for ch in range(PLOT_CHANNELS):
            ax = self.fig.add_subplot(gs_left[ch])
            ax.set_facecolor("#1a1a2e")
            ax.tick_params(colors="#888888", labelsize=7)
            ax.set_ylabel(
                f"{CHANNEL_NAMES[ch]}\n(uV)", color=CHANNEL_COLORS[ch],
                fontsize=8, rotation=0, labelpad=25, va="center",
            )
            ax.yaxis.set_label_position("right")
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            ax.spines["left"].set_color("#333333")
            ax.spines["bottom"].set_color("#333333")
            ax.set_xlim(-self.window_sec, 0)
            ax.set_ylim(*self._ylim[ch])

            if ch < PLOT_CHANNELS - 1:
                ax.set_xticklabels([])
            else:
                ax.set_xlabel("Time (s)", color="#888888", fontsize=8)

            (line,) = ax.plot(
                [], [], color=CHANNEL_COLORS[ch], linewidth=0.8,
            )
            # zero-reference line
            ax.axhline(0, color=CHANNEL_COLORS[ch], linewidth=0.5,
                       alpha=0.12, linestyle="--")

            self.ax_eeg.append(ax)
            self.eeg_lines.append(line)

        # 3D Head model (right panel, top)
        self.ax_head = self.fig.add_axes([0.64, 0.55, 0.30, 0.40], projection='3d')
        self.ax_head.set_proj_type('ortho')
        self.ax_head.set_facecolor("#0f0f11")
        self.ax_head.set_xlim(-1.2, 1.2); self.ax_head.set_ylim(-1.2, 1.2); self.ax_head.set_zlim(-1.2, 1.2)
        self.ax_head.set_xticklabels([]); self.ax_head.set_yticklabels([]); self.ax_head.set_zticklabels([])
        self.ax_head.grid(False)
        self.ax_head.xaxis.pane.set_visible(False)
        self.ax_head.yaxis.pane.set_visible(False)
        self.ax_head.zaxis.pane.set_visible(False)
        self.ax_head.view_init(elev=0, azim=-90)
        self._build_head_model()

        # Right panel: Band Power bar chart
        ax_bp = self.fig.add_axes([0.62, 0.08, 0.36, 0.46])
        ax_bp.set_facecolor("#1a1a2e")
        ax_bp.tick_params(colors="#888888", labelsize=8)
        ax_bp.set_ylabel("Relative Power", color="#888888", fontsize=9)
        ax_bp.set_ylim(0, 1.05)
        ax_bp.set_xlim(-0.8, len(BANDS) * PLOT_CHANNELS - 0.2)
        ax_bp.spines["top"].set_visible(False)
        ax_bp.spines["right"].set_visible(False)
        ax_bp.spines["left"].set_color("#333333")
        ax_bp.spines["bottom"].set_color("#333333")

        # Band name tick labels centered under each group of 4 bars
        tick_positions = [
            bi * PLOT_CHANNELS + (PLOT_CHANNELS - 1) / 2
            for bi in range(len(BANDS))
        ]
        ax_bp.set_xticks(tick_positions)
        ax_bp.set_xticklabels(
            [b.capitalize() for b in BANDS], fontsize=8,
        )

        # Legend for channels
        from matplotlib.patches import Patch
        ax_bp.legend(
            handles=[
                Patch(facecolor=CHANNEL_COLORS[i], label=CHANNEL_NAMES[i])
                for i in range(PLOT_CHANNELS)
            ],
            loc="upper right", fontsize=7, framealpha=0.5,
            facecolor="#1a1a2e", edgecolor="#333333", labelcolor="#cccccc",
        )

        self.ax_bp = ax_bp

        # PPG subplot
        gs_ppg = self.fig.add_gridspec(
            1, 1, left=0.06, right=0.56, top=0.25, bottom=0.18, hspace=0,
        )
        self.ax_ppg = self.fig.add_subplot(gs_ppg[0])
        self.ax_ppg.set_facecolor("#1a1a2e")
        self.ax_ppg.tick_params(colors="#888888", labelsize=6)
        self.ax_ppg.set_ylabel("PPG", color="#ff6b6b", fontsize=7)
        self.ax_ppg.yaxis.set_label_position("right")
        for s in self.ax_ppg.spines.values(): s.set_color("#333333")
        self.ax_ppg.set_xlim(-PPG_BUFFER_SEC, 0)
        self.ax_ppg.set_ylim(*self._ppg_ylim)
        self.ax_ppg.set_xticklabels([])
        (self.ppg_line,) = self.ax_ppg.plot([], [], color="#ff6b6b", linewidth=0.5)
        self.ax_ppg.axhline(0, color="#ff6b6b", linewidth=0.3, alpha=0.1, linestyle="--")

        # ACC subplot
        gs_acc = self.fig.add_gridspec(
            1, 1, left=0.06, right=0.56, top=0.17, bottom=0.10, hspace=0,
        )
        self.ax_acc = self.fig.add_subplot(gs_acc[0])
        self.ax_acc.set_facecolor("#1a1a2e")
        self.ax_acc.tick_params(colors="#888888", labelsize=5)
        self.ax_acc.set_ylabel("ACC g", color="#4ecdc4", fontsize=6)
        self.ax_acc.yaxis.set_label_position("right")
        for s in self.ax_acc.spines.values(): s.set_color("#333333")
        self.ax_acc.set_xlim(-IMU_BUFFER_SEC, 0)
        self.ax_acc.set_ylim(-0.15, 0.15)
        self.ax_acc.set_xticklabels([])
        self.acc_lines = [
            self.ax_acc.plot([], [], color=c, linewidth=0.4)[0]
            for c in ["#4ecdc4", "#ffe66d", "#a37eba"]
        ]

        # GYRO subplot
        gs_gyro = self.fig.add_gridspec(
            1, 1, left=0.06, right=0.56, top=0.09, bottom=0.005, hspace=0,
        )
        self.ax_gyro = self.fig.add_subplot(gs_gyro[0])
        self.ax_gyro.set_facecolor("#1a1a2e")
        self.ax_gyro.tick_params(colors="#888888", labelsize=5)
        self.ax_gyro.set_ylabel("GYR /s", color="#ff7f0e", fontsize=6)
        self.ax_gyro.yaxis.set_label_position("right")
        ax_labels = ["X", "Y", "Z"]
        for s in self.ax_gyro.spines.values(): s.set_color("#333333")
        self.ax_gyro.set_xlim(-IMU_BUFFER_SEC, 0)
        self.ax_gyro.set_ylim(-5, 5)
        self.ax_gyro.set_xlabel("Time (s)", color="#888888", fontsize=6)
        self.gyro_lines = [
            self.ax_gyro.plot([], [], color=c, linewidth=0.4)[0]
            for c in ["#ff6b6b", "#ffe66d", "#a37eba"]
        ]

        # Heart rate (red)
        self.hr_text = self.fig.text(
            0.95, 0.75, "HR: --", ha="right", va="center",
            color="#ff6b6b", fontsize=26, fontweight="bold", fontfamily="monospace",
        )
        # Battery below HR
        self.batt_text = self.fig.text(
            0.95, 0.68, "", ha="right", va="center",
            color="#aaaaaa", fontsize=10, fontfamily="monospace",
        )
        # HRV value (green)
        self.hrv_text = self.fig.text(
            0.95, 0.58, "", ha="right", va="center",
            color="#2ecc71", fontsize=26, fontweight="bold", fontfamily="monospace",
        )
        # HRV detail
        self.hrv_detail = self.fig.text(
            0.95, 0.50, "", ha="right", va="top",
            color="#2ecc71", fontsize=11, fontfamily="monospace",
        )

        # Status text
        self.status_text = self.fig.text(
            0.5, 0.965, "", ha="center", va="top",
            color="#888888", fontsize=9, fontfamily="monospace",
        )

        # Record button (bottom-right, below band power chart)
        self.btn_ax = self.fig.add_axes([0.80, 0.035, 0.07, 0.035])
        self.btn_record = Button(self.btn_ax, "Record", color="#333333", hovercolor="#555555")
        self.btn_record.label.set_color("#ffffff")
        self.btn_record.label.set_fontsize(9)
        self.btn_record.on_clicked(self._toggle_record)

        # Description text input (next to button)
        self.desc_ax = self.fig.add_axes([0.62, 0.035, 0.16, 0.035])
        self.desc_input = TextBox(self.desc_ax, "", color="#1a1a2e", hovercolor="#2a2a3e",
                                  textalignment="left")
        self.desc_input.label.set_color("#888888")
        self.desc_input.label.set_fontsize(8)
        self.desc_input.set_val("state label...")

        # Recording indicator
        self.rec_indicator = self.fig.text(
            0.88, 0.045, "", ha="left", va="center",
            color="#ff4444", fontsize=16, fontweight="bold", fontfamily="monospace",
        )

    def _init_bars(self):
        """Create persistent bar rectangles (updated in-place for blit)."""
        self.bp_bars = []  # list[list[Rectangle]]: [band_idx][channel_idx]
        bar_width = 0.7

        for bi in range(len(BANDS)):
            ch_bars = []
            for ch in range(PLOT_CHANNELS):
                x = bi * PLOT_CHANNELS + ch
                rect = Rectangle(
                    (x - bar_width / 2, 0), bar_width, 0,
                    color=CHANNEL_COLORS[ch], alpha=0.85,
                )
                self.ax_bp.add_patch(rect)
                ch_bars.append(rect)
            self.bp_bars.append(ch_bars)

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------
    def _toggle_record(self, event=None):
        if self._recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self):
        desc = (self.desc_input.text or "").strip()
        if not desc:
            desc = "recording"
        # Sanitize filename
        safe_desc = "".join(c if c.isalnum() or c in "._- " else "_" for c in desc)
        safe_desc = safe_desc.replace(" ", "_")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path(__file__).parent / "recordings"
        out_dir.mkdir(exist_ok=True)
        self._rec_path = out_dir / f"{ts}_{safe_desc}.csv"
        self._rec_file = open(self._rec_path, "w", newline="")
        self._rec_writer = csv.writer(self._rec_file)
        # Header: timestamp, EEG channels, PPG, ACC, GYRO, HR
        header = ["timestamp", "TP9", "AF7", "AF8", "TP10",
                   "PPG_ambient", "PPG_IR", "PPG_Red",
                   "ACC_X", "ACC_Y", "ACC_Z",
                   "GYRO_X", "GYRO_Y", "GYRO_Z", "HR"]
        self._rec_writer.writerow(header)
        self._recording = True
        self._rec_samples = 0
        self._rec_start_time = time.time()
        self._rec_last_ts = 0.0
        self.btn_record.label.set_text("Stop")
        self.btn_record.color = "#662222"
        self.btn_record.hovercolor = "#883333"
        self.rec_indicator.set_text("● REC")
        print(f"[Recording] Started: {self._rec_path}")

    def _stop_recording(self):
        self._recording = False
        if self._rec_file:
            self._rec_file.close()
            self._rec_file = None
            self._rec_writer = None
        elapsed = time.time() - self._rec_start_time
        self.btn_record.label.set_text("Record")
        self.btn_record.color = "#333333"
        self.btn_record.hovercolor = "#555555"
        self.rec_indicator.set_text("")
        print(f"[Recording] Saved: {self._rec_path} "
              f"({self._rec_samples} samples, {elapsed:.0f}s)")

    def _write_recording_row(self, timestamp, eeg, ppg, acc, gyro, hr):
        if not self._recording or self._rec_writer is None:
            return
        row = [f"{timestamp:.6f}"]
        row += [f"{v:.6f}" for v in (eeg or [0]*4)[:4]]
        row += [f"{v:.6f}" for v in (ppg or [0]*3)[:3]]
        row += [f"{v:.6f}" for v in (acc or [0]*3)[:3]]
        row += [f"{v:.6f}" for v in (gyro or [0]*3)[:3]]
        row.append(str(hr) if hr is not None else "")
        self._rec_writer.writerow(row)
        self._rec_samples += 1

    # ------------------------------------------------------------------
    # Config persistence
    # ------------------------------------------------------------------
    def _load_config(self):
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
            self.window_sec = float(cfg.get("window_sec", self.window_sec))
            for k, v in cfg.get("ylim", {}).items():
                idx = int(k)
                if 0 <= idx < PLOT_CHANNELS:
                    self._ylim[idx] = [float(v[0]), float(v[1])]
            ppg = cfg.get("ppg_ylim")
            if ppg:
                self._ppg_ylim = [float(ppg[0]), float(ppg[1])]
            print(f"Loaded config: {CONFIG_PATH}")
        except Exception:
            pass

    def _save_config(self):
        cfg = {
            "window_sec": self.window_sec,
            "ylim": {str(i): self._ylim[i] for i in range(PLOT_CHANNELS)},
            "ppg_ylim": self._ppg_ylim,
        }
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
        print(f"Saved config: window={self.window_sec:.1f}s ylim={self._ylim} ppg={self._ppg_ylim}")

    # ------------------------------------------------------------------
    # Interactive events (click to select, arrows to adjust)
    # ------------------------------------------------------------------
    def _setup_events(self):
        self.fig.canvas.mpl_connect("button_press_event", self._on_click)
        self.fig.canvas.mpl_connect("key_press_event", self._on_key)

    def _on_click(self, event):
        if event.inaxes is None:
            return
        # Check EEG channels
        for ch, ax in enumerate(self.ax_eeg):
            if event.inaxes == ax:
                self._select(ch)
                return
        # Check PPG
        if event.inaxes == self.ax_ppg:
            self._select(-1)
            return
        self._select(None)

    def _on_key(self, event):
        if event.key == "up":
            self._adjust_ylim(+Y_STEP)
        elif event.key == "down":
            self._adjust_ylim(-Y_STEP)
        elif event.key == "left":
            self._adjust_window(-T_STEP)
        elif event.key == "right":
            self._adjust_window(+T_STEP)

    def _select(self, ch: int | None):
        """Highlight the selected channel's subplot. ch=-1 = PPG."""
        self._selected_ch = ch
        # EEG channels
        for i, ax in enumerate(self.ax_eeg):
            if i == ch:
                for spine in ax.spines.values():
                    spine.set_color(HIGHLIGHT_COLOR); spine.set_linewidth(1.5)
            else:
                for spine in ax.spines.values():
                    spine.set_color("#333333"); spine.set_linewidth(1.0)
        # PPG
        if ch == -1:
            for spine in self.ax_ppg.spines.values():
                spine.set_color(HIGHLIGHT_COLOR); spine.set_linewidth(1.5)
        else:
            for spine in self.ax_ppg.spines.values():
                spine.set_color("#333333"); spine.set_linewidth(1.0)

    def _adjust_ylim(self, delta: float):
        """Adjust y-range of the selected channel. Positive = expand."""
        if self._selected_ch is None:
            print("Click a channel first to select it.")
            return
        idx = self._selected_ch
        if idx == -1:
            # PPG
            lo, hi = self._ppg_ylim
            span = (hi - lo) / 2
            span = max(0.5, span + delta * 0.1)  # finer step for PPG
            self._ppg_ylim = [-span, span]
            self.ax_ppg.set_ylim(-span, span)
        else:
            lo, hi = self._ylim[idx]
            span = (hi - lo) / 2
            span = max(5, span + delta)
            self._ylim[idx] = [-span, span]
            self.ax_eeg[idx].set_ylim(-span, span)
        self._save_config()

    def _adjust_window(self, delta: float):
        """Adjust time window for all channels. Right = longer."""
        self.window_sec = max(1.0, min(30.0, self.window_sec + delta))
        self.window_samples = int(self.sfreq * self.window_sec)
        for ax in self.ax_eeg:
            ax.set_xlim(-self.window_sec, 0)
        self._save_config()

    # ------------------------------------------------------------------
    # 3D Head Model
    # ------------------------------------------------------------------
    def _build_head_model(self):
        """Load free_head.obj head mesh + set up orientation tracking."""
        obj_path = Path(__file__).parent / "free_head.obj"
        if not obj_path.exists():
            self._head_verts = np.array([[0, 0, 0]])
            self._head_faces = []
            return

        # Parse OBJ file
        vertices = []
        faces = []
        with open(obj_path, "r") as f:
            for line in f:
                if line.startswith("v "):
                    parts = line.strip().split()
                    vertices.append([float(parts[1]), float(parts[2]), float(parts[3])])
                elif line.startswith("f "):
                    parts = line.strip().split()[1:]
                    faces.append([int(p.split("/")[0]) - 1 for p in parts])

        verts = np.array(vertices, dtype=np.float32)
        print(f"Loaded free_head.obj: {len(verts)} vertices, {len(faces)} faces")

        # Center and scale
        center = (verts.min(axis=0) + verts.max(axis=0)) / 2
        verts = verts - center
        scale = np.max(np.abs(verts)) * 1.05
        verts = verts / scale

        # Reorient: top of head (+Y) → +Z, nose (+Z) → -Y (towards viewer)
        x = verts[:, 0].copy()
        y = verts[:, 1].copy()
        z = verts[:, 2].copy()
        verts[:, 0] = x    # X stays
        verts[:, 1] = -z   # nose (+Z) → -Y (towards viewer)
        verts[:, 2] = y    # top (+Y) → +Z (up)

        self._head_verts = verts
        self._head_faces = faces

        self.head_mesh = Poly3DCollection(
            [self._head_verts[f] for f in faces],
            facecolor="#e8c9a0", alpha=0.85, edgecolor="#c4956b", linewidth=0.08,
        )
        self.ax_head.add_collection3d(self.head_mesh)

        # Orientation state
        self._pitch = 0.0; self._roll = 0.0; self._yaw = 0.0
        self._last_gyro_ts = 0.0

    def _rotate_verts(self, verts, pitch, roll, yaw):
        """Apply pitch(X), roll(Y), yaw(Z) rotation to vertices."""
        cp, sp = np.cos(pitch), np.sin(pitch)
        cr, sr = np.cos(roll), np.sin(roll)
        cy, sy = np.cos(yaw), np.sin(yaw)
        # Roll around Y
        x, y, z = verts[:, 0], verts[:, 1], verts[:, 2]
        x1, y1, z1 = x*cr + z*sr, y, -x*sr + z*cr
        # Pitch around X
        x2, y2, z2 = x1, y1*cp - z1*sp, y1*sp + z1*cp
        # Yaw around Z
        x3, y3, z3 = x2*cy - y2*sy, x2*sy + y2*cy, z2
        return np.stack([x3, y3, z3], axis=1)

    def _update_head_orientation(self):
        """Update 3D head rotation from ACC + GYRO."""
        if self.acc_buffer is None or self.acc_buffer.n_samples < 10:
            return

        acc = self.acc_buffer.get_window(int(IMU_SFREQ * 0.5))
        if acc.shape[0] < 3: return
        am = np.mean(acc[-int(IMU_SFREQ*0.3):, :], axis=0)
        self._pitch = np.arctan2(-am[0], np.sqrt(am[1]*am[1] + am[2]*am[2]))
        self._roll = np.arctan2(am[1], am[2])

        if self.gyro_buffer is not None and self.gyro_buffer.n_samples > 3:
            ts = self.gyro_buffer.get_timestamps(int(IMU_SFREQ * 0.5))
            gd = self.gyro_buffer.get_window(int(IMU_SFREQ * 0.5))
            if gd.shape[0] > 2 and len(ts) > 1:
                gz = np.mean(gd[-int(IMU_SFREQ*0.2):, 2])
                if self._last_gyro_ts > 0 and ts[-1] - self._last_gyro_ts < 1.0:
                    self._yaw += gz * (ts[-1] - self._last_gyro_ts) * np.pi / 180
                self._last_gyro_ts = ts[-1]

        # Rotate head mesh
        rv = self._rotate_verts(self._head_verts, self._pitch, self._roll, self._yaw)
        self.head_mesh.set_verts([rv[f] for f in self._head_faces])

    # ------------------------------------------------------------------
    # Animation
    # ------------------------------------------------------------------
    _frame_count = 0
    _print_count = 0

    def _update(self, frame: int):
        """Animation callback — redraw EEG lines and band power bars."""
        artists: list = []
        self._frame_count += 1

        # 1. Update EEG waveforms
        data = self.buffer.get_window(self.window_samples)
        times = self.buffer.get_timestamps(self.window_samples)
        n = self.buffer.n_samples

        if self._frame_count % 20 == 0:
            self._print_count += 1
            print(f"[frame {self._frame_count}] buffer={n} data.shape={data.shape}", end="\r")

        if data.shape[0] > 0 and times.size > 0:
            t_rel = times - times[-1]  # relative time (0 = now)

            for ch in range(PLOT_CHANNELS):
                if ch < data.shape[1]:
                    line = self.eeg_lines[ch]
                    line.set_data(t_rel, data[:, ch])
                    artists.append(line)
        else:
            for line in self.eeg_lines:
                line.set_data([], [])
                artists.append(line)

        # 2. Compute band power (throttled, longer window, EMA smoothed)
        now = time.time()
        if (
            now - self._last_bp_time >= BANDPOWER_INTERVAL
            and data.shape[0] >= int(self.sfreq * 0.5)
        ):
            self._last_bp_time = now
            bp_window = int(self.sfreq * BP_WINDOW_SEC)
            eeg_data = data[-bp_window:, :PLOT_CHANNELS]
            if eeg_data.shape[0] >= int(self.sfreq * 0.5):
                try:
                    bp = compute_band_power(
                        eeg_data, self.sfreq,
                        nperseg=int(self.sfreq * 2)  # 2s Welch segments
                    )
                    # EMA smoothing on relative power
                    if self._bp_smoothed is None:
                        self._bp_smoothed = bp
                    else:
                        for band in BANDS:
                            for key in ("abs", "rel"):
                                self._bp_smoothed[band][key] = (
                                    BP_SMOOTH * bp[band][key]
                                    + (1 - BP_SMOOTH) * self._bp_smoothed[band][key]
                                )
                    with self._bp_lock:
                        self._latest_bp = self._bp_smoothed
                except Exception:
                    pass

        # 2.5. Update PPG waveform & heart rate
        if self.ppg_buffer is not None:
            ppg_data = self.ppg_buffer.get_window(int(PPG_SFREQ * PPG_BUFFER_SEC))
            if ppg_data.shape[0] > 10 and ppg_data.shape[1] >= 2:
                # Use IR channel (index 1) for heart rate
                ppg_ir = ppg_data[:, 1]
                ppg_centered = ppg_ir - np.mean(ppg_ir)

                # Compute heart rate every 2 seconds
                now = time.time()
                if now - self._hr_last_calc >= 2.0 and len(ppg_centered) >= int(PPG_SFREQ * 3):
                    self._hr_last_calc = now
                    try:
                        from scipy.signal import find_peaks, butter, filtfilt
                        # Bandpass 0.7–4 Hz (42–240 BPM range)
                        nyq = PPG_SFREQ / 2
                        b, a = butter(2, [0.7/nyq, 4.0/nyq], btype="band")
                        filtered = filtfilt(b, a, ppg_centered)
                        peaks, _ = find_peaks(filtered, distance=int(PPG_SFREQ * 0.4), height=0.1*np.std(filtered))
                        if len(peaks) >= 2:
                            intervals = np.diff(peaks) / PPG_SFREQ  # seconds
                            bpm = 60.0 / np.median(intervals)
                            if HR_MIN_BPM <= bpm <= HR_MAX_BPM:
                                # EMA smooth
                                if self._hr_value is None:
                                    self._hr_value = int(bpm)
                                else:
                                    self._hr_value = int(self._hr_value * 0.6 + bpm * 0.4)
                    except Exception:
                        pass

                # PPG waveform (relative time, centered)
                ppg_times = self.ppg_buffer.get_timestamps(
                    int(PPG_SFREQ * PPG_BUFFER_SEC)
                )
                if ppg_times.size > 0 and ppg_data.shape[0] > 0:
                    ppg_t_rel = ppg_times[-min(len(ppg_data), len(ppg_times)):] - ppg_times[-1]
                    self.ppg_line.set_data(
                        ppg_t_rel[-ppg_data.shape[0]:],
                        ppg_centered[-len(ppg_t_rel):] / max(np.std(ppg_centered), 0.01)
                    )
                artists.append(self.ppg_line)

            # Heart rate display
            hr_display = f"HR: {self._hr_value}" if self._hr_value is not None else "HR: --"
            self.hr_text.set_text(hr_display)
            artists.append(self.hr_text)

            # HRV metrics
            if self._hr_value is not None and now - self._hr_last_calc < 3.0:
                try:
                    from scipy.signal import find_peaks, butter, filtfilt
                    nyq = PPG_SFREQ / 2
                    b, a = butter(2, [0.7/nyq, 4.0/nyq], btype="band")
                    filtered = filtfilt(b, a, ppg_centered)
                    peaks, _ = find_peaks(
                        filtered, distance=int(PPG_SFREQ * 0.4),
                        height=0.1 * np.std(filtered)
                    )
                    if len(peaks) >= 3:
                        ibi = np.diff(peaks) / PPG_SFREQ * 1000  # ms
                        sdnn = np.std(ibi)
                        rmssd = np.sqrt(np.mean(np.diff(ibi) ** 2))
                        pnn50 = np.sum(np.abs(np.diff(ibi)) > 50) / max(len(ibi) - 1, 1) * 100
                        # HRV: RMSSD is the primary short-term HRV metric
                        self.hrv_text.set_text(f"HRV: {rmssd:.0f}")
                        self.hrv_detail.set_text(
                            f"SDNN:{sdnn:.0f}ms  RMSSD:{rmssd:.0f}ms  pNN50:{pnn50:.0f}%"
                        )
                except Exception:
                    pass
            artists.append(self.hrv_text)
            artists.append(self.hrv_detail)

        # 2.6. Update IMU (ACC + GYRO) waveforms
        for buf, lines, ax in [
            (self.acc_buffer, self.acc_lines, self.ax_acc),
            (self.gyro_buffer, self.gyro_lines, self.ax_gyro),
        ]:
            if buf is not None:
                imu_data = buf.get_window(int(IMU_SFREQ * IMU_BUFFER_SEC))
                imu_times = buf.get_timestamps(int(IMU_SFREQ * IMU_BUFFER_SEC))
                if imu_data.shape[0] > 5 and imu_times.size > 0:
                    t_rel = imu_times[-min(len(imu_data), len(imu_times)):] - imu_times[-1]
                    for ch in range(min(3, imu_data.shape[1])):
                        lines[ch].set_data(
                            t_rel[-imu_data.shape[0]:],
                            imu_data[-len(t_rel):, ch]
                        )
                    artists.extend(lines)

        # Battery display (read from bridge's temp file, every 5s)
        if not hasattr(self, '_batt_last_read'):
            self._batt_last_read = 0.0
        _now = time.time()
        if _now - self._batt_last_read > 5.0:
            self._batt_last_read = _now
            try:
                batt_file = Path(__file__).parent / ".muse_battery.txt"
                if batt_file.exists():
                    self._battery_pct = float(batt_file.read_text().strip())
            except Exception:
                pass
        batt_str = f"BAT: {self._battery_pct:.0f}%" if self._battery_pct is not None else ""
        self.batt_text.set_text(batt_str)
        artists.append(self.batt_text)

        # 2.8. Update 3D head orientation
        if self._frame_count % 5 == 0:  # throttle to every 250ms
            self._update_head_orientation()

        # 3. Update band power bar heights
        with self._bp_lock:
            bp = self._latest_bp
        if bp is not None:
            for bi, (bname, _) in enumerate(BANDS.items()):
                if bname not in bp:
                    continue
                rel = bp[bname].get("rel", np.zeros(PLOT_CHANNELS))
                for ch in range(PLOT_CHANNELS):
                    bar = self.bp_bars[bi][ch]
                    h = float(rel[ch]) if ch < len(rel) else 0.0
                    bar.set_height(h)
                    bar.set_y(0)
                    artists.append(bar)

        # 4. Status line
        if bp:
            alpha_rel = bp.get("alpha", {}).get("rel", np.zeros(PLOT_CHANNELS))
            avg_alpha = float(np.mean(alpha_rel))
            status = (
                f"Muse S EEG  |  Samples: {data.shape[0]:5d}  |  "
                f"Alpha rel: {avg_alpha:.3f}"
                + ("  [SIMULATE]" if self.simulate else "")
                + (f"  [REC {self._rec_samples}]" if self._recording else "")
            )
        else:
            status = (
                f"Muse S EEG  |  Samples: {data.shape[0]:5d}  |  "
                f"Buffering..."
                + ("  [SIMULATE]" if self.simulate else "")
                + (f"  [REC {self._rec_samples}]" if self._recording else "")
            )
        self.status_text.set_text(status)
        artists.append(self.status_text)

        # 4.5. Recording: write new EEG samples to CSV with all sensor data
        if self._recording and data.shape[0] > 0 and times.size > 0:
            new_mask = times > self._rec_last_ts
            if np.any(new_mask):
                new_idx = np.where(new_mask)[0]

                # Get latest PPG, ACC, GYRO data
                ppg_now = None
                if self.ppg_buffer is not None:
                    ppg_data = self.ppg_buffer.get_window(1)
                    if ppg_data.shape[0] > 0 and ppg_data.shape[1] >= 3:
                        ppg_now = ppg_data[-1, :3].tolist()

                acc_now = None
                if self.acc_buffer is not None:
                    acc_data = self.acc_buffer.get_window(1)
                    if acc_data.shape[0] > 0 and acc_data.shape[1] >= 3:
                        acc_now = acc_data[-1, :3].tolist()

                gyro_now = None
                if self.gyro_buffer is not None:
                    gyro_data = self.gyro_buffer.get_window(1)
                    if gyro_data.shape[0] > 0 and gyro_data.shape[1] >= 3:
                        gyro_now = gyro_data[-1, :3].tolist()

                for i in new_idx:
                    ts = float(times[i])
                    eeg = data[i, :4].tolist() if data.shape[1] >= 4 else [0]*4
                    self._write_recording_row(ts, eeg, ppg_now, acc_now, gyro_now, self._hr_value)
                self._rec_last_ts = float(times[new_idx[-1]])
            artists.append(self.rec_indicator)

        return artists

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    def run(self):
        """Start the real-time animation loop."""
        print()
        print("Keyboard controls:")
        print("  Click a channel → highlight it")
        print("  Up/Down      → adjust amplitude range")
        print("  Left/Right   → adjust time window")
        print("  Settings auto-save to .muse_viewer_config.json")
        print()
        self.ani = FuncAnimation(
            self.fig,
            self._update,
            interval=self.refresh_ms,
            blit=False,
            cache_frame_data=False,
        )
        plt.show()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Muse S Real-time EEG Viewer with Band Power",
    )
    parser.add_argument(
        "-w", "--window", type=float, default=DEFAULT_WINDOW,
        help=f"Waveform window in seconds (default: {DEFAULT_WINDOW})",
    )
    parser.add_argument(
        "-r", "--refresh", type=int, default=DEFAULT_REFRESH,
        help=f"Animation refresh interval in ms (default: {DEFAULT_REFRESH})",
    )
    parser.add_argument(
        "-s", "--stream", default="Muse",
        help="LSL stream name to search for (default: 'Muse')",
    )
    parser.add_argument(
        "-t", "--timeout", type=float, default=LSL_SCAN_TIMEOUT,
        help=f"LSL stream discovery timeout (default: {LSL_SCAN_TIMEOUT}s)",
    )
    parser.add_argument(
        "--simulate", action="store_true",
        help="Run in simulation mode with synthetic EEG data (no Muse required)",
    )
    args = parser.parse_args()

    print("=" * 60)
    if args.simulate:
        print("  Muse S - EEG Viewer  [SIMULATE MODE]")
    else:
        print("  Muse S - Real-time EEG Viewer with Band Power")
    print("=" * 60)
    print()

    # 1. Create buffers
    buffer = RingBuffer(
        capacity=int(EEG_SFREQ * args.window * 2),
        n_channels=N_EEG_CHANNELS,
    )
    ppg_buffer = RingBuffer(
        capacity=int(PPG_SFREQ * PPG_BUFFER_SEC * 2),
        n_channels=PPG_CHANNELS,
    )

    # 2. Start data sources
    if args.simulate:
        source = SyntheticGenerator(buffer, sfreq=EEG_SFREQ)
        source.start()
        sfreq = EEG_SFREQ
        ppg_source = None
        acc_source = None
        gyro_source = None
        acc_buffer = None
        gyro_buffer = None
    else:
        source = LSLSubscriber(buffer, stream_name=args.stream)
        if not source.connect(timeout=args.timeout):
            sys.exit(1)
        source.start()
        sfreq = source.sfreq

        # Connect PPG stream
        ppg_source = LSLSubscriber(ppg_buffer, stream_name=args.stream, stream_type="PPG")
        if ppg_source.connect(timeout=args.timeout):
            ppg_source.start()
            print("PPG stream connected.")

        # Connect ACC stream
        acc_buffer = RingBuffer(
            capacity=int(IMU_SFREQ * IMU_BUFFER_SEC * 2),
            n_channels=IMU_CHANNELS,
        )
        acc_source = LSLSubscriber(acc_buffer, stream_name=args.stream, stream_type="ACC")
        if acc_source.connect(timeout=args.timeout):
            acc_source.start()
            print("ACC stream connected.")

        # Connect GYRO stream
        gyro_buffer = RingBuffer(
            capacity=int(IMU_SFREQ * IMU_BUFFER_SEC * 2),
            n_channels=IMU_CHANNELS,
        )
        gyro_source = LSLSubscriber(gyro_buffer, stream_name=args.stream, stream_type="GYRO")
        if gyro_source.connect(timeout=args.timeout):
            gyro_source.start()
            print("GYRO stream connected.")

    # 3. Launch viewer
    viewer = MuseViewer(
        buffer,
        sfreq=sfreq,
        ppg_buffer=ppg_buffer,
        acc_buffer=acc_buffer if acc_source is not None else None,
        gyro_buffer=gyro_buffer if gyro_source is not None else None,
        window_sec=args.window,
        refresh_ms=args.refresh,
        simulate=args.simulate,
    )

    print()
    print("Starting visualization...")
    print("  Left:  4-channel EEG waveform")
    print("  Right: Band power bar chart (updates every 1s)")
    print("  Close the window to exit.")
    print()

    try:
        viewer.run()
    except KeyboardInterrupt:
        pass
    finally:
        source.stop()
        for s in [ppg_source, acc_source, gyro_source]:
            if s is not None: s.stop()
        print("Done.")


if __name__ == "__main__":
    main()
