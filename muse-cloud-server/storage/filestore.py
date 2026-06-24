"""
Raw MUSB .bin file storage for session recordings.
Writes in the same binary format as muse_raw_stream.py so files
can be replayed with muse_replay.py.
"""

import os
import struct
import datetime
from pathlib import Path
from typing import Optional, Dict


class FileStoreBackend:
    """
    Manages raw MUSB .bin files for sessions.

    File format (from muse_raw_stream.py):
      [magic: 'MUSB'(4)] [version: 1B] [start_timestamp_ms: Q(8)]
      [reserved: 16B]
      Per packet: [packet_num: H(2)] [relative_ms: I(4)] [type: B(1)]
                  [size: H(2)] [data: N bytes]
    """

    def __init__(self, storage_dir: str = "./muse_sessions"):
        self.storage_dir = Path(storage_dir)
        os.makedirs(self.storage_dir, exist_ok=True)

    def _session_path(self, session_id: str) -> Path:
        return self.storage_dir / f"{session_id}.bin"

    def open_session(self, session_id: str,
                     session_start: Optional[datetime.datetime] = None) -> str:
        """
        Open a new binary file for the session. Returns the file path.
        """
        if session_start is None:
            session_start = datetime.datetime.now()

        filepath = self._session_path(session_id)
        fh = open(filepath, 'wb')

        # Header
        magic = b'MUSB'
        version = 2
        start_ms = int(session_start.timestamp() * 1000)

        header = struct.pack('<4sBQ16s',
                             magic,
                             version,
                             start_ms,
                             b'\x00' * 16)
        fh.write(header)
        fh.flush()
        fh.close()

        return str(filepath)

    def write_packet(self, session_id: str, data: bytes,
                     packet_num: int,
                     timestamp: Optional[datetime.datetime] = None,
                     session_start: Optional[datetime.datetime] = None) -> None:
        """
        Append a single BLE notification packet to the session file.
        Opens in append mode each time (safe across coroutines if serialized).
        """
        filepath = self._session_path(session_id)

        if timestamp is None:
            timestamp = datetime.datetime.now()

        if session_start is None:
            session_start = datetime.datetime.fromtimestamp(
                os.path.getctime(filepath))

        relative_ms = int((timestamp - session_start).total_seconds() * 1000)
        packet_type = data[0] if data else 0xFF

        with open(filepath, 'ab') as fh:
            header = struct.pack('<HIBH',
                                 packet_num & 0xFFFF,
                                 relative_ms,
                                 packet_type,
                                 len(data))
            fh.write(header + data)
            if packet_num % 100 == 0:
                fh.flush()

    def get_file_info(self, session_id: str) -> Dict:
        """Get size info about a session's raw file."""
        filepath = self._session_path(session_id)
        if not filepath.exists():
            return {"exists": False}
        size = filepath.stat().st_size
        return {
            "exists": True,
            "filepath": str(filepath),
            "file_size_bytes": size,
            "file_size_mb": size / (1024 * 1024),
        }

    def read_raw_file(self, session_id: str) -> Optional[bytes]:
        """Return the entire raw .bin file content."""
        filepath = self._session_path(session_id)
        if not filepath.exists():
            return None
        return filepath.read_bytes()
