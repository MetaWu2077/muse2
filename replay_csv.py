#!/usr/bin/env python3
"""
Replay a recorded CSV file as LSL streams for NeuroSkill analysis.

Reads the CSV from lsl_viewer.py recordings and publishes it as
LSL streams at the original speed. NeuroSkill can then connect
and process it as if it were live data.

Usage:
    python replay_csv.py recordings/20260618_120000_meditation.csv
    python replay_csv.py recordings/20260618_120000_meditation.csv --speed 2x
"""

import argparse
import csv
import logging
import sys
import time
from pathlib import Path

import numpy as np
from pylsl import StreamInfo, StreamOutlet, local_clock

logger = logging.getLogger("replay")


def main():
    parser = argparse.ArgumentParser(description="Replay CSV as LSL for NeuroSkill")
    parser.add_argument("csv_file", help="Path to the recorded CSV file")
    parser.add_argument("--speed", default="1x", help="Playback speed: 1x, 2x, 5x, 10x")
    parser.add_argument("--source-id", default="Replay", help="LSL source ID")
    args = parser.parse_args()

    csv_path = Path(args.csv_file)
    if not csv_path.exists():
        print(f"File not found: {csv_path}")
        sys.exit(1)

    speed = float(args.speed.replace("x", ""))

    # Read the CSV
    rows = []
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    if not rows:
        print("Empty CSV file")
        sys.exit(1)

    print(f"Loaded {len(rows)} samples from {csv_path.name}")
    print(f"Duration: {float(rows[-1]['timestamp']) - float(rows[0]['timestamp']):.1f}s")
    print(f"Playback speed: {speed}x")
    print(f"Real playback time: {(float(rows[-1]['timestamp']) - float(rows[0]['timestamp'])) / speed:.1f}s")

    # Create LSL outlets (matching the bridge format)
    eeg_info = StreamInfo("Muse-Replay", "EEG", 4, 256.0, "float32", args.source_id + "-EEG")
    channels = eeg_info.desc().append_child("channels")
    for label in ["TP9", "AF7", "AF8", "TP10"]:
        ch = channels.append_child("channel")
        ch.append_child_value("label", label)
        ch.append_child_value("unit", "microvolts")
        ch.append_child_value("type", "EEG")
    eeg_outlet = StreamOutlet(eeg_info)

    # Also create PPG stream if data present
    has_ppg = "PPG_ambient" in rows[0]
    if has_ppg:
        ppg_info = StreamInfo("Muse-Replay", "PPG", 3, 64.0, "float32", args.source_id + "-PPG")
        ppg_outlet = StreamOutlet(ppg_info)

    has_acc = "ACC_X" in rows[0]
    has_gyro = "GYRO_X" in rows[0]

    print()
    print("=" * 60)
    print("  Replaying: " + csv_path.name)
    print(f"  LSL stream: Muse-Replay/EEG (4ch, 256Hz)")
    print("=" * 60)
    print()
    print("Now open NeuroSkill → Settings → LSL → Scan")
    print("Connect to: Muse-Replay/EEG")
    print()
    print("Press Ctrl+C to stop.")
    print()

    # Replay
    t0_data = float(rows[0]["timestamp"])
    t0_wall = time.perf_counter()
    last_print = 0

    try:
        for i, row in enumerate(rows):
            # Calculate target elapsed time
            t_data = float(row["timestamp"]) - t0_data
            t_target = t_data / speed
            t_now = time.perf_counter() - t0_wall

            # Sleep to match playback speed
            if t_target > t_now:
                time.sleep(t_target - t_now)

            # Push EEG
            now = local_clock()
            eeg = [float(row.get("TP9", 0)), float(row.get("AF7", 0)),
                   float(row.get("AF8", 0)), float(row.get("TP10", 0))]
            eeg_outlet.push_sample(eeg, now)

            # Push PPG
            if has_ppg:
                ppg = [float(row.get(f"PPG_{k}", 0)) for k in ["ambient", "IR", "Red"]]
                ppg_outlet.push_sample(ppg, now)

            # Progress
            if i - last_print >= 500:
                pct = i / len(rows) * 100
                elapsed = time.perf_counter() - t0_wall
                print(f"\r  Progress: {pct:.0f}%  ({i}/{len(rows)})  "
                      f"Elapsed: {elapsed:.0f}s", end="", flush=True)
                last_print = i

    except KeyboardInterrupt:
        print("\nReplay stopped.")

    print(f"\nDone. Replayed {len(rows)} samples.")


if __name__ == "__main__":
    main()
