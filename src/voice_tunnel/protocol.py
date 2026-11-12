"""Wire protocol helpers for voice-tunnel."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

PROTOCOL_VERSION = 1
MAX_LINE = 1024 * 1024


class ProtocolError(Exception):
    """Malformed or oversized protocol message."""


def encode_message(message: dict[str, Any]) -> bytes:
    data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_LINE:
        raise ProtocolError("message too large")
    return data + b"\n"


async def read_message(reader: Any) -> dict[str, Any] | None:
    """Read one newline-delimited JSON object. Returns None on EOF."""
    try:
        line = await reader.readline()
    except (ConnectionError, asyncio.IncompleteReadError):  # type: ignore[name-defined]
        return None
    if not line:
        return None
    if len(line) > MAX_LINE:
        raise ProtocolError("message too large")
    try:
        message = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid json") from exc
    if not isinstance(message, dict) or "type" not in message:
        raise ProtocolError("message must be an object with type")
    return message


async def write_message(writer: Any, message: dict[str, Any]) -> None:
    writer.write(encode_message(message))
    await writer.drain()


def new_request_id() -> str:
    return uuid.uuid4().hex
