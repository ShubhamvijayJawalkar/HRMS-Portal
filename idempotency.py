"""CC-07 idempotent writes.

Writes that carry an ``Idempotency-Key`` header are deduplicated: the first
call runs and its response is stored; retries (same key + same payload,
within the TTL) replay the stored response instead of re-running, so a
flaky client / job retry can't double-apply (duplicate leave application,
double payroll finalise, duplicate break, ...).

Semantics
---------
* no header                 -> request runs normally (no guarantee)
* fresh key                 -> claim row INSERTed, handler runs, response
  stored on success; on failure the claim is released so a retry starts clean
* replay (same key, same body, unexpired) -> stored response returned
* key reused with a *different* body -> 409 (conflict; client should use a
  new key for a new intent)
* concurrent duplicate key  -> PK collision on the claim -> 409
  ("already in progress")

The claim row uses ``response_status = 0`` as an in-flight sentinel with a
NULL body; expired rows are purged by the same scheduler job that clears
password-reset tokens.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from functools import wraps

from flask import jsonify, request

IDEMPOTENCY_TTL_HOURS = 24


def idempotent(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        key = (request.headers.get("Idempotency-Key") or "").strip()
        if not key:
            return fn(*args, **kwargs)
        from app import get_db  # lazy: no circular import at module load

        body = request.get_json(silent=True) or {}
        route = f"{request.method} {request.path}"
        payload_digest = route + json.dumps(body, sort_keys=True, default=str)
        request_hash = hashlib.sha256(payload_digest.encode()).hexdigest()

        conn = get_db()
        try:
            existing = conn.execute(
                "SELECT response_status, response_body, request_hash FROM idempotency_keys "
                "WHERE key = ? AND route = ? AND expires_at > ?",
                [key, route, datetime.now()],
            ).fetchone()
        except Exception:
            conn.close()
            return fn(*args, **kwargs)

        if existing:
            conn.close()
            if existing[2] != request_hash:
                return jsonify({"error": "Idempotency-Key reused with a different request"}), 409
            stored = existing[1]
            if stored is not None:
                if isinstance(stored, str):
                    stored = json.loads(stored)
                return jsonify(stored), existing[0]
            return jsonify({"error": "Request already in progress"}), 409

        try:
            conn.execute(
                "INSERT INTO idempotency_keys (key, route, request_hash, response_status, expires_at) "
                "VALUES (?, ?, ?, 0, ?)",
                [key, route, request_hash, datetime.now() + timedelta(hours=IDEMPOTENCY_TTL_HOURS)],
            )
        except Exception:
            conn.close()
            return jsonify({"error": "Request already in progress"}), 409

        try:
            resp = fn(*args, **kwargs)
        except Exception:
            try:
                conn.execute("DELETE FROM idempotency_keys WHERE key = ? AND route = ?", [key, route])
            except Exception:
                pass
            conn.close()
            raise

        try:
            if isinstance(resp, tuple) and len(resp) == 2 and 200 <= resp[1] < 300:
                try:
                    stored_body = resp[0].get_json()
                except Exception:
                    stored_body = None
                conn.execute(
                    "UPDATE idempotency_keys SET response_status = ?, response_body = ? "
                    "WHERE key = ? AND route = ?",
                    [resp[1], json.dumps(stored_body) if stored_body is not None else None, key, route],
                )
            else:
                conn.execute("DELETE FROM idempotency_keys WHERE key = ? AND route = ?", [key, route])
        except Exception:
            pass
        conn.close()
        return resp

    return wrapper