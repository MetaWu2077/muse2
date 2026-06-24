#!/usr/bin/env python3
"""
BLE Advertising Packet Visualizer for Muse <-> Windows diagnostics.

Two modes:
  --live      Use bleak to scan live BLE advertisements (no extra hardware needed)
  pcap file   Consumes a pcapng from Wireshark + nRF Sniffer (optional)

Outputs:
  1. PNG timeline of advertising events per device (channel-coded)
  2. PNG RSSI-over-time plot for Muse devices only
  3. PNG histogram of advertising interval (ms) -- expected ~320ms for Muse
  4. JSON+CSV summary of every Muse advertisement seen
  5. Console verdict: "Radio receiving Muse?  YES / NO" with evidence

Usage:
    python ble_adv_visualizer.py --live                        # live scan 30s
    python ble_adv_visualizer.py --live --duration 60          # live scan 60s
    python ble_adv_visualizer.py adv_capture.pcapng            # from pcap file
    python ble_adv_visualizer.py adv_capture.pcapng --target-mac 00:55:DA:XX

Dependencies:
    pip install scapy matplotlib pandas bleak
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from collections import defaultdict

import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Patch

# Muse vendor BLE identifiers (confirmed from muse-js / muse-rs sources).
MUSE_PRIMARY_SERVICE_16 = 0xFE8D                 # 16-bit service UUID broadcast in ADV_IND
MUSE_VENDOR_UUID_SUFFIX = "4c4d454d96bef03bac821358"  # 273e____-4c4d-454d-96be-f03bac821358
MUSE_NAME_PREFIXES      = ("Muse", "MuseS", "Muse-")

CHANNEL_COLORS = {37: "#1f77b4", 38: "#ff7f0e", 39: "#2ca02c"}


def live_scan(duration: float = 30.0) -> pd.DataFrame:
    """Use bleak to scan live BLE advertisements (no extra hardware needed)."""
    import asyncio
    from bleak import BleakScanner

    print(f"[+] Live BLE scan for {duration:.0f} seconds...")
    print("    Make sure Muse S is powered on and phone BT is OFF.")
    print()

    events: list[dict] = []

    def callback(device, adv_data):
        ts = time.time()
        rssi = adv_data.rssi if adv_data else None
        name = device.name or (adv_data.local_name if adv_data else None) or ""
        uuids = []
        if adv_data and adv_data.service_uuids:
            uuids = [f"0x{u:04X}" if len(f"{u:X}") <= 4 else u for u in adv_data.service_uuids]
        events.append({
            "timestamp":  ts,
            "channel":    None,     # bleak doesn't expose channel on Windows
            "rssi":       rssi,
            "pdu_type":   "ADV_IND",
            "adv_addr":   device.address.upper(),
            "local_name": name,
            "service_uuids": ",".join(uuids),
            "raw_hex":    "",
        })

    async def run():
        scanner = BleakScanner(detection_callback=callback)
        await scanner.start()
        await asyncio.sleep(duration)
        await scanner.stop()

    asyncio.run(run())

    df = pd.DataFrame(events)
    print(f"[+] Live scan complete: {len(df)} adv packets, "
          f"{df['adv_addr'].nunique()} unique devices.")
    return df


def load_pcap(path: Path) -> pd.DataFrame:
    """Parse a BLE pcapng into a flat DataFrame of advertising events."""
    try:
        from scapy.all import rdpcap
        from scapy.layers.bluetooth4LE import BTLE, BTLE_ADV, BTLE_ADV_IND, BTLE_ADV_NONCONN_IND, BTLE_SCAN_RSP
    except ImportError:
        sys.exit("scapy with BLE layer required: pip install scapy")

    pkts = rdpcap(str(path))
    rows = []
    for p in pkts:
        if not p.haslayer(BTLE_ADV):
            continue
        adv = p[BTLE_ADV]
        ts = float(p.time)
        # nRF sniffer pseudo-header exposes channel + RSSI via metadata layer
        channel = getattr(p, "rf_channel", None) or getattr(p, "channel", None)
        rssi    = getattr(p, "signal_power", None) or getattr(p, "rssi", None)

        adv_addr = getattr(adv, "AdvA", None) or getattr(adv.payload, "AdvA", None)
        pdu_type = adv.PDU_type if hasattr(adv, "PDU_type") else type(adv.payload).__name__

        # AD structures -> assemble local name + service UUID list
        local_name = ""
        service_uuids = []
        data = bytes(adv.payload)
        # Walk AD structures: <len><type><...data...>
        i = 6  # skip AdvA (6 bytes) inside payload for ADV_IND
        try:
            while i < len(data):
                ln = data[i]
                if ln == 0:
                    break
                ad_type = data[i+1]
                ad_data = data[i+2:i+1+ln]
                if ad_type in (0x08, 0x09):              # Shortened / Complete Local Name
                    local_name = ad_data.decode("utf-8", "ignore")
                elif ad_type in (0x02, 0x03):            # 16-bit Service UUIDs
                    for k in range(0, len(ad_data), 2):
                        service_uuids.append(f"0x{int.from_bytes(ad_data[k:k+2],'little'):04X}")
                elif ad_type in (0x06, 0x07):            # 128-bit Service UUIDs
                    for k in range(0, len(ad_data), 16):
                        service_uuids.append(ad_data[k:k+16][::-1].hex())
                i += 1 + ln
        except Exception:
            pass

        rows.append({
            "timestamp":  ts,
            "channel":    channel,
            "rssi":       rssi,
            "pdu_type":   str(pdu_type),
            "adv_addr":   str(adv_addr).upper() if adv_addr else "",
            "local_name": local_name,
            "service_uuids": ",".join(service_uuids),
            "raw_hex":    data.hex(),
        })
    return pd.DataFrame(rows)


def is_muse(row) -> bool:
    name = (row.get("local_name") or "")
    uuids = (row.get("service_uuids") or "").lower()
    if any(name.startswith(pfx) for pfx in MUSE_NAME_PREFIXES):
        return True
    if "0xfe8d" in uuids:
        return True
    if MUSE_VENDOR_UUID_SUFFIX in uuids:
        return True
    return False


def plot_timeline(df: pd.DataFrame, out: Path):
    devices = df["adv_addr"].unique().tolist()
    device_y = {d: i for i, d in enumerate(devices)}

    fig, ax = plt.subplots(figsize=(14, max(3, 0.4 * len(devices) + 2)))

    # Handle live scan (no channel info) vs pcap (has channel info)
    has_channels = df["channel"].notna().any()
    if has_channels:
        for ch, sub in df.groupby("channel"):
            ax.scatter(
                sub["timestamp"], [device_y[d] for d in sub["adv_addr"]],
                s=18, c=CHANNEL_COLORS.get(int(ch) if ch else -1, "#888"),
                label=f"ch {int(ch)}", alpha=0.85, edgecolors="none",
            )
    else:
        # Live scan — color by RSSI strength
        rssi = df["rssi"].fillna(-100)
        ax.scatter(
            df["timestamp"], [device_y[d] for d in df["adv_addr"]],
            s=18, c=rssi, cmap="RdYlGn", vmin=-100, vmax=-40,
            alpha=0.85, edgecolors="none",
        )

    ax.set_yticks(list(device_y.values()))
    ax.set_yticklabels(
        [f"{d}  {'  <-- MUSE' if any(is_muse(r) for _, r in df[df.adv_addr==d].iterrows()) else ''}"
         for d in devices], fontsize=8,
    )
    ax.set_xlabel("capture time (s, epoch)")
    ax.set_title("BLE advertising events  -  per-device timeline")
    if has_channels:
        handles = [Patch(color=c, label=f"ch {ch}") for ch, c in CHANNEL_COLORS.items()]
        ax.legend(handles=handles, loc="upper right")
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    fig.savefig(out, dpi=140)
    plt.close(fig)


def plot_rssi(df_muse: pd.DataFrame, out: Path):
    if df_muse.empty or df_muse["rssi"].isna().all():
        return False
    fig, ax = plt.subplots(figsize=(12, 4))
    for addr, sub in df_muse.groupby("adv_addr"):
        ax.plot(sub["timestamp"], sub["rssi"], marker="o", ms=3, lw=0.8, label=addr)
    ax.axhline(-90, color="red", ls="--", lw=1, label="-90 dBm (marginal)")
    ax.axhline(-75, color="orange", ls="--", lw=1, label="-75 dBm (good)")
    ax.set_xlabel("capture time (s)")
    ax.set_ylabel("RSSI (dBm)")
    ax.set_title("Muse advertising RSSI as seen by the sniffer (proxy for MateBook radio)")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8, loc="lower right")
    fig.tight_layout()
    fig.savefig(out, dpi=140)
    plt.close(fig)
    return True


def plot_interval_hist(df_muse: pd.DataFrame, out: Path):
    if df_muse.empty:
        return False
    fig, ax = plt.subplots(figsize=(10, 4))
    for addr, sub in df_muse.groupby("adv_addr"):
        deltas = sub.sort_values("timestamp")["timestamp"].diff().dropna() * 1000
        deltas = deltas[deltas < 2000]               # filter outliers
        ax.hist(deltas, bins=60, alpha=0.6, label=f"{addr}  (n={len(deltas)})")
    ax.axvline(320, color="red", ls="--", label="Muse nominal 320 ms")
    ax.set_xlabel("inter-advertisement interval (ms)")
    ax.set_ylabel("count")
    ax.set_title("Muse advertising interval distribution")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out, dpi=140)
    plt.close(fig)
    return True


def verdict(df: pd.DataFrame, df_muse: pd.DataFrame) -> dict:
    out = {
        "total_adv_packets":     int(len(df)),
        "unique_devices_seen":   int(df["adv_addr"].nunique()),
        "muse_adv_count":        int(len(df_muse)),
        "muse_devices":          df_muse["adv_addr"].unique().tolist(),
        "channels_seen_for_muse": sorted({int(c) for c in df_muse["channel"].dropna().unique()}) if not df_muse.empty else [],
        "median_interval_ms":    None,
        "median_rssi_dbm":       None,
        "radio_receiving_muse":  False,
        "diagnosis":             "",
    }
    if not df_muse.empty:
        deltas = df_muse.sort_values("timestamp")["timestamp"].diff().dropna() * 1000
        if len(deltas):
            out["median_interval_ms"] = round(float(deltas.median()), 1)
        if df_muse["rssi"].notna().any():
            out["median_rssi_dbm"] = round(float(df_muse["rssi"].median()), 1)
        out["radio_receiving_muse"] = True

    if out["radio_receiving_muse"]:
        chs = out["channels_seen_for_muse"]
        if set(chs) >= {37, 38, 39}:
            out["diagnosis"] = (
                "Muse is broadcasting normally on all 3 primary advertising channels. "
                "Sniffer is decoding ADV_IND PDUs cleanly. If MateBook still cannot connect, "
                "the failure is at Intel/Realtek driver, Windows BTH stack, or GATT-handshake "
                "level, NOT at the RF/PHY layer."
            )
        else:
            out["diagnosis"] = (
                f"Muse is advertising, but only on channels {chs}. Investigate 2.4 GHz "
                "interference (Wi-Fi ch 1/6/11 overlap) or a low-power broadcast mode."
            )
    else:
        out["diagnosis"] = (
            "NO Muse advertisements captured. Either the headband is not powered/in "
            "pairing mode, the sniffer is too far away, or the device is broadcasting "
            "on a non-standard channel. Power-cycle the Muse (long-press until LEDs "
            "chase) and recapture within 1 m of the laptop."
        )
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pcap", type=Path, nargs="?", default=None,
                    help="pcapng file (optional if --live is used)")
    ap.add_argument("--live", action="store_true",
                    help="Scan live BLE advertisements via bleak (no pcap needed)")
    ap.add_argument("--duration", type=float, default=30.0,
                    help="Live scan duration in seconds (default: 30)")
    ap.add_argument("--target-mac", help="Force-tag this MAC as the Muse under test")
    ap.add_argument("--outdir", type=Path, default=Path("./ble_report"))
    args = ap.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)

    if args.live:
        print("[+] Live BLE advertising scan mode")
        df = live_scan(args.duration)
    elif args.pcap:
        print(f"[+] Loading {args.pcap} ...")
        df = load_pcap(args.pcap)
    else:
        sys.exit("ERROR: provide a pcap file OR use --live for live scanning.\n"
                 "  python ble_adv_visualizer.py --live\n"
                 "  python ble_adv_visualizer.py capture.pcapng")

    if df.empty:
        sys.exit("No BLE advertising packets found in capture.")

    df["is_muse"] = df.apply(is_muse, axis=1)
    if args.target_mac:
        df.loc[df["adv_addr"] == args.target_mac.upper(), "is_muse"] = True
    df_muse = df[df["is_muse"]].copy()

    print(f"[+] {len(df)} adv packets, {df['adv_addr'].nunique()} unique devices, "
          f"{len(df_muse)} Muse packets from {df_muse['adv_addr'].nunique()} Muse device(s).")

    plot_timeline(df,            args.outdir / "01_timeline_all_devices.png")
    plot_rssi(df_muse,           args.outdir / "02_muse_rssi.png")
    plot_interval_hist(df_muse,  args.outdir / "03_muse_interval_hist.png")

    df_muse.to_csv(args.outdir / "muse_advertisements.csv", index=False)
    v = verdict(df, df_muse)
    (args.outdir / "verdict.json").write_text(json.dumps(v, indent=2))

    print("\n=== DIAGNOSTIC VERDICT ===")
    for k, val in v.items():
        print(f"  {k:25s}: {val}")
    print(f"\nReport written to: {args.outdir.resolve()}")


if __name__ == "__main__":
    main()
