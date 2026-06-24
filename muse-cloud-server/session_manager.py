"""
Session manager: create, track, and clean up streaming sessions.
"""

import asyncio
import datetime
import logging
import uuid
from typing import Dict, Optional

import config
from session_context import SessionContext
from storage.filestore import FileStoreBackend
from storage.base import StorageBackend

logger = logging.getLogger(__name__)


class SessionManager:
    """
    Manages active WebSocket streaming sessions.

    Responsibilities:
    - Create new sessions when a phone connects
    - Track active sessions by session_id
    - Timeout idle sessions (no data for config.WS_HEARTBEAT_TIMEOUT seconds)
    - Clean up resources on session end
    """

    def __init__(self,
                 file_store: FileStoreBackend,
                 db: Optional[StorageBackend] = None,
                 decoder_factory=None):
        self.file_store = file_store
        self.db = db
        self.decoder_factory = decoder_factory
        self._sessions: Dict[str, SessionContext] = {}
        self._timeout_task: Optional[asyncio.Task] = None

    @property
    def active_count(self) -> int:
        return len(self._sessions)

    def create_session(self,
                       device_name: str = "",
                       device_address: str = "",
                       preset: str = "p1034") -> SessionContext:
        """
        Create a new streaming session.
        Opens the raw binary file and sets up the decoder.
        """
        session_id = uuid.uuid4().hex[:12]  # Short ID for readability

        ctx = SessionContext(
            session_id=session_id,
            device_name=device_name,
            device_address=device_address,
            preset=preset,
        )

        # Instantiate a fresh decoder per session
        if self.decoder_factory:
            ctx.decoder = self.decoder_factory()

        # Open the raw file
        ctx.start(self.file_store)

        # Create DB session record
        if self.db:
            from storage.base import SessionInfo
            asyncio.ensure_future(
                self.db.create_session(SessionInfo(
                    session_id=session_id,
                    device_address=device_address,
                    device_name=device_name,
                    preset=preset,
                    started_at=ctx.session_start,
                ))
            )

        self._sessions[session_id] = ctx
        logger.info("Session %s created (device=%s, preset=%s)",
                    session_id, device_name, preset)

        # Start timeout checker if not already running
        if self._timeout_task is None:
            self._timeout_task = asyncio.ensure_future(self._timeout_loop())

        return ctx

    def get_session(self, session_id: str) -> Optional[SessionContext]:
        return self._sessions.get(session_id)

    async def close_session(self, session_id: str) -> Optional[Dict]:
        """
        Finalize and remove a session. Returns session summary.
        """
        ctx = self._sessions.pop(session_id, None)
        if ctx is None:
            return None

        summary = await ctx.finalize(self.file_store, self.db)
        logger.info("Session %s closed: %d packets, %.0fs",
                    session_id, ctx.packet_count,
                    summary.get('duration_seconds', 0))
        return summary

    async def _timeout_loop(self) -> None:
        """
        Periodically check for sessions that haven't received data
        within the heartbeat timeout.
        """
        while True:
            await asyncio.sleep(15)  # Check every 15 seconds
            now = datetime.datetime.now()
            to_close = []

            for sid, ctx in self._sessions.items():
                if ctx.last_packet_time is None:
                    # No packets received at all; check session age
                    age = (now - ctx.session_start).total_seconds()
                    if age > config.WS_HEARTBEAT_TIMEOUT:
                        to_close.append(sid)
                else:
                    idle = (now - ctx.last_packet_time).total_seconds()
                    if idle > config.WS_HEARTBEAT_TIMEOUT:
                        to_close.append(sid)

            for sid in to_close:
                logger.warning("Session %s timed out", sid)
                await self.close_session(sid)

            if not self._sessions:
                self._timeout_task = None
                break

    async def shutdown(self) -> None:
        """Close all active sessions."""
        if self._timeout_task:
            self._timeout_task.cancel()
            self._timeout_task = None

        for sid in list(self._sessions.keys()):
            await self.close_session(sid)
