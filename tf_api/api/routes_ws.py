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
_global_count = 0

# Redis pub/sub — one async listener thread per unique channel subscription
_redis_listeners: dict[str, asyncio.Task] = {}
_redis_lock = asyncio.Lock()


async def _verify_token(token: str) -> tuple[bool, str]:
    try:
        from jose import JWTError, jwt
        from tf_api.api.routes_auth import SECRET_KEY, ALGORITHM
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("type") != "access":
            return False, ""
        return True, payload.get("sub", "unknown")
    except JWTError:
        return False, ""


async def _send_json(ws: WebSocket, data: dict[str, Any]) -> None:
    try:
        await ws.send_text(dumps(data))
    except (ConnectionError, OSError, ValueError):
        pass


async def _try_close(ws: WebSocket, code: int = 4401) -> None:
    with suppress(ConnectionError, OSError, RuntimeError):
        await ws.close(code=code)


@router.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    global _global_count
    await ws.accept()

    if _global_count >= _MAX_GLOBAL_CONNECTIONS:
        await _send_json(ws, {"error": "Server full — too many connections"})
        await _try_close(ws, 4401)
        return

    authenticated = False
    username = "unknown"
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
        ok, username = await _verify_token(token)
        if ok:
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
        camera_key = ",".join(cameras) if cameras else "global"

    _connections.setdefault(camera_key, []).append(ws)
    _user_connections[username] = _user_connections.get(username, 0) + 1
    _global_count += 1

    logger.info(
        "WS client connected (user=%s, cameras=%s, total=%d, global=%d)",
        username, camera_key, len(_connections[camera_key]), _global_count,
    )

    await _send_json(ws, {"type": "connected", "cameras": cameras, "client_id": id(ws)})

    # Start Redis pub/sub listener for each subscribed camera
    redis_tasks = []
    for cam in cameras:
        task = await _ensure_redis_listener(cam)
        if task is not None:
            redis_tasks.append(task)

    # If Redis is not available, fall back to in-process LiveEventBus
    from tf_common.live_bus import AsyncLiveEventBus

    live_bus = AsyncLiveEventBus()
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
                last_pong = time.monotonic()
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
                continue
    except WebSocketDisconnect:
        pass
    finally:
        conns = _connections.get(camera_key, [])
        if ws in conns:
            conns.remove(ws)
        _user_connections[username] = max(_user_connections.get(username, 0) - 1, 0)
        _global_count = max(_global_count - 1, 0)
        for t in redis_tasks:
            t.cancel()
        live_bus.close()
        logger.info(
            "WS client disconnected (user=%s, cameras=%s, global=%d)",
            username, camera_key, _global_count,
        )


async def _ensure_redis_listener(camera_id: str) -> asyncio.Task | None:
    """Start a background Redis pub/sub listener for a camera if not already running.
    Returns the listener task so it can be cancelled on disconnect."""
    channel = f"traffic:live:{camera_id}"
    async with _redis_lock:
        if channel in _redis_listeners:
            return _redis_listeners[channel]
        try:
            task = asyncio.create_task(_redis_sub_loop(channel))
            _redis_listeners[channel] = task
            return task
        except Exception:
            return None


async def _redis_sub_loop(channel: str) -> None:
    """Listen for messages on a Redis channel and broadcast to WS clients."""
    try:
        import redis.asyncio as aioredis
        r = aioredis.Redis(
            host="localhost", port=6379, db=0,
            decode_responses=True, socket_connect_timeout=2,
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
    except (ConnectionError, OSError, ImportError):
        logger.debug("Redis pub/sub not available for channel %s", channel)
    except asyncio.CancelledError:
        pass
    finally:
        async with _redis_lock:
            _redis_listeners.pop(channel, None)


async def broadcast_raw(data: dict[str, Any]) -> None:
    """Broadcast a raw dict to all WS clients subscribed to a matching camera."""
    cameras = data.get("cameras") or [data.get("camera_id", "")]
    message = dumps(data)
    for key, conns in list(_connections.items()):
        if key == "global" or any(c in key for c in cameras if c):
            for ws in list(conns):
                try:
                    await ws.send_text(message)
                except (ConnectionError, OSError, ValueError):
                    with suppress(Exception):
                        conns.remove(ws)


async def broadcast(camera_id: str, data: dict[str, Any]) -> None:
    """Broadcast a message to all WS clients subscribed to a camera."""
    await broadcast_raw({"camera_id": camera_id, **data})


def broadcast_sync(camera_id: str, data: dict[str, Any]) -> None:
    """Synchronous convenience wrapper for use from non-async contexts (e.g. threads)."""
    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            asyncio.run_coroutine_threadsafe(broadcast(camera_id, data), loop)
    except RuntimeError:
        # No running loop — create one
        asyncio.run(broadcast(camera_id, data))


def get_connection_stats() -> dict[str, Any]:
    return {
        "global_connections": _global_count,
        "max_global": _MAX_GLOBAL_CONNECTIONS,
        "user_connections": dict(_user_connections),
        "camera_subscriptions": {k: len(v) for k, v in _connections.items()},
    }
