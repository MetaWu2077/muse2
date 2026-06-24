#!/usr/bin/env python3
"""
Combined OSC-LSL Bridge — all sensor data in ONE stream for NeuroSkill.

Publishes a single "EXG" stream with all channels:
  Ch 1-4: EEG   (TP9, AF7, AF8, TP10)     @ 256 Hz
  Ch 5-7: PPG   (ambient, IR, red)         @ 64 Hz  (upsampled)
  Ch 8-10: ACC   (X, Y, Z)                 @ 52 Hz  (upsampled)
  Ch 11-13: GYRO (X, Y, Z)                 @ 52 Hz  (upsampled)

Also publishes separate streams for lsl_viewer.py compatibility.

Usage:
    python osc_lsl_combined.py
    python osc_lsl_combined.py --port 5000
"""

import argparse
import logging
import time
from collections import deque
from pathlib import Path

import numpy as np
from pylsl import StreamInfo, StreamOutlet, local_clock
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer

# Constants
EEG_CHANNELS = 4
EEG_RATE = 256
PPG_CHANNELS = 3
PPG_RATE = 64
ACC_CHANNELS = 3
ACC_RATE = 52
GYRO_CHANNELS = 3
GYRO_RATE = 52

# Combined stream config
COMBINED_RATE = 256  # Use EEG rate as master
COMBINED_CHANNELS = EEG_CHANNELS + PPG_CHANNELS + ACC_CHANNELS + GYRO_CHANNELS  # 13

COMBINED_LABELS = [
    "TP9", "AF7", "AF8", "TP10",           # EEG
    "PPG_ambient", "PPG_IR", "PPG_Red",     # PPG
    "ACC_X", "ACC_Y", "ACC_Z",              # Accelerometer
    "GYRO_X", "GYRO_Y", "GYRO_Z",           # Gyroscope
]

logger = logging.getLogger("combined")


class CombinedBridge:
    def __init__(self, source_id: str = "MuseBridge"):
        self.source_id = source_id

        # Latest values for each sub-system (updated by OSC callbacks)
        self._eeg_latest: list[float] = [0.0] * EEG_CHANNELS
        self._ppg_latest: list[float] = [0.0] * PPG_CHANNELS
        self._acc_latest: list[float] = [0.0] * ACC_CHANNELS
        self._gyro_latest: list[float] = [0.0] * GYRO_CHANNELS
        self._last_eeg_ts = 0.0
        self._last_ppg_ts = 0.0
        self._last_acc_ts = 0.0
        self._last_gyro_ts = 0.0

        # Battery
        self._battery: float | None = None

        # Outlets
        self.combined_outlet: StreamOutlet | None = None
        self.eeg_outlet: StreamOutlet | None = None
        self.ppg_outlet: StreamOutlet | None = None
        self.acc_outlet: StreamOutlet | None = None
        self.gyro_outlet: StreamOutlet | None = None

        # Create combined outlet (primary for NeuroSkill)
        self._create_combined_outlet()
        # Also create individual outlets (for lsl_viewer.py)
        self._create_individual_outlets()

        # Start push thread
        self._running = True
        self._push_thread = __import__('threading').Thread(
            target=self._push_loop, daemon=True
        )
        self._push_thread.start()

        # Stats
        self._push_count = 0
        self._last_stats = time.time()

    def _create_combined_outlet(self):
        info = StreamInfo(
            "Muse", "EXG", COMBINED_CHANNELS,
            COMBINED_RATE, "float32",
            self.source_id
        )
        channels = info.desc().append_child("channels")
        for i, label in enumerate(COMBINED_LABELS):
            ch = channels.append_child("channel")
            ch.append_child_value("label", label)
            ch.append_child_value("unit", "microvolts" if i < 4 else "raw")
            ch.append_child_value("type", "EEG" if i < 4 else
                                  "PPG" if i < 7 else
                                  "accelerometer" if i < 10 else "gyroscope")
        self.combined_outlet = StreamOutlet(info)
        logger.info(f"Created combined outlet: Muse/EXG ({COMBINED_CHANNELS}ch, {COMBINED_RATE}Hz)")

    def _create_individual_outlets(self):
        def make(name, stype, nch, srate, labels, units, kinds):
            info = StreamInfo(name, stype, nch, srate, "float32",
                            self.source_id + "-" + stype)
            channels = info.desc().append_child("channels")
            for label, unit, kind in zip(labels, units, kinds):
                ch = channels.append_child("channel")
                ch.append_child_value("label", label)
                ch.append_child_value("unit", unit)
                ch.append_child_value("type", kind)
            return StreamOutlet(info)

        self.eeg_outlet = make(
            "Muse-EEG", "EEG", EEG_CHANNELS, EEG_RATE,
            COMBINED_LABELS[:4],
            ["microvolts"] * 4, ["EEG"] * 4
        )
        self.ppg_outlet = make(
            "Muse-PPG", "PPG", PPG_CHANNELS, PPG_RATE,
            COMBINED_LABELS[4:7],
            ["raw"] * 3, ["PPG"] * 3
        )
        self.acc_outlet = make(
            "Muse-ACC", "ACC", ACC_CHANNELS, ACC_RATE,
            COMBINED_LABELS[7:10],
            ["g"] * 3, ["accelerometer"] * 3
        )
        self.gyro_outlet = make(
            "Muse-GYRO", "GYRO", GYRO_CHANNELS, GYRO_RATE,
            COMBINED_LABELS[10:13],
            ["dps"] * 3, ["gyroscope"] * 3
        )
        logger.info("Individual outlets: Muse-EEG, Muse-PPG, Muse-ACC, Muse-GYRO")

    def _push_loop(self):
        """Push combined samples at EEG rate, using latest values from all sensors."""
        interval = 1.0 / COMBINED_RATE
        next_push = time.perf_counter()
        while self._running:
            now = local_clock()
            # Build combined sample from latest values
            sample = (
                list(self._eeg_latest) +
                list(self._ppg_latest) +
                list(self._acc_latest) +
                list(self._gyro_latest)
            )
            self.combined_outlet.push_sample(sample, now)
            self._push_count += 1

            # Print stats periodically
            if self._push_count % (COMBINED_RATE * 5) == 0:
                elapsed = time.time() - self._last_stats
                if elapsed > 0:
                    logger.info(f"Combined: {self._push_count / elapsed:.0f} pkt/s, "
                              f"latest EEG: {[round(v, 1) for v in self._eeg_latest]}")
                self._push_count = 0
                self._last_stats = time.time()

            # Sleep to maintain rate
            next_push += interval
            sleep_time = next_push - time.perf_counter()
            if sleep_time > 0:
                time.sleep(sleep_time)

    # OSC handlers

    def handle_eeg(self, address: str, *args):
        values = [float(v) for v in args]
        n_samples = len(values) // 5  # OSC sends 5 channels, we take 4
        if n_samples == 0:
            return
        sample = values[(n_samples - 1) * 5:(n_samples - 1) * 5 + 4]
        self._eeg_latest = list(sample)
        self._last_eeg_ts = local_clock()
        # Also push to individual EEG outlet
        self.eeg_outlet.push_sample(self._eeg_latest, self._last_eeg_ts)

    def handle_ppg(self, address: str, *args):
        values = [float(v) for v in args]
        if not values:
            return
        n_channels = 8 if len(values) >= 16 else 4
        n_samples = len(values) // n_channels
        if n_samples == 0:
            return
        sample = values[(n_samples - 1) * n_channels:(n_samples - 1) * n_channels + 3]
        self._ppg_latest = list(sample)
        self._last_ppg_ts = local_clock()
        self.ppg_outlet.push_sample(self._ppg_latest, self._last_ppg_ts)

    def handle_acc(self, address: str, *args):
        self._acc_latest = [float(v) for v in args[:3]]
        self._last_acc_ts = local_clock()
        self.acc_outlet.push_sample(self._acc_latest, self._last_acc_ts)

    def handle_gyro(self, address: str, *args):
        self._gyro_latest = [float(v) for v in args[:3]]
        self._last_gyro_ts = local_clock()
        self.gyro_outlet.push_sample(self._gyro_latest, self._last_gyro_ts)

    def handle_battery(self, address: str, *args):
        if args:
            self._battery = float(args[0])
            try:
                Path(".muse_battery.txt").write_text(str(self._battery))
            except Exception:
                pass

    def stop(self):
        self._running = False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", "--port", type=int, default=5000)
    parser.add_argument("-i", "--ip", default="0.0.0.0")
    parser.add_argument("--source-id", default="MuseBridge")
    parser.add_argument("--log", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    bridge = CombinedBridge(source_id=args.source_id)

    dispatcher = Dispatcher()
    dispatcher.map("/muse/eeg", bridge.handle_eeg)
    dispatcher.map("/muse/ppg", bridge.handle_ppg)
    dispatcher.map("/muse/acc", bridge.handle_acc)
    dispatcher.map("/muse/gyro", bridge.handle_gyro)
    dispatcher.map("/muse/batt", bridge.handle_battery)
    dispatcher.set_default_handler(lambda *a: None)

    server = ThreadingOSCUDPServer((args.ip, args.port), dispatcher)

    print()
    print("=" * 60)
    print("  Combined OSC-LSL Bridge")
    print(f"  Primary: Muse/EXG ({COMBINED_CHANNELS}ch, {COMBINED_RATE}Hz)")
    print(f"    Ch 1-4:  EEG  (TP9, AF7, AF8, TP10)")
    print(f"    Ch 5-7:  PPG  (ambient, IR, red)")
    print(f"    Ch 8-10: ACC  (X, Y, Z)")
    print(f"    Ch 11-13:GYRO (X, Y, Z)")
    print(f"  Also: Muse-EEG, Muse-PPG, Muse-ACC, Muse-GYRO for lsl_viewer.py")
    print("=" * 60)
    print()
    print("Connect NeuroSkill to: Muse/EXG")
    print("Or run: python lsl_viewer.py")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        server.shutdown()
        bridge.stop()


if __name__ == "__main__":
    main()
