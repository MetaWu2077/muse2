"""Quick MUSB .bin completeness analyzer."""
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(__file__))
import muse_athena_protocol as proto
from muse_raw_stream import MuseRawStream


def analyze(path: str) -> None:
    stream = MuseRawStream(path)
    stream.open_read()
    session_start = stream.session_start
    packets = list(stream.read_packets())
    stream.close()

    file_size = os.path.getsize(path)
    expected_end = 29
    for p in packets:
        expected_end += 9 + len(p.data)

    with open(path, "rb") as f:
        f.seek(expected_end)
        trailing = f.read()

    seq_gaps = []
    time_gaps_ms = []
    prev_num = None
    prev_rel_ms = None
    rel_times = []

    type_counts = Counter()
    tag_counts = Counter()
    eeg_samples = 0
    imu_samples = 0
    optics_samples = 0
    battery_count = 0
    unknown_packets = 0
    payload_sizes = Counter()

    for p in packets:
        rel_ms = int((p.timestamp - session_start).total_seconds() * 1000)
        rel_times.append(rel_ms)

        if prev_num is not None:
            diff = (p.packet_num - prev_num) % 65536
            if diff not in (0, 1):
                seq_gaps.append((prev_num, p.packet_num, diff))
        prev_num = p.packet_num

        if prev_rel_ms is not None:
            dt = rel_ms - prev_rel_ms
            if dt > 500:
                time_gaps_ms.append((prev_rel_ms, rel_ms, dt))
        prev_rel_ms = rel_ms

        payload_sizes[len(p.data)] += 1
        parsed = proto.parse_payload(p.data)
        types = []

        if parsed["EEG"]:
            types.append("EEG")
            for sp in parsed["EEG"]:
                eeg_samples += sp["n_samples"]
                tag_counts[sp["tag"]] += 1
        if parsed["ACCGYRO"]:
            types.append("IMU")
            for sp in parsed["ACCGYRO"]:
                imu_samples += sp["n_samples"]
                tag_counts[sp["tag"]] += 1
        if parsed["OPTICS"]:
            types.append("OPTICS")
            for sp in parsed["OPTICS"]:
                optics_samples += sp["n_samples"]
                tag_counts[sp["tag"]] += 1
        if parsed["BATTERY"]:
            types.append("BATTERY")
            battery_count += len(parsed["BATTERY"])
            for sp in parsed["BATTERY"]:
                tag_counts[sp["tag"]] += 1

        if not any([parsed["EEG"], parsed["ACCGYRO"], parsed["OPTICS"], parsed["BATTERY"]]):
            unknown_packets += 1
            if len(p.data) >= 14:
                tag_counts[f"raw_tag_{p.data[9]:02x}"] += 1

        key = "+".join(types) if types else "UNKNOWN"
        type_counts[key] += 1

    duration_s = (rel_times[-1] - rel_times[0]) / 1000.0 if len(rel_times) >= 2 else 0
    total_packets = len(packets)

    if len(rel_times) > 1:
        dts = [rel_times[i] - rel_times[i - 1] for i in range(1, len(rel_times))]
        avg_dt = sum(dts) / len(dts)
        max_dt = max(dts)
        p99_dt = sorted(dts)[int(len(dts) * 0.99)]
    else:
        avg_dt = max_dt = p99_dt = 0

    truncated = len(trailing) > 0 or expected_end != file_size

    print("=" * 60)
    print("b.bin parse report (MUSB v2)")
    print("=" * 60)
    print(f"file_size:      {file_size:,} bytes ({file_size / 1024 / 1024:.2f} MB)")
    print(f"session_start:  {session_start}")
    if packets:
        print(f"first_packet:   {packets[0].timestamp}")
        print(f"last_packet:    {packets[-1].timestamp}")
    print(f"duration:       {duration_s:.1f} s ({duration_s / 60:.2f} min)")
    print(f"packet_count:   {total_packets:,}")
    if duration_s:
        print(f"packet_rate:    {total_packets / duration_s:.1f} pkt/s")
    print(f"avg_interval:   {avg_dt:.1f} ms (max {max_dt} ms, p99 {p99_dt} ms)")
    print()
    print("--- integrity ---")
    print(f"bytes_parsed:   {expected_end:,} / {file_size:,}")
    print(f"trailing:       {len(trailing)} bytes {'TRUNCATED' if trailing else 'OK'}")
    print(f"seq_gaps:       {len(seq_gaps)}")
    for a, b, d in seq_gaps[:5]:
        print(f"  #{a} -> #{b} (gap {d})")
    if len(seq_gaps) > 5:
        print(f"  ... total {len(seq_gaps)}")
    print(f"time_gaps>500ms: {len(time_gaps_ms)}")
    for t0, t1, dt in time_gaps_ms[:8]:
        print(f"  {t0 / 1000:.1f}s -> {t1 / 1000:.1f}s  gap {dt}ms")
    if len(time_gaps_ms) > 8:
        print(f"  ... total {len(time_gaps_ms)}")
    print()
    print("--- decoded sensors ---")
    if duration_s:
        print(f"EEG samples:    {eeg_samples:,}  (~{eeg_samples / duration_s:.0f} Hz)")
        print(f"IMU samples:    {imu_samples:,}  (~{imu_samples / duration_s:.0f} Hz)")
        print(f"Optics samples: {optics_samples:,}  (~{optics_samples / duration_s:.0f} Hz)")
    else:
        print(f"EEG samples:    {eeg_samples:,}")
        print(f"IMU samples:    {imu_samples:,}")
        print(f"Optics samples: {optics_samples:,}")
    print(f"Battery pkts:   {battery_count}")
    print(f"unknown pkts:   {unknown_packets}")
    print()
    print("packet types:")
    for k, v in type_counts.most_common():
        print(f"  {k:30s} {v:6,} ({100 * v / total_packets:.1f}%)")
    print()
    print("TAG distribution:")
    tag_names = {
        0x11: "EEG_4CH",
        0x12: "EEG_8CH",
        0x34: "OPTICS_4",
        0x35: "OPTICS_8",
        0x36: "OPTICS_16",
        0x47: "ACCGYRO",
        0x88: "BATTERY_1",
        0x98: "BATTERY_2",
    }
    for k, v in sorted(tag_counts.items(), key=lambda x: -x[1]):
        name = tag_names.get(k if isinstance(k, int) else None, str(k))
        print(f"  {name:20s} {v:6,}")
    print()
    print("payload sizes (top):")
    for sz, c in payload_sizes.most_common(8):
        print(f"  {sz:3d} bytes: {c:,}")

    print()
    print("=" * 60)
    print("completeness")
    print("=" * 60)
    issues = []
    if truncated:
        issues.append("trailing bytes — possible truncation")
    if seq_gaps:
        issues.append(f"{len(seq_gaps)} sequence number gaps")
    if time_gaps_ms:
        issues.append(f"{len(time_gaps_ms)} time gaps >500ms")
    if duration_s > 0:
        eeg_hz = eeg_samples / duration_s
        if eeg_hz < 200:
            issues.append(f"EEG rate low ({eeg_hz:.0f} Hz, expect ~256)")
        elif eeg_hz > 280:
            issues.append(f"EEG rate high ({eeg_hz:.0f} Hz)")
    if total_packets and unknown_packets > total_packets * 0.01:
        issues.append(f"unknown packets {100 * unknown_packets / total_packets:.1f}%")
    if not total_packets:
        issues.append("no packets")

    if not issues:
        print("OK — data looks complete:")
        print(f"  - valid MUSB v2, no truncation")
        print(f"  - {duration_s / 60:.1f} min, ~{total_packets / duration_s:.0f} pkt/s")
        print(f"  - EEG ~{eeg_samples / duration_s:.0f} Hz, IMU ~{imu_samples / duration_s:.0f} Hz")
    else:
        print("issues:")
        for i, x in enumerate(issues, 1):
            print(f"  {i}. {x}")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(__file__), "..", "b.bin"
    )
    analyze(os.path.abspath(path))
