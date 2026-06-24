#!/usr/bin/env python3
"""
BrainVision EEG Parser - reads .vhdr/.vmrk/.eeg and converts to CSV.
Applies 0.5Hz high-pass filter to remove DC drift/reference instability.

Usage:
    python parse_brainvision.py BP_practice_data/EEG_0001.vhdr
    python parse_brainvision.py BP_practice_data/EEG_0001.eeg
"""

import argparse
import csv
import struct
import sys
from pathlib import Path

import numpy as np
from scipy.signal import butter, filtfilt


def parse_vhdr(path: Path) -> dict:
    info = {"channels": [], "resolution": 1.0, "unit": "uV",
            "sfreq": 500, "n_channels": 0, "datafile": "", "markerfile": ""}
    section = None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line.startswith("["): section = line.strip("[]").strip(); continue
            if not line or line.startswith(";"): continue
            if section == "Common Infos":
                if line.startswith("DataFile="): info["datafile"] = line.split("=",1)[1]
                elif line.startswith("MarkerFile="): info["markerfile"] = line.split("=",1)[1]
                elif line.startswith("NumberOfChannels="): info["n_channels"] = int(line.split("=",1)[1])
                elif line.startswith("SamplingInterval="):
                    info["sfreq"] = 1_000_000 / int(line.split("=",1)[1])
            elif section == "Channel Infos":
                if line.startswith("Ch"):
                    parts = line.split(",")
                    name = parts[0].split("=")[1]
                    res = float(parts[2]) if len(parts) > 2 else 1.0
                    unit = parts[3] if len(parts) > 3 else "uV"
                    info["channels"].append({"name": name, "resolution": res, "unit": unit})
            elif section == "Comment": break
    return info


def parse_vmrk(path: Path) -> list:
    markers = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line.startswith("Mk") and "=" in line:
                parts = line.split("=",1)[1].split(",")
                if len(parts) >= 3:
                    markers.append({"type": parts[0], "description": parts[1], "position": int(parts[2])})
    return markers


def read_binary(path: Path, n_channels: int) -> np.ndarray:
    data = []
    with open(path, "rb") as f:
        while True:
            chunk = f.read(4 * n_channels)
            if len(chunk) < 4 * n_channels: break
            data.append(struct.unpack(f"<{n_channels}f", chunk))
    return np.array(data, dtype=np.float64)


def main():
    parser = argparse.ArgumentParser(description="BrainVision EEG Parser")
    parser.add_argument("vhdr_file", help="Path to .vhdr or .eeg file")
    parser.add_argument("--out", help="Output CSV path (auto if omitted)")
    parser.add_argument("--no-filter", action="store_true", help="Skip high-pass filter")
    args = parser.parse_args()

    vhdr_path = Path(args.vhdr_file)
    if vhdr_path.suffix.lower() == ".eeg":
        vhdr_path = vhdr_path.with_suffix(".vhdr")
    if not vhdr_path.exists():
        print(f"Header not found: {vhdr_path}"); sys.exit(1)

    base_dir = vhdr_path.parent
    info = parse_vhdr(vhdr_path)
    print(f"File:      {vhdr_path.name}")
    print(f"Channels:  {info['n_channels']}")
    print(f"Rate:      {info['sfreq']} Hz")
    print(f"Filter:    {'OFF (--no-filter)' if args.no_filter else '0.5Hz high-pass'}")

    marker_path = base_dir / info.get("markerfile", "")
    markers = []
    if marker_path.exists():
        markers = parse_vmrk(marker_path)
        if markers:
            print(f"Markers:   {len(markers)}")
            for m in markers:
                print(f"  @ {m['position']/info['sfreq']:.1f}s: {m['type']}={m['description']}")

    data_path = base_dir / info.get("datafile", "")
    if not data_path.exists():
        print(f"Binary not found: {data_path}"); sys.exit(1)

    print(f"Reading {data_path.name}...")
    raw = read_binary(data_path, info["n_channels"])
    print(f"Read {raw.shape[0]} samples ({raw.shape[0]/info['sfreq']:.0f}s)")

    # High-pass filter at 0.5Hz to remove DC drift / reference instability
    if not args.no_filter:
        print("Applying 0.5Hz high-pass filter...")
        nyq = info["sfreq"] / 2
        b, a = butter(2, 0.5 / nyq, btype="highpass")
        for ch in range(raw.shape[1]):
            raw[:, ch] = filtfilt(b, a, raw[:, ch])
        print("Done filtering.")

    out_path = Path(args.out) if args.out else vhdr_path.with_suffix(".csv")
    print(f"Writing {out_path}...")

    channel_names = [ch["name"] for ch in info["channels"]]
    resolutions = [ch["resolution"] for ch in info["channels"]]
    marker_map = {m["position"]: f"{m['type']}:{m['description']}" for m in markers}

    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "marker"] + channel_names)
        for i, sample in enumerate(raw):
            ts = i / info["sfreq"]
            marker = marker_map.get(i + 1, "")
            values = [sample[j] * resolutions[j] for j in range(len(sample))]
            writer.writerow([f"{ts:.6f}", marker] + [f"{v:.6f}" for v in values])

    print(f"Done. {raw.shape[0]} samples -> {out_path}")
    print(f"Ready: python brainvision_report.py {out_path}")


if __name__ == "__main__":
    main()
