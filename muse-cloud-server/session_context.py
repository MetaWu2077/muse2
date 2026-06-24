"""
Single streaming session context.
Holds the WebSocket, decoder, metric buffers, and file handle for one session.
"""

import asyncio
import datetime
import time
from typing import Optional, Dict, Any
from dataclasses import dataclass, field

from storage.filestore import FileStoreBackend
from storage.base import StorageBackend


@dataclass
class SessionContext:
    """
    Per-session state for a single streaming connection.

    Lifecycle:
      1. Created when phone connects via WebSocket
      2. Raw .bin file opened immediately
      3. BLE packets arrive → write to file + decode + buffer metrics
      4. Periodic flush writes buffered metrics to DB every ~1 sec
      5. WebSocket close → finalize session
    """

    session_id: str
    device_name: str = ""
    device_address: str = ""
    preset: str = "p1034"

    # Timing
    session_start: datetime.datetime = field(default_factory=datetime.datetime.now)
    last_packet_time: Optional[datetime.datetime] = None

    # Packet accounting
    packet_count: int = 0
    eeg_packets: int = 0
    imu_packets: int = 0
    ppg_packets: int = 0
    decode_errors: int = 0

    # Decoder integration (set after init)
    decoder = None  # MuseRealtimeDecoder from amused-src

    # Metric buffers for batch DB writes
    _heart_rate_buffer: list = field(default_factory=list)     # [(time, bpm), ...]
    _band_power_buffer: list = field(default_factory=list)     # [(time, ch, d,t,a,b,g), ...]

    # EEG sample accumulator for band power FFT (per channel)
    _eeg_accum: dict = field(default_factory=dict)  # {ch: [float, ...]}
    _last_bp_time: float = 0.0  # last band power compute time

    # File storage
    filepath: str = ""

    # Last flush time
    _last_flush: float = 0.0

    # Lock for file writes (serialize across asyncio tasks)
    _file_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # Flush task
    _flush_task: Optional[asyncio.Task] = None

    def start(self, file_store: FileStoreBackend) -> str:
        """Open the session file and return the file path."""
        self.session_start = datetime.datetime.now()
        self.filepath = file_store.open_session(
            self.session_id, self.session_start)
        self._last_flush = time.monotonic()
        return self.filepath

    async def handle_packet(self, data: bytes,
                             file_store: FileStoreBackend,
                             db: Optional[StorageBackend] = None) -> None:
        """
        Process a raw BLE notification payload:
        1. Write to raw .bin file
        2. Decode using MuseRealtimeDecoder
        3. Buffer derived metrics for DB
        """
        now = datetime.datetime.now()
        self.last_packet_time = now
        self.packet_count += 1

        # 1. Write raw packet to binary file (thread-safe)
        async with self._file_lock:
            file_store.write_packet(
                session_id=self.session_id,
                data=data,
                packet_num=self.packet_count,
                timestamp=now,
                session_start=self.session_start
            )

        # 2. Decode
        if self.decoder is not None:
            try:
                decoded = self.decoder.decode(data, now)
                if decoded.eeg:
                    self.eeg_packets += 1
                if decoded.imu:
                    self.imu_packets += 1
                if decoded.ppg:
                    self.ppg_packets += 1

                # Buffer heart rate
                if decoded.heart_rate and 40 < decoded.heart_rate < 200:
                    self._heart_rate_buffer.append((now, decoded.heart_rate))

                # Accumulate EEG samples and compute band power every ~1s
                if decoded.eeg:
                    for ch_name, values in decoded.eeg.items():
                        if ch_name not in self._eeg_accum:
                            self._eeg_accum[ch_name] = []
                        self._eeg_accum[ch_name].extend(values)
                        # Keep last 256 samples (1 second at 256 Hz)
                        if len(self._eeg_accum[ch_name]) > 256:
                            self._eeg_accum[ch_name] = self._eeg_accum[ch_name][-256:]

                    self._eeg_bp_count += 1
                    # Compute once per second (~64 EEG packets at 256 Hz)
                    if self._eeg_bp_count % 64 == 0 and db:
                        bufsize = max((len(v) for v in self._eeg_accum.values()), default=0)
                        logger.debug("[%s] BP attempt #%d, buffer=%d samples",
                                     self.session_id, self._eeg_bp_count, bufsize)
                        band_power = self._compute_band_power(self._eeg_accum)
                        if band_power:
                            logger.info("[%s] BP computed: %d channels",
                                        self.session_id, len(band_power))
                            for ch_name, bands in band_power.items():
                                self._band_power_buffer.append(
                                    (now, ch_name,
                                     bands.get('delta', 0),
                                     bands.get('theta', 0),
                                     bands.get('alpha', 0),
                                     bands.get('beta', 0),
                                     bands.get('gamma', 0))
                                )
                        else:
                            logger.warning("[%s] BP compute returned None (buf=%d)",
                                           self.session_id, bufsize)

            except Exception:
                self.decode_errors += 1

        # 3. Periodic flush to DB
        if db and time.monotonic() - self._last_flush >= 1.0:
            await self._flush_to_db(db)
            self._last_flush = time.monotonic()

    async def _flush_to_db(self, db: StorageBackend) -> None:
        """Batch-write buffered metrics to the database."""
        if not self._heart_rate_buffer and not self._band_power_buffer:
            return

        hr_batch = self._heart_rate_buffer[:]
        bp_batch = self._band_power_buffer[:]
        self._heart_rate_buffer.clear()
        self._band_power_buffer.clear()

        # Write heart rate samples
        for ts, bpm in hr_batch:
            try:
                await db.write_heart_rate(self.session_id, ts, bpm)
            except Exception:
                pass  # Re-buffer would be complex; drop on DB error after retry

        # Write band power samples
        for ts, ch, d, t, a, b, g in bp_batch:
            try:
                await db.write_band_power(self.session_id, ts, ch, d, t, a, b, g)
            except Exception:
                pass

    async def finalize(self, file_store: FileStoreBackend,
                        db: Optional[StorageBackend] = None) -> Dict[str, Any]:
        """
        Close the session: flush remaining buffers, update DB record.
        Called when WebSocket disconnects.
        """
        ended_at = datetime.datetime.now()

        # Flush remaining buffered data
        if db:
            await self._flush_to_db(db)
            await db.flush_buffers(self.session_id)

        # Get file info
        file_info = file_store.get_file_info(self.session_id)

        # Update session record
        if db:
            await db.close_session(
                session_id=self.session_id,
                ended_at=ended_at,
                total_packets=self.packet_count,
                raw_file_path=self.filepath,
                raw_file_size_bytes=file_info.get('file_size_bytes', 0)
            )

        return {
            "session_id": self.session_id,
            "packet_count": self.packet_count,
            "eeg_packets": self.eeg_packets,
            "imu_packets": self.imu_packets,
            "ppg_packets": self.ppg_packets,
            "decode_errors": self.decode_errors,
            "duration_seconds": (ended_at - self.session_start).total_seconds(),
            "file_info": file_info,
        }

    @staticmethod
    def _compute_band_power(eeg_data: Dict[str, list]) -> Optional[Dict[str, Dict[str, float]]]:
        """
        Compute EEG band power from channel data using simple FFT.
        Returns dict of channel -> {delta, theta, alpha, beta, gamma}.
        """
        try:
            import numpy as np
            from scipy.signal import welch

            result = {}
            fs = 256.0

            bands = {
                'delta': (0.5, 4),
                'theta': (4, 8),
                'alpha': (8, 13),
                'beta': (13, 30),
                'gamma': (30, 45),
            }

            for ch_name, values in eeg_data.items():
                if len(values) < 64:
                    logger.debug("BP: %s only %d samples (need 64)", ch_name, len(values))
                    continue
                signal = np.array(values[-256:])
                signal = signal - np.mean(signal)

                freqs, psd = welch(signal, fs, nperseg=min(128, len(signal)))

                ch_bands = {}
                for band_name, (low, high) in bands.items():
                    mask = (freqs >= low) & (freqs < high)
                    if mask.any():
                        try:
                            ch_bands[band_name] = float(np.trapezoid(psd[mask], freqs[mask]))
                        except AttributeError:
                            ch_bands[band_name] = float(np.trapz(psd[mask], freqs[mask]))

                result[ch_name] = ch_bands

            if result:
                logger.info("BP computed: %d channels, alpha: %s",
                            len(result),
                            {ch: f'{b["alpha"]:.1f}' for ch, b in result.items()})
                return result
            logger.warning("BP: result empty (all channels < 64 samples)")
            return None
        except Exception as e:
            logger.error("BP error: %s", e, exc_info=True)
            return None
