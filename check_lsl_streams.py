#!/usr/bin/env python3
"""
LSL Stream Discovery Diagnostic Tool.

Run this to verify LSL streams are visible on the network.
Useful for debugging NeuroSkill LSL connection issues.

Usage:
    python check_lsl_streams.py              # scan all types for 10s
    python check_lsl_streams.py --timeout 30 # scan for 30s
    python check_lsl_streams.py --list-all   # show everything found
"""

import argparse
import time
from pylsl import resolve_streams, resolve_byprop


def scan_all(timeout: float = 10.0):
    """Scan for ALL LSL streams regardless of type."""
    print(f"Scanning for ALL LSL streams ({timeout}s timeout)...")
    print("-" * 60)

    streams = resolve_streams(timeout=timeout)

    if not streams:
        print("NO LSL streams found.")
        print()
        print("Possible causes:")
        print("  1. osc_lsl_bridge.py is not running")
        print("  2. Windows Firewall blocking UDP multicast (port 16571)")
        print("  3. pylsl / liblsl version mismatch")
        print("  4. Network adapter filtering multicast")
        return False

    print(f"Found {len(streams)} LSL stream(s):")
    print()

    for i, sinfo in enumerate(streams):
        print(f"  Stream #{i+1}:")
        print(f"    Name:       {sinfo.name()}")
        print(f"    Type:       {sinfo.type()}")
        print(f"    Channels:   {sinfo.channel_count()}")
        print(f"    Rate:       {sinfo.nominal_srate()} Hz")
        print(f"    Format:     {sinfo.channel_format()}")
        print(f"    Source ID:  {sinfo.source_id()}")
        print(f"    UID:        {sinfo.uid()}")
        print(f"    Hostname:   {sinfo.hostname()}")
        print(f"    Version:    {sinfo.version()}")

        # Show channel labels if available
        try:
            xml = sinfo.as_xml()
            if "label" in xml:
                import re
                labels = re.findall(r'<label>(.*?)</label>', xml)
                if labels:
                    print(f"    Labels:     {', '.join(labels)}")
        except Exception:
            pass
        print()

    return True


def scan_by_type(stream_type: str, timeout: float = 5.0):
    """Scan for streams of a specific type."""
    print(f"Scanning for type='{stream_type}' streams ({timeout}s)...")

    streams = resolve_byprop("type", stream_type, timeout=timeout)

    if streams:
        print(f"  Found {len(streams)} stream(s) of type '{stream_type}'")
        for s in streams:
            print(f"    - {s.name()} ({s.channel_count()}ch, {s.nominal_srate()}Hz) [{s.source_id()}]")
    else:
        print(f"  No streams found with type '{stream_type}'")

    return len(streams) > 0


def check_lsl_library():
    """Print LSL library version info."""
    import pylsl
    print(f"pylsl version: {pylsl.__version__ if hasattr(pylsl, '__version__') else 'unknown'}")

    try:
        from pylsl import library_version, protocol_version
        print(f"liblsl version: {library_version()}")
        print(f"LSL protocol:   {protocol_version()}")
    except Exception as e:
        print(f"Could not get LSL version info: {e}")

    try:
        from pylsl import local_clock
        print(f"local_clock:    {local_clock():.3f}")
    except Exception as e:
        print(f"local_clock error: {e}")


def main():
    parser = argparse.ArgumentParser(description="LSL Stream Discovery Diagnostic")
    parser.add_argument("-t", "--timeout", type=float, default=10.0,
                       help="Scan timeout in seconds")
    parser.add_argument("--list-all", action="store_true",
                       help="List all stream details")
    parser.add_argument("--type", default=None,
                       help="Filter by stream type (EEG, PPG, etc.)")
    args = parser.parse_args()

    print("=" * 60)
    print("  LSL Stream Discovery Diagnostic")
    print("=" * 60)
    print()

    # Check LSL library
    print("LSL Library Info:")
    check_lsl_library()
    print()

    # Scan
    if args.type:
        scan_by_type(args.type, args.timeout)
    else:
        # First scan for types NeuroSkill cares about
        found_eeg = scan_by_type("EEG", min(args.timeout, 5.0))
        print()
        found_exg = scan_by_type("EXG", min(args.timeout, 5.0))
        print()

        if not found_eeg and not found_exg:
            # Fallback: scan all
            print("No EEG/EXG streams found. Scanning all types...")
            print()
            scan_all(args.timeout)
        elif args.list_all:
            print()
            scan_all(args.timeout)

    print("-" * 60)
    print()
    print("Tips for NeuroSkill LSL discovery issues on Windows:")
    print("  1. Run this diagnostic WHILE osc_lsl_bridge.py is running")
    print("  2. Check Windows Firewall: allow python.exe (UDP 16571)")
    print("  3. Ensure both scripts use the same Python environment")
    print("  4. Try: python check_lsl_streams.py --timeout 30")


if __name__ == "__main__":
    main()
