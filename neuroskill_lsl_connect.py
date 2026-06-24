#!/usr/bin/env python3
"""
Programmatically connect NeuroSkill to the OSC-LSL bridge via the daemon API.

This bypasses the NeuroSkill GUI's LSL scanner and directly:
  1. Discovers LSL streams via the daemon API
  2. Pairs the EEG stream
  3. Starts a recording session

Usage:
    python neuroskill_lsl_connect.py
    python neuroskill_lsl_connect.py --source-id Muse_Muse_MuseBridge

Requirements:
    pip install requests
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("Installing requests...")
    os.system(f"{sys.executable} -m pip install requests")
    import requests


DAEMON_PORT = 18444
AUTH_TOKEN_PATH = Path(os.environ.get("APPDATA", "")) / "skill" / "daemon" / "auth.token"
SETTINGS_PATH = Path(os.environ.get("LOCALAPPDATA", "")) / "NeuroSkill" / "settings.json"


def get_auth_token() -> str:
    if AUTH_TOKEN_PATH.exists():
        return AUTH_TOKEN_PATH.read_text().strip()
    raise FileNotFoundError(f"Auth token not found at {AUTH_TOKEN_PATH}")


def api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    url = f"http://127.0.0.1:{DAEMON_PORT}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if method == "GET":
        resp = requests.get(url, headers=headers, timeout=10)
    elif method == "POST":
        resp = requests.post(url, headers=headers, json=body or {}, timeout=10)
    else:
        raise ValueError(f"Unknown method: {method}")

    if resp.status_code == 200:
        return resp.json() if resp.text else {}
    else:
        print(f"  API error [{resp.status_code}]: {resp.text[:200]}")
        return {}


def main():
    parser = argparse.ArgumentParser(
        description="Connect NeuroSkill daemon to LSL stream programmatically"
    )
    parser.add_argument(
        "--source-id", default="Muse_Muse_MuseBridge",
        help="Source ID of the LSL EEG stream to connect to"
    )
    parser.add_argument(
        "--stream-type", default="EEG",
        help="LSL stream type to look for"
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  NeuroSkill LSL Connector")
    print("=" * 60)
    print()

    # 1. Get auth token
    try:
        token = get_auth_token()
        print(f"[OK] Auth token loaded")
    except FileNotFoundError as e:
        print(f"[FAIL] {e}")
        print("Make sure NeuroSkill is installed and has been run at least once.")
        sys.exit(1)

    # 2. Check daemon is reachable
    status = api("GET", "/v1/status", token)
    if not status:
        print("[FAIL] NeuroSkill daemon not reachable. Is NeuroSkill running?")
        sys.exit(1)
    print(f"[OK] Daemon reachable (state: {status.get('state', 'unknown')})")

    # 3. Cancel any existing retry/connection attempts
    resp = api("POST", "/v1/control/cancel-retry", token)
    print(f"[OK] Cancelled any existing connection attempts")

    # 4. Forget hardware devices so daemon doesn't try them
    for device in status.get("paired_devices", []):
        dev_id = device.get("id", "")
        if dev_id and not dev_id.startswith("lsl:"):
            api("POST", "/v1/devices/forget", token, {"id": dev_id})
            print(f"[OK] Forgotten device: {dev_id}")

    # 5. Discover LSL streams
    streams = api("GET", "/v1/lsl/discover", token)
    if not streams:
        print("[FAIL] No LSL streams found.")
        print("Make sure osc_lsl_bridge.py is running first.")
        sys.exit(1)

    print(f"[OK] Found {len(streams)} LSL stream(s):")
    eeg_stream = None
    for s in streams:
        print(f"     {s['name']}/{s['stream_type']} "
              f"({s['channels']}ch, {s['sample_rate']}Hz) "
              f"[{s['source_id']}]")
        if s["stream_type"] == args.stream_type and s["source_id"] == args.source_id:
            eeg_stream = s

    if not eeg_stream:
        # Try matching by source_id only
        for s in streams:
            if s["source_id"] == args.source_id:
                eeg_stream = s
                break

    if not eeg_stream:
        print(f"[FAIL] No stream matching source_id='{args.source_id}' "
              f"type='{args.stream_type}'")
        print("Available source_ids:", [s["source_id"] for s in streams])
        sys.exit(1)

    print()
    print(f"[TARGET] {eeg_stream['name']}/{eeg_stream['stream_type']} "
          f"({eeg_stream['channels']}ch, {eeg_stream['sample_rate']}Hz)")
    print()

    # 6. Pair the stream
    pair_resp = api("POST", "/v1/lsl/pair", token, {
        "sourceId": eeg_stream["source_id"],
        "name": eeg_stream["name"],
        "streamType": eeg_stream["stream_type"],
        "channels": eeg_stream["channels"],
        "sampleRate": eeg_stream["sample_rate"],
    })
    if pair_resp.get("ok"):
        print(f"[OK] Paired with LSL stream")
    else:
        print(f"[WARN] Pair response: {pair_resp}")

    # 7. Enable LSL auto-connect in settings
    try:
        settings = json.loads(SETTINGS_PATH.read_text())
        settings["lsl_auto_connect"] = True
        settings["scanner"]["ble"] = False
        settings["scanner"]["usb_serial"] = False
        settings["scanner"]["cortex"] = False
        SETTINGS_PATH.write_text(json.dumps(settings, indent=2))
        print(f"[OK] LSL auto-connect enabled, hardware scanners disabled")
    except Exception as e:
        print(f"[WARN] Could not update settings: {e}")

    # 8. Start recording session
    print()
    print("Starting recording session...")
    resp = api("POST", "/v1/control/start-session", token, {})
    print(f"[OK] Start-session triggered")

    # 9. Wait and verify connection
    print()
    print("Waiting for connection to establish...")
    for i in range(15):
        time.sleep(2)
        status = api("GET", "/v1/status", token)
        state = status.get("state", "unknown")
        device = status.get("device_name", "none")
        samples = status.get("sample_count", 0)
        target = status.get("target_name", "none")
        error = status.get("device_error", "")

        print(f"  [{i*2+2:2d}s] state={state:16s} target={str(target):24s} "
              f"samples={samples:6d} error={error or 'none'}")

        if state == "streaming" or state == "connected":
            print()
            print("=" * 60)
            print("  CONNECTED!")
            print(f"  Device: {device}")
            print(f"  Samples: {samples}")
            print(f"  LSL stream is live in NeuroSkill!")
            print("=" * 60)
            print()
            print("You can now use:")
            print("  neuroskill status")
            print("  neuroskill brain flow")
            print("  python lsl_viewer.py  (separate terminal)")
            return

        if state == "disconnected" and i > 5:
            print()
            print("[NOTE] Daemon still disconnected. If LSL auto-connect isn't")
            print("triggering, open the NeuroSkill app and go to:")
            print("  Settings → LSL → 'Scan for LSL Streams' → click the EEG stream")
            sys.exit(1)

    print()
    print("[TIMEOUT] Connection not established within 30 seconds.")
    print()
    print("Manual steps:")
    print("  1. Open NeuroSkill app")
    print("  2. Go to Settings → LSL")
    print("  3. Click 'Scan for LSL Streams'")
    print("  4. Find 'Muse/EEG' and click 'Pair & Connect'")


if __name__ == "__main__":
    main()
