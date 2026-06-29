#!/usr/bin/env python3
"""
Muse Cloud Server — FastAPI WebSocket + REST API
Receives raw BLE data from Android phones, stores to MySQL + files,
decodes and serves time-series data via REST API.

Usage:
    1. cp .env.example .env        # First time only
    2. Edit .env with your MySQL credentials
    3. pip install -r requirements.txt
    4. python server.py            # Starts on http://0.0.0.0:8000
"""

import asyncio
import json
import logging
import os
import sys
import struct
import datetime
from contextlib import asynccontextmanager
from typing import Optional

# Ensure amused-src is on the path so we can import the decoder
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'amused-src'))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException
from fastapi.responses import Response, HTMLResponse
from pydantic import BaseModel, Field

import config
from session_manager import SessionManager
from storage.filestore import FileStoreBackend
from storage.base import StorageBackend

# ── Logging ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format='%(asctime)s [%(levelname)-5s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger("muse-server")


# ── Report generation tracking ───────────────────────────────────────
# Prevent concurrent generation of the same report
_ongoing_generations: set = set()


# ── Decoder factory ───────────────────────────────────────────────────
def _generate_report_sync(bin_path: str, report_path: str):
    """Synchronous wrapper for report generation (runs in thread)."""
    from report_generator import generate_report
    try:
        generate_report(bin_path, report_path)
        logger.info("Report generated: %s", report_path)
    finally:
        _ongoing_generations.discard(report_path)


def create_decoder():
    """Create a fresh MuseRealtimeDecoder for each session."""
    try:
        from muse_realtime_decoder import MuseRealtimeDecoder
        return MuseRealtimeDecoder()
    except ImportError:
        logger.warning("muse_realtime_decoder not found — metrics extraction disabled")
        return None


# ── Storage ───────────────────────────────────────────────────────────
file_store = FileStoreBackend(config.STORAGE_DIR)
db: Optional[StorageBackend] = None


async def get_db() -> Optional[StorageBackend]:
    """Connect to MySQL if credentials are configured."""
    global db
    if db is not None:
        return db

    if not config.MYSQL_PASSWORD:
        logger.info("MySQL: no password set — running in file-only mode")
        logger.info("  Set MYSQL_PASSWORD in .env to enable database storage.")
        return None

    try:
        from storage.mysql_backend import MySQLBackend
        backend = MySQLBackend(
            host=config.MYSQL_HOST,
            port=config.MYSQL_PORT,
            user=config.MYSQL_USER,
            password=config.MYSQL_PASSWORD,
            database=config.MYSQL_DATABASE,
            pool_size=config.MYSQL_POOL_SIZE,
        )
        await backend.connect()
        db = backend
        logger.info("MySQL: connected — database storage ENABLED")
        return db
    except Exception as e:
        logger.warning("MySQL: connection failed — running in file-only mode")
        logger.warning("  Error: %s", e)
        logger.warning("  Check MUSE_MYSQL_* settings in your .env file.")
        return None


# ── Session manager ───────────────────────────────────────────────────
session_manager = SessionManager(
    file_store=file_store,
    db=None,  # Lazy init in lifespan
    decoder_factory=create_decoder,
)


# ── App lifecycle ─────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown."""

    # ═══════════════════════════════════════════════════════════════
    #  STARTUP
    # ═══════════════════════════════════════════════════════════════
    os.makedirs(config.STORAGE_DIR, exist_ok=True)

    print()
    print("=" * 54)
    print("  Muse Cloud Server v1.0")
    print("=" * 54)
    print(f"  Listen:   {config.HOST}:{config.PORT}")
    print(f"  Storage:  {os.path.abspath(config.STORAGE_DIR)}")
    print("-" * 54)

    # DB: try to connect in background (don't block startup)
    if config.MYSQL_PASSWORD:
        print(f"  MySQL:    connecting to {config.MYSQL_HOST}:{config.MYSQL_PORT}/{config.MYSQL_DATABASE} ...")
        asyncio.ensure_future(_connect_db_async())
    else:
        print(f"  MySQL:    not configured (file-only mode)")
        print(f"            To enable: set MUSE_MYSQL_PASSWORD in .env")

    print("-" * 54)
    print(f"  Dashboard: http://{config.HOST}:{config.PORT}/dashboard")
    print(f"  REST API:  http://{config.HOST}:{config.PORT}/health")
    print(f"  WebSocket: ws://{config.HOST}:{config.PORT}/ws/session")
    print("=" * 54)
    print()

    yield

    # ═══════════════════════════════════════════════════════════════
    #  SHUTDOWN
    # ═══════════════════════════════════════════════════════════════
    logger.info("Shutting down...")
    await session_manager.shutdown()
    if db:
        await db.close()
    logger.info("Server stopped.")


async def _connect_db_async():
    """Connect to MySQL in background. Updates session_manager when ready."""
    global db
    try:
        database = await get_db()
        if database:
            session_manager.db = database
            logger.info("MySQL now available — database storage ENABLED")
            print(f"\n  >>> MySQL connected: {config.MYSQL_HOST}:{config.MYSQL_PORT}/{config.MYSQL_DATABASE}\n")
        else:
            logger.warning("MySQL connection failed — running file-only mode")
            print(f"\n  >>> MySQL connection failed. Running in file-only mode.\n")
    except Exception as e:
        logger.error("MySQL background connect error: %s", e)


app = FastAPI(
    title="Muse Cloud Server",
    version="1.0.0",
    lifespan=lifespan,
)


# ══════════════════════════════════════════════════════════════════════
# WebSocket — Primary data channel
# ══════════════════════════════════════════════════════════════════════

@app.websocket("/ws/session")
async def ws_session(ws: WebSocket):
    """
    Phone → Cloud streaming endpoint.

    Protocol:
      1. Server accepts connection and sends {"type":"connected"}
      2. Phone may send heartbeat: {"type":"heartbeat"} → pong
      3. On GO, phone sends hello: {"type":"hello","device":"MuseS-xxx","preset":"p1034"}
      4. Server responds: {"type":"hello_ack","session_id":"abc123",...}
      5. Phone sends binary frames at ~100/sec
      6. Phone sends session_end when meditation finishes
      7. Phone may send another hello to start a new session on the same connection
    """
    async def _start_session_from_hello(hello: dict):
        nonlocal session_ctx
        if session_ctx is not None:
            await session_manager.close_session(session_ctx.session_id)
            session_ctx = None

        device_name = hello.get("device", "Unknown")
        device_address = hello.get("address", "")
        preset = hello.get("preset", "p1034")

        ctx = session_manager.create_session(
            device_name=device_name,
            device_address=device_address,
            preset=preset,
        )

        ack = {
            "type": "hello_ack",
            "session_id": ctx.session_id,
            "server_time": datetime.datetime.now().isoformat(),
        }
        await ws.send_text(json.dumps(ack))
        logger.info("[%s] streaming started (device=%s)", ctx.session_id, device_name)
        return ctx

    await ws.accept()
    session_ctx = None

    # Idle connection — session starts when phone sends hello (on GO).
    await ws.send_text(json.dumps({
        "type": "connected",
        "server_time": datetime.datetime.now().isoformat(),
    }))

    try:
        # ── Main receive loop ──
        while True:
            data = await ws.receive()

            if "text" in data:
                msg = json.loads(data["text"])
                msg_type = msg.get("type", "")

                if msg_type == "hello":
                    session_ctx = await _start_session_from_hello(msg)

                elif msg_type == "heartbeat":
                    await ws.send_text(json.dumps({"type": "pong"}))

                elif msg_type == "session_end":
                    if session_ctx:
                        ended_id = session_ctx.session_id
                        summary = await session_manager.close_session(ended_id)
                        session_ctx = None
                        await ws.send_text(json.dumps({
                            "type": "session_ended",
                            "session_id": ended_id,
                        }))
                        if summary:
                            logger.info("[%s] ended by client: %d pkts in %.0fs",
                                       ended_id,
                                       summary['packet_count'],
                                       summary['duration_seconds'])
                            bin_path = summary.get('file_info', {}).get('filepath', '')
                            if bin_path and os.path.exists(bin_path):
                                report_path = bin_path.replace('.bin', '.report.html')
                                if report_path not in _ongoing_generations:
                                    _ongoing_generations.add(report_path)
                                    try:
                                        asyncio.ensure_future(
                                            asyncio.to_thread(
                                                _generate_report_sync, bin_path, report_path
                                            )
                                        )
                                    except Exception as e:
                                        _ongoing_generations.discard(report_path)
                                        logger.warning("[%s] report generation failed: %s",
                                                        ended_id, e)

                elif msg_type == "session_journal":
                    sid = (msg.get("session_id") or "").strip()
                    journal = (msg.get("journal") or "").strip()
                    if sid and journal:
                        ok = await session_manager.update_journal(sid, journal)
                        if ok:
                            await ws.send_text(json.dumps({
                                "type": "journal_ack",
                                "session_id": sid,
                            }))
                        else:
                            await ws.send_text(json.dumps({
                                "type": "journal_error",
                                "session_id": sid,
                                "error": "session not found",
                            }))
                    else:
                        await ws.send_text(json.dumps({
                            "type": "journal_error",
                            "error": "session_id and journal required",
                        }))

                elif msg_type == "status":
                    logger.debug("[%s] status: %s",
                                 session_ctx.session_id if session_ctx else "?", msg)

            elif "bytes" in data:
                raw = data["bytes"]
                if len(raw) < 21 or session_ctx is None:
                    continue

                frame_type = raw[0]
                if frame_type != 0x01:
                    continue

                # Parse session_id from frame
                sid = raw[1:17].rstrip(b' ').decode('utf-8', errors='replace')
                if sid != session_ctx.session_id:
                    continue

                # seq_num (informational — logged for gap detection)
                seq_num = struct.unpack('>I', raw[17:21])[0]

                # Raw BLE payload
                ble_payload = raw[21:]

                await session_ctx.handle_packet(
                    data=ble_payload,
                    file_store=file_store,
                    db=session_manager.db,
                )

    except WebSocketDisconnect:
        logger.info("[%s] disconnected", session_ctx.session_id if session_ctx else "?")
    except RuntimeError as e:
        # Starlette raises RuntimeError when receiving after disconnect
        if "disconnect" in str(e).lower():
            logger.info("[%s] disconnected", session_ctx.session_id if session_ctx else "?")
        else:
            logger.exception("[%s] runtime error: %s",
                             session_ctx.session_id if session_ctx else "?", e)
    except Exception as e:
        logger.exception("[%s] error: %s",
                         session_ctx.session_id if session_ctx else "?", e)
    finally:
        if session_ctx:
            summary = await session_manager.close_session(session_ctx.session_id)
            if summary:
                logger.info("[%s] done: %d pkts in %.0fs",
                           summary['session_id'],
                           summary['packet_count'],
                           summary['duration_seconds'])
                # Auto-generate report after session ends
                bin_path = summary.get('file_info', {}).get('filepath', '')
                if bin_path and os.path.exists(bin_path):
                    report_path = bin_path.replace('.bin', '.report.html')
                    if report_path not in _ongoing_generations:
                        _ongoing_generations.add(report_path)
                        try:
                            asyncio.ensure_future(
                                asyncio.to_thread(
                                    _generate_report_sync, bin_path, report_path
                                )
                            )
                            logger.info("[%s] report generation started", session_ctx.session_id)
                        except Exception as e:
                            _ongoing_generations.discard(report_path)
                            logger.warning("[%s] report generation failed: %s",
                                          session_ctx.session_id, e)


# ── Helper: auto-refresh HTML pages for pending reports ──────────────
def _waiting_html(session_id: str, title: str, message: str,
                  icon_html: str = "", refresh_sec: int = 3) -> HTMLResponse:
    """Return an auto-refreshing page shown while report is not ready."""
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta http-equiv="refresh" content="{refresh_sec}">
<title>{title}</title>
<style>
body {{ background:#0f1923; color:#e0e0e0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       display:flex; justify-content:center; align-items:center; height:100vh; margin:0; }}
.card {{ text-align:center; max-width:480px; padding:32px; }}
.spinner {{ width:40px; height:40px; border:3px solid #2a3f4f; border-top-color:#4fc3f7;
           border-radius:50%; animation:spin 1s linear infinite; margin:0 auto 16px; }}
.pulse {{ width:16px; height:16px; background:#ef5350; border-radius:50%;
         margin:0 auto 16px; animation:pulse 1.5s ease-in-out infinite; }}
@keyframes spin {{ to {{ transform:rotate(360deg); }} }}
@keyframes pulse {{ 0%,100% {{ opacity:1; }} 50% {{ opacity:0.3; }} }}
h2 {{ color:#e0e0e0; margin-bottom:8px; }}
p {{ color:#78909c; margin:4px 0; }}
.note {{ color:#546e7a; font-size:12px; margin-top:16px; }}
</style></head>
<body><div class="card">
{icon_html}
<h2>{title}</h2>
<p>{message}</p>
<p class="note">此页面将自动刷新 · Auto-refreshing every {refresh_sec}s</p>
</div></body></html>""", status_code=202)


# ══════════════════════════════════════════════════════════════════════
# REST API
# ══════════════════════════════════════════════════════════════════════

@app.get("/api/sessions/{session_id}/report")
async def view_report(session_id: str):
    """View the HTML report for a session.

    Behavior:
    - Report .html exists → serve it
    - Report being generated → show auto-refresh "generating" page
    - Session still active → show auto-refresh "recording" page
    - .bin exists, session ended → generate on-demand
    - No .bin file → 404
    """
    report_path = os.path.join(config.STORAGE_DIR, f"{session_id}.report.html")
    bin_path = os.path.join(config.STORAGE_DIR, f"{session_id}.bin")

    # Report already generated — serve it
    if os.path.exists(report_path):
        with open(report_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())

    # No .bin file at all — invalid session
    if not os.path.exists(bin_path):
        raise HTTPException(404, "Session not found. No data file exists for this session.")

    # Report is currently being generated (async, from session close) — auto-refresh
    if report_path in _ongoing_generations:
        return _waiting_html(
            session_id,
            "正在生成报告...",
            f"Generating report for session {session_id}",
            '<div class="spinner"></div>',
            refresh_sec=3,
        )

    # Session still active (WebSocket connected) — auto-refresh
    ctx = session_manager.get_session(session_id)
    if ctx is not None:
        return _waiting_html(
            session_id,
            "会话正在录制中",
            f"设备 {ctx.device_name} 正在流式传输数据，报告将在会话结束后自动生成。",
            '<div class="pulse"></div>',
            refresh_sec=5,
        )

    # Generate report on-demand (runs in thread pool to avoid blocking)
    _ongoing_generations.add(report_path)
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _generate_report_sync, bin_path, report_path)
    except Exception as e:
        # Note: _generate_report_sync's finally already removed from set,
        # but guard against double-remove
        _ongoing_generations.discard(report_path)
        logger.exception("On-demand report generation failed: %s", e)
        raise HTTPException(500, f"Report generation failed: {e}")

    if not os.path.exists(report_path):
        _ongoing_generations.discard(report_path)
        raise HTTPException(500, "Report generation completed but file not found.")

    with open(report_path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/health")
async def health():
    """Server health check."""
    return {
        "status": "ok",
        "active_sessions": session_manager.active_count,
        "db_connected": session_manager.db is not None,
    }


@app.get("/dashboard")
async def dashboard():
    """Web dashboard for viewing and analyzing session data."""
    dash_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    try:
        with open(dash_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        raise HTTPException(404, "dashboard.html not found")


@app.get("/api/sessions")
async def list_sessions(limit: int = Query(200, ge=1, le=1000),
                        offset: int = Query(0, ge=0)):
    """List sessions from MySQL + filesystem, with live stats for active ones."""

    # 1. Query from MySQL (if available)
    db_sessions = {}
    if session_manager.db is not None:
        db_rows = await session_manager.db.list_sessions(limit=5000, offset=0)
        for r in db_rows:
            db_sessions[r["session_id"]] = r

    # 2. Scan local .bin files
    import glob as _glob
    file_sessions = {}
    for f in sorted(_glob.glob(os.path.join(config.STORAGE_DIR, "*.bin")),
                    key=lambda x: os.path.getmtime(x), reverse=True):
        sid = os.path.splitext(os.path.basename(f))[0]
        size = os.path.getsize(f)
        mtime = os.path.getmtime(f)
        file_sessions[sid] = {
            "session_id": sid,
            "device_name": "",
            "device_address": "",
            "preset": "?",
            "started_at": datetime.datetime.fromtimestamp(mtime).isoformat(),
            "ended_at": None,
            "total_packets": 0,
            "raw_file_path": f,
            "raw_file_size_bytes": size,
        }

    # 3. Merge: DB records take priority, fill gaps from filesystem
    all_ids = set(db_sessions.keys()) | set(file_sessions.keys())
    sessions = []
    for sid in all_ids:
        if sid in db_sessions:
            s = db_sessions[sid]
            # Enrich with file size from filesystem if available
            if sid in file_sessions:
                s["raw_file_size_bytes"] = file_sessions[sid]["raw_file_size_bytes"]
                s.setdefault("raw_file_path", file_sessions[sid]["raw_file_path"])
            else:
                s.setdefault("raw_file_size_bytes", 0)
        else:
            s = file_sessions[sid]
        sessions.append(s)

    # Sort by started_at descending
    sessions.sort(key=lambda x: x.get("started_at", ""), reverse=True)

    # 4. Merge live stats for currently active sessions
    for s in sessions:
        ctx = session_manager.get_session(s["session_id"])
        if ctx is not None:
            s["total_packets"] = ctx.packet_count
            s["eeg_packets"] = ctx.eeg_packets
            s["imu_packets"] = ctx.imu_packets
            s["ppg_packets"] = ctx.ppg_packets
            s["raw_file_size_bytes"] = os.path.getsize(ctx.filepath) if ctx.filepath and os.path.exists(ctx.filepath) else s.get("raw_file_size_bytes", 0)
            s["active"] = True
        else:
            s["active"] = False

    return sessions[offset:offset+limit]


@app.get("/api/sessions/{session_id}/live")
async def get_live_stats(session_id: str):
    """Get live stats for an active session."""
    ctx = session_manager.get_session(session_id)
    if ctx is None:
        raise HTTPException(404, "Session not active or not found")

    # Calculate current file size
    file_size = 0
    if ctx.filepath and os.path.exists(ctx.filepath):
        file_size = os.path.getsize(ctx.filepath)

    duration_sec = (datetime.datetime.now() - ctx.session_start).total_seconds()

    return {
        "session_id": session_id,
        "active": True,
        "packet_count": ctx.packet_count,
        "eeg_packets": ctx.eeg_packets,
        "imu_packets": ctx.imu_packets,
        "ppg_packets": ctx.ppg_packets,
        "decode_errors": ctx.decode_errors,
        "duration_seconds": round(duration_sec, 1),
        "file_size_bytes": file_size,
        "device_name": ctx.device_name,
        "device_address": ctx.device_address,
        "preset": ctx.preset,
        "started_at": ctx.session_start.isoformat(),
        "last_packet_at": ctx.last_packet_time.isoformat() if ctx.last_packet_time else None,
    }


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    """Session metadata + stats."""
    if session_manager.db is not None:
        s = await session_manager.db.get_session(session_id)
        if s:
            if not s.get("journal"):
                sidecar = _read_journal_sidecar(session_id)
                if sidecar:
                    s["journal"] = sidecar
            return s

    # File-only mode: read .bin file info
    filepath = os.path.join(config.STORAGE_DIR, f"{session_id}.bin")
    if not os.path.exists(filepath):
        raise HTTPException(404, "Session not found")
    size = os.path.getsize(filepath)
    mtime = os.path.getmtime(filepath)
    return {
        "session_id": session_id,
        "device_name": "(file-only mode)",
        "device_address": "",
        "preset": "?",
        "started_at": datetime.datetime.fromtimestamp(mtime).isoformat(),
        "ended_at": None,
        "total_packets": 0,
        "raw_file_path": filepath,
        "raw_file_size_bytes": size,
        "journal": _read_journal_sidecar(session_id),
    }


class SessionJournalBody(BaseModel):
    journal: str = Field(..., min_length=1, max_length=20000)


@app.post("/api/sessions/{session_id}/journal")
async def post_session_journal(session_id: str, body: SessionJournalBody):
    """Attach user meditation notes to a completed session."""
    ok = await session_manager.update_journal(session_id, body.journal)
    if not ok:
        raise HTTPException(404, "Session not found or journal empty")
    return {"session_id": session_id, "ok": True}


def _read_journal_sidecar(session_id: str) -> Optional[str]:
    path = os.path.join(config.STORAGE_DIR, f"{session_id}.journal.txt")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


@app.get("/api/sessions/{session_id}/heartrate")
async def get_heart_rate(session_id: str,
                         from_time: Optional[str] = Query(None),
                         to_time: Optional[str] = Query(None)):
    """Heart rate time-series for a session."""
    if session_manager.db is None:
        raise HTTPException(503, "Database not available")
    from_dt = datetime.datetime.fromisoformat(from_time) if from_time else None
    to_dt = datetime.datetime.fromisoformat(to_time) if to_time else None
    rows = await session_manager.db.get_heart_rate(session_id, from_dt, to_dt)
    return [{"time": r[0].isoformat(), "bpm": r[1]} for r in rows]


@app.get("/api/sessions/{session_id}/bandpower")
async def get_band_power(session_id: str,
                         channel: Optional[str] = Query(None),
                         from_time: Optional[str] = Query(None),
                         to_time: Optional[str] = Query(None)):
    """EEG band power time-series."""
    if session_manager.db is None:
        raise HTTPException(503, "Database not available")
    from_dt = datetime.datetime.fromisoformat(from_time) if from_time else None
    to_dt = datetime.datetime.fromisoformat(to_time) if to_time else None
    rows = await session_manager.db.get_band_power(
        session_id, channel, from_dt, to_dt)
    return [{
        "time": r[0].isoformat(), "channel": r[1],
        "delta": r[2], "theta": r[3], "alpha": r[4],
        "beta": r[5], "gamma": r[6],
    } for r in rows]


@app.get("/api/sessions/{session_id}/raw")
async def download_raw(session_id: str):
    """Download the raw MUSB .bin file."""
    content = file_store.read_raw_file(session_id)
    if content is None:
        raise HTTPException(404, "Raw file not found")
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename={session_id}.bin"}
    )


# ══════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn

    # Kill any old process holding our port (Windows port-release delay workaround)
    if os.name == 'nt':
        import subprocess
        try:
            result = subprocess.run(
                ['powershell', '-Command',
                 f"Get-NetTCPConnection -LocalPort {config.PORT} -ErrorAction SilentlyContinue "
                 f"| ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force }}"],
                capture_output=True, timeout=10
            )
        except Exception:
            pass

    uvicorn.run(
        "server:app",
        host=config.HOST,
        port=config.PORT,
        log_level=config.LOG_LEVEL,
        ws_max_size=2**20,
    )
