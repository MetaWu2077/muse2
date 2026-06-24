#!/usr/bin/env python3
"""
Test LSL stream generator for NeuroSkill debugging.

Creates a minimal EEG stream to test what format NeuroSkill accepts.
Run this, then check if NeuroSkill's LSL scanner stops and shows results.

Usage:
    python test_neuroskill_lsl.py
    python test_neuroskill_lsl.py --channels 4 --name Muse
"""

import argparse
import time
import numpy as np
from pylsl import StreamInfo, StreamOutlet


def create_test_stream(name="Muse", channels=4, srate=256.0,
                       source_id="MuseBridgeTest"):
    """Create a test EEG stream with different configurations."""

    # Standard Muse channel labels
    if channels == 4:
        labels = ["TP9", "AF7", "AF8", "TP10"]
    elif channels == 5:
        labels = ["TP9", "AF7", "AF8", "TP10", "Right AUX"]
    else:
        labels = [f"Ch{i+1}" for i in range(channels)]

    info = StreamInfo(
        name,           # stream name
        "EEG",          # type
        channels,       # channel count
        srate,          # nominal srate
        "float32",      # format
        source_id       # source_id (no extra prefix!)
    )

    # Add metadata
    info.desc().append_child_value("manufacturer", "Muse")

    chan_xml = info.desc().append_child("channels")
    for label in labels:
        ch = chan_xml.append_child("channel")
        ch.append_child_value("label", label)
        ch.append_child_value("unit", "microvolts")
        ch.append_child_value("type", "EEG")

    outlet = StreamOutlet(info)
    print(f"Created LSL outlet: {name}/EEG ({channels}ch, {srate}Hz)")
    print(f"  Source ID: {source_id}")
    print(f"  Labels: {labels}")
    return outlet, labels


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="Muse")
    parser.add_argument("--channels", type=int, default=4)
    parser.add_argument("--srate", type=float, default=256.0)
    parser.add_argument("--source-id", default="MuseBridgeTest")
    args = parser.parse_args()

    print("=" * 60)
    print("  NeuroSkill LSL Format Test")
    print("=" * 60)
    print()

    # Try multiple stream formats to see which one NeuroSkill accepts
    variants = [
        # (name, channels, srate, source_id)
        ("Muse", 4, 256.0, "MuseBridgeTest"),
        ("Muse-EEG", 4, 256.0, "Muse_EEG_Test"),
    ]

    outlets = []
    for vname, vch, vrate, vsrc in variants:
        outlet, labels = create_test_stream(vname, vch, vrate, vsrc)
        outlets.append(outlet)
        print()

    print("Streams are running. Check NeuroSkill LSL scanner now.")
    print("Press Ctrl+C to stop.")
    print()

    # Push dummy data periodically to keep streams alive
    try:
        while True:
            for outlet in outlets:
                data = np.random.randn(outlet.info().channel_count()).astype(np.float32) * 10.0
                outlet.push_sample(data.tolist())
            time.sleep(1.0 / 256.0)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
