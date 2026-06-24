#!/usr/bin/env python3
"""
Compare LSL stream metadata between our bridge and NeuroSkill virtual source.

1. Start NeuroSkill virtual source: POST /v1/lsl/virtual-source/start
2. Our bridge should already be running (osc_lsl_bridge.py)
3. This script resolves both and dumps full XML for comparison.
"""

import json
import sys
import time
from pathlib import Path

import requests
from pylsl import resolve_streams

AUTH_TOKEN_PATH = Path.home() / "AppData" / "Roaming" / "skill" / "daemon" / "auth.token"
DAEMON = "http://127.0.0.1:18444"


def get_token():
    return AUTH_TOKEN_PATH.read_text().strip()


def api(method, path, body=None):
    headers = {"Authorization": f"Bearer {get_token()}", "Content-Type": "application/json"}
    if method == "GET":
        return requests.get(f"{DAEMON}{path}", headers=headers, timeout=10).json()
    else:
        return requests.post(f"{DAEMON}{path}", headers=headers, json=body or {}, timeout=10).json()


def dump_stream_xml(stream_info, label=""):
    """Dump complete LSL stream info as XML."""
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  Name:       {stream_info.name()}")
    print(f"  Type:       {stream_info.type()}")
    print(f"  Channels:   {stream_info.channel_count()}")
    print(f"  Rate:       {stream_info.nominal_srate()}")
    print(f"  Format:     {stream_info.channel_format()}")
    print(f"  Source ID:  {stream_info.source_id()}")
    print(f"  UID:        {stream_info.uid()}")
    print(f"  Hostname:   {stream_info.hostname()}")
    print(f"  Version:    {stream_info.version()}")
    print(f"  Session ID: {stream_info.session_id()}")
    print(f"  Created at: {stream_info.created_at()}")

    # Full XML
    xml = stream_info.as_xml()
    print(f"\n  --- Raw XML ({len(xml)} bytes) ---")
    # Just print first 500 and last 200 chars
    if len(xml) > 700:
        print(f"  {xml[:500]}")
        print(f"  ...")
        print(f"  {xml[-200:]}")
    else:
        print(f"  {xml}")


def main():
    print("=" * 60)
    print("  LSL Stream Comparison: Bridge vs Virtual Source")
    print("=" * 60)

    # 1. Start NeuroSkill virtual source
    print("\nStarting NeuroSkill virtual source...")
    try:
        resp = api("POST", "/v1/lsl/virtual-source/start")
        print(f"  Virtual source: {resp}")
    except Exception as e:
        print(f"  Failed to start virtual source: {e}")
        print("  Make sure NeuroSkill daemon is running.")

    time.sleep(2)

    # 2. Resolve ALL LSL streams
    print("\nResolving ALL LSL streams (10s timeout)...")
    all_streams = resolve_streams(wait_time=10.0)

    if not all_streams:
        print("NO LSL streams found!")
        print("Make sure osc_lsl_bridge.py is running.")
        sys.exit(1)

    print(f"Found {len(all_streams)} stream(s)")

    # 3. Dump each stream
    virtual = None
    bridge_eeg = None
    others = []

    for s in all_streams:
        name = s.name()
        stype = s.type()
        src = s.source_id()

        if "SkillVirtual" in name or "skill-virtual" in src:
            virtual = s
        elif stype == "EEG" and "Muse" in name:
            bridge_eeg = s
        else:
            others.append(s)

    if virtual:
        dump_stream_xml(virtual, "NEUROSKILL VIRTUAL SOURCE")

    if bridge_eeg:
        dump_stream_xml(bridge_eeg, "OUR BRIDGE (Muse/EEG)")

    for s in others:
        dump_stream_xml(s, f"OTHER: {s.name()}/{s.type()}")

    # 4. Side-by-side comparison
    if virtual and bridge_eeg:
        print(f"\n{'='*60}")
        print(f"  KEY DIFFERENCES")
        print(f"{'='*60}")

        checks = [
            ("Name", virtual.name(), bridge_eeg.name()),
            ("Type", virtual.type(), bridge_eeg.type()),
            ("Channels", str(virtual.channel_count()), str(bridge_eeg.channel_count())),
            ("Rate", str(virtual.nominal_srate()), str(bridge_eeg.nominal_srate())),
            ("Format", virtual.channel_format(), bridge_eeg.channel_format()),
            ("Source ID", virtual.source_id(), bridge_eeg.source_id()),
            ("Hostname", virtual.hostname(), bridge_eeg.hostname()),
            ("Version", str(virtual.version()), str(bridge_eeg.version())),
        ]

        import re

        all_match = True
        for field, v_val, b_val in checks:
            ok = v_val == b_val
            if not ok:
                all_match = False
            flag = "OK" if ok else "MISMATCH <<<"
            print(f"  {field:12s}: V={v_val:30s} | B={b_val:30s} {flag}")

        # Compare XML details
        v_xml = virtual.as_xml()
        b_xml = bridge_eeg.as_xml()

        has_manufacturer_v = "manufacturer" in v_xml.lower()
        has_manufacturer_b = "manufacturer" in b_xml.lower()
        print(f"  manufacturer : V={str(has_manufacturer_v):5s} | B={str(has_manufacturer_b):5s} " +
              ("OK" if has_manufacturer_v == has_manufacturer_b else "MISMATCH"))

        v_labels = re.findall(r'<label>(.*?)</label>', v_xml)
        b_labels = re.findall(r'<label>(.*?)</label>', b_xml)
        print(f"  labels       : V={str(v_labels)[:60]}")
        print(f"               : B={str(b_labels)[:60]} " +
              ("OK" if v_labels == b_labels else "DIFFERENT"))

        print(f"  XML size     : V={len(v_xml):5d} bytes | B={len(b_xml):5d} bytes")

        # Full XML dump for manual inspection
        print(f"\n  --- Virtual XML (first 400 chars) ---")
        print(f"  {v_xml[:400]}")
        print(f"\n  --- Bridge XML (first 400 chars) ---")
        print(f"  {b_xml[:400]}")

        if all_match:
            print(f"\n  All header metadata matches.")

    # 5. Stop virtual source
    print("\nStopping virtual source...")
    api("POST", "/v1/lsl/virtual-source/stop")


if __name__ == "__main__":
    main()
