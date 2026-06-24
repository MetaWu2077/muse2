#!/usr/bin/env python
"""
Muse S LSL 流启动脚本.

扫描蓝牙 Muse 设备，连接并启动 Lab Streaming Layer 数据流.
Muse S 支持：EEG (4ch) + PPG (3ch) + ACC (3ch) + GYRO (3ch).

Usage:
    python stream_muse.py                    # 自动扫描连接第一个 Muse
    python stream_muse.py --name Muse-XXXX   # 按设备名连接
    python stream_muse.py --address XX:XX    # 按 MAC 地址连接
    python stream_muse.py --backend bluemuse # 指定 BLE 后端
    python stream_muse.py --preset p21       # 使用预设 (Muse S: p21=全部传感器)

Backend options (Windows):
    auto      - 自动选择 (默认，优先 bleak)
    bleak     - 跨平台 BLE (推荐)
    bluemuse  - Windows 专用 BlueMuse GUI 后端
    bgapi     - BLED112 加密狗后端
"""

from __future__ import annotations

import argparse
import concurrent.futures
import logging
import sys
import time

# ---------------------------------------------------------------------------
# Bleak 3.x compatibility patch for muselsl
#
# Bleak 3.x on Windows has TWO problems:
#   1. BleakScanner.discover() (even with return_adv=True) returns EMPTY on
#      Windows — it's a known bug in bleak 3.0.x WinRT backend.
#   2. Passive scanning (the default) doesn't find Muse S — it requires
#      ACTIVE scanning mode.
#
# Fix: Use BleakScanner(callback, scanning_mode="active") + start()/stop()
# instead of BleakScanner.discover(). This is the ONLY combination that
# reliably finds Muse S on Windows.
# ---------------------------------------------------------------------------

import asyncio as _asyncio
import bleak as _bleak
from muselsl import backends as _muselsl_backends


def _patched_bleak_scan(self, timeout: float = 10.0) -> list[dict]:
    """Fixed scan for bleak >= 3.x on Windows — uses active scan + callback."""
    start = time.monotonic()
    print(f"[{0:.1f}s] Scanning for BLE devices ({timeout:.0f}s)...")

    devices_dict: dict[str, dict] = {}

    def _on_device(device: _bleak.BLEDevice, adv: _bleak.AdvertisementData) -> None:
        name = device.name or (adv.local_name if adv else None)
        # Don't overwrite entries that already have a name
        existing = devices_dict.get(device.address)
        if existing is None or (name and not existing.get("name")):
            devices_dict[device.address] = {"name": name, "address": device.address}

    async def _scan() -> dict[str, dict]:
        scanner = _bleak.BleakScanner(
            _on_device, scanning_mode="active"
        )
        await scanner.start()
        await _asyncio.sleep(timeout)
        await scanner.stop()
        return devices_dict

    try:
        # If there's already a running event loop, use it
        loop = _asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — run in a new one
        devices_dict = _asyncio.run(_scan())
    else:
        # We're inside a running loop; muselsl uses _wait() which wraps
        # the call in a ThreadPoolExecutor, so we need a new loop.
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(_asyncio.run, _scan())
            devices_dict = future.result()

    devices = list(devices_dict.values())
    elapsed = time.monotonic() - start
    print(f"[{elapsed:.1f}s] Scan complete, {len(devices)} devices found.")
    return devices


def _patched_refresh_address(self, start: float) -> None:
    """Fixed address refresh for bleak >= 3.x on Windows."""
    if not self._name:
        return
    elapsed = time.monotonic() - start
    print(f"[{elapsed:.1f}s] Scanning for {self._name}...")

    found_address: str | None = None

    def _on_device(device: _bleak.BLEDevice, adv: _bleak.AdvertisementData) -> None:
        nonlocal found_address
        name = device.name or (adv.local_name if adv else None)
        if name and self._name in name:
            found_address = device.address

    async def _scan() -> str | None:
        scanner = _bleak.BleakScanner(
            _on_device, scanning_mode="active"
        )
        await scanner.start()
        await _asyncio.sleep(5.0)
        await scanner.stop()
        return found_address

    try:
        loop = _asyncio.get_running_loop()
    except RuntimeError:
        address = _asyncio.run(_scan())
    else:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(_asyncio.run, _scan())
            address = future.result()

    if address:
        if address != self._address:
            print(f"[{elapsed:.1f}s] Updated address: {address}")
        self._address = address
    else:
        print(f"[{elapsed:.1f}s] {self._name} not seen during scan")


# Apply patches
_muselsl_backends.BleakBackend.scan = _patched_bleak_scan
_muselsl_backends.BleakDevice._refresh_address = _patched_refresh_address

# ---------------------------------------------------------------------------
# Now safe to use muselsl
# ---------------------------------------------------------------------------

from muselsl import list_muses, stream


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Muse S LSL stream starter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    conn = p.add_mutually_exclusive_group()
    conn.add_argument(
        "-a", "--address",
        help="Device MAC address (e.g. 00:55:DA:B0:XX:XX)",
    )
    conn.add_argument(
        "-n", "--name",
        help="Device name (e.g. MuseS-XXXX)",
    )
    p.add_argument(
        "-b", "--backend",
        default="auto",
        choices=["auto", "bluemuse", "bleak", "bgapi"],
        help="BLE backend (default: auto)",
    )
    p.add_argument(
        "-i", "--interface",
        default=None,
        help="Interface: hci0 for gatt, or COM port for bgapi",
    )
    p.add_argument(
        "--preset",
        default="p21",
        help="Stream preset (default: p21 for Muse S full sensors)",
    )
    p.add_argument(
        "--disable-eeg",
        action="store_true",
        help="Disable EEG streaming",
    )
    p.add_argument(
        "--disable-light",
        action="store_true",
        help="Turn off LED light on Muse S headband",
    )
    p.add_argument(
        "--ppg",
        action="store_true",
        default=True,
        help="Include PPG data (default: True)",
    )
    p.add_argument(
        "--acc",
        action="store_true",
        default=True,
        help="Include accelerometer data (default: True)",
    )
    p.add_argument(
        "--gyro",
        action="store_true",
        default=True,
        help="Include gyroscope data (default: True)",
    )
    p.add_argument(
        "-r", "--retries",
        type=int,
        default=3,
        help="Connection retry count (default: 3)",
    )
    p.add_argument(
        "--log",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    return p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("stream_muse")

    # 1. Resolve device address
    address: str | None = args.address
    name: str | None = args.name

    if address:
        # User provided the MAC — skip the scan, connect directly.
        # This is the most reliable path on Windows where bleak scanning is broken.
        print()
        print(f"Connecting directly to: {name or address} ({address})")
        print("(Skipping BLE scan — address was provided explicitly)")
    else:
        # Scan for Muse devices (required when no address given)
        print()
        print("Scanning for Muse devices...")
        print("Make sure your Muse S is powered on (single press, LED blinking).")
        print()

        try:
            muses = list_muses(backend=args.backend, interface=args.interface)
        except Exception as exc:
            logger.error("Device scan failed: %s", exc)
            logger.info(
                "Troubleshooting:\n"
                "  - Is Bluetooth ON?\n"
                "  - Is Muse S powered on? (LED should blink)\n"
                "  - Is phone Bluetooth OFF? (Muse only connects to one device)\n"
                "  - Try: python stream_muse.py --backend bluemuse\n"
                "  - Or use known MAC: python stream_muse.py --address XX:XX:XX:XX:XX:XX"
            )
            sys.exit(1)

        if not muses:
            print("No Muse devices found.")
            print()
            print("Troubleshooting:")
            print("  1. Single-press Muse S power button → LED should blink")
            print("  2. Turn OFF Bluetooth on your phone (Muse only connects to one device)")
            print("  3. Check Windows Settings > Bluetooth > Add device")
            print("  4. Try: python stream_muse.py --name MuseS-XXXX")
            print("  5. On Windows, install BlueMuse: cd BlueMuse_2.4.0.0 && powershell -File Install.ps1")
            print("     Then: python stream_muse.py --backend bluemuse")
            print("  6. Or provide MAC directly: python stream_muse.py --address XX:XX:XX:XX:XX:XX")
            sys.exit(1)

        print(f"Found {len(muses)} device(s):")
        for m in muses:
            print(f"  - {m.get('name', 'Unknown')}  [{m.get('address', 'N/A')}]")

        # Pick device from scan results
        if args.name:
            target = next(
                (m for m in muses if m.get("name") == args.name), None
            )
        else:
            target = muses[0]  # default: first found

        if target is None:
            print("Specified device not found in scan results.")
            sys.exit(1)

        address = target["address"]
        name = target.get("name", name or "Unknown")

    print(f"\nConnecting to: {name or 'Unknown'} ({address})")
    print(f"Backend: {args.backend}")
    print(f"Preset: {args.preset}")
    print(f"Sensors: EEG={not args.disable_eeg}, PPG={args.ppg}, "
          f"ACC={args.acc}, GYRO={args.gyro}")
    print()
    print("Starting LSL stream... (Press Ctrl+C to stop)")
    print()

    # 2. Start streaming
    try:
        stream(
            address=address,
            backend=args.backend,
            interface=args.interface if args.interface else None,
            ppg_enabled=args.ppg,
            acc_enabled=args.acc,
            gyro_enabled=args.gyro,
            eeg_disabled=args.disable_eeg,
            disable_light=args.disable_light,
            preset=args.preset,
            retries=args.retries,
        )
    except KeyboardInterrupt:
        print("\nStream stopped by user.")
    except Exception as exc:
        logger.error("Stream error: %s", exc)
        raise


if __name__ == "__main__":
    main()
