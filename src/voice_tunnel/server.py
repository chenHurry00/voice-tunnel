"""Async TCP server for voice-tunnel."""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .config import ServerConfig
from .protocol import PROTOCOL_VERSION, ProtocolError, new_request_id, read_message, write_message

LOG = logging.getLogger("voice_tunnel.server")


@dataclass
class AgentConnection:
    host: str
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    last_seen: float = field(default_factory=time.time)
    active: bool = False


class TunnelServer:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.agents: dict[str, AgentConnection] = {}
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.active_host: str | None = None
        self.manual_host: str | None = None
        self.manual_until: float = 0.0
        self._server: asyncio.AbstractServer | None = None

    # ------------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, self.config.bind, self.config.port)
        addrs = ", ".join(str(sock.getsockname()) for sock in (self._server.sockets or []))
        LOG.info("voice-tunnel server listening on %s", addrs)
        self._reaper_task = asyncio.create_task(self._reaper())
        try:
            async with self._server:
                await self._server.serve_forever()
        finally:
            self._reaper_task.cancel()

    async def _reaper(self) -> None:
        while True:
            await asyncio.sleep(5)
            now = time.time()
            if self.manual_host and now >= self.manual_until:
                LOG.info("manual target expired: %s", self.manual_host)
                self.manual_host = None
                self.manual_until = 0.0
            for host, conn in list(self.agents.items()):
                if conn.active and now - conn.last_seen > self.config.heartbeat_timeout:
                    LOG.warning("agent %s heartbeat timeout", host)
                    conn.active = False
                    if self.active_host == host:
                        self.active_host = None

    # ------------------------------------------------------------------ connection

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        role = "unknown"
        host = "?"
        try:
            hello = await asyncio.wait_for(read_message(reader), timeout=5)
            if not hello or hello.get("type") != "hello":
                await write_message(writer, {"type": "hello_ack", "ok": False, "error": "hello required"})
                return
            if int(hello.get("version", 0)) != PROTOCOL_VERSION:
                await write_message(writer, {"type": "hello_ack", "ok": False, "error": "protocol version mismatch"})
                return
            if not hmac.compare_digest(str(hello.get("token", "")), self.config.token):
                await write_message(writer, {"type": "hello_ack", "ok": False, "error": "bad token"})
                LOG.warning("rejected %s: bad token", peer)
                return
            role = str(hello.get("role", "agent"))
            if role == "agent":
                host = str(hello.get("host", "")).strip()
                if not host:
                    await write_message(writer, {"type": "hello_ack", "ok": False, "error": "host required"})
                    return
                await write_message(writer, {"type": "hello_ack", "ok": True})
                await self._agent_loop(host, reader, writer)
            elif role == "control":
                await write_message(writer, {"type": "hello_ack", "ok": True})
                await self._control_loop(reader, writer)
            else:
                await write_message(writer, {"type": "hello_ack", "ok": False, "error": "unknown role"})
        except (ProtocolError, asyncio.TimeoutError) as exc:
            LOG.warning("connection %s/%s: %s", peer, role, exc)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if role == "agent" and host in self.agents:
                conn = self.agents.pop(host)
                if conn.writer is writer:
                    LOG.info("agent disconnected: %s", host)
                    if self.active_host == host:
                        self.active_host = None
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, RuntimeError):
                pass

    async def _agent_loop(self, host: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        old = self.agents.get(host)
        if old is not None:
            LOG.warning("agent %s reconnected, closing old connection", host)
            old.writer.close()
        conn = AgentConnection(host=host, reader=reader, writer=writer)
        self.agents[host] = conn
        LOG.info("agent connected: %s", host)

        while True:
            message = await read_message(reader)
            if message is None:
                return
            conn.last_seen = time.time()
            kind = message.get("type")
            if kind == "ack":
                request_id = str(message.get("request_id", ""))
                future = self.pending.pop(request_id, None)
                if future is not None and not future.done():
                    future.set_result(message)
            elif kind == "active":
                state = message.get("state")
                conn.active = state == "enter"
                if conn.active:
                    self.active_host = host
                    LOG.info("active target -> %s", host)
                elif self.active_host == host:
                    self.active_host = None
                    LOG.info("active target cleared (left %s)", host)
            elif kind == "heartbeat":
                if bool(message.get("active")):
                    conn.active = True
                    if self.active_host is None and self.manual_host is None:
                        # recover from a lost enter event only when nothing else is active
                        self.active_host = host
            elif kind == "ping":
                await write_message(writer, {"type": "pong", "ts": time.time()})
            else:
                LOG.debug("agent %s: ignored %s", host, kind)

    async def _control_loop(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            message = await read_message(reader)
            if message is None:
                return
            if message.get("type") != "control":
                await write_message(writer, {"type": "result", "id": message.get("id"), "ok": False, "error": "control required"})
                continue
            command = str(message.get("command", ""))
            request_id = message.get("id")
            try:
                data = await self._dispatch_control(command, message)
                reply: dict[str, Any] = {"type": "result", "id": request_id, "ok": True, "data": data}
            except Exception as exc:  # noqa: BLE001 - report all control errors to CLI
                reply = {"type": "result", "id": request_id, "ok": False, "error": str(exc)}
            await write_message(writer, reply)

    async def _dispatch_control(self, command: str, message: dict[str, Any]) -> Any:
        if command == "send":
            target = str(message.get("target") or "active")
            text = str(message.get("text", ""))
            if not text:
                raise ValueError("text is empty")
            if len(text) > 20_000:
                raise ValueError("text too long")
            conn = await self.send_text(target, text)
            return {"target": target, "request_id": conn.get("request_id"), "ack": conn}
        if command == "targets":
            return [self._agent_info(c) for c in self.agents.values()]
        if command == "status":
            return {
                "active_target": self.current_target(),
                "active_host": self.active_host,
                "default_target": self.config.default_target,
                "manual_host": self.manual_host,
                "agents": [self._agent_info(c) for c in self.agents.values()],
            }
        if command in {"preview", "final"}:
            target = str(message.get("target") or "active")
            text = str(message.get("text", ""))
            resolved = self.current_target() if target == "active" else target
            if not resolved:
                raise ValueError("no active target")
            conn = self.agents.get(resolved)
            if conn is None:
                raise ValueError(f"agent not connected: {resolved}")
            payload = {"type": "partial" if command == "preview" else command, "text": text}
            if command == "preview":
                payload["stable_len"] = int(message.get("stable_len", -1))
            await write_message(conn.writer, payload)
            return {"target": resolved, "sent": True}
        if command == "set_target":
            host = message.get("host")
            if host:
                self.manual_host = str(host)
                self.manual_until = time.time() + float(message.get("ttl", 600))
                LOG.info("manual target -> %s (ttl %ss)", self.manual_host, message.get("ttl", 600))
            else:
                self.manual_host = None
                self.manual_until = 0.0
                LOG.info("manual target cleared")
            return {"manual_host": self.manual_host}
        raise ValueError(f"unknown command: {command}")

    # ------------------------------------------------------------------ operations

    def _agent_info(self, conn: AgentConnection) -> dict[str, Any]:
        return {
            "host": conn.host,
            "active": conn.active,
            "last_seen": round(time.time() - conn.last_seen, 1),
        }

    def current_target(self) -> str | None:
        if self.manual_host and time.time() < self.manual_until:
            return self.manual_host
        return self.active_host or self.config.default_target

    async def send_text(self, target: str, text: str) -> dict[str, Any]:
        if target == "active":
            resolved = self.current_target()
            if not resolved:
                raise ValueError("no active target")
            target = resolved
        conn = self.agents.get(target)
        if conn is None:
            raise ValueError(f"agent not connected: {target}")
        request_id = new_request_id()
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await write_message(
            conn.writer,
            {"type": "insert_text", "request_id": request_id, "text": text, "created_at": time.time()},
        )
        try:
            ack = await asyncio.wait_for(future, timeout=self.config.ack_timeout)
        except asyncio.TimeoutError as exc:
            self.pending.pop(request_id, None)
            raise ValueError(f"agent {target} did not ack within {self.config.ack_timeout}s") from exc
        if not ack.get("ok"):
            raise ValueError(f"agent {target} rejected text: {ack.get('error')}")
        return {"request_id": request_id, **ack}
