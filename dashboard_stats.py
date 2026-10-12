"""Redis-backed, short-lived cache for the admin dashboard counters."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

import redis

logger = logging.getLogger(__name__)

TTL_SECONDS = 15
CACHE_PREFIX = "hrms:dashboard-stats"

_lock = threading.RLock()
_redis_clients: dict[str, redis.Redis] = {}
_memory_cache: dict[str, tuple[float, str]] = {}
_monotonic = time.monotonic


def cache_key(schema: str) -> str:
    return f"{CACHE_PREFIX}:{schema}"


def _redis_client() -> redis.Redis | None:
    url = os.getenv("REDIS_URL")
    if not url:
        return None
    with _lock:
        client = _redis_clients.get(url)
        if client is None:
            try:
                client = redis.Redis.from_url(url, decode_responses=True)
            except (redis.exceptions.RedisError, ValueError) as exc:
                logger.error("Dashboard stats Redis URL is invalid; using local fallback: %s", exc)
                return None
            _redis_clients[url] = client
        return client


def _read_redis(client: redis.Redis | None, key: str) -> dict[str, Any] | None:
    if client is None:
        return None
    try:
        raw = client.get(key)
    except redis.exceptions.RedisError as exc:
        logger.warning("Dashboard stats Redis read failed; using local fallback: %s", exc)
        return None
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
        logger.warning("Dashboard stats Redis value is invalid; refreshing it: %s", exc)
        return None
    return value if isinstance(value, dict) else None


def _read_memory(key: str) -> dict[str, Any] | None:
    entry = _memory_cache.get(key)
    if entry is None:
        return None
    expires_at, raw = entry
    if expires_at <= _monotonic():
        _memory_cache.pop(key, None)
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        _memory_cache.pop(key, None)
        return None
    return value if isinstance(value, dict) else None


def _write_redis(client: redis.Redis | None, key: str, raw: str) -> None:
    if client is not None:
        try:
            client.setex(key, TTL_SECONDS, raw)
        except redis.exceptions.RedisError as exc:
            logger.warning("Dashboard stats Redis write failed; cached locally: %s", exc)


def _write(client: redis.Redis | None, key: str, value: dict[str, Any]) -> None:
    raw = json.dumps(value, separators=(",", ":"), default=str)
    _memory_cache[key] = (_monotonic() + TTL_SECONDS, raw)
    _write_redis(client, key, raw)


def refresh(key: str, compute: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Compute once and replace both the Redis value and local fallback."""
    with _lock:
        value = compute()
        _write(_redis_client(), key, value)
        return value


def get(key: str, compute: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Serve the shared cache; compute on a cold/expired cache without stampeding."""
    client = _redis_client()
    value = _read_redis(client, key)
    if value is not None:
        return value
    with _lock:
        value = _read_redis(client, key)
        if value is not None:
            return value
        value = _read_memory(key)
        if value is not None:
            _write_redis(
                client, key, json.dumps(value, separators=(",", ":"), default=str)
            )
            return value
        value = compute()
        _write(client, key, value)
        return value
