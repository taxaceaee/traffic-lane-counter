"""WebSocket API — authenticated live streaming with heartbeat + Redis pub/sub.

24/7 operational hardening:
- Per-token connection limits (max 5 concurrent per user)
- Global connection limit (max 500)
- Heartbeat timeout (auto-disconnect idle clients after 90s)
- Redis pub/sub listener shared across clients per camera
- Clean disconnection logging
"""

import asyncio
import logging
import os
import time
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from tf_common.safe_path import validate_identifier
from tf_common.serializer import dumps, loads

logger = logging.getLogger("trafficflow.ws")

router = APIRouter(tags=["ws"])

_MAX_CONNECTIONS_PER_USER = 5
_MAX_GLOBAL_CONNECTIONS = 500
_AUTH_TIMEOUT = 10.0


def _heartbeat_interval() -> float:
    return float(os.getenv("WS_HEARTBEAT_INTERVAL", "30.0"))


def _heartbeat_timeout() -> float:
    return float(os.getenv("WS_HEARTBEAT_TIMEOUT", "90.0"))

_connections: dict[str, list[WebSocket]] = {}
_user_connections: dict[str, int] = {}
_ws_cameras: dict[int, set[str]] = {}
_global_count = 0

# Redis pub/sub — one async listener thread per unique channel subscription
_redis_listeners: dict[str, dict[str, Any]] = {}
_redis_lock = asyncio.Lock()
_state_lock = asyncio.Lock()
_event_loop: asyncio.AbstractEventLoop | None = None


async def _verify_token(token: str) -> tuple[bool, str]:
    try:
        from tf_api.api.routes_auth import decode_access_token
        payload = decode_access_token(token)
        return True, f"{payload.get('sub', 'unknown')}|{payload.get('role', 'viewer')}"
    except Exception:
        return False, ""


async def _send_json(ws: WebSocket, data: dict[str, Any]) -> None:
    try:
        await ws.send_text(dumps(data))
    except (ConnectionError, OSError, ValueError):
        logger.debug("WebSocket send failed because the client disconnected")


async def _try_close(ws: WebSocket, code: int = 4401) -> None:
    with suppress(ConnectionError, OSError, RuntimeError):
        await ws.close(code=code)


@router.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    global _global_count, _event_loop
    origin = ws.headers.get("origin")
    allowed_origins = {
        value.strip()
        for value in os.getenv("WS_ALLOWED_ORIGINS", os.getenv("CORS_ORIGINS", "")).split(",")
        if value.strip()
    }
    if origin and allowed_origins and origin not in allowed_origins:
        await _try_close(ws, 4403)
        return
    await ws.accept()
    _event_loop = asyncio.get_running_loop()

    if _global_count >= _MAX_GLOBAL_CONNECTIONS:
        await _send_json(ws, {"error": "Server full — too many connections"})
        await _try_close(ws, 4401)
        return

    authenticated = False
    username = "unknown"
    role = "viewer"
    cameras: list[str] = []
    camera_key = "global"

    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=_AUTH_TIMEOUT)
        msg = loads(raw)
    except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
        await _send_json(ws, {"error": "Auth timeout or invalid message"})
        await _try_close(ws, 4401)
        return

    token = msg.get("token") if isinstance(msg, dict) else None
    if token:
        ok, identity = await _verify_token(token)
        if ok:
            username, role = identity.split("|", 1)
            authenticated = True

    if not authenticated:
        await _send_json(ws, {"error": "Authentication required"})
        await _try_close(ws, 4401)
        return

    user_conns = _user_connections.get(username, 0)
    if user_conns >= _MAX_CONNECTIONS_PER_USER:
        await _send_json(ws, {"error": f"Max {_MAX_CONNECTIONS_PER_USER} connections per user"})
        await _try_close(ws, 4401)
        return

    cameras_param = msg.get("cameras", "") if isinstance(msg, dict) else ""
    if cameras_param:
        cameras = [c.strip() for c in cameras_param.split(",") if c.strip()]
        if len(cameras) > 32:
            await _send_json(ws, {"error": "Too many camera subscriptions"})
            await _try_close(ws, 4400)
            return
        try:
            for camera_id in cameras:
                validate_identifier(camera_id, name="camera_id")
        except ValueError:
            await _send_json(ws, {"error": "Invalid camera subscription"})
            await _try_close(ws, 4400)
            return
        camera_key = ",".join(cameras) if cameras else "global"
    elif role not in {"admin", "Administrator"}:
        await _send_json(ws, {"error": "A camera subscription is required"})
        await _try_close(ws, 4403)
        return

    async with _state_lock:
        if _global_count >= _MAX_GLOBAL_CONNECTIONS:
            await _try_close(ws, 4401)
            return
        _connections.setdefault(camera_key, []).append(ws)
        _ws_cameras[id(ws)] = set(cameras)
        _user_connections[username] = _user_connections.get(username, 0) + 1
        _global_count += 1

    logger.info(
        "WS client connected (user=%s, cameras=%s, total=%d, global=%d)",
        username, camera_key, len(_connections[camera_key]), _global_count,
    )

    await _send_json(ws, {"type": "connected", "cameras": cameras, "client_id": id(ws)})

    # Start Redis pub/sub listener for each subscribed camera
    redis_channels: list[str] = []
    if os.getenv("REDIS_HOST"):
        for cam in cameras:
            channel = await _ensure_redis_listener(cam)
            if channel is not None:
                redis_channels.append(channel)

    # If Redis is not available, fall back to in-process LiveEventBus
    from tf_common.live_bus import AsyncLiveEventBus

    live_bus = AsyncLiveEventBus()
    # Redis is the cross-process source of truth.  Use the in-process bus only
    # when Redis is intentionally disabled, otherwise every event is duplicated.
    if not os.getenv("REDIS_HOST"):
        live_bus.subscribe(cameras if cameras else None)

    # Heartbeat + message receive loop (single task, no separate ping task)
    last_pong = time.monotonic()

    try:
        while True:
            # ── Check in-process LiveEventBus for events ──────────────
            bus_event = await live_bus.get(timeout=_heartbeat_interval())
            if bus_event is not None:
                camera_id, bus_data = bus_event
                # Forward to this client
                try:
                    await ws.send_text(dumps(bus_data))
                except (ConnectionError, OSError, ValueError):
                    break
                continue  # go back and check for more events

            # ── No event from LiveEventBus → heartbeat / user message ─
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=0.1)
                try:
                    msg = loads(raw)
                    if msg.get("type") == "pong":
                        last_pong = time.monotonic()
                        continue
                except (ValueError, TypeError):
                    pass
            except asyncio.TimeoutError:
                elapsed = time.monotonic() - last_pong
                if elapsed >= _heartbeat_timeout():
                    logger.info("WS heartbeat timeout (%.1fs) — closing", elapsed)
                    with suppress(ConnectionError, OSError, RuntimeError):
                        await ws.close(code=4401)
                    break
                try:
                    await ws.send_text(dumps({"type": "ping"}))
                except (ConnectionError, OSError, ValueError):
                    break
            except (WebSocketDisconnect, RuntimeError):
                # A client may close between receive/send operations. Do not
                # let that disconnect escape into ASGI or kill the shared
                # Redis listener for other subscribed clients.
                break
                continue
    except WebSocketDisconnect:
        pass
    finally:
        async with _state_lock:
            conns = _connections.get(camera_key, [])
            if ws in conns:
                conns.remove(ws)
            if not conns:
                _connections.pop(camera_key, None)
            _ws_cameras.pop(id(ws), None)
            _user_connections[username] = max(_user_connections.get(username, 0) - 1, 0)
            _global_count = max(_global_count - 1, 0)
        for channel in redis_channels:
            await _release_redis_listener(channel)
        live_bus.close()
        logger.info(
            "WS client disconnected (user=%s, cameras=%s, global=%d)",
            username, camera_key, _global_count,
        )


async def _ensure_redis_listener(camera_id: str) -> str | None:
    """Start a background Redis pub/sub listener for a camera if not already running.
    Returns the channel key so disconnect can release via refcount."""
    channel = f"traffic:live:{camera_id}"
    async with _redis_lock:
        if channel in _redis_listeners:
            _redis_listeners[channel]["refcount"] += 1
            return channel
        try:
            task = asyncio.create_task(_redis_sub_loop(channel))
            _redis_listeners[channel] = {"task": task, "refcount": 1}
            return channel
        except Exception:
            return None


async def _release_redis_listener(channel: str) -> None:
    async with _redis_lock:
        entry = _redis_listeners.get(channel)
        if entry is None:
            return
        entry["refcount"] -= 1
        if entry["refcount"] > 0:
            return
        task = entry["task"]
        _redis_listeners.pop(channel, None)
    task.cancel()


async def _redis_sub_loop(channel: str) -> None:
    """Listen for messages on a Redis channel and broadcast to WS clients."""
    try:
        import redis.asyncio as aioredis
        while True:
            try:
                r = aioredis.Redis(
                    host=os.getenv("REDIS_HOST", "localhost"),
                    port=int(os.getenv("REDIS_PORT", "6379")),
                    db=0,
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=30,
                )
                async with r.pubsub() as pubsub:
                    await pubsub.subscribe(channel)
                    async for message in pubsub.listen():
                        if message.get("type") != "message":
                            continue
                        data = message.get("data", "")
                        if not data:
                            continue
                        try:
                            payload = loads(data)
                            await broadcast_raw(payload)
                        except (ValueError, TypeError):
                            logger.debug("Redis sub: skipping non-JSON message on %s", channel)
            except asyncio.CancelledError:
                raise
            except (ConnectionError, OSError, ImportError) as exc:
                logger.warning("Redis pub/sub unavailable for %s: %s; retrying", channel, type(exc).__name__)
            except Exception:
                logger.warning("Redis pub/sub listener failed for %s; retrying", channel, exc_info=True)
            await asyncio.sleep(2.0)
    except asyncio.CancelledError:
        pass
    finally:
        async with _redis_lock:
            entry = _redis_listeners.get(channel)
            if entry is not None and entry.get("task") is asyncio.current_task():
                _redis_listeners.pop(channel, None)


async def broadcast_raw(data: dict[str, Any]) -> None:
    """Broadcast a raw dict to all WS clients subscribed to a matching camera."""
    cameras = data.get("cameras") or [data.get("camera_id", "")]
    message = dumps(data)
    for _key, conns in list(_connections.items()):
        for ws in list(conns):
            subscribed = _ws_cameras.get(id(ws), set())
            if subscribed and not any(cam in subscribed for cam in cameras if cam):
                continue
            try:
                await ws.send_text(message)
            except (ConnectionError, OSError, ValueError, RuntimeError, WebSocketDisconnect):
                with suppress(Exception):
                    conns.remove(ws)
                _ws_cameras.pop(id(ws), None)


def broadcast_raw_sync(data: dict[str, Any]) -> None:
    """Synchronous convenience wrapper for raw broadcast payloads."""
    loop = _event_loop
    if loop is not None and loop.is_running():
        asyncio.run_coroutine_threadsafe(broadcast_raw(data), loop)


def get_connection_stats() -> dict[str, Any]:
    return {
        "global_connections": _global_count,
        "max_global": _MAX_GLOBAL_CONNECTIONS,
        "user_connections": dict(_user_connections),
        "camera_subscriptions": {k: len(v) for k, v in _connections.items()},
    }
