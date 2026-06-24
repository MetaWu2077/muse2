#!/usr/bin/env python3
"""
OSC → LSL Bridge for Android Muse Bridge App.

Receives OSC data from the Android Muse Bridge app over WiFi/UDP,
converts it to Lab Streaming Layer (LSL) streams compatible with
the existing lsl_viewer.py and band_power.py tools.

Usage:
    python osc_lsl_bridge.py                  # default: port 5000
    python osc_lsl_bridge.py --port 9000      # custom port
    python osc_lsl_bridge.py --list           # list available LSL streams (check)

OSC Paths (from Android app):
    /muse/eeg/tp9   float32[12]   TP9 EEG samples (12 per chunk, 256 Hz)
    /muse/eeg/af7   float32[12]   AF7 EEG samples
    /muse/eeg/af8   float32[12]   AF8 EEG samples
    /muse/eeg/tp10  float32[12]   TP10 EEG samples
    /muse/eeg/aux   float32[12]   Right AUX EEG samples
    /muse/ppg/ambient  float32[6]  PPG ambient
    /muse/ppg/ir       float32[6]  PPG infrared
    /muse/ppg/red      float32[6]  PPG red
    /muse/acc       float32[3]   Accelerometer [x,y,z]
    /muse/gyro      float32[3]   Gyroscope [x,y,z]
    /muse/batt      float32      Battery percentage

Dependencies:
    pip install python-osc pylsl numpy
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import deque
from pathlib import Path
from typing import Callable

import numpy as np
from pylsl import StreamInfo, StreamOutlet, local_clock
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer

# ---------------------------------------------------------------------------
# Constants (matching muselsl constants.py)
# ---------------------------------------------------------------------------
MUSE_NB_EEG_CHANNELS = 4  # Match NeuroSkill virtual source: 4ch Muse standard
MUSE_SAMPLING_EEG_RATE = 256
LSL_EEG_CHUNK = 12

MUSE_NB_PPG_CHANNELS = 3
MUSE_SAMPLING_PPG_RATE = 64
LSL_PPG_CHUNK = 6

MUSE_NB_ACC_CHANNELS = 3
MUSE_SAMPLING_ACC_RATE = 52
LSL_ACC_CHUNK = 1

MUSE_NB_GYRO_CHANNELS = 3
MUSE_SAMPLING_GYRO_RATE = 52
LSL_GYRO_CHUNK = 1

EEG_CHANNEL_LABELS = ["TP9", "AF7", "AF8", "TP10"]  # Match NeuroSkill virtual source exactly
PPG_CHANNEL_LABELS = ["PPG1", "PPG2", "PPG3"]
ACC_CHANNEL_LABELS = ["X", "Y", "Z"]
GYRO_CHANNEL_LABELS = ["X", "Y", "Z"]

logger = logging.getLogger("osc_lsl_bridge")


# ---------------------------------------------------------------------------
# LSL Stream Manager
# ---------------------------------------------------------------------------

class LslStreamManager:
    """Creates and manages LSL outlets, feeding data from OSC callbacks."""

    def __init__(self, source_id: str = "MuseBridge"):
        self.source_id = source_id
        self.start_time = local_clock()

        # Battery tracking
        self._battery: float | None = None
        self._batt_path = Path(__file__).parent / ".muse_battery.txt"

        # LSL outlets
        self.eeg_outlet: StreamOutlet | None = None
        self.ppg_outlet: StreamOutlet | None = None
        self.acc_outlet: StreamOutlet | None = None
        self.gyro_outlet: StreamOutlet | None = None

        # Accumulation buffers (channel → deque of (timestamp, sample))
        # We accumulate 12 EEG samples before pushing to LSL
        self.eeg_buffers: list[deque] = [deque() for _ in range(MUSE_NB_EEG_CHANNELS)]
        self.ppg_buffers: list[deque] = [deque() for _ in range(MUSE_NB_PPG_CHANNELS)]

        # Create LSL outlets
        self._create_eeg_outlet()
        self._create_ppg_outlet()
        self._create_acc_outlet()
        self._create_gyro_outlet()

        # Stats
        self.eeg_packets = 0
        self.ppg_packets = 0
        self.acc_packets = 0
        self.gyro_packets = 0
        self.last_stats_time = time.time()

    def _create_eeg_outlet(self):
        info = StreamInfo(
            "Muse-EEG", "EEG", MUSE_NB_EEG_CHANNELS,
            MUSE_SAMPLING_EEG_RATE, "float32",
            self.source_id + "-EEG"
        )
        channels = info.desc().append_child("channels")
        for label in EEG_CHANNEL_LABELS:
            channels.append_child("channel") \
                .append_child_value("label", label) \
                .append_child_value("unit", "microvolts") \
                .append_child_value("type", "EEG")
        self.eeg_outlet = StreamOutlet(info, LSL_EEG_CHUNK)
        logger.info(f"Created LSL outlet: Muse-EEG ({MUSE_NB_EEG_CHANNELS}ch, {MUSE_SAMPLING_EEG_RATE}Hz)")

    def _create_ppg_outlet(self):
        info = StreamInfo(
            "Muse-PPG", "PPG", MUSE_NB_PPG_CHANNELS,
            MUSE_SAMPLING_PPG_RATE, "float32",
            self.source_id + "-PPG"
        )
        channels = info.desc().append_child("channels")
        for label in PPG_CHANNEL_LABELS:
            channels.append_child("channel") \
                .append_child_value("label", label) \
                .append_child_value("unit", "raw") \
                .append_child_value("type", "PPG")
        self.ppg_outlet = StreamOutlet(info, LSL_PPG_CHUNK)
        logger.info("Created LSL outlet: Muse-PPG (3ch, 64Hz)")

    def _create_acc_outlet(self):
        info = StreamInfo(
            "Muse-ACC", "ACC", MUSE_NB_ACC_CHANNELS,
            MUSE_SAMPLING_ACC_RATE, "float32",
            self.source_id + "-ACC"
        )
        channels = info.desc().append_child("channels")
        for label in ACC_CHANNEL_LABELS:
            channels.append_child("channel") \
                .append_child_value("label", label) \
                .append_child_value("unit", "g") \
                .append_child_value("type", "accelerometer")
        self.acc_outlet = StreamOutlet(info, LSL_ACC_CHUNK)
        logger.info("Created LSL outlet: Muse-ACC (3ch, 52Hz)")

    def _create_gyro_outlet(self):
        info = StreamInfo(
            "Muse-GYRO", "GYRO", MUSE_NB_GYRO_CHANNELS,
            MUSE_SAMPLING_GYRO_RATE, "float32",
            self.source_id + "-GYRO"
        )
        channels = info.desc().append_child("channels")
        for label in GYRO_CHANNEL_LABELS:
            channels.append_child("channel") \
                .append_child_value("label", label) \
                .append_child_value("unit", "dps") \
                .append_child_value("type", "gyroscope")
        self.gyro_outlet = StreamOutlet(info, LSL_GYRO_CHUNK)
        logger.info("Created LSL outlet: Muse-GYRO (3ch, 52Hz)")

    # ---- OSC Handlers ----

    def handle_eeg(self, address: str, *args):
        """Handle /muse/eeg with 5*N floats: take first 4 channels (drop AUX)."""
        values = [float(v) for v in args]
        n_total = len(values)
        OSC_CHANNELS = 5  # Android app sends 5 channels
        n_samples = n_total // OSC_CHANNELS
        if n_samples == 0:
            return

        now = local_clock()
        dt = 1.0 / MUSE_SAMPLING_EEG_RATE  # 1/256 s per sample

        for s in range(n_samples):
            sample = values[s * OSC_CHANNELS:(s + 1) * OSC_CHANNELS]
            # Take only first 4 channels (TP9, AF7, AF8, TP10), drop Right AUX
            ts = now - (n_samples - 1 - s) * dt
            self.eeg_outlet.push_sample(sample[:MUSE_NB_EEG_CHANNELS], ts)
            self.eeg_packets += 1

        self._print_stats()

    def handle_ppg(self, address: str, *args):
        """Handle /muse/ppg with ns*ch floats: N samples × C channels batched."""
        values = [float(v) for v in args]
        if not values:
            return
        # Determine channel count: preset p1034 → 8ch
        n_channels = 8 if len(values) >= 16 else 4
        n_samples = len(values) // n_channels
        if n_samples == 0:
            return

        now = local_clock()
        dt = 1.0 / MUSE_SAMPLING_PPG_RATE  # 1/64 s per sample

        for s in range(n_samples):
            sample = values[s * n_channels:(s + 1) * n_channels]
            # Push all available channels to the PPG outlet (first 3 match existing PPG stream)
            ts = now - (n_samples - 1 - s) * dt
            self.ppg_outlet.push_sample(sample[:MUSE_NB_PPG_CHANNELS], ts)
            self.ppg_packets += 1

        self._print_stats()

    def handle_acc(self, address: str, *args):
        """Handle /muse/acc with 3 float values [x,y,z]."""
        now = local_clock()
        values = [float(v) for v in args[:3]]
        self.acc_outlet.push_sample(values, now)
        self.acc_packets += 1
        self._print_stats()

    def handle_gyro(self, address: str, *args):
        """Handle /muse/gyro with 3 float values [x,y,z]."""
        now = local_clock()
        values = [float(v) for v in args[:3]]
        self.gyro_outlet.push_sample(values, now)
        self.gyro_packets += 1
        self._print_stats()

    def handle_battery(self, address: str, *args):
        """Handle /muse/batt with a single float."""
        if args:
            self._battery = float(args[0])
            try: self._batt_path.write_text(str(self._battery))
            except Exception: pass

    def _print_stats(self):
        now = time.time()
        if now - self.last_stats_time >= 5.0:
            elapsed = now - self.last_stats_time
            logger.info(
                f"Throughput — EEG: {self.eeg_packets / elapsed:.0f} pkt/s, "
                f"PPG: {self.ppg_packets / elapsed:.0f} pkt/s, "
                f"ACC: {self.acc_packets / elapsed:.0f} pkt/s, "
                f"GYRO: {self.gyro_packets / elapsed:.0f} pkt/s"
            )
            self.eeg_packets = 0
            self.ppg_packets = 0
            self.acc_packets = 0
            self.gyro_packets = 0
            self.last_stats_time = now


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="OSC → LSL Bridge for Android Muse Bridge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "-p", "--port", type=int, default=5000,
        help="UDP port to listen for OSC messages (default: 5000)",
    )
    p.add_argument(
        "-i", "--ip", default="0.0.0.0",
        help="IP address to bind (default: 0.0.0.0 = all interfaces)",
    )
    p.add_argument(
        "--source-id", default="MuseBridge",
        help="Source identifier for LSL stream names",
    )
    p.add_argument(
        "--log", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    print()
    print("=" * 60)
    print("  OSC → LSL Bridge")
    print(f"  Listening on UDP {args.ip}:{args.port}")
    print(f"  LSL Source ID: {args.source_id}")
    print("=" * 60)
    print()
    print("Waiting for OSC data from Android Muse Bridge app...")
    print("Make sure your Android phone is on the same WiFi network.")
    print()
    print("Once data arrives, open another terminal and run:")
    print("  python lsl_viewer.py")
    print()
    print("Press Ctrl+C to stop.")
    print()

    # Create LSL stream manager
    manager = LslStreamManager(source_id=args.source_id)

    # Set up OSC dispatcher
    dispatcher = Dispatcher()

    # EEG: all 5 channels in one message /muse/eeg [TP9, AF7, AF8, TP10, AUX]
    dispatcher.map("/muse/eeg", manager.handle_eeg)

    # PPG: batched samples
    dispatcher.map("/muse/ppg", manager.handle_ppg)

    # IMU & battery
    dispatcher.map("/muse/acc", manager.handle_acc)
    dispatcher.map("/muse/gyro", manager.handle_gyro)
    dispatcher.map("/muse/batt", manager.handle_battery)

    # Default handler for unknown paths
    # Default handler for unknown paths
    _packet_counts: dict[str, int] = {}

    def _count_handler(addr: str, *args):
        _packet_counts[addr] = _packet_counts.get(addr, 0) + 1
        total = sum(_packet_counts.values())
        # Print every packet for first 10, then every 50th
        if total <= 10 or total % 50 == 0:
            logger.info(f"RECEIVED #{total}: {addr} = {args[:3]}..."
                       if len(args) > 3 else f"RECEIVED #{total}: {addr} = {args}")

    dispatcher.set_default_handler(_count_handler)

    # Start OSC server
    server = ThreadingOSCUDPServer((args.ip, args.port), dispatcher)
    logger.info(f"OSC server started on {args.ip}:{args.port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        server.shutdown()
        print("OSC server stopped.")


if __name__ == "__main__":
    main()
