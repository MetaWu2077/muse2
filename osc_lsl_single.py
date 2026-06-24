#!/usr/bin/env python3
"""
Single-stream OSC-LSL bridge for NeuroSkill testing.
Only publishes ONE EEG stream (no PPG, ACC, GYRO).

Hypothesis: NeuroSkill GUI scanner gets confused by 4 streams
all named "Muse" with different types.

Usage:
    python osc_lsl_single.py
    python osc_lsl_single.py --port 5002
"""

import argparse
import logging
import time
from pylsl import StreamInfo, StreamOutlet, local_clock
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer

logger = logging.getLogger("single")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", "--port", type=int, default=5002)
    parser.add_argument("-i", "--ip", default="0.0.0.0")
    parser.add_argument("--source-id", default="MuseBridge")
    parser.add_argument("--log", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # Create ONLY ONE EEG stream (4ch, matching NeuroSkill virtual source)
    info = StreamInfo(
        "Muse", "EEG", 4, 256.0, "float32", args.source_id
    )
    channels = info.desc().append_child("channels")
    for label in ["TP9", "AF7", "AF8", "TP10"]:
        ch = channels.append_child("channel")
        ch.append_child_value("label", label)
        ch.append_child_value("unit", "microvolts")
        ch.append_child_value("type", "EEG")
    outlet = StreamOutlet(info)
    logger.info("Created SINGLE LSL outlet: Muse/EEG (4ch, 256Hz)")

    def handle_eeg(addr, *args):
        values = [float(v) for v in args]
        n_samples = len(values) // 5
        if n_samples == 0:
            return
        now = local_clock()
        dt = 1.0 / 256.0
        for s in range(n_samples):
            sample = values[s * 5:(s + 1) * 5]
            outlet.push_sample(sample[:4], now - (n_samples - 1 - s) * dt)

    dispatcher = Dispatcher()
    dispatcher.map("/muse/eeg", handle_eeg)
    dispatcher.set_default_handler(lambda addr, *args: None)

    server = ThreadingOSCUDPServer((args.ip, args.port), dispatcher)

    print()
    print("=" * 60)
    print("  SINGLE-STREAM OSC-LSL Bridge (EEG only)")
    print(f"  UDP: {args.ip}:{args.port}")
    print(f"  1 stream: Muse/EEG (4ch, 256Hz)")
    print("=" * 60)
    print("Now check NeuroSkill LSL scanner - should show 1 stream.")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
