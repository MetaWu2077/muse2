#!/usr/bin/env python3
"""Quick EEG data analyzer — run while osc_lsl_bridge.py is streaming."""
from pylsl import resolve_byprop, StreamInlet
import numpy as np, time
from scipy import signal

streams = resolve_byprop('type', 'EEG', timeout=3)
if not streams:
    print('No LSL stream. Is osc_lsl_bridge.py running?')
    exit(1)

inlet = StreamInlet(streams[0])
# Flush buffered data
while True:
    s, _ = inlet.pull_chunk(timeout=0.1, max_samples=256)
    if not s: break

print('Capturing 5 seconds...')
all_data = []; t0 = time.time()
while time.time() - t0 < 5:
    s, ts = inlet.pull_chunk(timeout=0.2, max_samples=256)
    if s: all_data.extend(s)

arr = np.array(all_data)
elapsed = time.time() - t0
rate = len(arr) / elapsed
print(f'Elapsed: {elapsed:.1f}s  Samples: {len(arr)}  Rate: {rate:.0f} Hz')
print(f'(Nominal: 256 Hz — {rate/256*100:.0f}% of expected)')
print()

for ch, name in enumerate(['TP9','AF7','AF8','TP10','AUX']):
    c = arr[:, ch]
    print(f'{name:5s}: n={len(c):5d}  [{c.min():7.1f} .. {c.max():7.1f}] uV  mean={c.mean():7.1f}  std={c.std():7.1f}')

print()
print('Frequency analysis (AF7, Welch PSD @ 256 Hz):')
ch = 1; c = arr[:, ch] - arr[:, ch].mean()
f, psd = signal.welch(c, fs=256, nperseg=min(256, len(arr)))
for lo, hi, label in [(0.5,4,'Delta'),(4,8,'Theta'),(8,12,'Alpha'),(12,30,'Beta'),(30,45,'Gamma')]:
    mask = (f >= lo) & (f < hi)
    if mask.any():
        pf = f[mask][psd[mask].argmax()]
        pp = psd[mask].max()
        print(f'  {label:6s} ({lo:4.1f}-{hi:2d}Hz): peak at {pf:5.1f}Hz  power={pp:.0f}')
