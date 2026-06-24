"""
Test client: simulates an Android phone sending Muse BLE data to the cloud server.
Run this to verify the server without needing a real phone or Muse device.

Usage:
    python test_client.py [--url ws://localhost:8000/ws/session] [--duration 30]
"""

import asyncio
import json
import struct
import time
import os
import sys
import argparse
import websockets


def build_binary_frame(session_id: str, seq_num: int, payload: bytes) -> bytes:
    """
    Build a binary WebSocket frame matching the Android CloudWebSocketManager format.

    Format:
      Byte 0:       0x01 (sensor data)
      Bytes 1-16:   session_id (UTF-8, space-padded to 16 bytes)
      Bytes 17-20:  seq_num (uint32 big-endian)
      Bytes 21+:    raw BLE payload
    """
    sid_bytes = session_id.encode('utf-8')
    # Space-pad to exactly 16 bytes
    padded_sid = sid_bytes.ljust(16, b' ')[:16]

    header = struct.pack('>B', 0x01)           # frame type
    header += padded_sid                        # session_id (16 bytes)
    header += struct.pack('>I', seq_num)        # seq_num (4 bytes)

    return header + payload


def make_fake_eeg_packet() -> bytes:
    """
    Generate a simulated EEG 4ch BLE notification payload.
    Matches the Muse S Athena format:
      - 14-byte header (byte 9 = 0x11 = TAG_EEG_4CH)
      - 28 bytes EEG data (4 samples x 4 channels, 14-bit LSB)
    """
    header = bytearray(14)
    header[9] = 0x11  # TAG_EEG_4CH

    # Generate fake EEG data: 28 bytes with pseudo-sinusoidal signal
    import random
    eeg_data = bytearray(28)
    for i in range(28):
        # Generate small variations around EEG offset (midpoint ~8192 raw = 0 µV)
        # 14-bit values are packed LSB-first across 28 bytes
        eeg_data[i] = random.randint(0, 255)

    return bytes(header) + bytes(eeg_data)


def make_fake_imu_packet() -> bytes:
    """Generate a simulated IMU (ACCGYRO) BLE notification."""
    header = bytearray(14)
    header[9] = 0x47  # TAG_ACCGYRO

    # 36 bytes = 18 int16 LE values
    imu_data = bytearray(36)
    for i in range(36):
        imu_data[i] = (i * 17) % 256  # Deterministic pattern

    return bytes(header) + bytes(imu_data)


async def run_test(server_url: str, duration: int):
    """Run a full test: connect, stream, disconnect."""

    print(f"Connecting to {server_url}...")
    async with websockets.connect(server_url, max_size=2**20) as ws:
        # 1. Send hello
        hello = json.dumps({
            "type": "hello",
            "device": "Test-Phone-Simulator",
            "address": "00:11:22:33:44:55",
            "preset": "p1034"
        })
        await ws.send(hello)
        print(f"-> Hello sent")

        # 2. Receive hello_ack
        ack_raw = await ws.recv()
        ack = json.loads(ack_raw)
        session_id = ack["session_id"]
        server_time = ack["server_time"]
        print(f"← Session created: {session_id}")
        print(f"  Server time: {server_time}")

        # 3. Stream fake data
        print(f"\nStreaming for {duration} seconds...")
        seq = 0
        start = time.monotonic()
        packet_count = 0
        last_heartbeat = 0

        while time.monotonic() - start < duration:
            seq += 1

            # Alternate between EEG and IMU packets (70% EEG, 30% IMU)
            if seq % 10 < 7:
                payload = make_fake_eeg_packet()
            else:
                payload = make_fake_imu_packet()

            frame = build_binary_frame(session_id, seq, payload)
            await ws.send(frame)
            packet_count += 1

            # Send heartbeat every 5 seconds
            now = time.monotonic()
            if now - last_heartbeat >= 5.0:
                hb = json.dumps({"type": "heartbeat", "battery": 85})
                await ws.send(hb)
                last_heartbeat = now

            # ~100 packets/sec = 10ms between packets
            await asyncio.sleep(0.01)

            # Progress indicator
            if packet_count % 500 == 0:
                elapsed = time.monotonic() - start
                rate = packet_count / elapsed if elapsed > 0 else 0
                print(f"  pkts: {packet_count}, seq: {seq}, "
                      f"rate: {rate:.0f} pkt/s")

        elapsed = time.monotonic() - start
        rate = packet_count / elapsed if elapsed > 0 else 0
        print(f"\nDone streaming.")
        print(f"  Total packets: {packet_count}")
        print(f"  Duration: {elapsed:.1f}s")
        print(f"  Rate: {rate:.0f} packets/sec")
        print(f"  Session ID: {session_id}")

        # Wait a moment for server to finalize
        await asyncio.sleep(1)

        return session_id


async def check_server(server_url: str):
    """Check server health."""
    try:
        from urllib.request import urlopen
    except ImportError:
        from urllib2 import urlopen
    # Extract base HTTP URL from ws:// URL
    http_url = server_url.replace("ws://", "http://").replace("wss://", "https://")
    http_url = http_url.replace("/ws/session", "/health")

    try:
        resp = urlopen(http_url, timeout=5)
        data = json.loads(resp.read().decode())
        print(f"\nServer health: {json.dumps(data, indent=2)}")
        return data
    except Exception as e:
        raise Exception(f"Health check failed: {e}")


async def main():
    parser = argparse.ArgumentParser(description="Muse Cloud Test Client")
    parser.add_argument("--url", default="ws://localhost:8000/ws/session",
                       help="Cloud server WebSocket URL")
    parser.add_argument("--duration", type=int, default=10,
                       help="Streaming duration in seconds")
    args = parser.parse_args()

    print("=" * 50)
    print("Muse Cloud Server - Test Client")
    print("=" * 50)

    # Check server
    try:
        await check_server(args.url)
    except Exception as e:
        print(f"WARNING: Could not reach server health endpoint: {e}")
        print("Make sure the server is running: python server.py")
        return

    # Run test
    session_id = await run_test(args.url, args.duration)

    # Check server again
    try:
        await asyncio.sleep(0.5)
        await check_server(args.url)
    except Exception:
        pass

    print(f"\n[OK] Test complete. Session ID: {session_id}")
    print(f"  Raw file: muse_sessions/{session_id}.bin")
    print(f"  Replay with: python -c \"from muse_raw_stream import MuseRawStream; "
          f"s=MuseRawStream('muse_sessions/{session_id}.bin'); "
          f"print(s.get_file_info())\"")


if __name__ == "__main__":
    asyncio.run(main())
