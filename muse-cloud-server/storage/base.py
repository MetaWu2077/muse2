"""
Abstract storage backend interface.
Implementations: MySQLBackend, InfluxDBBackend, etc.
"""

from abc import ABC, abstractmethod
from typing import Optional, Dict, List, Any
from dataclasses import dataclass
from datetime import datetime


@dataclass
class SessionInfo:
    session_id: str
    device_address: str = ""
    device_name: str = ""
    preset: str = "p1034"
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    raw_file_path: str = ""
    raw_file_size_bytes: int = 0
    total_packets: int = 0
    journal: Optional[str] = None
    tags: Optional[Dict] = None


class StorageBackend(ABC):
    """Abstract interface for cloud storage backends."""

    @abstractmethod
    async def connect(self) -> None:
        """Initialize connection pool / resources."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Release connection pool / resources."""
        ...

    @abstractmethod
    async def create_session(self, session: SessionInfo) -> None:
        """Insert a new session record."""
        ...

    @abstractmethod
    async def close_session(self, session_id: str, ended_at: datetime,
                            total_packets: int, raw_file_path: str,
                            raw_file_size_bytes: int) -> None:
        """Mark session as ended and update stats."""
        ...

    @abstractmethod
    async def write_heart_rate(self, session_id: str,
                                time: datetime, bpm: float) -> None:
        """Insert or batch a heart rate sample."""
        ...

    @abstractmethod
    async def write_band_power(self, session_id: str, time: datetime,
                                channel: str,
                                delta: float, theta: float,
                                alpha: float, beta: float,
                                gamma: float) -> None:
        """Insert or batch an EEG band power sample."""
        ...

    @abstractmethod
    async def flush_buffers(self, session_id: str) -> None:
        """Flush any batched writes for the given session."""
        ...

    @abstractmethod
    async def get_session(self, session_id: str) -> Optional[Dict]:
        """Retrieve session metadata by ID."""
        ...

    @abstractmethod
    async def list_sessions(self, limit: int = 50,
                            offset: int = 0) -> List[Dict]:
        """List recent sessions."""
        ...

    @abstractmethod
    async def get_heart_rate(self, session_id: str,
                              from_time: Optional[datetime] = None,
                              to_time: Optional[datetime] = None
                              ) -> List[Dict]:
        """Query heart rate time-series for a session."""
        ...

    @abstractmethod
    async def get_band_power(self, session_id: str,
                              channel: Optional[str] = None,
                              from_time: Optional[datetime] = None,
                              to_time: Optional[datetime] = None
                              ) -> List[Dict]:
        """Query EEG band power time-series for a session."""
        ...
