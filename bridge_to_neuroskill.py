#!/usr/bin/env python3
"""
Bridge: OSC → LSL + NeuroSkill-compatible CSV Recorder.

Receives OSC from Android Muse Bridge, publishes LSL streams (for
lsl_viewer.py), and simultaneously records EEG data as NeuroSkill-
compatible CSV files for post-hoc analysis.

This bypasses NeuroSkill's broken LSL GUI scanner completely.

Usage:
    python bridge_to_neuroskill.py
    python bridge_to_neuroskill.py --port 5000 --out sessions/
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

import numpy as np
from pylsl import StreamInfo, StreamOutlet, local_clock
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer

# ---------------------------------------------------------------------------
# Constants (matching the existing osc_lsl_bridge.py)
# ---------------------------------------------------------------------------
MUSE_NB_EEG_CHANNELS = 5
MUSE_SAMPLING_EEG_RATE = 256
LSL_EEG_CHUNK = 12

MUSE_NB_PPG_CHANNELS = 3
MUSE_SAMPLING_PPG_RATE = 64

MUSE_NB_ACC_CHANNELS = 3
MUSE_SAMPLING_ACC_RATE = 52

MUSE_NB_GYRO_CHANNELS = 3
MUSE_SAMPLING_GYRO_RATE = 52

EEG_CHANNEL_LABELS = ["TP9", "AF7", "AF8", "TP10", "Right AUX"]

logger = logging.getLogger("bridge")


class NeuroSkillCSVRecorder:
    """Records EEG data in NeuroSkill-compatible CSV format.

    NeuroSkill CSV format:
        timestamp, TP9, AF7, AF8, TP10, [AUX, ...]
    """

    def __init__(self, output_dir: str = "sessions"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self._csv_file = None
        self._csv_writer = None
        self._lock = Lock()
        self._sample_count = 0
        self._start_time = None

        # Create a new session file
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._csv_path = self.output_dir / f"muse_session_{ts}.csv"

    def start(self):
        self._csv_file = open(self._csv_path, "w", newline="")
        self._csv_writer = csv.writer(self._csv_file)
        # Header
        self._csv_writer.writerow(["timestamp"] + EEG_CHANNEL_LABELS)
        self._start_time = time.time()
        logger.info(f"Recording to: {self._csv_path}")

    def write_sample(self, timestamp: float, sample: list[float]):
        with self._lock:
            if self._csv_writer is None:
                return
            row = [f"{timestamp:.6f}"] + [f"{v:.6f}" for v in sample]
            self._csv_writer.writerow(row)
            self._sample_count += 1

    def stop(self):
        with self._lock:
            if self._csv_file:
                self._csv_file.close()
                self._csv_file = None
                self._csv_writer = None
        elapsed = time.time() - self._start_time if self._start_time else 0
        logger.info(
            f"Session saved: {self._csv_path} "
            f"({self._sample_count} samples, {elapsed:.0f}s)"
        )

    @property
    def path(self) -> Path:
        return self._csv_path


class LslStreamManager:
    """Creates and manages LSL outlets + CSV recording."""

    def __init__(self, source_id: str = "MuseBridge",
                 recorder: NeuroSkillCSVRecorder | None = None):
        self.source_id = source_id
        self.recorder = recorder

        # Create LSL outlets
        self.eeg_outlet = self._make_outlet(
            "EEG", MUSE_NB_EEG_CHANNELS, MUSE_SAMPLING_EEG_RATE,
            EEG_CHANNEL_LABELS, "microvolts", "EEG"
        )
        self.ppg_outlet = self._make_outlet(
            "PPG", MUSE_NB_PPG_CHANNELS, MUSE_SAMPLING_PPG_RATE,
            ["PPG1", "PPG2", "PPG3"], "raw", "PPG"
        )
        self.acc_outlet = self._make_outlet(
            "ACC", MUSE_NB_ACC_CHANNELS, MUSE_SAMPLING_ACC_RATE,
            ["X", "Y", "Z"], "g", "accelerometer"
        )
        self.gyro_outlet = self._make_outlet(
            "GYRO", MUSE_NB_GYRO_CHANNELS, MUSE_SAMPLING_GYRO_RATE,
            ["X", "Y", "Z"], "dps", "gyroscope"
        )

        # Stats
        self.eeg_packets = 0
        self.last_stats_time = time.time()

        logger.info("LSL outlets ready (EEG, PPG, ACC, GYRO)")

    def _make_outlet(self, stype, nch, srate, labels, unit, kind):
        info = StreamInfo(
            "Muse", stype, nch, srate, "float32",
            f"Muse_{self.source_id}"
        )
        info.desc().append_child_value("manufacturer", "Muse")
        channels = info.desc().append_child("channels")
        for label in labels:
            channels.append_child("channel") \
                .append_child_value("label", label) \
                .append_child_value("unit", unit) \
                .append_child_value("type", kind)
        return StreamOutlet(info, 12 if stype == "EEG" else 6)

    def handle_eeg(self, address: str, *args):
        values = [float(v) for v in args]
        n_total = len(values)
        n_samples = n_total // MUSE_NB_EEG_CHANNELS
        if n_samples == 0:
            return

        now = local_clock()
        dt = 1.0 / MUSE_SAMPLING_EEG_RATE

        for s in range(n_samples):
            sample = values[s * 5:(s + 1) * 5]
            while len(sample) < MUSE_NB_EEG_CHANNELS:
                sample.append(0.0)
            ts = now - (n_samples - 1 - s) * dt
            self.eeg_outlet.push_sample(sample[:5], ts)
            self.eeg_packets += 1

            # Record to CSV for NeuroSkill
            if self.recorder:
                self.recorder.write_sample(ts, sample[:5])

        self._print_stats()

    def handle_ppg(self, address: str, *args):
        values = [float(v) for v in args]
        if not values:
            return
        n_channels = 8 if len(values) >= 16 else 4
        n_samples = len(values) // n_channels
        if n_samples == 0:
            return
        now = local_clock()
        dt = 1.0 / MUSE_SAMPLING_PPG_RATE
        for s in range(n_samples):
            sample = values[s * n_channels:(s + 1) * n_channels]
            ts = now - (n_samples - 1 - s) * dt
            self.ppg_outlet.push_sample(sample[:MUSE_NB_PPG_CHANNELS], ts)

    def handle_acc(self, address: str, *args):
        now = local_clock()
        self.acc_outlet.push_sample([float(v) for v in args[:3]], now)

    def handle_gyro(self, address: str, *args):
        now = local_clock()
        self.gyro_outlet.push_sample([float(v) for v in args[:3]], now)

    def handle_battery(self, address: str, *args):
        if args:
            try:
                Path(".muse_battery.txt").write_text(str(float(args[0])))
            except Exception:
                pass

    def _print_stats(self):
        now = time.time()
        if now - self.last_stats_time >= 5.0:
            elapsed = now - self.last_stats_time
            rec_status = f" | CSV: {self.recorder._sample_count} samples" if self.recorder else ""
            logger.info(
                f"Throughput — EEG: {self.eeg_packets / elapsed:.0f} pkt/s{rec_status}"
            )
            self.eeg_packets = 0
            self.last_stats_time = now


def main():
    parser = argparse.ArgumentParser(
        description="OSC → LSL + NeuroSkill CSV Bridge"
    )
    parser.add_argument("-p", "--port", type=int, default=5000,
                       help="UDP port for OSC (default: 5000)")
    parser.add_argument("-i", "--ip", default="0.0.0.0",
                       help="IP to bind (default: 0.0.0.0)")
    parser.add_argument("--source-id", default="MuseBridge",
                       help="LSL source ID")
    parser.add_argument("-o", "--out", default="sessions",
                       help="CSV output directory for NeuroSkill")
    parser.add_argument("--no-csv", action="store_true",
                       help="Skip CSV recording (LSL only)")
    parser.add_argument("--log", default="INFO",
                       choices=["DEBUG", "INFO", "WARNING"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # CSV recorder for NeuroSkill
    recorder = None if args.no_csv else NeuroSkillCSVRecorder(args.out)
    if recorder:
        recorder.start()

    # LSL streams
    manager = LslStreamManager(source_id=args.source_id, recorder=recorder)

    # OSC dispatcher
    dispatcher = Dispatcher()
    dispatcher.map("/muse/eeg", manager.handle_eeg)
    dispatcher.map("/muse/ppg", manager.handle_ppg)
    dispatcher.map("/muse/acc", manager.handle_acc)
    dispatcher.map("/muse/gyro", manager.handle_gyro)
    dispatcher.map("/muse/batt", manager.handle_battery)

    # Default handler for unknown OSC paths
    def unknown_handler(addr: str, *args):
        logger.debug(f"Unknown OSC: {addr} = {args[:3]}")

    dispatcher.set_default_handler(unknown_handler)

    # Start OSC server
    server = ThreadingOSCUDPServer((args.ip, args.port), dispatcher)

    print()
    print("=" * 60)
    print("  OSC → LSL + NeuroSkill CSV Bridge")
    print(f"  UDP: {args.ip}:{args.port}")
    print(f"  LSL source ID: {args.source_id}")
    if recorder:
        print(f"  Recording to: {recorder.path}")
        print(f"  Import this file in NeuroSkill for analysis")
    print("=" * 60)
    print()
    print("Also run in another terminal:  python lsl_viewer.py")
    print("Press Ctrl+C to stop.")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        server.shutdown()
        if recorder:
            recorder.stop()
        print("Done.")


if __name__ == "__main__":
    main()
