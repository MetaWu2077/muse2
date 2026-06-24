#!/usr/bin/env python3
"""
Variant of osc_lsl_bridge.py that outputs EXG-type LSL streams.

NeuroSkill might prefer "EXG" type over "EEG" for LSL discovery.
This variant changes the stream type to EXG and drops the AUX channel.

Usage:
    python osc_lsl_bridge_exg.py
    python osc_lsl_bridge_exg.py --port 5001
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import numpy as np
from pylsl import StreamInfo, StreamOutlet, local_clock
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer

MUSE_NB_EEG_CHANNELS = 4  # Only 4 main channels (no AUX)
MUSE_SAMPLING_EEG_RATE = 256
MUSE_NB_PPG_CHANNELS = 3
MUSE_SAMPLING_PPG_RATE = 64
MUSE_NB_ACC_CHANNELS = 3
MUSE_SAMPLING_ACC_RATE = 52
MUSE_NB_GYRO_CHANNELS = 3
MUSE_SAMPLING_GYRO_RATE = 52

EEG_CHANNEL_LABELS = ["TP9", "AF7", "AF8", "TP10"]
PPG_CHANNEL_LABELS = ["PPG1", "PPG2", "PPG3"]

logger = logging.getLogger("bridge_exg")


class LslStreamManager:
    def __init__(self, source_id: str = "MuseBridge"):
        self.source_id = source_id
        self.start_time = local_clock()
        self._battery: float | None = None

        # Create outlets with EXG type
        self.eeg_outlet = self._make_outlet(
            "EXG", MUSE_NB_EEG_CHANNELS, MUSE_SAMPLING_EEG_RATE,
            EEG_CHANNEL_LABELS, "microvolts", "EEG"
        )
        self.ppg_outlet = self._make_outlet(
            "PPG", MUSE_NB_PPG_CHANNELS, MUSE_SAMPLING_PPG_RATE,
            PPG_CHANNEL_LABELS, "raw", "PPG"
        )
        self.acc_outlet = self._make_outlet(
            "ACC", MUSE_NB_ACC_CHANNELS, MUSE_SAMPLING_ACC_RATE,
            ["X", "Y", "Z"], "g", "accelerometer"
        )
        self.gyro_outlet = self._make_outlet(
            "GYRO", MUSE_NB_GYRO_CHANNELS, MUSE_SAMPLING_GYRO_RATE,
            ["X", "Y", "Z"], "dps", "gyroscope"
        )

        self.eeg_packets = 0
        self.last_stats_time = time.time()

        logger.info(f"LSL outlets ready (EXG type, {MUSE_NB_EEG_CHANNELS}ch)")

    def _make_outlet(self, stype, nch, srate, labels, unit, kind):
        source_id_clean = self.source_id.replace("Muse_", "")  # avoid double Muse_
        info = StreamInfo(
            "Muse", stype, nch, srate, "float32",
            source_id_clean
        )
        info.desc().append_child_value("manufacturer", "Muse")
        channels = info.desc().append_child("channels")
        for label in labels:
            channels.append_child("channel") \
                .append_child_value("label", label) \
                .append_child_value("unit", unit) \
                .append_child_value("type", kind)
        return StreamOutlet(info)

    def handle_eeg(self, address: str, *args):
        values = [float(v) for v in args]
        n_samples = len(values) // 5  # input has 5 channels, we use first 4
        if n_samples == 0:
            return
        now = local_clock()
        dt = 1.0 / MUSE_SAMPLING_EEG_RATE
        for s in range(n_samples):
            sample = values[s * 5:(s + 1) * 5]
            ts = now - (n_samples - 1 - s) * dt
            self.eeg_outlet.push_sample(sample[:MUSE_NB_EEG_CHANNELS], ts)
            self.eeg_packets += 1
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
        self.acc_outlet.push_sample([float(v) for v in args[:3]], local_clock())

    def handle_gyro(self, address: str, *args):
        self.gyro_outlet.push_sample([float(v) for v in args[:3]], local_clock())

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
            logger.info(f"EEG: {self.eeg_packets / elapsed:.0f} pkt/s")
            self.eeg_packets = 0
            self.last_stats_time = now


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", "--port", type=int, default=5001)
    parser.add_argument("-i", "--ip", default="0.0.0.0")
    parser.add_argument("--source-id", default="MuseBridge")
    parser.add_argument("--log", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    print()
    print("=" * 60)
    print("  OSC → LSL Bridge (EXG variant for NeuroSkill)")
    print(f"  UDP: {args.ip}:{args.port}")
    print(f"  EXG stream: 4ch EEG (no AUX) + PPG + ACC + GYRO")
    print("=" * 60)
    print()

    manager = LslStreamManager(source_id=args.source_id)

    dispatcher = Dispatcher()
    dispatcher.map("/muse/eeg", manager.handle_eeg)
    dispatcher.map("/muse/ppg", manager.handle_ppg)
    dispatcher.map("/muse/acc", manager.handle_acc)
    dispatcher.map("/muse/gyro", manager.handle_gyro)
    dispatcher.map("/muse/batt", manager.handle_battery)

    def unknown(addr, *args):
        logger.debug(f"Unknown OSC: {addr}")

    dispatcher.set_default_handler(unknown)

    server = ThreadingOSCUDPServer((args.ip, args.port), dispatcher)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
